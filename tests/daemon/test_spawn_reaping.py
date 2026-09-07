"""Detached children must be reaped without another spawn or host shutdown."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from memtomem_stm.config import STMConfig
from memtomem_stm.daemon import spawn
from memtomem_stm.utils import child_reaper


@pytest.fixture(autouse=True)
def isolated_claims(monkeypatch):
    """Claim the pids into a per-test table, and outlive no reaper.

    ``_detached_claims`` is process-global and no timer expires it, so a test
    that leaves a real (immediately recyclable) pid behind spares whatever
    inherits that number — including a genuine leak in a later
    ``real_child_sweep`` test, which would then pass vacuously on the sweep's
    central contract.

    Joining the reaper threads before the monkeypatch unwinds is the other half:
    a reaper still waiting on a fake child would retire its claim against the
    *restored* globals, at whatever point in a later test that lands.
    """
    monkeypatch.setattr(child_reaper, "_detached_claims", {})
    yield
    for thread in threading.enumerate():
        if thread.name == "stm-daemon-reaper":
            thread.join(timeout=5)
            assert not thread.is_alive(), "a reaper thread outlived its test"


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "child was not reaped"
        time.sleep(0.01)


def exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def break_spawn(monkeypatch, failure, launched):
    """Make the reaper thread or the ``Popen`` fail, without breaking the world.

    ``spawn.threading`` *is* the stdlib module, so patching ``Thread`` on it
    fails every thread creation in the interpreter for the duration of the test
    (an ``asyncio`` executor growing a worker, a plugin's background thread).
    Rebinding the name inside ``spawn`` reaches only the code under test.
    """

    def fail_thread(*_a, **_kw):
        raise RuntimeError("cannot start thread")

    def fail_popen(*_a, **_kw):
        launched.append(True)
        raise OSError("cannot spawn")

    threads: list[threading.Thread] = []

    def make_thread(*args, **kwargs):
        thread = threading.Thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(
        spawn,
        "threading",
        SimpleNamespace(
            Event=threading.Event,
            Thread=fail_thread if failure == "thread" else make_thread,
        ),
    )
    monkeypatch.setattr(spawn.subprocess, "Popen", fail_popen)
    return threads


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX zombie observation")
@pytest.mark.parametrize("code", [0, 3])
def test_exited_child_disappears_without_another_popen(monkeypatch, code):
    real_popen = subprocess.Popen
    children = []

    def launch(_cmd, **kwargs):
        child = real_popen([sys.executable, "-c", f"raise SystemExit({code})"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(spawn.subprocess, "Popen", launch)
    try:
        for _ in range(3):
            spawn._spawn_detached()
            child = children[-1]
            # kill(0) observes existence, including zombies, without reaping.
            # No Popen/poll/wait on this path can accidentally fix the defect.
            # The pid disappears the moment the reaper's waitpid returns, a few
            # statements before Popen records the status, so wait for both —
            # asserting on the pid alone races the reaper thread.
            wait_until(lambda: not exists(child.pid) and child.returncode is not None)
            assert child.returncode == code
            # The claim is what spares this pid from a teardown sweep. Reaping
            # frees the number, so the claim has to be retired, or it would
            # spare whatever child inherits it (#906). Retirement happens after
            # the wait returns, so poll for it rather than racing the reaper.
            claim = child_reaper._detached_claims[child.pid]
            wait_until(lambda: claim.retired_at is not None)
    finally:
        for child in children:
            with contextlib.suppress(ProcessLookupError):
                child.kill()  # a no-op once the reaper recorded the exit
            child.wait(timeout=5)


def test_wait_runs_after_claim_outside_lock(monkeypatch):
    done = threading.Event()
    observed = []

    class Child:
        pid = 31337

        def wait(self):
            acquired = child_reaper._detached_lock.acquire(timeout=1)
            claimed = child_reaper._detached_claims.get(self.pid)
            observed.append((claimed is not None and claimed.retired_at is None, acquired))
            if acquired:
                child_reaper._detached_lock.release()
            done.set()
            return 0

    monkeypatch.setattr(spawn.subprocess, "Popen", lambda *_a, **_kw: Child())
    spawn._spawn_detached()
    assert done.wait(5)
    assert observed == [(True, True)]
    # Claimed for the whole life of the child, and retired by the one thing that
    # proves the pid stopped naming it: the wait that consumed its exit status.
    claim = child_reaper._detached_claims[31337]
    wait_until(lambda: claim.retired_at is not None)


@pytest.mark.parametrize("failure", ["thread", "popen"])
def test_spawn_failure_does_not_leave_waiter_or_child(monkeypatch, tmp_path, failure):
    launched: list[bool] = []
    threads = break_spawn(monkeypatch, failure, launched)
    cfg = STMConfig(data_dir=tmp_path)
    assert spawn.request_spawn(cfg) is False
    assert bool(launched) == (failure == "popen")
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert not child_reaper._detached_claims  # nothing spawned, nothing claimed


@pytest.mark.parametrize("failure", ["thread", "popen"])
def test_spawn_error_reaches_the_caller_that_asks_for_it(monkeypatch, tmp_path, failure):
    # Degrading to False is right for the hot path, which only ever falls back
    # to cold surfacing. `mms daemon start` retries on a budget and reports at
    # the end, so a swallowed failure there costs it both: the budget is never
    # spent (a failed spawn is indistinguishable from a deferral) and the
    # timeout message blames a daemon log the child never got to write.
    cfg = STMConfig(data_dir=tmp_path)
    break_spawn(monkeypatch, failure, [])
    with pytest.raises(RuntimeError if failure == "thread" else OSError):
        spawn.request_spawn(cfg, propagate_errors=True)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process cleanup")
def test_host_exits_promptly_and_leaves_live_shared_child():
    code = """
import subprocess, sys
from memtomem_stm.daemon import spawn
real = subprocess.Popen

def launch(cmd, **kwargs):
    child = real([sys.executable, '-c', 'import time; time.sleep(15)'], **kwargs)
    print(child.pid, flush=True)
    return child
spawn.subprocess.Popen = launch
spawn._spawn_detached()
"""
    host = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    pid = None
    try:
        assert host.stdout is not None
        pid = int(host.stdout.readline())
        assert host.wait(timeout=5) == 0
        assert exists(pid), "host shutdown killed the shared daemon"
    finally:
        if pid is not None:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
        if host.poll() is None:
            host.kill()
        host.wait(timeout=5)
