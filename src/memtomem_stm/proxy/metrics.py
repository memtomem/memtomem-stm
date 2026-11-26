"""Proxy call metrics and token tracking."""

from __future__ import annotations

import asyncio
import logging
import math
import time as _time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TYPE_CHECKING

from memtomem_stm.utils.json_out import has_lone_surrogate

if TYPE_CHECKING:
    from memtomem_stm.proxy.metrics_store import MetricsStore

logger = logging.getLogger(__name__)

# LRU cap for per-(server|tool) aggregate counters. Bounds worst-case memory on
# long-lived daemons or multi-tenant gateways where upstream names churn.
MAX_TRACKED_KEYS = 10_000

# Cap for ``CallMetrics.error_message`` persisted in ``proxy_metrics.db``.
# Long enough for STM-composed messages (which can name a server or tool);
# short enough that no single row can blow up the DB.
MAX_ERROR_MESSAGE_CHARS = 500

# Per-stage latency samples kept for the p50/p95/p99 in ``get_summary()``: a
# rolling window of the most recent successful live calls, not lifetime.
LATENCY_WINDOW = 10_000

# Response-cache counts waiting for the next metrics write (#1329), keyed by
# ``(UTC day, server, tool)``; the value is ``[hits, hit_chars, misses,
# unstorable]``, the column order of ``proxy_cache_daily``. The cap bounds a
# process whose store keeps failing: a bucket beyond it is dropped (live
# counters still count) rather than growing without limit.
CacheBucketKey = tuple[str, str, str]
CacheDeltas = dict[CacheBucketKey, list[int]]
MAX_CACHE_BUCKETS = 1024

# How often a running proxy tries to write cache counts that no metrics write
# has carried yet, so a proxy serving only cache hits still reaches
# ``mms stats``. A process that exits abruptly loses whatever is still
# buffered: since the last successful write, which a busy loop or a failing
# store can push past one interval. The write runs on the event loop like every
# per-call metrics write, through its own connection with a short lock budget:
# a DB another process holds locked makes it fail fast and retry on the next
# tick, instead of stalling every call on the loop for the full busy timeout.
CACHE_FLUSH_INTERVAL_SECONDS = 30.0
CACHE_FLUSH_BUSY_TIMEOUT_MS = 50


def _utc_day() -> str:
    """The UTC calendar day a cache event is counted under."""
    return _time.strftime("%Y-%m-%d", _time.gmtime(_time.time()))


# ``compression_strategy`` of a response whose annotated text blocks were kept
# apart instead of compressed (#1112): no compressor ran, so the base is
# ``none``, and the suffix names why. The tuner drops these rows by this exact
# value, so the writer and that reader share this one definition.
METADATA_BYPASS_STRATEGY = "none→metadata_bypass"
STRUCTURED_BYPASS_STRATEGY = "none→structured_bypass"

# ``server`` of a ``source='hook'`` row. Native tools have no upstream server,
# so the column names the host that ran the tool instead: ``builtin:<host_tag>``.
# The host rides in ``server`` rather than in a column of its own because the
# daily rollup is keyed by ``(day, source, server, tool)``: per-host totals then
# survive trimming with no change to the rollup trigger, and the schema
# fingerprint other STM versions compare against stays the same. A bare
# ``builtin`` is a row written before the host was recorded.
HOOK_SERVER = "builtin"
_HOOK_SERVER_PREFIX = HOOK_SERVER + ":"


def hook_server_label(host_tag: str) -> str:
    """The ``server`` value ``mms hook`` records for a call from ``host_tag``."""
    return f"{_HOOK_SERVER_PREFIX}{host_tag}" if host_tag else HOOK_SERVER


def hook_host_of(server: str) -> str | None:
    """The host tag in a hook row's ``server``, or ``None`` when none was recorded.

    Only meaningful for ``source='hook'`` rows: an upstream MCP server may carry
    any name, this prefix included.
    """
    if not server.startswith(_HOOK_SERVER_PREFIX):
        return None
    return server[len(_HOOK_SERVER_PREFIX) :] or None


# The values of ``CallMetrics.context_query_source``.
CONTEXT_QUERY_AGENT = "agent"
CONTEXT_QUERY_NONE = "none"


def context_query_source(context_query: str | None) -> str:
    """Whether the caller supplied a usable ``_context_query`` on this call.

    ``agent`` for a string with something other than whitespace in it, ``none``
    for everything else: the same test the compressors apply before they score
    with it. It says a query arrived, not that anything used it: whether a
    scorer ran depends on the path the response took.
    """
    return CONTEXT_QUERY_AGENT if context_query and context_query.strip() else CONTEXT_QUERY_NONE


# Every value ``mms hook`` stores in ``hook_compression_status``: what the
# native output-compression stage decided for that call. Readers render only
# these names and fold anything else into "other", so a value written by
# something other than this code never reaches a terminal verbatim.
HOOK_COMPRESSION_STATUSES = frozenset(
    {
        "compressed",
        # Not compressed, by gate:
        "below_threshold",
        "already_compressed",
        "retention_guard",
        "budget_too_small",
        "unsafe_result",
        # Not a candidate for compression:
        "disabled",
        "ineligible_event",
        "ineligible_tool",
        "unsupported_shape",
        # The host cannot replace tool output (Codex, Cursor, Kimi).
        "unsupported_host",
        # The host can, but the hook was not registered with ``--host claude``
        # (#1437): replacement is refused under host auto-detection.
        "replacement_not_requested",
        "error",
    }
)


def format_error_message_from_exc(exc: BaseException) -> str:
    """Format an exception for ``CallMetrics.error_message`` (capped).

    Only for exceptions whose text STM composed (the overall-deadline
    ``TimeoutError``): the message is kept verbatim. An exception from an
    upstream or its transport goes through ``proxy/manager.py:_safe_error_text``
    instead, which drops the message (#1082). UPSTREAM_ERROR rows store a
    fixed summary of the upstream tool's ``isError`` text, not the text itself
    (``proxy/manager.py:_upstream_error_row_text``, #1084).
    """
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_MESSAGE_CHARS]


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
    TIMEOUT = "timeout"  # asyncio.TimeoutError on upstream call_tool (#206)
    # JSON-RPC errors (-32600..-32603), or an upstream result the SDK rejects
    # (no ``error_code`` then)
    PROTOCOL = "protocol"
    UPSTREAM_ERROR = "upstream_error"  # result.isError=True from upstream
    DELIVERY = "delivery"  # configured structured output budget cannot make progress
    OVERSIZE = "oversize"  # inbound MCP message/result exceeded byte budget
    PROGRAMMING = "programming"  # TypeError, AttributeError, etc.
    INTERNAL_ERROR = "internal_error"  # raised inside COMPRESS/SURFACE/INDEX pipeline
    # Internal state lock (#208) acquisition exceeded ``lock_timeout_seconds``;
    # call propagates as an MCP error since a lock timeout indicates a bug
    # (deadlock, stuck holder), not a slow external dependency.
    LOCK_TIMEOUT = "lock_timeout"
    # Fast-fail: the per-upstream circuit breaker was open, upstream not called
    # (#608). Distinct from TRANSPORT/TIMEOUT so dashboards can separate real
    # faults from breaker rejections during an outage window.
    CIRCUIT_OPEN = "circuit_open"
    # Note: LLM compression timeout (#207) is NOT an error category — the call
    # succeeds via graceful truncate fallback. The signal lives in the
    # ``compression_strategy`` column as ``"llm_summary→timeout_fallback"``;
    # dashboards filter via ``WHERE compression_strategy LIKE '%timeout_fallback%'``.


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Compute the *p*-th percentile (0-100) from a pre-sorted list.

    Uses linear interpolation between closest ranks (same as numpy 'linear').
    The difference form ``lo + frac*(hi-lo)`` is the accurate one and stays
    the answer while ``hi - lo`` is finite; it overflows to ``inf`` on finite
    inputs of opposite sign near the float limit, where the equivalent
    weighted form takes over — a percentile always lies between two samples,
    so it is never itself unrepresentable (#856).
    """
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    k = (p / 100) * (n - 1)
    lo = int(math.floor(k))
    hi = min(lo + 1, n - 1)
    if sorted_vals[lo] == sorted_vals[hi]:
        return sorted_vals[lo]
    frac = k - lo
    span = sorted_vals[hi] - sorted_vals[lo]
    if math.isfinite(span):
        return sorted_vals[lo] + frac * span
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


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
    # Why a call failed, populated by ProxyManager at the same call sites that
    # set ``error_category`` so post-mortem inspection of ``proxy_metrics.db``
    # can tell failures apart without re-running them. For an exception it is
    # the fixed vocabulary of ``proxy/manager.py:_safe_error_text`` — type name,
    # ``HTTP <code>``, a reserved JSON-RPC code, or STM-composed text — never
    # the exception's message (#1082). For an upstream ``isError=True`` result
    # it is ``upstream isError (<n> chars)`` or the non-text placeholder, never
    # the upstream's text (#1084). Callers cap free text at
    # ``MAX_ERROR_MESSAGE_CHARS``; the fixed ``isError`` summary is short by
    # construction.
    error_message: str | None = None
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
    # Did the AUTO selector run for this call? The column above cannot say —
    # AUTO is resolved before it is written — so a reader generalizing about
    # AUTO's behavior from these rows was reading calls that may have run under
    # a pin (#933).
    #
    # Strictly "the selector ran", not "this label is what it chose": the
    # ratio-guard ladder rewrites the label afterwards, so an AUTO call that
    # degraded records ``hybrid→progressive_fallback`` with this flag True.
    # What the flag delimits is the population of calls AUTO decided; the label
    # beside it keeps its own meaning, which is what ran.
    #
    # Tri-state, and the ``None`` arm is the load-bearing one:
    #
    #   ``None``  — no provenance recorded. Rows written before this column
    #               existed, rows with no strategy at all, and ``mms hook``
    #               rows, which never run the selector. NOT "the selector did
    #               not run": provenance is simply unknown, so a reader that
    #               needs AUTO calls must exclude these rather than count them
    #               either way.
    #   ``True``  — ``auto_select_strategy`` ran and picked this call's
    #               strategy.
    #   ``False`` — it did not: the strategy was in force before the call ran,
    #               or a branch settled the strategy without consulting it.
    strategy_auto_selected: bool | None = None
    ratio_violation: bool = False
    # Scorer fallback: True when EmbeddingScorer fell back to BM25 during this call
    # because its endpoint failed. A pass routed to BM25 by the credential screen
    # (``relevance_scorer.privacy_scan_enabled``) asked no endpoint and is not one.
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
    # Tri-state contract for the nullable outcome columns (``index_ok``,
    # ``extract_ok``, ``surfacing_on_progressive_ok``). Stated in full here,
    # where the schema lives; other sites keep only the part they act on.
    #
    #   ``None``  — this row records NO outcome. Either the stage did not run
    #               (disabled, engine missing, below ``min_chars``, or for the
    #               surfacing_on_progressive fields the call did not take the
    #               progressive path), or it was SCHEDULED in the background
    #               and its result is never written back to this row.
    #   ``True``  — recorded success (including a terminal pre-skip, e.g. the
    #               privacy gate declining before any write).
    #   ``False`` — recorded NON-success: the stage ran and failed, or its
    #               scheduling was refused (shed at the background cap or
    #               during ``stop()``, #868). The paired ``*_error`` column
    #               distinguishes the two.
    #
    # Readers must therefore distinguish ``None`` from ``False``: the first is
    # "nothing to report here", the second is "reported, and it did not
    # succeed".
    index_ok: bool | None = None
    index_error: str | None = None
    chunks_indexed: int = 0
    extract_ok: bool | None = None
    extract_error: str | None = None
    # Surfacing on the progressive path (F6). ``None`` when the call did
    # not go through progressive *or* when ``injection_mode='prepend'``
    # which still skips surfacing to preserve the
    # ``stm_proxy_read_more`` offset invariant. ``True``/``False`` once
    # progressive surfacing ran successfully or failed.
    surfacing_on_progressive_ok: bool | None = None
    surface_error: str | None = None
    # Provenance of the row in the shared ``proxy_metrics.db``. ``"mcp"`` (the
    # default) is a proxied upstream MCP tool call written by ``ProxyManager``;
    # ``"hook"`` is a native/built-in tool call (Read/Bash/Grep/…) recorded by
    # ``mms hook``, which never reaches the MCP proxy. The column lets readers
    # (CLI/stats, the tuner) separate native-tool spend — otherwise invisible —
    # from proxied spend without splitting the store.
    source: str = "mcp"
    # None marks old/unclassified writers; never infer their accounting basis.
    compression_accounting: str | None = None
    delivery_format: str = "legacy"
    wire_bytes: int | None = None
    # ``source='hook'`` rows only: the native compression stage's decision, one
    # of ``HOOK_COMPRESSION_STATUSES``. ``None`` on MCP rows and on hook rows
    # written before the column existed. Kept apart from
    # ``compression_strategy``, which names a strategy that ran — a call the
    # gate passed over ran none. Last, so no positional caller shifts.
    hook_compression_status: str | None = None
    # ``context_query_source(...)`` for the two MCP success rows that carry a
    # ``compression_accounting`` stamp. ``None`` on every other row — errors,
    # a response with no text, ``mms hook`` rows, rows written before the
    # column — which a reader leaves out rather than counts as "no query".
    # Only the fact is kept; the query's text is never stored here.
    context_query_source: str | None = None


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
        self._cache_hit_chars = 0
        self._cache_misses = 0
        self._cache_unstorable = 0
        self._reconnects = 0
        self._metrics_store = metrics_store
        # Attributed cache counts not yet on disk. They ride the next
        # ``record``/``record_error`` write, the periodic flush, or
        # ``flush_cache_counts`` at shutdown, so the hit path itself never
        # touches SQLite (#1329).
        self._cache_deltas: CacheDeltas = {}
        self._cache_cap_warned = False
        # Set by a failed periodic flush, cleared by any committed batch: one
        # warning per failure streak, not one every tick.
        self._periodic_flush_warned = False
        self._by_server = _BoundedCounterDict(
            lambda: {"calls": 0, "original_chars": 0, "compressed_chars": 0},
            max_size=max_tracked_keys,
        )
        self._by_tool = _BoundedCounterDict(
            lambda: {"calls": 0, "original_chars": 0, "compressed_chars": 0},
            max_size=max_tracked_keys,
        )
        self._structured = {
            "calls": 0,
            "original_chars": 0,
            "serialized_base_chars": 0,
            "wire_bytes": 0,
        }
        self._rps_tracker = RPSTracker()
        # Error tracking
        self._total_errors = 0
        # _errors_by_category is bounded by the ErrorCategory enum — safe as defaultdict.
        self._errors_by_category: dict[str, int] = defaultdict(int)
        self._errors_by_server = _BoundedCounterDict(int, max_size=max_tracked_keys)
        # Progressive delivery tracking
        self._progressive_first_chunks = 0
        self._progressive_continuations = 0
        # LTM hints from parent trust-UX channel (parent PR #231). Count is a
        # per-surfacing-call event count (not a per-hint count) to keep the
        # units intuitive for operators reading ``stm_proxy_stats``; the last
        # count is how many hints the most recent non-empty list carried.
        self._total_hint_events = 0
        self._last_hint_count = 0
        # Per-call latencies for percentile computation (bounded rolling window)
        self._clean_latencies: deque[float] = deque(maxlen=LATENCY_WINDOW)
        self._compress_latencies: deque[float] = deque(maxlen=LATENCY_WINDOW)
        self._surface_latencies: deque[float] = deque(maxlen=LATENCY_WINDOW)
        self._total_latencies: deque[float] = deque(maxlen=LATENCY_WINDOW)

    @property
    def metrics_store(self) -> MetricsStore | None:
        """The persistent store this tracker writes through, if any.

        Read-only access for consumers that need the persisted history
        (e.g. the #465 exposure health filter) without a second wiring
        path for the same object.
        """
        return self._metrics_store

    def record(self, metrics: CallMetrics) -> None:
        self._rps_tracker.record()
        self._total_calls += 1
        is_structured = metrics.compression_accounting == "initial_structured_v1"
        if is_structured:
            self._structured["calls"] += 1
            self._structured["original_chars"] += metrics.original_chars
            self._structured["serialized_base_chars"] += metrics.compressed_chars
            self._structured["wire_bytes"] += metrics.wire_bytes or 0
        original = 0 if is_structured else metrics.original_chars
        compressed = 0 if is_structured else metrics.compressed_chars
        self._total_original += original
        self._total_compressed += compressed
        self._total_surfaced += metrics.surfaced_chars
        self._total_original_tokens += 0 if is_structured else metrics.original_tokens
        self._total_compressed_tokens += 0 if is_structured else metrics.compressed_tokens
        self._total_clean_ms += metrics.clean_ms
        self._total_compress_ms += metrics.compress_ms
        self._total_surface_ms += metrics.surface_ms

        self._clean_latencies.append(metrics.clean_ms)
        self._compress_latencies.append(metrics.compress_ms)
        self._surface_latencies.append(metrics.surface_ms)
        self._total_latencies.append(metrics.clean_ms + metrics.compress_ms + metrics.surface_ms)

        s = self._by_server[metrics.server]
        s["calls"] += 1
        s["original_chars"] += original
        s["compressed_chars"] += compressed

        t = self._by_tool[f"{metrics.server}/{metrics.tool}"]
        t["calls"] += 1
        t["original_chars"] += original
        t["compressed_chars"] += compressed

        # Persist to SQLite
        self._persist(metrics, "Failed to persist metrics")

    def _persist(self, metrics: CallMetrics, failure: str) -> None:
        """Write one metrics row, carrying the buffered cache counts with it.

        The counts are taken out of the buffer first and merged back unless
        the store reports them committed, so each count reaches disk exactly
        once. A store failure stays best-effort, as before #1329. Counts are
        only passed when there are some, so a store without the keyword (a
        test double) keeps working."""
        store = self._metrics_store
        if store is None:
            return
        deltas, self._cache_deltas = self._cache_deltas, {}
        try:
            if deltas:
                committed = store.record(metrics, cache_deltas=deltas)
            else:
                store.record(metrics)
                committed = True
        except Exception:
            self._restore_cache_deltas(deltas)
            logger.warning(failure, exc_info=True)
            return
        if not committed:
            self._restore_cache_deltas(deltas)
        elif deltas:
            self._periodic_flush_warned = False

    def flush_cache_counts(self, *, periodic: bool = False) -> None:
        """Write the buffered cache counts on their own.

        A failure is logged and the counts stay buffered; nothing raises.
        ``periodic`` is the timer's (and the early teardown) attempt: it uses
        the short ``CACHE_FLUSH_BUSY_TIMEOUT_MS`` lock budget and warns once
        per failure streak. The final shutdown attempt keeps the store's full
        budget and warns every time."""
        store = self._metrics_store
        if store is None or not self._cache_deltas:
            return
        deltas, self._cache_deltas = self._cache_deltas, {}
        try:
            if periodic:
                store.record_cache_counts(deltas, busy_timeout_ms=CACHE_FLUSH_BUSY_TIMEOUT_MS)
            else:
                store.record_cache_counts(deltas)
        except Exception:
            self._restore_cache_deltas(deltas)
            if not periodic:
                logger.warning("Failed to persist cache counts", exc_info=True)
            elif not self._periodic_flush_warned:
                self._periodic_flush_warned = True
                logger.warning(
                    "Failed to persist cache counts; kept for the next write "
                    "(further periodic failures are not logged until one succeeds)",
                    exc_info=True,
                )
            return
        self._periodic_flush_warned = False

    async def run_cache_flush_loop(
        self, interval_seconds: float = CACHE_FLUSH_INTERVAL_SECONDS
    ) -> None:
        """Flush the buffered cache counts every ``interval_seconds`` until
        cancelled. The write is synchronous on the loop, as ``record()`` is;
        an empty buffer writes nothing."""
        while True:
            await asyncio.sleep(interval_seconds)
            self.flush_cache_counts(periodic=True)

    def _restore_cache_deltas(self, deltas: CacheDeltas) -> None:
        for key, counts in deltas.items():
            bucket = self._cache_bucket(key)
            if bucket is not None:
                for i, value in enumerate(counts):
                    bucket[i] += value

    def _cache_bucket(self, key: CacheBucketKey) -> list[int] | None:
        bucket = self._cache_deltas.get(key)
        if bucket is None:
            if len(self._cache_deltas) >= MAX_CACHE_BUCKETS:
                if not self._cache_cap_warned:
                    self._cache_cap_warned = True
                    logger.warning(
                        "Dropping persisted cache counts for new server/tool buckets: "
                        "%d buckets are waiting for a metrics write",
                        MAX_CACHE_BUCKETS,
                    )
                return None
            bucket = self._cache_deltas[key] = [0, 0, 0, 0]
        return bucket

    def _buffer_cache(
        self,
        server: str | None,
        tool: str | None,
        *,
        hits: int = 0,
        hit_chars: int = 0,
        misses: int = 0,
        unstorable: int = 0,
    ) -> None:
        """Add to the persisted cache counts — a dict update, no store call.

        The only side effect beyond the dict is the one overflow warning per
        tracker that ``_cache_bucket`` logs when the bucket cap is reached.

        Unattributed calls (``server``/``tool`` not given), a tracker with no
        store, and labels SQLite cannot bind (lone surrogates reach the cache
        key path) stay in memory only, so one bad label cannot hold back the
        batch it would join."""
        if self._metrics_store is None or server is None or tool is None:
            return
        if has_lone_surrogate(server) or has_lone_surrogate(tool):
            return
        bucket = self._cache_bucket((_utc_day(), server, tool))
        if bucket is not None:
            for i, value in enumerate((hits, hit_chars, misses, unstorable)):
                bucket[i] += value

    def record_cache_hit(
        self, chars: int = 0, *, server: str | None = None, tool: str | None = None
    ) -> None:
        """Record a response served from the cache.

        Cache hits are deliberately NOT folded into ``record()``: ``_total_calls``
        is the denominator for the per-stage latency averages and the unit of the
        per-server/per-tool char counters, and a hit runs none of that pipeline.
        ``chars`` is the size of the served (stored, compressed) text so the
        cache's benefit is visible next to the compression savings it would
        otherwise be structurally excluded from (#558); ``get_summary`` exposes
        the reconciliation as ``total_invocations = total_calls + cache_hits +
        total_errors``.

        ``server``/``tool`` attribute the hit for the persisted daily counts
        (#1329); without them it is counted in memory only.
        """
        self._cache_hits += 1
        self._cache_hit_chars += chars
        self._buffer_cache(server, tool, hits=1, hit_chars=chars)

    def record_cache_miss(self, *, server: str | None = None, tool: str | None = None) -> None:
        self._cache_misses += 1
        self._buffer_cache(server, tool, misses=1)

    def record_cache_unstorable(
        self, *, server: str | None = None, tool: str | None = None
    ) -> None:
        """Record a counted cache miss whose response the cache refused to store.

        Without this, a tool whose responses are never storable (mixed/non-text
        content, transient retrieval keys, …) re-misses forever: ``cache_misses``
        grows unbounded while its hit-rate contribution stays 0%, and the
        diagnostics point at a tool the cache can never help (#558). The proxy
        calls this only for a call whose own lookup recorded a miss — the manager
        carries that fact from the lookup rather than re-checking eligibility at
        completion (#1473) — so ``cache_misses - cache_unstorable`` is the miss
        count the storable hit rate uses. Best-effort: store-side failures
        after an accepted response (sqlite errors, the privacy write gate) are
        not counted.
        """
        self._cache_unstorable += 1
        self._buffer_cache(server, tool, unstorable=1)

    def record_reconnect(self) -> None:
        self._reconnects += 1

    def record_progressive_first(self) -> None:
        self._progressive_first_chunks += 1

    def record_progressive_continuation(self) -> None:
        self._progressive_continuations += 1

    def record_hints(self, hints: list[str]) -> None:
        """Record a non-empty list of LTM hints from a surfacing call.

        Operators read ``stm_proxy_stats`` to see how many hints the most
        recent call carried; the event counter answers "did parent send any
        notices during this run?". The hint text is not kept: it is Core's
        own, and the stats output is read by the agent (#1082). Empty lists
        are no-ops.
        """
        if not hints:
            return
        self._total_hint_events += 1
        self._last_hint_count = len(hints)

    def record_error(self, metrics: CallMetrics) -> None:
        """Record a failed tool call for error tracking."""
        self._rps_tracker.record()
        self._total_errors += 1
        if metrics.error_category is not None:
            self._errors_by_category[metrics.error_category.value] += 1
        self._errors_by_server[metrics.server] += 1

        self._persist(metrics, "Failed to persist error metrics")

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
            "structured_delivery": dict(self._structured),
            # ``total_calls`` counts successful live pipeline calls only: cache
            # hits never reach ``record()``, and a failed call only reaches
            # ``record_error()`` (every pre-``record()`` stage failure raises,
            # and the hit path swallows surfacing errors), so the three counters
            # are mutually exclusive per call and their sum reconciles with
            # actual tool invocations (#558).
            "total_invocations": self._total_calls + self._cache_hits + self._total_errors,
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
            "cache_hit_chars": self._cache_hit_chars,
            "cache_misses": self._cache_misses,
            "cache_unstorable": self._cache_unstorable,
            "reconnects": self._reconnects,
            "latency_percentiles": {
                "clean_ms": self._percentiles(self._clean_latencies),
                "compress_ms": self._percentiles(self._compress_latencies),
                "surface_ms": self._percentiles(self._surface_latencies),
                "total_ms": self._percentiles(self._total_latencies),
            },
            # How many calls the percentiles above cover: an empty window
            # reports all-zero percentiles, so readers gate on this count.
            "latency_samples": len(self._total_latencies),
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
            "total_hint_events": self._total_hint_events,
            "last_hint_count": self._last_hint_count,
            "by_server": by_server,
        }
