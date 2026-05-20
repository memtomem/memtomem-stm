"""Feedback tracking and auto-tuning for surfacing."""

from __future__ import annotations

import logging
from pathlib import Path

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.feedback_store import FeedbackStore, inspect_feedback_db

logger = logging.getLogger(__name__)

# Canonical UX order: positive → negative → "I already had this". The
# formatter renders these in the same order in the agent-facing rating
# spec, so the validator and the agent-visible hint cannot drift (#350).
VALID_RATINGS: tuple[str, ...] = ("helpful", "not_relevant", "already_known")


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

    def bootstrap_status(self) -> dict[str, object]:
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
    ) -> None:
        self._store.record_surfacing(
            surfacing_id,
            server,
            tool,
            query,
            memory_ids,
            scores,
        )

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

    def maybe_adjust(self, tool: str) -> float | None:
        """Check negative feedback ratio and adjust min_score for a tool.

        When the tool has insufficient samples, falls back to the global
        ratio across all tools (cold-start mitigation).

        Returns new min_score if adjusted, None otherwise.
        """
        if not self._config.auto_tune_enabled:
            return None

        ratio = self._store.get_tool_negative_ratio(
            tool,
            min_samples=self._config.auto_tune_min_samples,
        )
        if ratio is None:
            # Cold start: use global ratio as fallback
            ratio = self._store.get_tool_negative_ratio(
                None,  # None = all tools
                min_samples=self._config.auto_tune_min_samples,
            )
            if ratio is None:
                return None

        current = self._adjustments.get(tool, self._config.min_score)
        increment = self._config.auto_tune_score_increment

        if ratio > 0.6:
            # Too much negative feedback -> raise threshold.
            new_score = min(current + increment, 0.05)
            if new_score != current:
                self._adjustments[tool] = new_score
                self._store.save_adjustment(tool, new_score)
                logger.info(
                    "AutoTune: %s min_score %.2f → %.2f (negative ratio: %.0f%%)",
                    tool,
                    current,
                    new_score,
                    ratio * 100,
                )
                return new_score
        elif ratio < 0.2:
            # Mostly helpful feedback -> lower threshold (surface more).
            new_score = max(current - increment, 0.005)
            if new_score != current:
                self._adjustments[tool] = new_score
                self._store.save_adjustment(tool, new_score)
                logger.info(
                    "AutoTune: %s min_score %.2f → %.2f (negative ratio: %.0f%%)",
                    tool,
                    current,
                    new_score,
                    ratio * 100,
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
