"""Append-only JSONL sink for tool-selection telemetry (#467).

The proxy is the one component in the call path, so it can record what an
advisory analyzer never sees: which tool the client model actually called,
out of which advertised candidate set, and how the call went. This log is
the substrate for offline replay/eval (#468) and every later learning stage
(#469/#470) — and the landing zone for the STM-native hard filter's (#465)
``reject_reasons``, so replay sees the tools that were withheld, not just
the ones advertised.

Schema v1 — three event types, every record self-describing via
``schema_version`` + ``ranker_version``; one JSON object per line, keys
sorted, so a replay harness can stream-parse and diff runs:

``selection``
    ``selection_id`` (joins the paired ``execution`` row), ``trace_id``
    (joins ``proxy_metrics.db`` for full per-stage diagnostics),
    ``server``, ``selected_tool`` (prefixed name, same vocabulary as
    ``candidate_tools``), ``candidate_tools`` (what the proxy last
    advertised — today the client model does the selecting),
    ``candidate_count``, ``reject_reasons`` (prefixed tool → reason code
    for every tool the #465 hard filter withheld from that advertisement;
    ``{}`` when nothing was rejected or the filter never ran — reason
    vocabulary in ``proxy/tool_eligibility.py``), ``candidate_features``
    (#466 ranking output) / ``graph_generation`` (reserved ``null`` until
    toolgraph#13/#15), ``args_sha256`` + ``args_chars`` (canonical-JSON
    hash — see redaction below), ``ts``.

``execution``
    ``selection_id`` / ``trace_id`` / ``server`` / ``selected_tool``
    mirroring the paired selection, ``ok``, ``latency_ms`` (proxy-side
    wall time for the full pipeline, what the agent experienced),
    ``error_type`` (exception class name; the *typed* error category
    stays in ``proxy_metrics.db``, joinable via ``trace_id`` — not
    duplicated here), ``cache_hit`` (``true`` when the result was served
    from the proxy response cache, ``false`` on a live upstream call,
    ``null`` when telemetry can't attribute it — e.g. an in-pipeline
    raise), ``retry_count`` / ``cost`` (reserved ``null``), ``ts``.

``feedback``
    ``selection_id`` (+ optional ``trace_id``), ``user_corrected``,
    ``operator_override``, ``ts``. Schema is pinned here and by tests,
    but nothing in the proxy emits it yet — emitters arrive with their
    signal sources (e.g. an operator-facing rating tool).

Redaction policy is structural, not filter-based: no field ever carries
raw arguments, results, prompts, resource URIs, or error message strings —
argument payloads appear only as a sha256 over their canonical JSON plus a
char count. As a belt-and-suspenders backstop (the storage-gating rule from
``privacy.py``), every serialized line is screened with
``contains_sensitive_content`` before persisting and dropped (counted) on a
match, mirroring the never-persist-secrets rule from #460/#462. Files are
created ``0o600`` (#458); the parent directory is created ``0o700``, but a
pre-existing parent's mode is left untouched — the same posture as every
other STM store (``MetricsStore``, ``memory_ops`` #456/#464): ``mkdir`` mode
applies at creation time only, and the file mode alone guards the content.

Sampling and write failures apply to the whole selection+execution pair at
selection time — ``log_selection`` returning ``None`` means the call was
sampled out or its record never reached disk, and the caller skips the
execution event, so neither produces orphan halves. Redaction drops, by
contrast, are per-record (never-persist beats pairing): an execution whose
paired selection was dropped can appear alone, so replay tooling must treat
``selection_id`` joins as left-outer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from memtomem_stm.proxy.metrics import _percentile
from memtomem_stm.proxy.privacy import contains_sensitive_content

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Per-record default when no ranking informed the call — the client model
# picked from the full advertised set unaided — so replay tooling can treat
# this version as the unranked baseline. Calls where a ranker ran stamp
# their own version (e.g. ``tool_relevance.RANKER_VERSION_BM25``) on BOTH
# halves of the pair via the ``ranker_version`` parameter, letting replay
# split cohorts on this field alone.
RANKER_VERSION = "v0-passthrough"


def _canonical_args(arguments: dict[str, Any] | None) -> str:
    """Serialize call arguments deterministically for hashing.

    ``default=str`` keeps non-JSON-native values (e.g. Path) from raising —
    the output is only ever hashed, never persisted, so a lossy fallback
    rendering is fine as long as it is stable.
    """
    return json.dumps(
        arguments or {},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


class SelectionTelemetryLog:
    """Append-only JSONL writer with rotation, sampling, and redaction.

    Write failures never propagate: a telemetry problem must not break the
    proxied call it accounts for. Failures are counted (``write_errors``)
    and logged at WARNING once per process, DEBUG thereafter.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = 50_000_000,
        max_backups: int = 3,
        sample_rate: float = 1.0,
        rng: random.Random | None = None,
    ) -> None:
        self._path = Path(path).expanduser()
        self._max_bytes = max_bytes
        self._max_backups = max_backups
        self._sample_rate = sample_rate
        # Injectable for deterministic sampling tests; rates 0.0 / 1.0
        # short-circuit and never consult it.
        self._rng = rng or random.Random()
        # Writers share the lock defensively, same posture as MetricsStore:
        # callers are asyncio single-thread today, but the guard makes the
        # store ready for an off-loop move without a rewrite.
        self._lock = threading.Lock()
        self._warned_write_failure = False
        # Counters are part of the observable contract (wire-in tests
        # snapshot them); mutate only under ``self._lock``.
        self.events_written = 0
        self.events_sampled_out = 0
        self.redaction_drops = 0
        self.write_errors = 0

    @property
    def path(self) -> Path:
        return self._path

    def initialize(self) -> None:
        """Create the log file privately (0600 file; parent created 0700).

        Also tightens a pre-existing *file* left at a permissive mode by an
        older run or umask — same rationale as
        ``sqlite_private.ensure_private_db_files``. A pre-existing *parent
        directory* keeps its mode (see module docstring).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        try:
            self._path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        """No-op — the file is opened per append so rotation never holds a
        stale descriptor. Exists so lifespan teardown can treat every store
        uniformly."""

    def snapshot(self) -> dict[str, int]:
        """Return this process's live write-path counters.

        These reflect the running instance only (reset at restart), unlike
        ``aggregate_selection_log`` which reads the persisted history off
        disk — the same in-memory-vs-store split as ``stm_index_stats`` /
        ``stm_surfacing_stats``. Read under ``self._lock`` so a concurrent
        ``_append`` can't tear a counter.
        """
        with self._lock:
            return {
                "events_written": self.events_written,
                "events_sampled_out": self.events_sampled_out,
                "redaction_drops": self.redaction_drops,
                "write_errors": self.write_errors,
            }

    # ── event emitters ───────────────────────────────────────────────────

    def log_selection(
        self,
        *,
        server: str,
        selected_tool: str,
        candidate_tools: list[str],
        arguments: dict[str, Any] | None,
        trace_id: str | None,
        candidate_features: dict[str, Any] | None = None,
        ranker_version: str | None = None,
        reject_reasons: dict[str, str] | None = None,
    ) -> str | None:
        """Record a selection event; returns its ``selection_id``.

        Returns ``None`` when the call is sampled out — the caller must then
        skip ``log_execution`` so pairs stay atomic. ``candidate_features``
        is the ranker's output object (#466) — the caller guarantees it
        carries no raw text, only scores/hashes; ``ranker_version`` stamps
        which ranker produced it (``None`` = the unranked default).
        ``reject_reasons`` is the #465 hard filter's verdict for the
        advertisement this call selected from — reason codes only, no tool
        metadata (``None`` records the empty map).
        """
        if not self._sampled_in():
            with self._lock:
                self.events_sampled_out += 1
            return None
        selection_id = uuid.uuid4().hex
        canonical = _canonical_args(arguments)
        appended = self._append(
            {
                "schema_version": SCHEMA_VERSION,
                "ranker_version": ranker_version or RANKER_VERSION,
                "event": "selection",
                "ts": time.time(),
                "selection_id": selection_id,
                "trace_id": trace_id,
                "server": server,
                "selected_tool": selected_tool,
                "candidate_tools": list(candidate_tools),
                "candidate_count": len(candidate_tools),
                "reject_reasons": dict(reject_reasons) if reject_reasons else {},
                "candidate_features": candidate_features,
                "graph_generation": None,
                "args_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "args_chars": len(canonical),
            }
        )
        # A write failure means the selection never reached disk — report
        # ``None`` so the caller skips the paired execution event rather
        # than persisting an orphan half (a transient failure could let the
        # execution write succeed). An intentional redaction drop still
        # returns the id: that is the documented left-outer case.
        return selection_id if appended else None

    def log_execution(
        self,
        *,
        selection_id: str,
        trace_id: str | None,
        server: str,
        selected_tool: str,
        ok: bool,
        latency_ms: float,
        error_type: str | None = None,
        ranker_version: str | None = None,
        cache_hit: bool | None = None,
    ) -> None:
        self._append(
            {
                "schema_version": SCHEMA_VERSION,
                "ranker_version": ranker_version or RANKER_VERSION,
                "event": "execution",
                "ts": time.time(),
                "selection_id": selection_id,
                "trace_id": trace_id,
                "server": server,
                "selected_tool": selected_tool,
                "ok": ok,
                "latency_ms": round(latency_ms, 3),
                "error_type": error_type,
                "retry_count": None,
                "cost": None,
                "cache_hit": cache_hit,
            }
        )

    def log_feedback(
        self,
        *,
        selection_id: str,
        trace_id: str | None = None,
        user_corrected: bool | None = None,
        operator_override: bool | None = None,
    ) -> None:
        self._append(
            {
                "schema_version": SCHEMA_VERSION,
                "ranker_version": RANKER_VERSION,
                "event": "feedback",
                "ts": time.time(),
                "selection_id": selection_id,
                "trace_id": trace_id,
                "user_corrected": user_corrected,
                "operator_override": operator_override,
            }
        )

    # ── internals ────────────────────────────────────────────────────────

    def _sampled_in(self) -> bool:
        if self._sample_rate >= 1.0:
            return True
        if self._sample_rate <= 0.0:
            return False
        return self._rng.random() < self._sample_rate

    def _append(self, record: dict[str, Any]) -> bool:
        """Persist one record; ``False`` only on a write failure.

        An intentional redaction drop returns ``True`` — the record was
        consumed, not failed — so ``log_selection`` keeps the documented
        left-outer pairing semantics, while a write failure tells it that
        nothing reached disk and the pair must be skipped.
        """
        line = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        # Structural redaction means no payload text should ever be here;
        # this screen is the storage-gating backstop (full DEFAULT_PATTERNS
        # per the privacy.py contract) against a secret smuggled through a
        # field nobody expected to carry one — e.g. an upstream tool *name*.
        # A false positive costs one dropped telemetry record, never data.
        if contains_sensitive_content(line):
            with self._lock:
                self.redaction_drops += 1
            logger.warning(
                "Dropped a selection-telemetry record that matched a sensitive-content pattern"
            )
            return True
        data = (line + "\n").encode("utf-8")
        # Synchronous file append on the asyncio event loop: while it runs,
        # every runnable coroutine stalls — other in-flight proxied calls
        # included. Accepted for the current local single-MCP-client
        # deployment (same contract as MetricsStore.record): a small
        # O_APPEND write is far cheaper than the upstream call it accounts
        # for. Multi-client serving is the reopen trigger to move this
        # off-loop; writers already serialize on ``self._lock``.
        with self._lock:
            try:
                self._rotate_if_needed_locked()
                fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
                self.events_written += 1
            except OSError:
                self.write_errors += 1
                if not self._warned_write_failure:
                    self._warned_write_failure = True
                    logger.warning(
                        "Selection telemetry write failed — proxied calls unaffected",
                        exc_info=True,
                    )
                else:
                    logger.debug("Selection telemetry write failed", exc_info=True)
                return False
        return True

    def _rotate_if_needed_locked(self) -> None:
        """Size-based rotation: ``log → log.1 → … → log.N``, oldest dropped.

        Renames preserve the 0600 mode; the fresh file is recreated 0600 by
        the ``os.open`` in ``_append``. With ``max_backups == 0`` the file
        is simply truncated by deletion (append recreates it).
        """
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size < self._max_bytes:
            return
        if self._max_backups <= 0:
            self._path.unlink(missing_ok=True)
            return
        for i in range(self._max_backups - 1, 0, -1):
            src = self._path.with_name(f"{self._path.name}.{i}")
            if src.exists():
                src.replace(self._path.with_name(f"{self._path.name}.{i + 1}"))
        self._path.replace(self._path.with_name(f"{self._path.name}.1"))


def _top(counter: Counter[str], n: int) -> list[list[Any]]:
    """Return the ``n`` highest-count entries as ``[[key, count], …]``.

    Sorted by count descending then key ascending so the order is stable
    across runs with the same data (ties never reshuffle a rendered table).
    """
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [[k, v] for k, v in ordered[:n]]


def aggregate_selection_log(path: Path | str, *, top_n: int = 10) -> dict[str, Any]:
    """Stream-aggregate a selection-telemetry JSONL log for read-side stats.

    Pure and side-effect-free: opens the *active* log file (rotated backups
    ``.1``/``.2``/… are counted via ``rotated_backups`` but not parsed — a
    50 MB default means rotation is rare, and reading the tail of history is
    a wait-for-signal extension), parses one JSON object per line, and never
    raises on a malformed line (counted in ``malformed`` and skipped) so a
    half-written tail or a hand-edited file can't break the stats tool.

    The high-cardinality maps (server / tool / error-type / reject-reason)
    are returned as ``top_n`` ``[key, count]`` lists plus a ``*_distinct``
    cardinality so truncation never reads as "that's all there was";
    ``by_ranker_version`` is returned in full (low cardinality, and the
    cohort split is the #468 replay signal).
    """
    path = Path(path).expanduser()
    rotated = sum(1 for _ in path.parent.glob(f"{path.name}.*")) if path.parent.exists() else 0
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "rotated_backups": rotated,
        "total_lines": 0,
        "malformed": 0,
        "events": {"selection": 0, "execution": 0, "feedback": 0},
        "by_ranker_version": [],
        "by_server": [],
        "by_server_distinct": 0,
        "by_selected_tool": [],
        "by_selected_tool_distinct": 0,
        "outcomes": {"ok": 0, "error": 0, "error_rate": 0.0},
        "by_error_type": [],
        "by_error_type_distinct": 0,
        "reject_reasons": [],
        "reject_reasons_distinct": 0,
        "latency_ms": {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0},
        "cache": {"hit": 0, "miss": 0, "unknown": 0, "hit_rate": 0.0},
    }
    if not result["exists"]:
        return result

    rankers: Counter[str] = Counter()
    servers: Counter[str] = Counter()
    tools: Counter[str] = Counter()
    error_types: Counter[str] = Counter()
    reject_reasons: Counter[str] = Counter()
    ok = err = 0
    cache_hit = cache_miss = cache_unknown = 0
    latencies: list[float] = []

    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                result["total_lines"] += 1
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    result["malformed"] += 1
                    continue
                if not isinstance(rec, dict):
                    result["malformed"] += 1
                    continue
                event = rec.get("event")
                if event == "selection":
                    result["events"]["selection"] += 1
                    rankers[str(rec.get("ranker_version"))] += 1
                    if rec.get("server") is not None:
                        servers[str(rec["server"])] += 1
                    if rec.get("selected_tool") is not None:
                        tools[str(rec["selected_tool"])] += 1
                    rr = rec.get("reject_reasons")
                    if isinstance(rr, dict):
                        for reason in rr.values():
                            reject_reasons[str(reason)] += 1
                elif event == "execution":
                    result["events"]["execution"] += 1
                    if rec.get("ok"):
                        ok += 1
                    else:
                        err += 1
                    etype = rec.get("error_type")
                    if etype is not None:
                        error_types[str(etype)] += 1
                    ch = rec.get("cache_hit")
                    if ch is True:
                        cache_hit += 1
                    elif ch is False:
                        cache_miss += 1
                    else:
                        cache_unknown += 1
                    lat = rec.get("latency_ms")
                    if isinstance(lat, (int, float)) and not isinstance(lat, bool):
                        latencies.append(float(lat))
                elif event == "feedback":
                    result["events"]["feedback"] += 1
                else:
                    result["malformed"] += 1
    except OSError:
        # Treat an unreadable file like an absent one rather than raising
        # out of an observability path — the tool reports what it could read.
        return result

    result["by_ranker_version"] = _top(rankers, len(rankers))
    result["by_server"] = _top(servers, top_n)
    result["by_server_distinct"] = len(servers)
    result["by_selected_tool"] = _top(tools, top_n)
    result["by_selected_tool_distinct"] = len(tools)
    result["by_error_type"] = _top(error_types, top_n)
    result["by_error_type_distinct"] = len(error_types)
    result["reject_reasons"] = _top(reject_reasons, top_n)
    result["reject_reasons_distinct"] = len(reject_reasons)
    result["outcomes"] = {
        "ok": ok,
        "error": err,
        "error_rate": round(err / (ok + err), 4) if (ok + err) else 0.0,
    }
    if latencies:
        latencies.sort()
        result["latency_ms"] = {
            "count": len(latencies),
            "p50": round(_percentile(latencies, 50), 2),
            "p95": round(_percentile(latencies, 95), 2),
            "p99": round(_percentile(latencies, 99), 2),
        }
    # hit_rate is over attributable executions only (hit + miss); unknowns
    # (in-pipeline raises before the field is set) are excluded from the
    # denominator so a burst of errors can't deflate the cache hit rate.
    attributable = cache_hit + cache_miss
    result["cache"] = {
        "hit": cache_hit,
        "miss": cache_miss,
        "unknown": cache_unknown,
        "hit_rate": round(cache_hit / attributable, 4) if attributable else 0.0,
    }
    return result
