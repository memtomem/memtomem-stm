"""Relevance gating — decide when to surface memories."""

from __future__ import annotations

import time
from collections import deque
from fnmatch import fnmatch

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.observability import _NOOP_OBSERVABILITY, SurfacingObservability

# Module-level constants
_MAX_RECENT_QUERIES = 50
_MAX_SURFACING_TIMESTAMPS = 200
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_SIMILARITY_THRESHOLD = 0.95


class RelevanceGate:
    """Determine whether to run proactive surfacing for a given tool call."""

    def __init__(
        self,
        config: SurfacingConfig,
        *,
        observability: SurfacingObservability | None = None,
    ) -> None:
        self._config = config
        # Internal recording sink — never None. The public ``observability``
        # is still ``SurfacingObservability | None`` so consumers can
        # short-circuit on absence; the no-op stand-in lets the call sites
        # stay unconditional.
        self._observability = observability if observability is not None else _NOOP_OBSERVABILITY
        self._recent_queries: deque[tuple[float, str]] = deque(maxlen=_MAX_RECENT_QUERIES)
        self._surfacing_timestamps: deque[float] = deque(maxlen=_MAX_SURFACING_TIMESTAMPS)

    def should_surface(
        self,
        server: str,
        tool: str,
        query: str | None,
    ) -> bool:
        """Gate a prospective surfacing. On ``True``, eagerly claim a slot
        in ``_surfacing_timestamps`` so concurrent callers see the budget
        consumption immediately — otherwise N coroutines all check the
        rate limit before any of them reaches ``record_surfacing`` and
        every one passes, bursting through the ``max_surfacings_per_minute``
        cap by up to the concurrency level.

        Cooldown claim stays with ``record_surfacing`` (see that method)
        because cooldown is a "skip if we already returned similar results"
        heuristic — claiming it for queries that ultimately return nothing
        would block legitimate retries on empty results.
        """
        # ``disabled`` and ``no_query`` are recorded by the engine instead —
        # the engine checks ``config.enabled`` before calling the gate, and
        # ``query is None`` is the extractor's outcome (engine-level).
        # Recording them here would double-count when the engine also bails.
        if not self._config.enabled or query is None:
            return False

        full_name = f"{server}__{tool}"

        # Explicit exclusions
        for pattern in self._config.exclude_tools:
            if fnmatch(full_name, pattern) or fnmatch(tool, pattern):
                self._observability.record_skip(tool, "gate_excluded_tool")
                return False

        # Write-tool heuristic. Match against both the bare tool name and the
        # ``server__tool`` full name — symmetric with ``exclude_tools`` above —
        # so a write pattern can target a specific server's tool (e.g.
        # ``github__create_*``) instead of only matching unqualified tool names.
        for pattern in self._config.write_tool_patterns:
            if fnmatch(full_name, pattern) or fnmatch(tool, pattern):
                self._observability.record_skip(tool, "gate_write_tool")
                return False

        # Per-tool override
        tool_cfg = self._config.context_tools.get(tool)
        if tool_cfg is not None and not tool_cfg.enabled:
            self._observability.record_skip(tool, "gate_tool_disabled")
            return False

        # Rate limit
        now = time.monotonic()
        while (
            self._surfacing_timestamps
            and now - self._surfacing_timestamps[0] > _RATE_LIMIT_WINDOW_SECONDS
        ):
            self._surfacing_timestamps.popleft()
        if len(self._surfacing_timestamps) >= self._config.max_surfacings_per_minute:
            self._observability.record_skip(tool, "gate_rate_limit")
            return False

        # Cooldown: skip if very similar query was recently surfaced
        for ts, prev_query in reversed(self._recent_queries):
            if now - ts >= self._config.cooldown_seconds:
                break
            if self._jaccard_similarity(query, prev_query) > _SIMILARITY_THRESHOLD:
                self._observability.record_skip(tool, "gate_cooldown")
                return False

        # Eagerly claim the rate-limit slot. A concurrent ``should_surface``
        # for a different query will now observe this timestamp and apply
        # the cap correctly. A note on failure paths: the slot is kept even
        # if the surfacing later fails, times out, or returns empty —
        # ``max_surfacings_per_minute`` counts attempts because an attempt
        # already consumed LTM/MCP resources and that is what the throttle
        # is defending against.
        self._surfacing_timestamps.append(now)
        return True

    def release_claim(self) -> None:
        """Give back the rate-limit slot :meth:`should_surface` claimed, for a
        caller that ends up starting no LTM work at all.

        The eager claim above is deliberate and its docstring says why: the cap
        counts *attempts*, because an attempt has already spent LTM/MCP
        resources. A caller that is turned away before spending any has not
        made an attempt by that definition, so keeping its slot would let a
        burst of refusals exhaust ``max_surfacings_per_minute`` and go on
        blocking surfacing after the condition that caused them cleared.

        Drops the newest timestamp rather than hunting for this caller's own:
        the cap is a count over a sliding window, so any one restores exactly
        the capacity that was taken, and the newest is this caller's except
        under a concurrent claim in between.
        """
        if self._surfacing_timestamps:
            self._surfacing_timestamps.pop()

    def record_surfacing(self, query: str) -> None:
        """Record that a surfacing was actually performed (call after success).

        Updates the cooldown history only — the rate-limit slot was
        already claimed in ``should_surface`` so this method intentionally
        does not touch ``_surfacing_timestamps``. Calling it for a
        cache-hit or empty-result path is not required and would only
        suppress legitimate similar-query retries.
        """
        now = time.monotonic()
        self._recent_queries.append((now, query))

    @staticmethod
    def _jaccard_similarity(a: str, b: str) -> float:
        tokens_a = set(a.lower().split())
        tokens_b = set(b.lower().split())
        if not tokens_a or not tokens_b:
            return 0.0
        intersection = tokens_a & tokens_b
        union = tokens_a | tokens_b
        return len(intersection) / len(union)
