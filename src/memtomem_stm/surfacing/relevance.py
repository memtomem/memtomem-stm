"""Relevance gating — decide when to surface memories."""

from __future__ import annotations

import time
from collections import deque
from fnmatch import fnmatch

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.observability import _NOOP_OBSERVABILITY, SurfacingObservability

# Module-level constants
_MAX_RECENT_QUERIES = 50
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_SIMILARITY_THRESHOLD = 0.95


class RateClaim:
    """One claimed rate-limit slot (see :meth:`RelevanceGate.should_surface`).

    Identity-based on purpose: two claims really can share one
    ``time.monotonic()`` reading (Windows ticks at ~15.6ms), so a bare
    timestamp token would let ``release_claim`` for an already-pruned claim
    remove a value-equal but *different* caller's live slot. Object identity
    names exactly one claim; ``deque.remove`` compares ``==``, which for this
    class is identity.
    """

    __slots__ = ("ts",)

    def __init__(self, ts: float) -> None:
        self.ts = ts


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
        # Do not put a fixed ``maxlen`` on rate claims. The configured limit
        # is allowed to exceed 200, and silently evicting live claims would
        # turn (for example) a 250/minute limit into no limit after the 200th
        # request. Window pruning in ``should_surface`` bounds this deque by
        # the operator-selected rate instead.
        self._surfacing_timestamps: deque[RateClaim] = deque()

    def should_surface(
        self,
        server: str,
        tool: str,
        query: str | None,
    ) -> RateClaim | None:
        """Gate a prospective surfacing. On pass, eagerly claim a slot in
        ``_surfacing_timestamps`` — so concurrent callers see the budget
        consumption immediately; otherwise N coroutines all check the
        rate limit before any of them reaches ``record_surfacing`` and
        every one passes, bursting through the ``max_surfacings_per_minute``
        cap by up to the concurrency level — and return the claim as the
        token :meth:`release_claim` takes back. ``None`` means rejected,
        no slot claimed.

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
            return None

        full_name = f"{server}__{tool}"

        # Explicit exclusions
        for pattern in self._config.exclude_tools:
            if fnmatch(full_name, pattern) or fnmatch(tool, pattern):
                self._observability.record_skip(tool, "gate_excluded_tool")
                return None

        # Write-tool heuristic. Match against both the bare tool name and the
        # ``server__tool`` full name — symmetric with ``exclude_tools`` above —
        # so a write pattern can target a specific server's tool (e.g.
        # ``github__create_*``) instead of only matching unqualified tool names.
        for pattern in self._config.write_tool_patterns:
            if fnmatch(full_name, pattern) or fnmatch(tool, pattern):
                self._observability.record_skip(tool, "gate_write_tool")
                return None

        # Per-tool override
        tool_cfg = self._config.context_tools.get(tool)
        if tool_cfg is not None and not tool_cfg.enabled:
            self._observability.record_skip(tool, "gate_tool_disabled")
            return None

        # Rate limit
        now = time.monotonic()
        while (
            self._surfacing_timestamps
            and now - self._surfacing_timestamps[0].ts > _RATE_LIMIT_WINDOW_SECONDS
        ):
            self._surfacing_timestamps.popleft()
        if len(self._surfacing_timestamps) >= self._config.max_surfacings_per_minute:
            self._observability.record_skip(tool, "gate_rate_limit")
            return None

        # Cooldown: skip if very similar query was recently surfaced
        for ts, prev_query in reversed(self._recent_queries):
            if now - ts >= self._config.cooldown_seconds:
                break
            if self._jaccard_similarity(query, prev_query) > _SIMILARITY_THRESHOLD:
                self._observability.record_skip(tool, "gate_cooldown")
                return None

        # Eagerly claim the rate-limit slot. A concurrent ``should_surface``
        # for a different query will now observe this timestamp and apply
        # the cap correctly. A note on failure paths: the slot is kept even
        # if the surfacing later fails, times out, or returns empty —
        # ``max_surfacings_per_minute`` counts attempts because an attempt
        # already consumed LTM/MCP resources and that is what the throttle
        # is defending against.
        claim = RateClaim(now)
        self._surfacing_timestamps.append(claim)
        return claim

    def release_claim(self, claim: RateClaim) -> None:
        """Give back the rate-limit slot :meth:`should_surface` claimed, for a
        caller that ends up starting no LTM work at all.

        The eager claim above is deliberate and its docstring says why: the cap
        counts *attempts*, because an attempt has already spent LTM/MCP
        resources. A caller that is turned away before spending any has not
        made an attempt by that definition, so keeping its slot would let a
        burst of refusals exhaust ``max_surfacings_per_minute`` and go on
        blocking surfacing after the condition that caused them cleared.

        ``claim`` is the token :meth:`should_surface` returned, so exactly the
        caller's own slot is removed (by identity — see :class:`RateClaim` for
        why a timestamp value cannot name a claim). Popping the newest instead
        would, under a concurrent claim in between, hand back the *other*
        caller's slot and leave this caller's older timestamp counting — whose
        earlier expiry frees capacity sooner than the surviving real attempt
        should allow. A claim that already left the deque after window pruning
        is simply gone; releasing it is a no-op rather than someone else's
        slot.
        """
        try:
            self._surfacing_timestamps.remove(claim)
        except ValueError:
            pass

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
