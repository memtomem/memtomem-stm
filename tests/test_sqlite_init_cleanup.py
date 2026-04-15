"""Init-time SQLite connection must be closed if tuning/schema setup fails.

If the new connection is left open, repeated init failures (e.g. a
transient ``sqlite3`` corruption) accumulate file descriptors and a stale
write lock, eventually preventing recovery on restart. Each store must
close the in-progress connection and leave ``self._db is None`` so the
store reports as un-initialized rather than half-initialized.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.compression_feedback_store import CompressionFeedbackStore
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.proxy.pending_store import SQLitePendingStore
from memtomem_stm.surfacing.feedback_store import FeedbackStore


_CASES: list[tuple[str, type[Any], str]] = [
    ("memtomem_stm.surfacing.feedback_store", FeedbackStore, "feedback.db"),
    (
        "memtomem_stm.proxy.compression_feedback_store",
        CompressionFeedbackStore,
        "cfb.db",
    ),
    ("memtomem_stm.proxy.pending_store", SQLitePendingStore, "pending.db"),
    ("memtomem_stm.proxy.cache", ProxyCache, "cache.db"),
    ("memtomem_stm.proxy.metrics_store", MetricsStore, "metrics.db"),
]


@pytest.mark.parametrize("module_path, store_cls, filename", _CASES)
def test_initialize_releases_connection_on_tune_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module_path: str,
    store_cls: type[Any],
    filename: str,
) -> None:
    mod = importlib.import_module(module_path)

    def boom(_db: Any) -> None:
        raise RuntimeError("simulated tune_connection failure")

    monkeypatch.setattr(mod, "tune_connection", boom)

    store = store_cls(tmp_path / filename)
    with pytest.raises(RuntimeError, match="simulated tune_connection failure"):
        store.initialize()

    assert store._db is None

    monkeypatch.setattr(mod, "tune_connection", lambda _db: None)
    store.initialize()
    assert store._db is not None
    store.close()
