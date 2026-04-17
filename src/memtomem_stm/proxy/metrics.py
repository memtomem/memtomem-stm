"""Proxy call metrics and token tracking."""

from __future__ import annotations

import logging
import math
import time as _time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem_stm.proxy.metrics_store import MetricsStore

logger = logging.getLogger(__name__)

# LRU cap for per-(server|tool) aggregate counters. Bounds worst-case memory on
# long-lived daemons or multi-tenant gateways where upstream names churn.
MAX_TRACKED_KEYS = 10_000


class _BoundedCounterDict:
    """LRU-bounded counter map with defaultdict-style lazy insertion.

    Bounds memory for long-lived trackers. When the size exceeds *max_size*,
    the least-recently-touched key is evicted. Both ``__getitem__`` and
    ``__setitem__`` count as a touch.
    """

    def __init__(self, factory: Callable[[], Any], max_size: int = MAX_TRACKED_KEYS) -> None:
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._factory = factory
        self._max_size = max_size

    def __getitem__(self, key: str) -> Any:
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        value = self._factory()
        self._data[key] = value
        if len(self._data) > self._max_size:
            self._data.popitem(last=False)
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        if len(self._data) > self._max_size:
            self._data.popitem(last=False)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self):
        return iter(self._data)

    def items(self):
        return self._data.items()


class ErrorCategory(StrEnum):
    """Classification of proxy call errors for metrics tracking."""

    TRANSPORT = "transport"  # OSError, ConnectionError, EOFError
    TIMEOUT = "timeout"  # asyncio.TimeoutError
    PROTOCOL = "protocol"  # JSON-RPC errors (-32600..-32603)
    UPSTREAM_ERROR = "upstream_error"  # result.isError=True from upstream
    PROGRAMMING = "programming"  # TypeError, AttributeError, etc.
    INTERNAL_ERROR = "internal_error"  # raised inside the COMPRESS/SURFACE/INDEX pipeline


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Compute the *p*-th percentile (0-100) from a pre-sorted list.

    Uses linear interpolation between closest ranks (same as numpy 'linear').
    """
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    k = (p / 100) * (n - 1)
    lo = int(math.floor(k))
    hi = min(lo + 1, n - 1)
    frac = k - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


@dataclass
class CallMetrics:
    server: str
    tool: str
    original_chars: int
    compressed_chars: int
    cleaned_chars: int = 0
    original_tokens: int = 0
    compressed_tokens: int = 0
    trace_id: str | None = None
    # Per-stage timing (ms) and surfacing size
    clean_ms: float = 0.0
    compress_ms: float = 0.0
    surface_ms: float = 0.0
    surfaced_chars: int = 0
    # Extraction tracking
    extraction_ms: float = 0.0
    extraction_facts: int = 0
    # Progressive delivery tracking
    progressive_enabled: bool = False
    progressive_chunk_index: int = 0
    # Error tracking
    is_error: bool = False
    error_category: ErrorCategory | None = None
    error_code: int | None = None
    # Compression fidelity tracking
    #
    # ``compression_strategy`` records the *effective* strategy used for this
    # call (with AUTO already resolved to a concrete strategy). ``None`` means
    # the strategy is unknown or the call did not go through compression
    # (e.g., cached hits that bypass the pipeline).
    #
    # ``ratio_violation`` is set True when the compressed length falls below
    # the dynamic ``min_result_retention`` budget enforced in ProxyManager.
    # This flags calls where compression cut more than the configured floor
    # — useful for auditing R4 (min_retention bypass) after the fact.
    compression_strategy: str | None = None
    ratio_violation: bool = False
    # Scorer fallback: True when EmbeddingScorer fell back to BM25 during this call.
    scorer_fallback: bool = False
    # Memory-pipeline status — populated by Stage 4 (INDEX) and Stage 4b (EXTRACT).
    #
    # Before these fields existed, ``auto_index_response`` and
    # ``extract_and_store`` swallowed failures internally and returned a
    # pass-through response. Operators watching ``proxy_metrics.db`` therefore
    # saw a healthy call rate while the memory pipeline was silently broken
    # (embedding service down, disk full, LTM connection dropped). Record the
    # outcome so dashboards can surface the break.
    #
    # ``None`` means "stage did not run for this call" — e.g. auto-index
    # disabled, response below ``min_chars`` threshold, or (for the
    # surfacing_on_progressive fields) the call did not go down the
    # progressive path. Readers must distinguish ``None`` from ``False``.
    index_ok: bool | None = None
    index_error: str | None = None
    chunks_indexed: int = 0
    extract_ok: bool | None = None
    extract_error: str | None = None
    # Surfacing on the progressive path — pre-provisioned for PR 2 (F6) so
    # the schema migration ships once. Stays ``None`` until the progressive
    # surfacing footer lands.
    surfacing_on_progressive_ok: bool | None = None
    surface_error: str | None = None


class RPSTracker:
    """Sliding-window requests-per-second counter."""

    def __init__(self, window_seconds: float = 60.0) -> None:
        self._window = window_seconds
        self._timestamps: deque[float] = deque()

    def record(self) -> None:
        self._timestamps.append(_time.monotonic())
        self._trim()

    def _trim(self) -> None:
        cutoff = _time.monotonic() - self._window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def rps(self) -> float:
        self._trim()
        if not self._timestamps:
            return 0.0
        return round(len(self._timestamps) / self._window, 2)

    def reset(self) -> None:
        self._timestamps.clear()


class TokenTracker:
    """Aggregate proxy call metrics (in-memory + optional persistent store)."""

    def __init__(
        self,
        metrics_store: MetricsStore | None = None,
        *,
        max_tracked_keys: int = MAX_TRACKED_KEYS,
    ) -> None:
        self._total_calls = 0
        self._total_original = 0
        self._total_compressed = 0
        self._total_surfaced = 0
        self._total_original_tokens = 0
        self._total_compressed_tokens = 0
        self._total_clean_ms = 0.0
        self._total_compress_ms = 0.0
        self._total_surface_ms = 0.0
        self._cache_hits = 0
        self._cache_misses = 0
        self._reconnects = 0
        self._metrics_store = metrics_store
        self._by_server = _BoundedCounterDict(
            lambda: {"calls": 0, "original_chars": 0, "compressed_chars": 0},
            max_size=max_tracked_keys,
        )
        self._by_tool = _BoundedCounterDict(
            lambda: {"calls": 0, "original_chars": 0, "compressed_chars": 0},
            max_size=max_tracked_keys,
        )
        self._rps_tracker = RPSTracker()
        # Error tracking
        self._total_errors = 0
        # _errors_by_category is bounded by the ErrorCategory enum — safe as defaultdict.
        self._errors_by_category: dict[str, int] = defaultdict(int)
        self._errors_by_server = _BoundedCounterDict(int, max_size=max_tracked_keys)
        # Progressive delivery tracking
        self._progressive_first_chunks = 0
        self._progressive_continuations = 0
        # Per-call latencies for percentile computation (bounded rolling window)
        _LATENCY_WINDOW = 10000
        self._clean_latencies: deque[float] = deque(maxlen=_LATENCY_WINDOW)
        self._compress_latencies: deque[float] = deque(maxlen=_LATENCY_WINDOW)
        self._surface_latencies: deque[float] = deque(maxlen=_LATENCY_WINDOW)
        self._total_latencies: deque[float] = deque(maxlen=_LATENCY_WINDOW)

    def record(self, metrics: CallMetrics) -> None:
        self._rps_tracker.record()
        self._total_calls += 1
        self._total_original += metrics.original_chars
        self._total_compressed += metrics.compressed_chars
        self._total_surfaced += metrics.surfaced_chars
        self._total_original_tokens += metrics.original_tokens
        self._total_compressed_tokens += metrics.compressed_tokens
        self._total_clean_ms += metrics.clean_ms
        self._total_compress_ms += metrics.compress_ms
        self._total_surface_ms += metrics.surface_ms

        self._clean_latencies.append(metrics.clean_ms)
        self._compress_latencies.append(metrics.compress_ms)
        self._surface_latencies.append(metrics.surface_ms)
        self._total_latencies.append(metrics.clean_ms + metrics.compress_ms + metrics.surface_ms)

        s = self._by_server[metrics.server]
        s["calls"] += 1
        s["original_chars"] += metrics.original_chars
        s["compressed_chars"] += metrics.compressed_chars

        t = self._by_tool[f"{metrics.server}/{metrics.tool}"]
        t["calls"] += 1
        t["original_chars"] += metrics.original_chars
        t["compressed_chars"] += metrics.compressed_chars

        # Persist to SQLite
        if self._metrics_store is not None:
            try:
                self._metrics_store.record(metrics)
            except Exception:
                logger.warning("Failed to persist metrics", exc_info=True)

    def record_cache_hit(self) -> None:
        self._cache_hits += 1

    def record_cache_miss(self) -> None:
        self._cache_misses += 1

    def record_reconnect(self) -> None:
        self._reconnects += 1

    def record_progressive_first(self) -> None:
        self._progressive_first_chunks += 1

    def record_progressive_continuation(self) -> None:
        self._progressive_continuations += 1

    def record_error(self, metrics: CallMetrics) -> None:
        """Record a failed tool call for error tracking."""
        self._rps_tracker.record()
        self._total_errors += 1
        if metrics.error_category is not None:
            self._errors_by_category[metrics.error_category.value] += 1
        self._errors_by_server[metrics.server] += 1

        if self._metrics_store is not None:
            try:
                self._metrics_store.record(metrics)
            except Exception:
                logger.warning("Failed to persist error metrics", exc_info=True)

    def _percentiles(self, values: deque[float] | list[float]) -> dict[str, float]:
        """Return p50/p95/p99 for a list of latency values."""
        if not values:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
        s = sorted(values)
        return {
            "p50": round(_percentile(s, 50), 2),
            "p95": round(_percentile(s, 95), 2),
            "p99": round(_percentile(s, 99), 2),
        }

    def get_summary(self) -> dict:
        savings = (
            round((1 - self._total_compressed / self._total_original) * 100, 1)
            if self._total_original > 0
            else 0.0
        )

        by_server = {}
        for name, s in self._by_server.items():
            pct = (
                round((1 - s["compressed_chars"] / s["original_chars"]) * 100, 1)
                if s["original_chars"] > 0
                else 0.0
            )
            by_server[name] = {**s, "savings_pct": pct}

        n = self._total_calls or 1
        return {
            "total_calls": self._total_calls,
            "total_original_chars": self._total_original,
            "total_compressed_chars": self._total_compressed,
            "total_surfaced_chars": self._total_surfaced,
            "total_original_tokens": self._total_original_tokens,
            "total_compressed_tokens": self._total_compressed_tokens,
            "total_token_savings_pct": (
                round((1 - self._total_compressed_tokens / self._total_original_tokens) * 100, 1)
                if self._total_original_tokens > 0
                else 0.0
            ),
            "total_savings_pct": savings,
            "avg_clean_ms": round(self._total_clean_ms / n, 2),
            "avg_compress_ms": round(self._total_compress_ms / n, 2),
            "avg_surface_ms": round(self._total_surface_ms / n, 2),
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "reconnects": self._reconnects,
            "latency_percentiles": {
                "clean_ms": self._percentiles(self._clean_latencies),
                "compress_ms": self._percentiles(self._compress_latencies),
                "surface_ms": self._percentiles(self._surface_latencies),
                "total_ms": self._percentiles(self._total_latencies),
            },
            "current_rps": self._rps_tracker.rps(),
            "total_errors": self._total_errors,
            "errors_by_category": dict(self._errors_by_category),
            "error_rate": (
                round(self._total_errors / (self._total_calls + self._total_errors) * 100, 1)
                if (self._total_calls + self._total_errors) > 0
                else 0.0
            ),
            "progressive_first_chunks": self._progressive_first_chunks,
            "progressive_continuations": self._progressive_continuations,
            "by_server": by_server,
        }
