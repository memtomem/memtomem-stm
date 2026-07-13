"""Surfacing LTM adapter backed by the shared local ``mms daemon``."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, cast, get_args

from memtomem_stm.config import STMConfig
from memtomem_stm.daemon import client
from memtomem_stm.daemon.protocol import (
    OP_LTM_CANDIDATE_PROPOSE,
    OP_LTM_CONTEXT_COMPOSE,
    OP_LTM_INCREMENT_ACCESS,
    OP_LTM_SCRATCH_LIST,
    OP_LTM_SEARCH,
)
from memtomem_stm.daemon.spawn import request_spawn
from memtomem_stm.surfacing.mcp_client import (
    ContextComposeResult,
    LtmCapabilities,
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
        self._compose_supported: bool | None = None
        self._candidate_propose_supported: bool | None = None

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

    @property
    def capabilities(self) -> LtmCapabilities:
        # The daemon owns core negotiation; operation responses distinguish a
        # capable core from an older one without exposing session state.
        return LtmCapabilities(
            context_compose_schema=0 if self._compose_supported is False else 1,
            candidate_propose_schema=0 if self._candidate_propose_supported is False else 1,
        )

    async def context_compose(
        self,
        query: str,
        *,
        agent_id: str | None = None,
        max_chars: int = 3000,
        top_k: int = 10,
        trace_id: str | None = None,
    ) -> ContextComposeResult | None:
        if self._compose_supported is False:
            return None
        state, resp = await client.ltm_request(
            self._daemon_config,
            OP_LTM_CONTEXT_COMPOSE,
            {
                "query": query,
                "agent_id": agent_id,
                "max_chars": max_chars,
                "top_k": top_k,
                "trace_id": trace_id,
            },
            timeout=self._timeout,
        )
        if state == "missing":
            # The daemon generation is gone. Forget capability verdicts from
            # that generation so an upgraded replacement is probed again.
            self._compose_supported = None
            self._candidate_propose_supported = None
            await self._spawn_best_effort()
            return None
        if state != "ok" or resp is None or not resp.get("ok"):
            if resp is not None and resp.get("status") == "unsupported":
                self._compose_supported = False
                return None
            raise RuntimeError("daemon context compose unavailable")
        self._compose_supported = True
        raw_pinned = resp.get("pinned", [])
        raw_retrieved = resp.get("retrieved", [])
        if not isinstance(raw_pinned, list) or not isinstance(raw_retrieved, list):
            raise ValueError("daemon context compose malformed")

        def decode(item: Any, *, pinned: bool):
            if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                raise ValueError("daemon context item malformed")
            result = RemoteSearchResult(
                item["content"],
                safe_float(item.get("score"), 1.0 if pinned else 0.0),
                source=str(item.get("source") or ""),
                namespace=str(item.get("namespace") or "default"),
                pinned=pinned,
            )
            if isinstance(item.get("chunk_id"), str):
                result.chunk.id = item["chunk_id"]
            return result

        pinned_items = tuple(decode(item, pinned=True) for item in raw_pinned)
        retrieved_items = tuple(decode(item, pinned=False) for item in raw_retrieved)
        warnings = resp.get("warnings", [])
        omitted = resp.get("omitted_block_ids", [])
        return ContextComposeResult(
            pinned_items,
            retrieved_items,
            tuple(v for v in warnings if isinstance(v, str)) if isinstance(warnings, list) else (),
            tuple(v for v in omitted if isinstance(v, str)) if isinstance(omitted, list) else (),
        )

    async def candidate_propose(
        self,
        content: str,
        *,
        source: str,
        source_ref: str,
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> dict[str, Any] | None:
        if self._candidate_propose_supported is False:
            return None
        state, resp = await client.ltm_request(
            self._daemon_config,
            OP_LTM_CANDIDATE_PROPOSE,
            {
                "content": content,
                "source": source,
                "source_ref": source_ref,
                "idempotency_key": idempotency_key,
                "trace_id": trace_id,
            },
            timeout=self._timeout,
        )
        if state != "ok" or resp is None or not resp.get("ok"):
            if resp is not None and resp.get("status") == "unsupported":
                self._candidate_propose_supported = False
                return None
            raise RuntimeError("daemon candidate proposal unavailable")
        self._candidate_propose_supported = True
        payload = resp.get("candidate")
        if not isinstance(payload, dict):
            raise ValueError("daemon candidate response malformed")
        return payload

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
            self._compose_supported = None
            self._candidate_propose_supported = None
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
