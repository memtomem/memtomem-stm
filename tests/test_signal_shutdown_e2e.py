"""End-to-end: a real STM server process, signalled the way an operator would.

The unit tests pin what each piece does. This pins the thing #906 is actually
about — that the process goes away and takes its children with it — against the
real MCP SDK, the real stdio transport and a real signal, none of which the
unit tests exercise.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals"),
    pytest.mark.real_child_sweep,
]

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FAKE_UPSTREAM = Path(__file__).resolve().parent / "_fake_memtomem_server.py"
_EXIT_BUDGET_SECONDS = 30.0
_HANDSHAKE_BUDGET_SECONDS = 30.0


def _start_server(tmp_path: Path, *, log_level: str = "WARNING") -> subprocess.Popen[bytes]:
    # A real proxied upstream, so the server owns a real stdio child: without
    # one the "leaves nothing behind" assertion has nothing to assert on.
    upstreams = {
        "fake": {"prefix": "fake", "command": sys.executable, "args": [str(_FAKE_UPSTREAM)]}
    }
    env = dict(
        os.environ,
        MEMTOMEM_STM_DATA_DIR=str(tmp_path),
        MEMTOMEM_STM_PROXY__ENABLED="true",
        MEMTOMEM_STM_PROXY__CONFIG_PATH=str(tmp_path / "proxy.json"),
        MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS=json.dumps(upstreams),
        MEMTOMEM_STM_SURFACING__ENABLED="false",
        MEMTOMEM_STM_LOG_LEVEL=log_level,
        # Short enough that a hung teardown fails this test rather than the CI job.
        MEMTOMEM_STM_TEARDOWN_WATCHDOG_SECONDS="10",
        PYTHONPATH=str(_REPO_ROOT / "src"),
    )
    return subprocess.Popen(
        [sys.executable, "-m", "memtomem_stm.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(_REPO_ROOT),
    )


def _read_line_or_fail(proc: subprocess.Popen[bytes]) -> bytes:
    """Read one line under a deadline, so a stalled startup fails this test
    rather than hanging the CI job with no budget of its own."""
    assert proc.stdout is not None
    result: list[bytes] = []
    reader = threading.Thread(target=lambda: result.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout=_HANDSHAKE_BUDGET_SECONDS)
    assert result, "server produced no response within the handshake budget"
    return result[0]


def _initialize(proc: subprocess.Popen[bytes]) -> None:
    """Drive the handshake so the lifespan has actually started."""
    assert proc.stdin is not None and proc.stdout is not None
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "e2e", "version": "0"},
        },
    }
    proc.stdin.write((json.dumps(request) + "\n").encode())
    proc.stdin.flush()
    line = _read_line_or_fail(proc)
    assert b'"result"' in line, line
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    proc.stdin.flush()


def _wait_for_stderr(proc: subprocess.Popen[bytes], needle: bytes) -> bool:
    """Wait until *needle* appears on the child's stderr, under a deadline."""
    assert proc.stderr is not None
    seen: list[bool] = []

    def _scan() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            if needle in line:
                seen.append(True)
                return

    scanner = threading.Thread(target=_scan, daemon=True)
    scanner.start()
    scanner.join(timeout=_HANDSHAKE_BUDGET_SECONDS)
    return bool(seen)


def _children(pid: int) -> set[int]:
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    return {int(tok) for tok in out.split() if tok.isdigit()}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["term", "int"])
def test_a_signalled_server_exits_and_leaves_nothing_behind(
    tmp_path: Path, sig: signal.Signals
) -> None:
    """Before this, the default disposition killed the process where it stood:
    the lifespan teardown never ran and every stdio child was reparented — the
    #906 end state, produced by the first command an operator reaches for."""
    proc = _start_server(tmp_path)
    try:
        _initialize(proc)
        before = _children(proc.pid)
        # The proxied upstream is a real stdio child; without one this test
        # would assert nothing about cleanup.
        assert before, "expected the proxied upstream to be running as a child"

        proc.send_signal(sig)
        started = time.monotonic()
        returncode = proc.wait(timeout=_EXIT_BUDGET_SECONDS)
        elapsed = time.monotonic() - started

        # Exit 0, promptly: the cancellation unwind ran the real teardown. The
        # watchdog budget here is 10s, so anything slower than that — or a
        # non-zero status — means the mechanism failed and only the backstop
        # saved us. (Closing fd 0 to fake EOF was the first design and does
        # exactly that: the blocked read never wakes.)
        assert returncode == 0, proc.stderr.read().decode()[-2000:] if proc.stderr else returncode
        assert elapsed < 5.0
        assert not any(_alive(pid) for pid in before)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10.0)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def test_a_twice_signalled_server_exits_with_the_signal_status(tmp_path: Path) -> None:
    """The conventional 128+signum, so a supervisor reading the status sees what
    actually happened rather than a generic failure.

    The second signal is sent only once the first has been *handled* — POSIX
    does not queue signals, so firing both immediately can collapse into one
    delivery and let the graceful path satisfy the assertion, which would hide
    a broken second-signal handler entirely.
    """
    proc = _start_server(tmp_path, log_level="INFO")
    try:
        _initialize(proc)
        proc.send_signal(signal.SIGINT)
        assert _wait_for_stderr(proc, b"Signal again to exit immediately"), (
            "the first signal was never handled"
        )
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=_EXIT_BUDGET_SECONDS) == 128 + int(signal.SIGINT)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10.0)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
