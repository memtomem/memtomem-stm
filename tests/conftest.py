"""Shared fixtures for memtomem-stm tests."""

from __future__ import annotations

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
