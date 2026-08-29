"""Shared fixtures for memtomem-stm tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.feedback_store import FeedbackStore
from helpers import set_home


@pytest.fixture(autouse=True)
def isolate_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Keep the developer's own ``~/.memtomem`` out of every test.

    ``STMConfig()`` reads the proxy config file when the environment overrides
    a field of an upstream server (#835), and its path defaults under ``~``. A
    contributor whose real ``stm_proxy.json`` happens to declare a server named
    like a fixture's would otherwise see that file complete the fixture's
    config — a failure that reproduces on one machine only. Redirecting the
    home directory isolates that default (and every other ``expanduser()``)
    without adding a ``MEMTOMEM_STM_*`` variable that tests would then see.

    This holds its OWN ``MonkeyPatch`` rather than taking the fixture: a test
    that calls ``monkeypatch.undo()`` (``test_helpers.py`` does, to prove
    ``set_home`` is undoable) would otherwise unwind this isolation too and
    restore the real home mid-test.
    """
    patch = pytest.MonkeyPatch()
    set_home(patch, tmp_path_factory.mktemp("home"))
    # HOME isolation alone no longer covers the config path: since #848 an
    # ambient MEMTOMEM_STM_PROXY__CONFIG_PATH (any case-equivalent spelling,
    # or the bare MEMTOMEM_STM_PROXY payload) steers no-flag CLI commands
    # straight past the redirected home to a developer's real config. Clear
    # exactly the variables that can name a config path; tests that exercise
    # them set their own afterward.
    for name in list(os.environ):
        lowered = name.lower()
        if lowered in ("memtomem_stm_proxy__config_path", "memtomem_stm_proxy"):
            patch.delenv(name, raising=False)
    yield
    patch.undo()


@pytest.fixture
def surfacing_config() -> SurfacingConfig:
    """SurfacingConfig with short timeouts, no webhooks."""
    return SurfacingConfig(
        enabled=True,
        timeout_seconds=1.0,
        fire_webhook=False,
        feedback_enabled=True,
        auto_tune_enabled=True,
        cache_ttl_seconds=5.0,
        cooldown_seconds=1.0,
    )


@pytest.fixture
def feedback_store(tmp_path: Path) -> FeedbackStore:
    db = tmp_path / "test_feedback.db"
    store = FeedbackStore(db)
    store.initialize()
    yield store
    store.close()


@pytest.fixture
def proxy_cache(tmp_path: Path) -> ProxyCache:
    db = tmp_path / "test_cache.db"
    cache = ProxyCache(db, max_entries=100)
    cache.initialize()
    yield cache
    cache.close()


@pytest.fixture
def token_tracker() -> TokenTracker:
    return TokenTracker(metrics_store=None)


@pytest.fixture(autouse=True)
def _no_real_child_sweep(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test's teardown sweep the test runner's own children.

    ``app_lifespan``'s teardown terminates this process's direct children as
    the #906 backstop. Under pytest "this process" is the test runner, whose
    children are xdist workers and whatever subprocess a fixture spawned — so
    any test driving the lifespan to teardown would kill them, machine- and
    ordering-dependently. Report none by default; a test that needs the real
    probe marks itself ``real_child_sweep`` and is then responsible for its own
    children.
    """
    if request.node.get_closest_marker("real_child_sweep"):
        return
    monkeypatch.setattr("memtomem_stm.utils.child_reaper.direct_child_pids", lambda: set())
    # The daemon binds the probe to its own module name at import, so patching
    # only child_reaper leaves its teardown sweep running the real pgrep.
    monkeypatch.setattr("memtomem_stm.daemon.server._direct_child_pids", lambda: set())
