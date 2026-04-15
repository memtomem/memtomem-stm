"""Thin tracker orchestrating ``CompressionFeedbackStore`` writes.

The tracker validates input, resolves the ``trace_id`` when the caller
omits one (best-effort lookup against ``MetricsStore``), and exposes a
stable API for the MCP tool layer. It deliberately does **not** modify
``ProxyManager`` — feedback collection is a learning loop that lives on
the side of the main request path.
"""

from __future__ import annotations

import logging
from pathlib import Path

from memtomem_stm.proxy.compression_feedback_store import (
    TRACE_LOOKUP_WINDOW_SECONDS,
    CompressionFeedbackStore,
    is_valid_kind,
    valid_kinds,
)
from memtomem_stm.proxy.metrics_store import MetricsStore

logger = logging.getLogger(__name__)


class CompressionFeedbackTracker:
    """Orchestrates ``CompressionFeedbackStore`` writes and trace lookup.

    Instantiated when proxy is enabled *and* ``compression_feedback.enabled``
    is true. Owns the feedback store connection; the ``MetricsStore`` is
    borrowed read-only for the optional ``trace_id`` correlation and is
    not closed by this class.
    """

    def __init__(
        self,
        db_path: Path,
        metrics_store: MetricsStore | None = None,
    ) -> None:
        self._store = CompressionFeedbackStore(db_path.expanduser())
        self._store.initialize()
        self._metrics_store = metrics_store

    @property
    def store(self) -> CompressionFeedbackStore:
        return self._store

    def close(self) -> None:
        self._store.close()

    def record(
        self,
        server: str,
        tool: str,
        missing: str,
        kind: str = "other",
        trace_id: str | None = None,
    ) -> str:
        """Persist a feedback report and return a user-facing status string.

        When ``trace_id`` is omitted we ask the metrics store for the
        freshest matching ``(server, tool)`` row within
        ``TRACE_LOOKUP_WINDOW_SECONDS``. If nothing is found the report
        is still stored with ``trace_id=NULL`` — the feedback itself is
        useful even when the analytical join back to ``proxy_metrics``
        isn't available.
        """
        if not server or not tool:
            return "Error: server and tool are required"
        if not missing:
            return "Error: missing description is required"
        if not is_valid_kind(kind):
            return f"Error: kind must be one of {valid_kinds()}"

        resolved_trace = trace_id
        if resolved_trace is None and self._metrics_store is not None:
            try:
                resolved_trace = self._metrics_store.lookup_recent_trace_id(
                    server, tool, TRACE_LOOKUP_WINDOW_SECONDS
                )
            except Exception:
                # Expected fallback: trace_id is a best-effort correlation,
                # not a hard requirement — the report still lands with
                # trace_id=None. Demoted from warning to avoid treating a
                # benign metrics-store query failure as an actionable error.
                logger.debug("trace_id lookup failed", exc_info=True)
                resolved_trace = None

        self._store.record(server, tool, kind, missing, resolved_trace)

        if resolved_trace is not None:
            return f"Compression feedback recorded ({kind}, trace_id={resolved_trace})"
        return f"Compression feedback recorded ({kind}, trace_id unresolved)"

    def get_stats(self, tool: str | None = None) -> dict:
        return self._store.get_stats(tool)
