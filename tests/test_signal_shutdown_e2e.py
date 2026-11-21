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
from collections.abc import Callable
from pathlib import Path
from typing import IO

import pytest

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals"),
    pytest.mark.real_child_sweep,
]

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FAKE_UPSTREAM = Path(__file__).resolve().parent / "_fake_memtomem_server.py"
_GATED_SERVER = Path(__file__).resolve().parent / "_gated_stm_server.py"
_EXIT_BUDGET_SECONDS = 30.0
_HANDSHAKE_BUDGET_SECONDS = 30.0


def _start_server(
    tmp_path: Path,
    *,
    log_level: str = "WARNING",
    argv: list[str] | None = None,
    stderr: IO[bytes] | int = subprocess.PIPE,
) -> subprocess.Popen[bytes]:
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
        argv if argv is not None else [sys.executable, "-m", "memtomem_stm.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr,
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


def _settle(proc: subprocess.Popen[bytes]) -> None:
    """Finish one request round trip after the handshake.

    A signal that lands in the first milliseconds after the handshake can cut
    the teardown short — measured locally when SIGINT followed the handshake
    immediately — and that shutdown exits 1. A test about the ordinary shutdown
    must not race that window, so it waits for an answered request first."""
    assert proc.stdin is not None
    proc.stdin.write(b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
    proc.stdin.flush()
    # Notifications may come first; the answer is the line carrying our id.
    for _ in range(20):
        message = json.loads(_read_line_or_fail(proc))
        if message.get("id") == 2:
            assert "result" in message, message
            return
    raise AssertionError("no answer to tools/list")


def _wait_until(condition: Callable[[], bool]) -> bool:
    """Poll *condition* under the handshake deadline."""
    deadline = time.monotonic() + _HANDSHAKE_BUDGET_SECONDS
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def _children(pid: int) -> set[int]:
    out = subprocess.run(
        ["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=10.0
    ).stdout
    return {int(tok) for tok in out.split() if tok.isdigit()}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _gone(pid: int) -> bool:
    """Exited, whether or not anyone has reaped it yet.

    The leaked-child sweep terminates a child without reaping it, so after the
    server exits the child can linger as a zombie of whichever process adopted
    it, and a signal-0 probe still finds a zombie. ``ps`` reads the state
    without reaping."""
    if not _alive(pid):
        return True
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10.0
    ).stdout.strip()
    if not state:
        # No status: it may have gone between the probes, or ps may have failed.
        # Only a second probe tells those apart.
        return not _alive(pid)
    return state.startswith("Z")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["term", "int"])
def test_a_signalled_server_exits_and_leaves_nothing_behind(
    tmp_path: Path, sig: signal.Signals
) -> None:
    """Before this, the default disposition killed the process where it stood:
    the lifespan teardown never ran and every stdio child was reparented — the
    #906 end state, produced by the first command an operator reaches for."""
    # A file, not a pipe: a failure message that read a pipe to EOF would wait
    # on any leaked child still holding its write end.
    stderr_path = tmp_path / "stderr.log"
    with stderr_path.open("wb") as stderr_file:
        proc = _start_server(tmp_path, stderr=stderr_file)
    try:
        _initialize(proc)
        _settle(proc)
        before = _children(proc.pid)
        # The proxied upstream is a real stdio child; without one this test
        # would assert nothing about cleanup.
        assert before, "expected the proxied upstream to be running as a child"

        proc.send_signal(sig)
        started = time.monotonic()
        returncode = proc.wait(timeout=_EXIT_BUDGET_SECONDS)
        elapsed = time.monotonic() - started

        # Exit 0, promptly: the cancellation unwind ran the real teardown to
        # its end. Status 1 means it was cut short, or the watchdog (budget
        # 10s here) ended it, in which case only the backstop saved us.
        # (Closing fd 0 to fake EOF was the first design and does exactly
        # that: the blocked read never wakes.)
        assert returncode == 0, stderr_path.read_bytes().decode(errors="replace")[-2000:]
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

    Two waits come before the second signal. The log line shows the first
    signal was *handled*: a SIGINT sent while another is still pending
    coalesces with it, so two sent back to back can arrive as one. The gate
    (see ``_gated_stm_server.py``) holds the teardown open: its
    ``gate-entered`` file only marks entry, and the absence of ``gate-left``
    after the exit is what shows the hold never unwound before the second
    signal ended the process. Without the gate, an idle server can finish its
    teardown and exit 0 soon after the log line (121-182 ms in local macOS
    runs), and a second signal sent later than that never reaches a handler
    (#1434).
    """
    gate_dir = tmp_path / "gate"
    gate_dir.mkdir()
    # A file, not a pipe: polling it needs no reader thread, and nothing waits
    # for EOF on a stream the proxied upstream also inherits.
    stderr_path = tmp_path / "stderr.log"
    with stderr_path.open("wb") as stderr_file:
        proc = _start_server(
            tmp_path,
            log_level="INFO",
            argv=[sys.executable, str(_GATED_SERVER), str(gate_dir)],
            stderr=stderr_file,
        )

    def log_tail() -> str:
        return stderr_path.read_bytes().decode(errors="replace")[-2000:]

    try:
        _initialize(proc)
        assert _children(proc.pid), "expected the proxied upstream to be running as a child"
        proc.send_signal(signal.SIGINT)
        assert _wait_until(
            lambda: b"Signal again to exit immediately" in stderr_path.read_bytes()
        ), "the first signal was never handled\n" + log_tail()
        assert _wait_until((gate_dir / "gate-entered").exists), (
            "teardown never reached ProxyManager.stop()\n" + log_tail()
        )
        proc.send_signal(signal.SIGINT)
        returncode = proc.wait(timeout=_EXIT_BUDGET_SECONDS)
        assert returncode == 128 + int(signal.SIGINT), log_tail()
        # The exit came while the gate still held the teardown, not after it.
        assert not (gate_dir / "gate-left").exists(), log_tail()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10.0)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


# Replaces ``ProxyManager.stop`` with a step that is cut short, the way a
# ``CancelledError`` out of a step whose guard catches only ``Exception`` cuts
# the close sequence short. The marker shows the injected exception is what ended
# it, rather than a cancellation that arrived first.
_CUT_SHORT_LAUNCHER = """
import asyncio, runpy, sys
from memtomem_stm.proxy import manager

async def _cut_short(self):
    sys.stderr.write("stop-raising\\n")
    sys.stderr.flush()
    raise asyncio.CancelledError

manager.ProxyManager.stop = _cut_short
runpy.run_module("memtomem_stm.server", run_name="__main__", alter_sys=True)
"""


def test_a_signalled_teardown_cut_short_exits_with_a_failure_status(tmp_path: Path) -> None:
    """Exit 0 after a signal says the close sequence ran to the end. One cut
    short by an escaping ``CancelledError`` used to exit 0 too, because the
    completion was recorded in a ``finally`` (#1459). The leaked-child sweep
    still collects the upstream the skipped stop() would have closed."""
    stderr_path = tmp_path / "stderr.log"
    with stderr_path.open("wb") as stderr_file:
        proc = _start_server(
            tmp_path,
            log_level="INFO",
            argv=[sys.executable, "-c", _CUT_SHORT_LAUNCHER],
            stderr=stderr_file,
        )

    def log_tail() -> str:
        return stderr_path.read_bytes().decode(errors="replace")[-2000:]

    try:
        _initialize(proc)
        before = _children(proc.pid)
        assert before, "expected the proxied upstream to be running as a child"
        proc.send_signal(signal.SIGINT)
        returncode = proc.wait(timeout=_EXIT_BUDGET_SECONDS)

        assert returncode == 1, log_tail()
        log = stderr_path.read_bytes()
        assert b"stop-raising" in log, log_tail()
        assert b"did not run to the end" in log, log_tail()
        assert _wait_until(lambda: all(_gone(pid) for pid in before)), log_tail()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10.0)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
