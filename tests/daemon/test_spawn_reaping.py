"""Detached children must be reaped without another spawn or host shutdown."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from memtomem_stm.config import STMConfig
from memtomem_stm.daemon import spawn
from memtomem_stm.utils import child_reaper


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
            # kill(0) observes existence, including zombies, without reaping.
            # No Popen/poll/wait on this path can accidentally fix the defect.
            wait_until(lambda: not exists(children[-1].pid))
            assert children[-1].returncode == code
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_wait_runs_after_claim_outside_lock(monkeypatch):
    done = threading.Event()
    observed = []

    class Child:
        pid = 31337

        def wait(self):
            acquired = child_reaper._detached_lock.acquire(timeout=1)
            observed.append((self.pid in child_reaper._detached_pids, acquired))
            if acquired:
                child_reaper._detached_lock.release()
            done.set()
            return 0

    monkeypatch.setattr(spawn.subprocess, "Popen", lambda *_a, **_kw: Child())
    spawn._spawn_detached()
    assert done.wait(5)
    assert observed == [(True, True)]


@pytest.mark.parametrize("failure", ["thread", "popen"])
def test_spawn_failure_does_not_leave_waiter_or_child(monkeypatch, tmp_path, failure):
    started = []
    real_thread = threading.Thread
    launched = []

    def make_thread(**kwargs):
        thread = real_thread(**kwargs)
        started.append(thread)
        return thread

    def fail_thread(**kwargs):
        raise RuntimeError("cannot start thread")

    def fail_popen(*args, **kwargs):
        launched.append(True)
        raise OSError("cannot spawn")

    monkeypatch.setattr(
        spawn.threading, "Thread", fail_thread if failure == "thread" else make_thread
    )
    monkeypatch.setattr(spawn.subprocess, "Popen", fail_popen)
    cfg = STMConfig(data_dir=tmp_path)
    assert spawn.request_spawn(cfg) is False
    assert bool(launched) == (failure == "popen")
    for thread in started:
        thread.join(timeout=5)
        assert not thread.is_alive()


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
