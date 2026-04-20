"""Thin tracker orchestrating ``ProgressiveReadsStore`` writes.

The tracker is called from the proxy hot path (``_apply_progressive``
and ``read_more``) for every progressive-delivery read event. Writes
are opportunistic: any exception is logged at DEBUG and swallowed so a
telemetry failure never degrades the response served to the caller.
"""

from __future__ import annotations

import logging
from pathlib import Path

from memtomem_stm.proxy.progressive_reads_store import ProgressiveReadsStore

logger = logging.getLogger(__name__)


class ProgressiveReadsTracker:
    """Records progressive-delivery read events to SQLite.

    Unlike ``CompressionFeedbackTracker`` this does not consult the
    metrics store for ``trace_id`` correlation — the caller always
    supplies ``trace_id`` (from the ``trace_id`` parameter threaded
    through ``_apply_progressive`` by PR #205, or from the cached
    ``ProgressiveResponse.trace_id`` attribute in ``read_more``).
    """

    def __init__(self, db_path: Path) -> None:
        self._store = ProgressiveReadsStore(db_path.expanduser())
        self._store.initialize()

    @property
    def store(self) -> ProgressiveReadsStore:
        return self._store

    def close(self) -> None:
        self._store.close()

    def record_initial(
        self,
        *,
        key: str,
        trace_id: str | None,
        server: str,
        tool: str,
        initial_chars: int,
        total_chars: int,
    ) -> None:
        """Record the first-chunk event (``offset=0``)."""
        try:
            self._store.record(
                key=key,
                trace_id=trace_id,
                server=server,
                tool=tool,
                offset=0,
                chars=initial_chars,
                served_to=initial_chars,
                total_chars=total_chars,
            )
        except Exception:
            logger.debug("progressive_reads record_initial failed", exc_info=True)

    def record_follow_up(
        self,
        *,
        key: str,
        trace_id: str | None,
        server: str,
        tool: str,
        offset: int,
        chars: int,
        total_chars: int,
    ) -> None:
        """Record a follow-up ``read_more`` event."""
        try:
            self._store.record(
                key=key,
                trace_id=trace_id,
                server=server,
                tool=tool,
                offset=offset,
                chars=chars,
                served_to=offset + chars,
                total_chars=total_chars,
            )
        except Exception:
            logger.debug("progressive_reads record_follow_up failed", exc_info=True)

    def get_stats(self, tool: str | None = None) -> dict:
        return self._store.get_stats(tool)
