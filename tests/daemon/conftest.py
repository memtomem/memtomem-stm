"""Guards for the daemon tests that touch the process-global claim table."""

from __future__ import annotations

import threading

import pytest

from memtomem_stm.utils import child_reaper


@pytest.fixture(autouse=True)
def isolated_detached_claims(monkeypatch: pytest.MonkeyPatch):
    """Claim into a per-test table, and let no reaper outlive the test.

    ``_detached_claims`` is process-global and nothing expires a live claim, so
    a test that leaves a real (immediately recyclable) pid behind spares
    whatever inherits that number — including a genuine leak in a later
    ``real_child_sweep`` test, which would then pass vacuously on the sweep's
    central contract.

    Joining the reapers is the other half, and it belongs here rather than in
    one module: a reaper still waiting on a fake child retires its claim
    against whatever globals are current when it wakes, which after teardown
    are the restored process-wide ones.
    """
    monkeypatch.setattr(child_reaper, "_detached_claims", {})
    monkeypatch.setattr(child_reaper, "_active_probes", {})
    yield
    for thread in threading.enumerate():
        if thread.name == "stm-daemon-reaper":
            thread.join(timeout=5)
            assert not thread.is_alive(), "a reaper thread outlived its test"
