"""End-to-end: a real STM server whose client goes away without closing stdin.

This is #906's branch (b), reproduced rather than simulated. A wrapper shell
starts the server in the background and exits: the pipes stay open — this test
process holds both ends, exactly as a leaked write end would — so no EOF ever
arrives and no signal is sent. Nothing in #910-#913 fires. All that changes is
the process tree, which is what #914 watches.

The negative case matters as much: a launcher that ``exec``-replaces itself
keeps its pid, and a server that shut down there would be a false positive
costing a live session.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX reparenting"),
    pytest.mark.real_child_sweep,
]

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FAKE_UPSTREAM = Path(__file__).resolve().parent / "_fake_memtomem_server.py"
_EXIT_BUDGET_SECONDS = 30.0
_HANDSHAKE_BUDGET_SECONDS = 30.0
_POLL_SECONDS = 0.2
# The handshake counts as activity, so the grace runs from the last request.
_GRACE_SECONDS = 0.5


def _env(tmp_path: Path) -> dict[str, str]:
    upstreams = {
        "fake": {"prefix": "fake", "command": sys.executable, "args": [str(_FAKE_UPSTREAM)]}
    }
    return dict(
        os.environ,
        MEMTOMEM_STM_DATA_DIR=str(tmp_path),
        MEMTOMEM_STM_PROXY__ENABLED="true",
        MEMTOMEM_STM_PROXY__CONFIG_PATH=str(tmp_path / "proxy.json"),
        MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS=json.dumps(upstreams),
        MEMTOMEM_STM_SURFACING__ENABLED="false",
        MEMTOMEM_STM_LOG_LEVEL="INFO",
        MEMTOMEM_STM_TEARDOWN_WATCHDOG_SECONDS="10",
        MEMTOMEM_STM_PARENT_LIVENESS_POLL_SECONDS=str(_POLL_SECONDS),
        MEMTOMEM_STM_PARENT_LIVENESS_GRACE_SECONDS=str(_GRACE_SECONDS),
        PYTHONPATH=str(_REPO_ROOT / "src"),
    )


def _start_behind(shell_command: str, tmp_path: Path) -> subprocess.Popen[bytes]:
    """Start the server behind ``sh -c`` so the launch shape is the variable.

    ``$PY`` is the interpreter; the server inherits this ``sh``'s stdin, stdout
    and stderr, which are the pipes Popen created here — except that a shell
    without job control hands an asynchronous command ``/dev/null`` for stdin,
    which is an instant EOF and the *ordinary* shutdown. The backgrounding
    wrapper below therefore duplicates the real stdin onto fd 3 and redirects it
    back explicitly, so the server keeps the pipe this test holds open.
    """
    return subprocess.Popen(
        ["sh", "-c", shell_command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(_env(tmp_path), PY=sys.executable),
        cwd=str(_REPO_ROOT),
    )


def _read_line_or_fail(proc: subprocess.Popen[bytes]) -> bytes:
    assert proc.stdout is not None
    result: list[bytes] = []
    reader = threading.Thread(target=lambda: result.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout=_HANDSHAKE_BUDGET_SECONDS)
    assert result, "server produced no response within the handshake budget"
    return result[0]


def _initialize(proc: subprocess.Popen[bytes]) -> None:
    assert proc.stdin is not None
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
    assert b'"result"' in (line := _read_line_or_fail(proc)), line
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    proc.stdin.flush()


def _ping(proc: subprocess.Popen[bytes], request_id: int) -> None:
    assert proc.stdin is not None
    proc.stdin.write(
        (json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "ping"}) + "\n").encode()
    )
    proc.stdin.flush()
    assert b'"result"' in (line := _read_line_or_fail(proc)), line


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_gone(pid: int, budget: float) -> float | None:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if not _alive(pid):
            return time.monotonic() - (deadline - budget)
        time.sleep(0.05)
    return None


def _server_pid(proc: subprocess.Popen[bytes]) -> int:
    """The backgrounded server's pid, echoed by the wrapper before it exits."""
    assert proc.stderr is not None
    for _ in range(200):
        line = proc.stderr.readline()
        if line.startswith(b"SERVERPID "):
            return int(line.split()[1])
    raise AssertionError("the wrapper never announced the server pid")


def _drain_stderr(proc: subprocess.Popen[bytes]) -> list[bytes]:
    """Collect stderr in the background so a full pipe cannot park the server."""
    lines: list[bytes] = []

    def _scan() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            lines.append(line)

    threading.Thread(target=_scan, daemon=True).start()
    return lines


def _cleanup(proc: subprocess.Popen[bytes], pid: int | None = None) -> None:
    if pid is not None and _alive(pid):
        os.kill(pid, 9)
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10.0)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def test_a_server_whose_launcher_exits_shuts_itself_down(tmp_path: Path) -> None:
    """The incident's shape: no EOF, because this process still holds the write
    end of the pipe the way a leaked descriptor would; no signal; nothing but a
    reparent. Before #914 this server ran until something killed it."""
    proc = _start_behind(
        'exec 3<&0; "$PY" -m memtomem_stm.server <&3 & echo "SERVERPID $!" >&2; wait $!', tmp_path
    )
    server_pid = None
    try:
        server_pid = _server_pid(proc)
        _initialize(proc)
        stderr_lines = _drain_stderr(proc)

        # Kill the wrapper, not the server: the server is reparented, its pipes
        # untouched and still held open by this process.
        proc.kill()
        proc.wait(timeout=10.0)
        assert _alive(server_pid), "the server should outlive its launcher, then notice"

        elapsed = _wait_gone(server_pid, _EXIT_BUDGET_SECONDS)
        assert elapsed is not None, b"".join(stderr_lines)[-2000:]
        # Confirmation poll plus grace, with room for a slow CI box.
        assert elapsed < 10.0
        assert any(b"Parent gone" in line for line in stderr_lines), b"".join(stderr_lines)[-2000:]
    finally:
        _cleanup(proc, server_pid)


def test_a_server_behind_an_exec_launcher_is_left_alone(tmp_path: Path) -> None:
    """An ``exec``-replacing launcher keeps its pid, so there is no reparent and
    nothing to infer. A shutdown here would cost a live session — the cost this
    backstop is shaped around — and the client is still talking."""
    proc = _start_behind('exec "$PY" -m memtomem_stm.server', tmp_path)
    try:
        _initialize(proc)
        _drain_stderr(proc)
        deadline = time.monotonic() + _POLL_SECONDS * 20
        request_id = 2
        while time.monotonic() < deadline:
            _ping(proc, request_id)
            request_id += 1
            time.sleep(_POLL_SECONDS)
        assert proc.poll() is None, "a live session was shut down by the backstop"

        # And the ordinary path still ends it: closing stdin is the EOF the
        # whole mechanism defers to.
        assert proc.stdin is not None
        proc.stdin.close()
        assert proc.wait(timeout=_EXIT_BUDGET_SECONDS) == 0
    finally:
        _cleanup(proc)
