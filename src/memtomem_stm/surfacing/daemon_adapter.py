"""Surfacing LTM adapter backed by the shared local ``mms daemon``."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, cast, get_args

from memtomem_stm.config import STMConfig
from memtomem_stm.daemon import client
from memtomem_stm.daemon.protocol import (
    OP_LTM_INCREMENT_ACCESS,
    OP_LTM_SCRATCH_LIST,
    OP_LTM_SEARCH,
)
from memtomem_stm.daemon.spawn import request_spawn
from memtomem_stm.surfacing.mcp_client import (
    RemoteSearchResult,
    SearchOutcome,
)
from memtomem_stm.utils.numeric import safe_float

logger = logging.getLogger(__name__)
_SEARCH_OUTCOMES = frozenset(get_args(SearchOutcome))


class DaemonLtmAdapter:
    """Forward LTM operations without owning an LTM process or daemon."""

    def __init__(self, daemon_config: STMConfig) -> None:
        self._daemon_config = daemon_config
        self._timeout = daemon_config.surfacing.timeout_seconds

    async def _spawn_best_effort(self) -> None:
        try:
            await asyncio.to_thread(request_spawn, self._daemon_config)
        except Exception:
            logger.debug("shared daemon auto-spawn failed", exc_info=True)

    async def warm_up(self) -> None:
        try:
            if await client.ping(self._daemon_config, timeout=min(2.0, self._timeout)) is None:
                await self._spawn_best_effort()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("shared daemon warm-up probe failed", exc_info=True)

    async def stop(self) -> None:
        # The proxy does not own the shared daemon or its LTM child.
        return None

    async def search(
        self,
        query: str,
        top_k: int | None = None,
        namespace: str | list[str] | None = None,
        context_window: int | None = None,
        *,
        trace_id: str | None = None,
        **kwargs: Any,
    ) -> tuple[list[RemoteSearchResult], list[str], SearchOutcome]:
        payload: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "namespace": namespace,
            "context_window": context_window,
            "trace_id": trace_id,
        }
        state, resp = await client.ltm_request(
            self._daemon_config, OP_LTM_SEARCH, payload, timeout=self._timeout
        )
        if state == "missing":
            await self._spawn_best_effort()
            return [], [], "daemon_starting"
        if state != "ok" or resp is None:
            await self._spawn_best_effort()
            return [], [], "transport_error"
        if not resp.get("ok"):
            status = resp.get("status")
            if status == "busy":
                return [], [], "daemon_busy"
            if status == "expired":
                raise asyncio.TimeoutError
            return [], [], "transport_error"

        raw_results = resp.get("results")
        raw_hints = resp.get("hints")
        raw_outcome = resp.get("outcome")
        if not isinstance(raw_results, list) or not isinstance(raw_hints, list):
            return [], [], "parse_error"
        if raw_outcome not in _SEARCH_OUTCOMES:
            return [], [], "parse_error"

        results: list[RemoteSearchResult] = []
        for item in raw_results:
            if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                continue
            score = safe_float(item.get("score"), float("nan"))
            if not math.isfinite(score):
                continue
            result = RemoteSearchResult(
                content=item["content"],
                score=score,
                source=str(item.get("source") or ""),
                namespace=str(item.get("namespace") or "default"),
            )
            chunk_id = item.get("chunk_id")
            if isinstance(chunk_id, str) and chunk_id:
                result.chunk.id = chunk_id
            results.append(result)
        hints = [hint for hint in raw_hints if isinstance(hint, str)]
        return results, hints, cast(SearchOutcome, raw_outcome)

    async def increment_access(self, chunk_ids: list[str], *, trace_id: str | None = None) -> None:
        if not chunk_ids:
            return
        await client.ltm_request(
            self._daemon_config,
            OP_LTM_INCREMENT_ACCESS,
            {"chunk_ids": chunk_ids, "trace_id": trace_id},
            timeout=self._timeout,
        )

    async def scratch_list(self, *, trace_id: str | None = None) -> list[dict]:
        timeout = max(0.05, min(0.5, self._timeout / 3))
        state, resp = await client.ltm_request(
            self._daemon_config,
            OP_LTM_SCRATCH_LIST,
            {"trace_id": trace_id},
            timeout=timeout,
        )
        if state != "ok" or resp is None or not resp.get("ok"):
            return []
        items = resp.get("items")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
