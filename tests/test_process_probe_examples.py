"""Run the documented probes, including their interruption/EOF cleanup paths."""

from pathlib import Path
import os
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX documentation examples")


def stopped(pid):
    """True once *pid* is no longer a *running* process.

    An orphaned worker cannot control whether its new parent reaps it: PID 1
    does on a normal system, but a container whose PID 1 is an ordinary process
    does not, and the exited worker then stays visible to ``kill(0)`` as a
    zombie. The example promises the workers stop themselves, not that somebody
    reaps them, so a zombie counts as stopped.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    state = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return state.startswith("Z")


def example(name):
    text = (Path(__file__).parents[1] / "CONTRIBUTING.md").read_text()
    section = text.split(f"<!-- process-probe: {name} -->", 1)[1]
    return section.split("```", 2)[1].split("\n", 1)[1]


@pytest.mark.parametrize("mode", ["eof", "timeout", "stubborn"])
def test_pty_example_reaps_child(tmp_path, mode):
    path = tmp_path / "pty_probe.py"
    path.write_text(example("pty"))
    child_code = "import os, time; print(os.getpid(), flush=True); "
    if mode == "stubborn":
        child_code = (
            "import os, time, signal; "
            "signal.signal(signal.SIGHUP, signal.SIG_IGN); "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print(os.getpid(), flush=True); time.sleep(30)"
        )
    else:
        child_code += "time.sleep(30)" if mode == "timeout" else "pass"
    result = subprocess.run(
        [sys.executable, str(path), sys.executable, "-c", child_code],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stderr
    pid = int(result.stdout.strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("interrupt", [None, signal.SIGINT, signal.SIGTERM])
def test_load_example_reaps_workers(tmp_path, interrupt):
    path = tmp_path / "load_probe.sh"
    path.write_text(example("load"))
    host = subprocess.Popen(["bash", str(path)], stdout=subprocess.PIPE, text=True)
    pids = []
    try:
        assert host.stdout is not None
        pids = [int(pid) for pid in host.stdout.readline().split(":", 1)[1].split()]
        assert len(pids) == 2
        if interrupt is not None:
            host.send_signal(interrupt)
        host.wait(timeout=6)
        assert host.returncode == (0 if interrupt is None else 128 + interrupt)
        for pid in pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        if host.poll() is None:
            host.terminate()
            host.wait(timeout=6)


def test_load_example_cleanup_survives_an_empty_worker_list(tmp_path):
    # The EXIT trap is installed before the first worker starts, so it has to run
    # with `pids` still empty — and under `set -u` an empty array expansion is an
    # *error* in bash 3.2, the /bin/bash macOS ships. The trap would then abort
    # on its first line, in exactly the run where an early failure (or an edit
    # the section invites) makes cleanup matter.
    text = example("load")
    # bash >= 4.4 accepts the unguarded form, so CI's bash cannot show the bug by
    # running it: pin the guard itself as well as the behaviour.
    assert '${pids[@]+"${pids[@]}"}' in text
    assert 'in "${pids[@]}"' not in text
    marker = "trap 'exit 143' TERM\n"
    assert marker in text
    path = tmp_path / "load_probe.sh"
    path.write_text(text.replace(marker, marker + "exit 7\n", 1))
    result = subprocess.run(["bash", str(path)], capture_output=True, text=True, timeout=8)
    assert result.returncode == 7, result.stderr  # the trap ran and changed nothing
    assert "unbound variable" not in result.stderr


def test_load_workers_have_their_own_deadline(tmp_path):
    path = tmp_path / "load_probe.sh"
    path.write_text(example("load"))
    host = subprocess.Popen(["bash", str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert host.stdout is not None
        pids = [int(pid) for pid in host.stdout.readline().split(":", 1)[1].split()]
        host.kill()  # EXIT traps cannot run; the workers must stop themselves.
        host.wait(timeout=5)
        deadline = time.monotonic() + 5
        while pids and time.monotonic() < deadline:
            pids = [pid for pid in pids if not stopped(pid)]
            time.sleep(0.02)
        assert not pids
    finally:
        if host.poll() is None:
            host.kill()
        host.wait(timeout=5)
