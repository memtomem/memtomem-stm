"""How a context-compose reply from the shared daemon reaches the skip ledger.

The engine tries ``context_compose`` first on a compose-capable daemon, so the
daemon's connection states and load-shedding statuses have to be classified on
that path the same way :meth:`DaemonLtmAdapter.search` classifies them: a
starting or busy daemon is an operational skip (no breaker failure, no fault
row), a daemon that does not answer is ``ltm_unavailable`` and gets a respawn
request, and none of them may fall through to a second, legacy request.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.config import STMConfig
from memtomem_stm.daemon.protocol import OP_LTM_CONTEXT_COMPOSE
from memtomem_stm.surfacing.daemon_adapter import DaemonLtmAdapter
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.mcp_client import LtmTransportError
from memtomem_stm.surfacing.observability import SurfacingObservability


def _config(tmp_path: Path) -> STMConfig:
    config = STMConfig(data_dir=tmp_path)
    config.surfacing.timeout_seconds = 1.0
    config.surfacing.warmup_enabled = False
    config.surfacing.min_response_chars = 0
    config.surfacing.min_query_tokens = 1
    config.surfacing.cooldown_seconds = 0
    config.surfacing.fire_webhook = False
    config.surfacing.feedback_enabled = False
    return config


class _Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: Any) -> None:
        self.request = AsyncMock(return_value=reply)
        monkeypatch.setattr(
            "memtomem_stm.surfacing.daemon_adapter.client.ltm_request", self.request
        )
        config = _config(tmp_path)
        self.adapter = DaemonLtmAdapter(config)
        self.spawn = AsyncMock()
        monkeypatch.setattr(self.adapter, "_spawn_best_effort", self.spawn)
        self.observability = SurfacingObservability()
        self.engine = SurfacingEngine(
            config.surfacing, mcp_adapter=self.adapter, observability=self.observability
        )

    async def surface(self) -> str:
        return await self.engine.surface("docs", "read_file", {}, "response", context_query="q")

    @property
    def ops(self) -> list[str]:
        return [call.args[1] for call in self.request.await_args_list]

    @property
    def skips(self) -> dict[str, int]:
        return self.observability.snapshot()["skip_reasons"].get("read_file", {})

    @property
    def breaker_failures(self) -> int:
        return self.engine._circuit_breaker._failures


@pytest.mark.asyncio
async def test_missing_daemon_is_daemon_starting_with_one_spawn_and_no_legacy_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch, ("missing", None))

    assert await h.surface() == "response"

    assert h.ops == [OP_LTM_CONTEXT_COMPOSE]
    h.spawn.assert_awaited_once()
    assert h.skips == {"daemon_starting": 1}
    assert h.breaker_failures == 0
    # The generation is gone: its capability verdicts must not survive it.
    assert h.adapter.capabilities.context_compose_schema == 4


@pytest.mark.asyncio
async def test_unanswering_daemon_is_ltm_unavailable_and_requests_a_respawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    h = _Harness(tmp_path, monkeypatch, ("unavailable", None))

    with caplog.at_level(logging.WARNING, logger="memtomem_stm.surfacing.engine"):
        assert await h.surface() == "response"

    assert h.ops == [OP_LTM_CONTEXT_COMPOSE]
    h.spawn.assert_awaited_once()
    assert h.skips == {"ltm_unavailable": 1}
    assert h.breaker_failures == 1
    # Not the "declared context_compose but the call failed" contract warning.
    assert not any("context_compose" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_busy_daemon_is_an_operational_skip_that_spares_the_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch, ("ok", {"ok": False, "status": "busy"}))

    for _ in range(4):
        assert await h.surface() == "response"

    assert h.ops == [OP_LTM_CONTEXT_COMPOSE] * 4
    h.spawn.assert_not_awaited()
    assert h.skips == {"daemon_busy": 4}
    assert h.breaker_failures == 0


@pytest.mark.asyncio
async def test_expired_daemon_request_is_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch, ("ok", {"ok": False, "status": "expired"}))

    assert await h.surface() == "response"

    assert h.ops == [OP_LTM_CONTEXT_COMPOSE]
    snapshot = h.observability.snapshot()
    assert snapshot["outcomes"].get("read_file", {}) == {"error_timeout": 1}
    assert h.skips == {}
    assert h.breaker_failures == 1


@pytest.mark.asyncio
async def test_daemon_reporting_its_core_unavailable_is_ltm_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch, ("ok", {"ok": False, "status": "unavailable"}))

    assert await h.surface() == "response"

    assert h.ops == [OP_LTM_CONTEXT_COMPOSE]
    # The daemon answered, so there is nothing to respawn.
    h.spawn.assert_not_awaited()
    assert h.skips == {"ltm_unavailable": 1}
    assert h.breaker_failures == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["invalid", "something-new"])
async def test_daemon_rejecting_the_request_stays_a_call_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    # Control: a declared compose surface rejecting our request is a call
    # failure, not a transport outage. Unchanged by #1346.
    h = _Harness(tmp_path, monkeypatch, ("ok", {"ok": False, "status": status}))

    assert await h.surface() == "response"

    assert h.ops == [OP_LTM_CONTEXT_COMPOSE]
    assert h.skips == {"ltm_call_failed": 1}
    assert h.breaker_failures == 1


@pytest.mark.asyncio
async def test_unsupported_daemon_still_falls_back_to_legacy_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Control: capability absence is the one compose answer that falls back.
    h = _Harness(tmp_path, monkeypatch, ("ok", {"ok": False, "status": "unsupported"}))
    h.request.side_effect = [
        ("ok", {"ok": False, "status": "unsupported"}),
        ("ok", {"ok": True, "results": [], "hints": [], "outcome": "empty_results"}),
    ]

    assert await h.surface() == "response"

    assert h.ops[0] == OP_LTM_CONTEXT_COMPOSE and len(h.ops) == 2
    assert h.adapter.capabilities.context_compose_schema == 0


@pytest.mark.asyncio
async def test_adapter_maps_states_to_the_engine_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch, ("unavailable", None))
    with pytest.raises(LtmTransportError):
        await h.adapter.context_compose("q")

    h.request.return_value = ("ok", {"ok": False, "status": "unavailable"})
    with pytest.raises(LtmTransportError):
        await h.adapter.context_compose("q")


def _item(chunk_id: str, content: str) -> dict[str, Any]:
    return {
        "content": content,
        "score": 0.5,
        "source": "memory.md",
        "namespace": "default",
        "chunk_id": chunk_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["chunk_id", "id", "block_id"])
async def test_one_unencodable_identity_drops_that_item_not_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    bad = _item("ok-id", "bad")
    bad[field] = "bad-\ud800"
    reply = (
        "ok",
        {
            "ok": True,
            "selected_context_compose_schema": 2,
            "pinned": [],
            "retrieved": [bad, _item("good-id", "good")],
        },
    )
    h = _Harness(tmp_path, monkeypatch, reply)

    bundle = await h.adapter.context_compose("q")

    assert bundle is not None
    assert [r.chunk.id for r in bundle.retrieved] == ["good-id"]
