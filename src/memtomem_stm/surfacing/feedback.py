"""Feedback tracking and auto-tuning for surfacing."""

from __future__ import annotations

import logging
from pathlib import Path

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.feedback_store import (
    FeedbackDbStatus,
    FeedbackStore,
    inspect_feedback_db,
)

logger = logging.getLogger(__name__)

# Canonical UX order: strong positive → weak positive → negative →
# "I already had this". The formatter renders these in the same order
# in the agent-facing rating spec, so the validator and the agent-visible
# hint cannot drift (#350). ``partially_helpful`` (#353 part 2) sits
# between ``helpful`` and the two negatives so the hint reads as a
# strength gradient — agents pick the closest band rather than
# defaulting to ``helpful`` when the memory only gave context.
VALID_RATINGS: tuple[str, ...] = (
    "helpful",
    "partially_helpful",
    "not_relevant",
    "already_known",
)


class FeedbackTracker:
    """Track surfacing feedback and optionally auto-tune min_score."""

    def __init__(self, config: SurfacingConfig, db_path: Path | None = None) -> None:
        self._config = config
        resolved = db_path if db_path is not None else config.feedback_db_path.expanduser()
        before = inspect_feedback_db(resolved)
        self._store = FeedbackStore(resolved)
        self._store.initialize()
        logger.info(
            "Surfacing feedback store initialized at %s (tables=%s, created_schema=%s)",
            self._store.db_path.expanduser().resolve(),
            "ready",
            not before["initialized"],
        )

    @property
    def store(self) -> FeedbackStore:
        return self._store

    def bootstrap_status(self) -> FeedbackDbStatus:
        return inspect_feedback_db(self._store.db_path)

    def close(self) -> None:
        self._store.close()

    def record_surfacing(
        self,
        surfacing_id: str,
        server: str,
        tool: str,
        query: str,
        memory_ids: list[str],
        scores: list[float],
        score_scale: str | None = None,
    ) -> None:
        self._store.record_surfacing(
            surfacing_id,
            server,
            tool,
            query,
            memory_ids,
            scores,
            score_scale=score_scale,
        )

    def record_fault(self, server: str, tool: str, kind: str) -> None:
        self._store.record_fault(server, tool, kind)

    def record_diagnostic(self, server: str, tool: str, kind: str) -> None:
        self._store.record_diagnostic(server, tool, kind)

    def record_diagnostic_recovery(self, server: str, tool: str, kind: str) -> None:
        self._store.record_diagnostic_recovery(server, tool, kind)

    def record_feedback(
        self,
        surfacing_id: str,
        rating: str,
        memory_id: str | None = None,
    ) -> str:
        if rating not in VALID_RATINGS:
            return f"Error: rating must be one of {list(VALID_RATINGS)}"

        ok = self._store.record_feedback(surfacing_id, rating, memory_id)
        if not ok:
            return f"Error: surfacing event '{surfacing_id}' not found"

        return f"Feedback recorded: {rating}"

    def get_stats(
        self,
        tool: str | None = None,
        since: float | None = None,
        limit: int = 10,
    ) -> dict:
        return self._store.get_stats(tool=tool, since=since, limit=limit)


class AutoTuner:
    """Auto-adjust min_score based on negative feedback ratios.

    Integrated into SurfacingEngine — when `auto_tune_enabled=True` and
    FeedbackTracker is available, the engine calls maybe_adjust(tool)
    and get_effective_min_score(tool) before filtering results.
    """

    def __init__(self, config: SurfacingConfig, store: FeedbackStore) -> None:
        self._config = config
        self._store = store
        # Resume tunings persisted from a previous process — without this
        # every restart silently throws away the AutoTuner's view.
        self._adjustments: dict[str, float] = dict(store.load_adjustments())
        self._feedback_watermarks: dict[str, tuple[int, int]] = {}
        logger.info(
            "AutoTuner bounds: floor=%.4f, ceiling=%.4f, increment=%.4f, base min_score=%.4f",
            config.auto_tune_score_floor,
            config.auto_tune_score_ceiling,
            config.auto_tune_score_increment,
            config.min_score,
        )
        self._purge_pinned_adjustments()
        self._clamp_loaded_adjustments()

    def _clamp_loaded_adjustments(self) -> None:
        """Re-clamp persisted adjustments to the CURRENT config band.

        #392 made the floor/ceiling configurable, but persisted values
        (#332) were resumed verbatim: narrowing the band between runs left
        an out-of-band threshold in effect until fresh feedback happened to
        fire ``maybe_adjust`` for that tool — with no feedback, forever
        (``_purge_pinned_adjustments`` only drops pinned tools, not
        out-of-band values). Clamping on load makes the configured bounds
        authoritative regardless of restart timing. The clamped value is
        persisted back so the DB row converges instead of re-clamping on
        every start. Runs after the pin purge, so purged tools keep their
        persisted row untouched per the purge contract.
        """
        floor = self._config.auto_tune_score_floor
        ceiling = self._config.auto_tune_score_ceiling
        for tool, score in list(self._adjustments.items()):
            clamped = min(max(score, floor), ceiling)
            if clamped != score:
                self._adjustments[tool] = clamped
                self._store.save_adjustment(tool, clamped)
                logger.info(
                    "AutoTuner: clamped persisted adjustment for %r from %.4f to %.4f "
                    "(current band [%.4f, %.4f])",
                    tool,
                    score,
                    clamped,
                    floor,
                    ceiling,
                )

    def _purge_pinned_adjustments(self) -> None:
        """Suppress persisted adjustments for operator-pinned tools.

        When a per-tool ``min_score`` override is set the engine always uses
        the pin (it skips ``maybe_adjust`` for that tool), so a previously
        learned adjustment is inert. Drop it from the in-memory map so
        ``get_min_score_snapshot`` and the surfacing stats don't report a
        stale value. The persisted row is left intact, so removing the pin
        later restores the learned threshold.
        """
        pinned = {
            tool
            for tool, tool_cfg in self._config.context_tools.items()
            if tool_cfg.min_score is not None
        }
        purged = [tool for tool in self._adjustments if tool in pinned]
        for tool in purged:
            del self._adjustments[tool]
        if purged:
            logger.info(
                "AutoTuner: suppressed %d persisted adjustment(s) for pinned tools: %s",
                len(purged),
                sorted(purged),
            )

    def maybe_adjust(self, tool: str) -> float | None:
        """Check feedback ratios and adjust min_score for a tool.

        Two independent band checks (#353 part 2):
        - ``negative_ratio > 0.6`` (``not_relevant`` + ``already_known``)
          → raise threshold so noisy memories stop surfacing.
        - ``helpful_ratio > 0.8`` (strict ``helpful``, **not**
          ``partially_helpful``) → lower threshold to surface more.

        Pre-#353 the lower branch checked ``negative_ratio < 0.2`` — that
        was equivalent in a three-value world but inverted under
        ``partially_helpful``, where a tool whose feedback was "useful
        context but not directly used" would drive ``min_score`` down
        the same way as repeated ``helpful``. Splitting the branches lets
        ``partially_helpful`` count strictly toward the denominator
        (neutral) without contributing to either direction.

        Each tool falls back to the global ratio when its own sample
        count is below ``auto_tune_min_samples`` (cold-start mitigation).
        Returns the new min_score if adjusted, ``None`` otherwise.
        """
        if not self._config.auto_tune_enabled:
            return None

        watermark = (
            self._store.get_feedback_count(tool),
            self._store.get_feedback_count(None),
        )
        if self._feedback_watermarks.get(tool) == watermark:
            return None
        self._feedback_watermarks[tool] = watermark

        min_samples = self._config.auto_tune_min_samples
        neg_ratio = self._store.get_tool_negative_ratio(tool, min_samples=min_samples)
        if neg_ratio is None:
            neg_ratio = self._store.get_tool_negative_ratio(None, min_samples=min_samples)
        helpful_ratio = self._store.get_tool_helpful_ratio(tool, min_samples=min_samples)
        if helpful_ratio is None:
            helpful_ratio = self._store.get_tool_helpful_ratio(None, min_samples=min_samples)

        if neg_ratio is None and helpful_ratio is None:
            return None

        current = self._adjustments.get(tool, self._config.min_score)
        increment = self._config.auto_tune_score_increment

        # Raise wins over lower when both fire — defensive: negative
        # feedback is the stronger signal to suppress.
        if neg_ratio is not None and neg_ratio > 0.6:
            new_score = min(current + increment, self._config.auto_tune_score_ceiling)
            if new_score != current:
                self._adjustments[tool] = new_score
                self._store.save_adjustment(tool, new_score)
                logger.info(
                    "AutoTune: %s min_score %.2f → %.2f (negative ratio: %.0f%%)",
                    tool,
                    current,
                    new_score,
                    neg_ratio * 100,
                )
                return new_score
        elif helpful_ratio is not None and helpful_ratio > 0.8:
            new_score = max(current - increment, self._config.auto_tune_score_floor)
            if new_score != current:
                self._adjustments[tool] = new_score
                self._store.save_adjustment(tool, new_score)
                logger.info(
                    "AutoTune: %s min_score %.2f → %.2f (helpful ratio: %.0f%%)",
                    tool,
                    current,
                    new_score,
                    helpful_ratio * 100,
                )
                return new_score

        return None

    def get_effective_min_score(self, tool: str) -> float:
        """Return the auto-tuned min_score for a tool, or the default."""
        return self._adjustments.get(tool, self._config.min_score)

    @property
    def adjustments(self) -> dict[str, float]:
        """Per-tool min_score adjustments applied this process.

        Returned as a snapshot copy so callers (e.g. the engine's
        ``get_min_score_snapshot`` and ``stm_surfacing_stats``) cannot
        mutate the tuner's internal state.
        """
        return dict(self._adjustments)
