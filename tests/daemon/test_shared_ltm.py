"""Regression coverage for issue #688 shared-daemon LTM routing."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.config import STMConfig
from memtomem_stm.daemon.protocol import (
    MAX_MESSAGE_BYTES,
    OP_LTM_CANDIDATE_PROPOSE,
    OP_LTM_CONTEXT_COMPOSE,
    OP_LTM_INCREMENT_ACCESS,
    OP_LTM_SCRATCH_LIST,
    OP_LTM_SEARCH,
    PROTOCOL_VERSION,
    build_request,
)
from memtomem_stm.daemon.discovery import config_fingerprint, handshake_path, write_handshake
from memtomem_stm.daemon.server import DaemonServer
from memtomem_stm.surfacing.daemon_adapter import DaemonLtmAdapter
from memtomem_stm.surfacing.mcp_client import ContextComposeResult, RemoteSearchResult
from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.observability import SurfacingObservability


def _config(tmp_path: Path) -> STMConfig:
    config = STMConfig(data_dir=tmp_path)
    config.surfacing.timeout_seconds = 1.0
    config.surfacing.warmup_enabled = False
    config.daemon.idle_timeout_seconds = 0
    return config


def _request(op: str, payload: dict) -> dict:
    return build_request("unused", op, payload, deadline_monotonic=time.monotonic() + 1.0)


def test_standalone_adapter_selection_and_identity_snapshot(tmp_path: Path) -> None:
    from memtomem_stm.server import _build_ltm_adapter

    config = _config(tmp_path)
    daemon_identity = config.model_copy(deep=True)
    config.surfacing.consumer_model = "gpt-4.1-nano-from-proxy-file"

    direct = _build_ltm_adapter(config, daemon_identity)
    assert isinstance(direct, McpClientSearchAdapter)

    config.surfacing.use_daemon = True
    shared = _build_ltm_adapter(config, daemon_identity)
    assert isinstance(shared, DaemonLtmAdapter)
    assert shared._daemon_config.surfacing.consumer_model == ""


def test_health_daemon_route_does_not_probe_private_ltm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem_stm.cli.proxy import _ltm_status

    config = _config(tmp_path)
    config.surfacing.use_daemon = True

    async def warm_ping(*args, **kwargs):
        return {"ltm": "warm", "pid": 1, "port": 2}

    monkeypatch.setattr("memtomem_stm.daemon.client.ping", warm_ping)
    monkeypatch.setattr(
        "memtomem_stm.cli.proxy._ltm_mcp_status",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("direct LTM probe must not run")
        ),
    )

    status = _ltm_status(config, 1.0)

    assert status["route"] == "daemon"
    assert status["daemon_reachable"] is True
    assert status["ltm_state"] == "warm"
    assert status["connected"] is True


@pytest.mark.asyncio
async def test_daemon_search_round_trip_preserves_structured_fields(tmp_path: Path) -> None:
    server = DaemonServer(_config(tmp_path))
    result = RemoteSearchResult("remember this", 0.0312345, "/notes/a.md", "work")
    result.chunk.id = "real-chunk-id"
    server._adapter = SimpleNamespace(
        search=AsyncMock(return_value=([result], ["trust hint"], "ok"))
    )

    response = await server._dispatch(
        _request(
            OP_LTM_SEARCH,
            {
                "query": "jwt handler",
                "top_k": 6,
                "namespace": ["work"],
                "context_window": 2,
                "trace_id": "trace-1",
            },
        )
    )

    assert response is not None and response["ok"] is True
    assert response["v"] == PROTOCOL_VERSION
    assert response["outcome"] == "ok"
    assert response["hints"] == ["trust hint"]
    assert response["results"] == [
        {
            "content": "remember this",
            "score": 0.0312345,
            "source": str(Path("/notes/a.md")),
            "namespace": "work",
            "chunk_id": "real-chunk-id",
        }
    ]


@pytest.mark.asyncio
async def test_daemon_v5_compose_and_candidate_roundtrip_and_validation(tmp_path: Path) -> None:
    server = DaemonServer(_config(tmp_path))
    pinned = RemoteSearchResult("policy", 1.0, "/policy.md", "global", pinned=True)
    pinned.chunk.id = "pin-1"
    compose = AsyncMock(return_value=ContextComposeResult((pinned,), (), ("warning",), ()))
    propose = AsyncMock(return_value={"candidate_id": "candidate-1", "status": "pending"})
    server._adapter = SimpleNamespace(context_compose=compose, candidate_propose=propose)

    composed = await server._dispatch(
        _request(
            OP_LTM_CONTEXT_COMPOSE,
            {"query": "q", "agent_id": None, "max_chars": 100, "top_k": 2},
        )
    )
    assert composed is not None and composed["ok"] is True
    assert composed["pinned"][0]["chunk_id"] == "pin-1"

    proposed = await server._dispatch(
        _request(
            OP_LTM_CANDIDATE_PROPOSE,
            {
                "content": "remember",
                "source": "memtomem-stm",
                "source_ref": "trace",
                "idempotency_key": "key",
            },
        )
    )
    assert proposed == {
        "v": PROTOCOL_VERSION,
        "ok": True,
        "candidate": {"candidate_id": "candidate-1", "status": "pending"},
    }

    for invalid_content in ("", "x" * 2_001):
        invalid = await server._dispatch(
            _request(
                OP_LTM_CANDIDATE_PROPOSE,
                {
                    "content": invalid_content,
                    "source": "memtomem-stm",
                    "source_ref": "trace",
                    "idempotency_key": "key",
                },
            )
        )
        assert invalid == {"v": PROTOCOL_VERSION, "ok": False, "status": "invalid"}


@pytest.mark.asyncio
async def test_daemon_adapter_memoizes_unsupported_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = AsyncMock(return_value=("ok", {"ok": False, "status": "unsupported"}))
    monkeypatch.setattr("memtomem_stm.surfacing.daemon_adapter.client.ltm_request", request)
    adapter = DaemonLtmAdapter(_config(tmp_path))

    assert await adapter.context_compose("q") is None
    assert await adapter.context_compose("q") is None
    assert request.await_count == 1
    assert adapter.capabilities.context_compose_schema == 0

    # A missing legacy fallback proves the daemon generation is gone and
    # resets memoized capability verdicts for its replacement.
    request.return_value = ("missing", None)
    monkeypatch.setattr(adapter, "_spawn_best_effort", AsyncMock())
    assert await adapter.search("q") == ([], [], "daemon_starting")
    assert adapter.capabilities.context_compose_schema == 1
    assert adapter.capabilities.candidate_propose_schema == 1


@pytest.mark.asyncio
async def test_daemon_adapter_end_to_end_over_loopback(tmp_path: Path) -> None:
    config = _config(tmp_path)
    server = DaemonServer(config)
    result = RemoteSearchResult("fleet memory", 0.04, "/fleet.md", "default")
    result.chunk.id = "fleet-id"
    increment = AsyncMock()
    server._adapter = SimpleNamespace(
        search=AsyncMock(return_value=([result], [], "ok")),
        scratch_list=AsyncMock(return_value=[{"key": "fleet", "value": "six"}]),
        increment_access=increment,
    )
    tcp = await asyncio.start_server(
        server._handle_conn, config.daemon.host, 0, limit=MAX_MESSAGE_BYTES
    )
    server._port = tcp.sockets[0].getsockname()[1]
    fingerprint = config_fingerprint(config)
    write_handshake(
        handshake_path(config.data_dir, fingerprint),
        pid=123,
        host=config.daemon.host,
        port=server._port,
        token=server._token,
        config_fingerprint=fingerprint,
        created_at=time.time(),
    )
    adapter = DaemonLtmAdapter(config)
    try:
        results, hints, outcome = await adapter.search("fleet query", top_k=3)
        assert outcome == "ok" and hints == []
        assert results[0].chunk.id == "fleet-id"
        assert await adapter.scratch_list() == [{"key": "fleet", "value": "six"}]
        await adapter.increment_access(["fleet-id"])
        increment.assert_awaited_once_with(["fleet-id"], trace_id=None)
    finally:
        tcp.close()
        await tcp.wait_closed()
        handshake_path(config.data_dir, fingerprint).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_daemon_scratch_and_increment_use_raw_adapter(tmp_path: Path) -> None:
    server = DaemonServer(_config(tmp_path))
    increment = AsyncMock()
    scratch = AsyncMock(return_value=[{"key": "focus", "value": "issue 688"}])
    server._adapter = SimpleNamespace(increment_access=increment, scratch_list=scratch)

    increment_response = await server._dispatch(
        _request(
            OP_LTM_INCREMENT_ACCESS,
            {"chunk_ids": ["a", "b"], "trace_id": "trace-2"},
        )
    )
    scratch_response = await server._dispatch(
        _request(OP_LTM_SCRATCH_LIST, {"trace_id": "trace-3"})
    )

    assert increment_response == {"v": PROTOCOL_VERSION, "ok": True}
    increment.assert_awaited_once_with(["a", "b"], trace_id="trace-2")
    assert scratch_response == {
        "v": PROTOCOL_VERSION,
        "ok": True,
        "items": [{"key": "focus", "value": "issue 688"}],
    }


@pytest.mark.asyncio
async def test_new_ltm_ops_are_rejected_on_non_loopback_binding(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.daemon.allow_non_loopback = True
    config.daemon.host = "0.0.0.0"
    server = DaemonServer(config)
    server._adapter = SimpleNamespace(search=AsyncMock())

    response = await server._dispatch(_request(OP_LTM_SEARCH, {"query": "q"}))

    assert response == {"v": PROTOCOL_VERSION, "ok": False, "status": "unavailable"}
    server._adapter.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_missing_daemon_spawns_and_returns_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = DaemonLtmAdapter(_config(tmp_path))
    spawned: list[STMConfig] = []

    async def missing(*args, **kwargs):
        return "missing", None

    monkeypatch.setattr("memtomem_stm.surfacing.daemon_adapter.client.ltm_request", missing)
    monkeypatch.setattr(
        "memtomem_stm.surfacing.daemon_adapter.request_spawn",
        lambda config: spawned.append(config) or True,
    )

    assert await adapter.search("query") == ([], [], "daemon_starting")
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_adapter_decodes_results_and_busy_is_operational_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = DaemonLtmAdapter(_config(tmp_path))
    responses = iter(
        [
            (
                "ok",
                {
                    "ok": True,
                    "results": [
                        {
                            "content": "memory",
                            "score": 0.0312345,
                            "source": "/m.md",
                            "namespace": "default",
                            "chunk_id": "chunk-1",
                        }
                    ],
                    "hints": ["hint"],
                    "outcome": "ok",
                },
            ),
            ("ok", {"ok": False, "status": "busy"}),
        ]
    )

    async def reply(*args, **kwargs):
        return next(responses)

    monkeypatch.setattr("memtomem_stm.surfacing.daemon_adapter.client.ltm_request", reply)

    results, hints, outcome = await adapter.search("query")
    assert outcome == "ok" and hints == ["hint"]
    assert results[0].score == 0.0312345
    assert results[0].chunk.id == "chunk-1"
    assert await adapter.search("query") == ([], [], "daemon_busy")


@pytest.mark.asyncio
async def test_adapter_stop_does_not_shutdown_shared_daemon(tmp_path: Path) -> None:
    await DaemonLtmAdapter(_config(tmp_path)).stop()


@pytest.mark.asyncio
async def test_mixed_ltm_operations_are_serialized(tmp_path: Path) -> None:
    server = DaemonServer(_config(tmp_path))
    active = 0
    peak = 0

    async def search(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return [], [], "empty_results"

    server._adapter = SimpleNamespace(search=search)
    requests = [_request(OP_LTM_SEARCH, {"query": f"q-{i}"}) for i in range(4)]
    await asyncio.gather(*(server._dispatch(request) for request in requests))

    assert peak == 1


@pytest.mark.asyncio
async def test_starting_and_busy_do_not_open_local_circuit(tmp_path: Path) -> None:
    config = _config(tmp_path).surfacing
    config.min_response_chars = 0
    config.min_query_tokens = 1
    config.cooldown_seconds = 0
    config.max_surfacings_per_minute = 100
    adapter = AsyncMock()
    adapter.search = AsyncMock(
        side_effect=[
            ([], [], "daemon_starting"),
            ([], [], "daemon_busy"),
            ([], [], "daemon_busy"),
            ([], [], "empty_results"),
        ]
    )
    obs = SurfacingObservability()
    engine = SurfacingEngine(config, mcp_adapter=adapter, observability=obs)

    for _ in range(4):
        await engine.surface(
            "svc",
            "read_file",
            {},
            "response",
            context_query="shared daemon query",
        )

    assert adapter.search.await_count == 4
    skips = obs.snapshot()["skip_reasons"]["read_file"]
    assert skips["daemon_starting"] == 1
    assert skips["daemon_busy"] == 2
