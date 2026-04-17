"""ProxyManager runner for bench_qa — fake upstream + per-run config isolation.

Design origin: ``tests/test_compression_ratio_guard.py::_make_manager_with_store``
already drives ``ProxyManager.call_tool()`` with an AsyncMock session. This
module generalizes that pattern for the bench_qa suite:

* every run gets its own ``tmp_path`` so ``proxy_metrics.db`` /
  ``stm_feedback.db`` / progressive store do not cross-talk;
* ``trace_id`` is derived deterministically from ``scenario_id`` + ``run_seed``
  so determinism-diff checks are meaningful;
* later PRs wire a real in-memory LTM adapter for ``surf-*`` scenarios
  (not needed for P1 smoke).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from memtomem_stm.proxy.config import (
    CompressionFeedbackConfig,
    CompressionStrategy,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.surfacing.config import SurfacingConfig


# Drift detector: ``CompressionFeedbackConfig.db_path`` and
# ``SurfacingConfig.feedback_db_path`` default to the *same* ``stm_feedback.db``
# in production (plan §Per-run isolation). Bench harness queries assume this —
# if a future PR bumps one default without the other, the scenario-level
# ``SELECT ... WHERE trace_id IN (:ids)`` would silently return empty rows.
# Fail loudly at import time instead.
_compression_default = CompressionFeedbackConfig().db_path
_surfacing_default = SurfacingConfig().feedback_db_path
assert _compression_default == _surfacing_default, (
    "bench_qa invariant: CompressionFeedbackConfig.db_path "
    f"({_compression_default}) must match SurfacingConfig.feedback_db_path "
    f"({_surfacing_default}); both point at the same stm_feedback.db in "
    "production. Update both defaults together."
)


_COMPRESSION_STRATEGY_BY_NAME: dict[str, CompressionStrategy] = {
    s.value: s for s in CompressionStrategy
}


def deterministic_trace_id(scenario_id: str, run_seed: int = 0) -> str:
    """Stable trace_id so two bench runs produce identical ``proxy_metrics.trace_id``.

    Using random UUIDs would make report-diff determinism checks always red.
    The ``"bench-"`` prefix lets operators filter bench rows out of production
    tuner dashboards.
    """
    digest = hashlib.sha256(f"{scenario_id}:{run_seed}".encode()).hexdigest()[:16]
    return f"bench-{digest}"


def _resolve_compression(name: str) -> CompressionStrategy:
    if name not in _COMPRESSION_STRATEGY_BY_NAME:
        raise AssertionError(
            f"expected_compressor={name!r} is not a valid CompressionStrategy; "
            f"known values: {sorted(_COMPRESSION_STRATEGY_BY_NAME)}"
        )
    return _COMPRESSION_STRATEGY_BY_NAME[name]


def make_proxy_manager(
    tmp_path: Path,
    *,
    compression: str | CompressionStrategy = CompressionStrategy.AUTO,
    max_result_chars: int = 50_000,
    min_retention: float = 0.65,
    server_name: str = "fake",
    tool_prefix: str = "fake",
) -> tuple[ProxyManager, MetricsStore, AsyncMock]:
    """Build a ``ProxyManager`` wired to a fresh per-run MetricsStore and an
    AsyncMock upstream session. Returns ``(manager, metrics_store, session)``.

    Callers set ``session.call_tool.return_value`` to the fixture payload
    (wrap plain strings with ``make_tool_result``).
    """
    if isinstance(compression, str):
        compression = _resolve_compression(compression)

    store = MetricsStore(tmp_path / "proxy_metrics.db")
    store.initialize()
    server_cfg = UpstreamServerConfig(
        prefix=tool_prefix,
        compression=compression,
        max_result_chars=max_result_chars,
        max_retries=0,
        reconnect_delay_seconds=0.0,
    )
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={server_name: server_cfg},
        min_result_retention=min_retention,
    )
    tracker = TokenTracker(metrics_store=store)
    mgr = ProxyManager(proxy_cfg, tracker)
    session = AsyncMock()
    mgr._connections[server_name] = UpstreamConnection(
        name=server_name,
        config=server_cfg,
        session=session,
        tools=[],
    )
    return mgr, store, session


def make_tool_result(text: str) -> SimpleNamespace:
    """Wrap a string in the MCP CallToolResult shape the proxy expects."""
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], isError=False)


def latest_metrics_row(store: MetricsStore) -> dict:
    """Read the most recent proxy_metrics row as a dict.

    Mirrors ``tests/test_compression_ratio_guard.py::_latest_row`` so both
    files converge on the same shape if more columns are added.
    """
    assert store._db is not None, "MetricsStore must be initialize()d before read"
    row = store._db.execute(
        "SELECT server, tool, original_chars, cleaned_chars, compressed_chars, "
        "compression_strategy, ratio_violation, trace_id "
        "FROM proxy_metrics ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {}
    return {
        "server": row[0],
        "tool": row[1],
        "original_chars": row[2],
        "cleaned_chars": row[3],
        "compressed_chars": row[4],
        "compression_strategy": row[5],
        "ratio_violation": row[6],
        "trace_id": row[7],
    }
