"""Relevance gating — decide when to surface memories."""

from __future__ import annotations

import time
from collections import deque
from fnmatch import fnmatch

from memtomem_stm.surfacing.config import SurfacingConfig

# Module-level constants
_MAX_RECENT_QUERIES = 50
_MAX_SURFACING_TIMESTAMPS = 200
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_SIMILARITY_THRESHOLD = 0.95


class RelevanceGate:
    """Determine whether to run proactive surfacing for a given tool call."""

    def __init__(self, config: SurfacingConfig) -> None:
        self._config = config
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
        if not self._config.enabled or query is None:
            return False

        full_name = f"{server}__{tool}"

        # Explicit exclusions
        for pattern in self._config.exclude_tools:
            if fnmatch(full_name, pattern) or fnmatch(tool, pattern):
                return False

        # Write-tool heuristic
        for pattern in self._config.write_tool_patterns:
            if fnmatch(tool, pattern):
                return False

        # Per-tool override
        tool_cfg = self._config.context_tools.get(tool)
        if tool_cfg is not None and not tool_cfg.enabled:
            return False

        # Rate limit
        now = time.monotonic()
        while (
            self._surfacing_timestamps
            and now - self._surfacing_timestamps[0] > _RATE_LIMIT_WINDOW_SECONDS
        ):
            self._surfacing_timestamps.popleft()
        if len(self._surfacing_timestamps) >= self._config.max_surfacings_per_minute:
            return False

        # Cooldown: skip if very similar query was recently surfaced
        for ts, prev_query in reversed(self._recent_queries):
            if now - ts >= self._config.cooldown_seconds:
                break
            if self._jaccard_similarity(query, prev_query) > _SIMILARITY_THRESHOLD:
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
