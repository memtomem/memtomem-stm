"""PR ⑦ — hot-reload of upstream connection config via reconnect.

Pins the acceptance criteria of the runtime-lifecycle plan section:
a reconnect applies the CURRENT config snapshot (not the connect-time one),
the replacement connection is prepared before the old one is torn down, and
a failed replacement leaves the previous connection serving.

Transports are mocked except in ``TestLiveStdioReconnect``, which spawns a
real local stdio child on purpose — anyio cancel-scope semantics are invisible
to mocks (no LIVE-SERVICE dependency; CLAUDE.md's mocked-transport rule targets
Ollama/network upstreams). The config loader is reseeded via
``ProxyConfigLoader.seed`` with a ``tmp_path``-based config path — the path
never exists on disk, so the mtime probe keeps returning the seeded snapshot
deterministically (a real file at a shared path like ``/tmp/proxy.json`` could
shadow the reseed mid-run).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from memtomem_stm.proxy.config import (
    ProxyConfig,
    TransportType,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import (
    ProxyManager,
    UpstreamConnection,
    _connection_fingerprint,
)
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.utils.circuit_breaker import CircuitBreaker

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_manager(tmp_path: Path, servers: dict[str, UpstreamServerConfig]) -> ProxyManager:
    proxy_cfg = ProxyConfig(config_path=tmp_path / "proxy.json", upstream_servers=servers)
    return ProxyManager(proxy_cfg, TokenTracker())


def _reseed(mgr: ProxyManager, tmp_path: Path, servers: dict[str, UpstreamServerConfig]) -> None:
    """Simulate a config-file hot-reload: install a new snapshot in the loader."""
    mgr._config_loader.seed(
        ProxyConfig(config_path=tmp_path / "proxy.json", upstream_servers=servers)
    )


def _seed_connection(
    mgr: ProxyManager, name: str, cfg: UpstreamServerConfig, *, stack: AsyncMock | None = None
) -> UpstreamConnection:
    conn = UpstreamConnection(
        name=name,
        config=cfg,
        session=AsyncMock(),
        tools=[],
        stack=stack if stack is not None else AsyncMock(),
    )
    mgr._connections[name] = conn
    return conn


def _mock_session() -> AsyncMock:
    s = AsyncMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.initialize = AsyncMock()
    s.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[]))
    return s


def _mock_transport() -> AsyncMock:
    t = AsyncMock()
    t.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
    t.__aexit__ = AsyncMock(return_value=False)
    return t


def _sse_cfg(url: str) -> UpstreamServerConfig:
    return UpstreamServerConfig(prefix="srv", transport=TransportType.SSE, url=url)


# ── reconnect reads the current snapshot ────────────────────────────────


class TestReconnectUsesCurrentSnapshot:
    async def test_reconnect_uses_current_config_snapshot(self, tmp_path):
        """An url edit that hot-reloaded after connect is applied by the next
        reconnect: the new transport opens with the CURRENT config, and
        ``conn.config`` is refreshed to it."""
        cfg_a = _sse_cfg("https://old.example/sse")
        cfg_b = _sse_cfg("https://new.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        _seed_connection(mgr, "srv", cfg_a)
        _reseed(mgr, tmp_path, {"srv": cfg_b})

        captured: list[UpstreamServerConfig] = []
        mock_session = _mock_session()

        def _capture_open(cfg):
            captured.append(cfg)
            return _mock_transport()

        with (
            patch.object(mgr, "_open_transport", side_effect=_capture_open),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
        ):
            await mgr._reconnect_server("srv")

        conn = mgr._connections["srv"]
        assert captured == [cfg_b]
        assert conn.config is cfg_b
        assert conn.session is mock_session
        assert conn.reconnect_generation == 1

    async def test_reconnect_falls_back_when_server_key_removed(self, tmp_path):
        """Removing a server from the file stays restart-only: a reconnect for
        a connection whose key vanished reuses the connect-time snapshot
        instead of crashing or tearing the server down."""
        cfg_a = _sse_cfg("https://old.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        _seed_connection(mgr, "srv", cfg_a)
        _reseed(mgr, tmp_path, {})

        captured: list[UpstreamServerConfig] = []
        mock_session = _mock_session()

        def _capture_open(cfg):
            captured.append(cfg)
            return _mock_transport()

        with (
            patch.object(mgr, "_open_transport", side_effect=_capture_open),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
        ):
            await mgr._reconnect_server("srv")

        conn = mgr._connections["srv"]
        assert captured == [cfg_a]
        assert conn.config is cfg_a
        assert conn.reconnect_generation == 1


# ── prepare-first swap ───────────────────────────────────────────────────


class TestPrepareFirstSwap:
    async def test_reconnect_failure_keeps_old_connection(self, tmp_path):
        """The replacement is prepared BEFORE the old connection is touched:
        when it can't be established, the previous session/stack/config all
        survive and the old stack is never closed."""
        cfg_a = _sse_cfg("https://old.example/sse")
        cfg_b = _sse_cfg("https://new.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        old_stack = AsyncMock()
        conn = _seed_connection(mgr, "srv", cfg_a, stack=old_stack)
        old_session = conn.session
        _reseed(mgr, tmp_path, {"srv": cfg_b})

        failing_transport = _mock_transport()
        failing_transport.__aenter__ = AsyncMock(side_effect=ConnectionError("refused"))

        with (
            patch.object(mgr, "_open_transport", return_value=failing_transport),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=_mock_session()),
        ):
            with pytest.raises(ConnectionError, match="refused"):
                await mgr._reconnect_server("srv")

        assert conn.session is old_session
        assert conn.stack is old_stack
        assert conn.config is cfg_a
        assert conn.reconnect_generation == 0
        old_stack.aclose.assert_not_awaited()

    async def test_reconnect_success_closes_old_stack_after_new_ready(self, tmp_path):
        """Swap ordering: the old stack is closed only after the replacement
        has fully initialized and discovered tools."""
        cfg = _sse_cfg("https://up.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg})
        events: list[str] = []

        old_stack = AsyncMock()

        async def _old_close():
            events.append("old_close")

        old_stack.aclose = AsyncMock(side_effect=_old_close)
        _seed_connection(mgr, "srv", cfg, stack=old_stack)

        mock_session = _mock_session()

        async def _init():
            events.append("initialize")

        async def _list_tools():
            events.append("list_tools")
            return SimpleNamespace(tools=[])

        mock_session.initialize = AsyncMock(side_effect=_init)
        mock_session.list_tools = AsyncMock(side_effect=_list_tools)

        with (
            patch.object(mgr, "_open_transport", return_value=_mock_transport()),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
        ):
            await mgr._reconnect_server("srv")

        assert events == ["initialize", "list_tools", "old_close"]
        old_stack.aclose.assert_awaited_once()
        assert mgr._connections["srv"].reconnect_generation == 1


# ── call-time change detection + damping ────────────────────────────────


def _ok_result(text="ok"):
    from types import SimpleNamespace

    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], isError=False)


async def _fetch(mgr: ProxyManager, server: str = "srv", tool: str = "t"):
    return await mgr._fetch_upstream(server, tool, {"_trace_id": None}, trace_id=None)


class TestConfigChangeDetection:
    async def test_url_change_prepares_then_swaps(self, tmp_path):
        """Acceptance (i): a connection-affecting edit is applied on the next
        uncached call — the replacement is prepared with the new config,
        swapped in, the old stack closed, and the call served on the NEW
        session."""
        cfg_a = _sse_cfg("https://old.example/sse")
        cfg_b = _sse_cfg("https://new.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        old_stack = AsyncMock()
        conn = _seed_connection(mgr, "srv", cfg_a, stack=old_stack)
        old_session = conn.session
        _reseed(mgr, tmp_path, {"srv": cfg_b})

        captured: list[UpstreamServerConfig] = []
        new_session = _mock_session()
        sentinel = _ok_result("fresh")
        new_session.call_tool = AsyncMock(return_value=sentinel)

        def _capture_open(cfg):
            captured.append(cfg)
            return _mock_transport()

        with (
            patch.object(mgr, "_open_transport", side_effect=_capture_open),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=new_session),
        ):
            result = await _fetch(mgr)

        assert result is sentinel
        assert captured == [cfg_b]
        assert conn.config is cfg_b
        assert conn.session is new_session
        old_stack.aclose.assert_awaited_once()
        old_session.call_tool.assert_not_awaited()

    async def test_failed_config_change_keeps_serving_and_damps(self, tmp_path):
        """Acceptance (i, failure half): when the edited config can't connect,
        the old connection keeps serving — and the broken edit is attempted
        exactly once, not on every call."""
        cfg_a = _sse_cfg("https://old.example/sse")
        cfg_b = _sse_cfg("https://new.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        old_stack = AsyncMock()
        conn = _seed_connection(mgr, "srv", cfg_a, stack=old_stack)
        conn.session.call_tool = AsyncMock(return_value=_ok_result())
        _reseed(mgr, tmp_path, {"srv": cfg_b})

        opens = 0

        def _failing_open(cfg):
            nonlocal opens
            opens += 1
            raise ConnectionError("refused")

        with patch.object(mgr, "_open_transport", side_effect=_failing_open):
            first = await _fetch(mgr)
            second = await _fetch(mgr)

        assert first.content[0].text == "ok"
        assert second.content[0].text == "ok"
        assert opens == 1  # damped: the same failed edit is not retried
        assert conn.config is cfg_a
        assert conn.stack is old_stack
        old_stack.aclose.assert_not_awaited()
        assert conn.last_failed_connection_fp == _connection_fingerprint(cfg_b)

    async def test_damping_clears_on_next_edit(self, tmp_path):
        """A further edit (different fingerprint) re-arms detection after a
        damped failure."""
        cfg_a = _sse_cfg("https://old.example/sse")
        cfg_b = _sse_cfg("https://broken.example/sse")
        cfg_c = _sse_cfg("https://fixed.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        conn = _seed_connection(mgr, "srv", cfg_a)
        conn.session.call_tool = AsyncMock(return_value=_ok_result())

        _reseed(mgr, tmp_path, {"srv": cfg_b})
        with patch.object(
            mgr, "_open_transport", side_effect=ConnectionError("refused")
        ) as failing:
            await _fetch(mgr)
        assert failing.call_count == 1
        assert conn.last_failed_connection_fp == _connection_fingerprint(cfg_b)

        _reseed(mgr, tmp_path, {"srv": cfg_c})
        new_session = _mock_session()
        new_session.call_tool = AsyncMock(return_value=_ok_result("fixed"))
        with (
            patch.object(mgr, "_open_transport", return_value=_mock_transport()) as ok_open,
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=new_session),
        ):
            result = await _fetch(mgr)

        assert ok_open.call_count == 1
        assert result.content[0].text == "fixed"
        assert conn.config is cfg_c
        assert conn.last_failed_connection_fp is None

    async def test_reconnect_honors_passed_cfg_over_diverged_snapshot(self, tmp_path):
        """Reload-generation race fix (codex #1/#2): ``_reconnect_server``
        attempts and redacts against the cfg the caller PASSED, ignoring a
        ``self._config`` that has since diverged. The detection wrapper passes
        the fingerprinted ``fresh_cfg``, so the config attempted == damped ==
        redacted are one and the same even if the file moved on.

        A re-read of ``self._config`` inside the reconnect (the pre-fix
        behavior) would instead attempt C here — leaving C retried on every
        call, B wrongly suppressed, and B's credential un-redacted."""
        cfg_b = _sse_cfg("https://alice:tok-b@b.example/sse")
        cfg_c = _sse_cfg("https://carol:tok-c@c.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg_b})
        _seed_connection(mgr, "srv", _sse_cfg("https://old.example/sse"))
        # The live snapshot is C — diverged from the B we pass in.
        _reseed(mgr, tmp_path, {"srv": cfg_c})

        attempted: list[str] = []

        def _open(cfg):
            attempted.append(cfg.url)
            return _mock_transport()

        with (
            patch.object(mgr, "_open_transport", side_effect=_open),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=_mock_session()),
        ):
            await mgr._reconnect_server("srv", cfg_b)

        assert attempted == [cfg_b.url]  # honored the passed cfg, not snapshot C
        assert mgr._connections["srv"].config is cfg_b

    async def test_damped_failure_redacts_the_attempted_credential(self, tmp_path):
        """The damped-failure warning is redacted against the config actually
        attempted, reproducing the reload race faithfully: the request resolved
        B (``fresh_cfg``) but the live snapshot has ALREADY rotated to C by the
        time the reconnect runs. The fix attempts B (the passed cfg) and redacts
        B's token; the damped fingerprint is B's. Under the pre-fix re-read,
        ``_reconnect_server`` would instead read the live snapshot C, attempt C,
        yet the wrapper would damp/redact with B — so C's credential would leak
        and B would be wrongly suppressed.

        Driven through ``_maybe_reconnect_for_config_change`` directly with the
        loader pre-seeded to C: this is exactly the state after the file moved
        on between the request pinning its ``cfg_snap`` (B) and the reconnect
        firing — no lock choreography needed because the divergence already
        exists when the wrapper is entered."""
        import logging

        cfg_a = _sse_cfg("https://old.example/sse")
        cfg_b = _sse_cfg("https://alice:tok-b@b.example/sse")
        cfg_c = _sse_cfg("https://carol:tok-c@c.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        conn = _seed_connection(mgr, "srv", cfg_a)
        # The live snapshot is already C; the request still holds B.
        _reseed(mgr, tmp_path, {"srv": cfg_c})

        attempted: list[str] = []

        def _open(cfg):
            attempted.append(cfg.url)
            raise ConnectionError(f"connection failed for {cfg.url}")

        records: list[logging.LogRecord] = []

        class _H(logging.Handler):
            def emit(self, record):
                records.append(record)

        mgr_logger = logging.getLogger("memtomem_stm.proxy.manager")
        handler = _H()
        mgr_logger.addHandler(handler)
        prev_level = mgr_logger.level
        mgr_logger.setLevel(logging.DEBUG)
        try:
            with patch.object(mgr, "_open_transport", side_effect=_open):
                await mgr._maybe_reconnect_for_config_change(conn, cfg_b)
        finally:
            mgr_logger.removeHandler(handler)
            mgr_logger.setLevel(prev_level)

        assert attempted == [cfg_b.url]  # attempted the resolved B, not live C
        assert conn.last_failed_connection_fp == _connection_fingerprint(cfg_b)
        all_text = "\n".join(r.getMessage() for r in records)
        assert "tok-c" not in all_text  # C never attempted, never logged
        assert "tok-b" not in all_text  # B attempted but redacted
        assert "***@b.example" in all_text

    async def test_generation_skip_does_not_damp_and_redetects_next_call(self, tmp_path):
        """When a config-change reconnect is skipped because another reconnect
        already advanced the generation while we waited on the lock, the wrapper
        must NOT damp the skipped config (it was never attempted) — so the very
        next request still detects and applies it. The skip's ``record_success``
        is acceptable: a concurrent reconnect genuinely completed."""
        cfg_a = _sse_cfg("https://old.example/sse")
        cfg_b = _sse_cfg("https://b.example/sse")  # what a concurrent reconnect landed
        cfg_c = _sse_cfg("https://c.example/sse")  # what THIS request wants
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        conn = _seed_connection(mgr, "srv", cfg_a)
        conn.session.call_tool = AsyncMock(return_value=_ok_result())

        opens: list[str] = []

        def _open(cfg):
            opens.append(cfg.url)
            return _mock_transport()

        # Hold the reconnect lock, start the wrapper (it blocks trying to
        # reconnect to C), then simulate a concurrent reconnect completing:
        # bump the generation and land B. Releasing the lock lets the wrapper's
        # reconnect wake, see the generation advanced, and skip.
        async with conn.reconnect_lock:
            task = asyncio.create_task(mgr._maybe_reconnect_for_config_change(conn, cfg_c))
            await asyncio.sleep(0)  # let the task reach the lock and block
            conn.reconnect_generation += 1
            conn.config = cfg_b
        await task

        assert opens == []  # C was never attempted (skipped)
        assert conn.last_failed_connection_fp is None  # skipped ≠ failed → not damped

        # Next request still sees C in the file and applies it.
        _reseed(mgr, tmp_path, {"srv": cfg_c})
        new_session = _mock_session()
        new_session.call_tool = AsyncMock(return_value=_ok_result("on-c"))
        with (
            patch.object(mgr, "_open_transport", side_effect=_open),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=new_session),
        ):
            result = await _fetch(mgr)

        assert opens == [cfg_c.url]  # re-detected and applied on the next call
        assert result.content[0].text == "on-c"
        assert conn.config is cfg_c

    async def test_successful_failure_triggered_reconnect_clears_damper(self, tmp_path):
        """Any successful reconnect (config-change OR failure-triggered)
        proves the current config connects and clears the damper."""
        cfg = _sse_cfg("https://up.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg})
        conn = _seed_connection(mgr, "srv", cfg)
        conn.last_failed_connection_fp = ("stale",)

        with (
            patch.object(mgr, "_open_transport", return_value=_mock_transport()),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=_mock_session()),
        ):
            await mgr._reconnect_server("srv")

        assert conn.last_failed_connection_fp is None
        assert conn.reconnect_generation == 1

    async def test_concurrent_calls_one_config_change_reconnect(self, tmp_path):
        """Acceptance (ii): two concurrent calls that both observe the config
        change collapse into ONE reconnect — the lock waiter skips via the
        generation check and proceeds on the freshly swapped session."""
        cfg_a = _sse_cfg("https://old.example/sse")
        cfg_b = _sse_cfg("https://new.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        conn = _seed_connection(mgr, "srv", cfg_a)
        _reseed(mgr, tmp_path, {"srv": cfg_b})

        opens = 0

        def _open(cfg):
            nonlocal opens
            opens += 1
            t = _mock_transport()

            async def _delayed_streams(*_args):
                # Yield control so the second call reaches detection while the
                # first reconnect is mid-establish.
                await asyncio.sleep(0.01)
                return (AsyncMock(), AsyncMock())

            t.__aenter__ = AsyncMock(side_effect=_delayed_streams)
            return t

        new_session = _mock_session()
        new_session.call_tool = AsyncMock(return_value=_ok_result())

        with (
            patch.object(mgr, "_open_transport", side_effect=_open),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=new_session),
        ):
            r1, r2 = await asyncio.gather(_fetch(mgr), _fetch(mgr))

        assert opens == 1
        assert conn.reconnect_generation == 1
        assert r1.content[0].text == "ok"
        assert r2.content[0].text == "ok"

    async def test_config_change_runs_before_breaker_and_closes_it_on_success(self, tmp_path):
        """Detection precedes the circuit-breaker fast-fail, and a successful
        config-change reconnect closes the breaker — a fixed url must not be
        fast-failed for up to circuit_reset_seconds by the OLD config's
        failure streak."""
        cfg_a = _sse_cfg("https://old.example/sse")
        cfg_b = _sse_cfg("https://new.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        conn = _seed_connection(mgr, "srv", cfg_a)
        breaker = CircuitBreaker(max_failures=1, reset_timeout=3600.0, name="upstream-srv")
        breaker.record_failure()
        assert breaker.is_open
        conn.breaker = breaker
        _reseed(mgr, tmp_path, {"srv": cfg_b})

        new_session = _mock_session()
        new_session.call_tool = AsyncMock(return_value=_ok_result())

        with (
            patch.object(mgr, "_open_transport", return_value=_mock_transport()),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=new_session),
        ):
            result = await _fetch(mgr)

        assert result.content[0].text == "ok"
        assert conn.config is cfg_b
        assert not breaker.is_open


class TestHotReloadClassification:
    async def test_non_connection_edit_does_not_reconnect_but_applies(self, tmp_path):
        """Per-server knobs outside the connection fingerprint hot-reload
        WITHOUT a reconnect: no transport churn, and the edited value (here
        ``max_retries``) governs the very next call."""
        cfg_a = UpstreamServerConfig(
            prefix="srv",
            transport=TransportType.SSE,
            url="https://up.example/sse",
            max_retries=2,
            reconnect_delay_seconds=0.0,
            max_reconnect_delay_seconds=0.0,
        )
        cfg_b = cfg_a.model_copy(update={"max_retries": 0})
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        conn = _seed_connection(mgr, "srv", cfg_a)
        _reseed(mgr, tmp_path, {"srv": cfg_b})

        # Successful call: the edit must not trigger any reconnect.
        conn.session.call_tool = AsyncMock(return_value=_ok_result())
        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as rec:
            await _fetch(mgr)
        rec.assert_not_awaited()

        # Failing call: the NEW max_retries=0 bounds the attempts (the stale
        # snapshot's max_retries=2 would have made three).
        conn.session.call_tool = AsyncMock(side_effect=ConnectionError("boom"))
        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError, match="boom"):
                await _fetch(mgr)
        assert conn.session.call_tool.await_count == 1

    async def test_stdio_connect_timeout_edit_does_not_reconnect(self, tmp_path):
        """For stdio, connect_timeout_seconds is consumed per connection
        attempt (not baked into a live transport), so an edit is NOT
        connection-affecting — it simply applies to the next reconnect."""
        cfg_a = UpstreamServerConfig(prefix="srv", command="echo", connect_timeout_seconds=30.0)
        cfg_b = cfg_a.model_copy(update={"connect_timeout_seconds": 5.0})
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        conn = _seed_connection(mgr, "srv", cfg_a)
        conn.session.call_tool = AsyncMock(return_value=_ok_result())
        _reseed(mgr, tmp_path, {"srv": cfg_b})

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as rec:
            await _fetch(mgr)
        rec.assert_not_awaited()

    async def test_sse_connect_timeout_edit_reconnects(self, tmp_path):
        """For network transports the connect budget is passed to the SDK
        client factory at open time, so editing it IS connection-affecting."""
        cfg_a = _sse_cfg("https://up.example/sse")
        cfg_b = cfg_a.model_copy(update={"connect_timeout_seconds": 5.0})
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        conn = _seed_connection(mgr, "srv", cfg_a)
        conn.session.call_tool = AsyncMock(return_value=_ok_result())
        _reseed(mgr, tmp_path, {"srv": cfg_b})

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as rec:
            await _fetch(mgr)
        rec.assert_awaited_once()

    async def test_fingerprint_ignores_inactive_transport_fields(self, tmp_path):
        """The fingerprint carries only the fields the active transport uses, so
        editing a dormant field (a stdio ``command`` on an SSE server, or an
        SSE ``url`` on a stdio server) does not churn a live connection."""
        sse = _sse_cfg("https://up.example/sse")
        sse_cmd_edit = sse.model_copy(update={"command": "changed", "args": ["x"]})
        assert _connection_fingerprint(sse) == _connection_fingerprint(sse_cmd_edit)

        stdio = UpstreamServerConfig(prefix="srv", command="echo", args=["a"])
        stdio_url_edit = stdio.model_copy(
            update={"headers": {"X": "y"}, "connect_timeout_seconds": 5.0}
        )
        assert _connection_fingerprint(stdio) == _connection_fingerprint(stdio_url_edit)

        # …but an ACTIVE-field edit still rotates the fingerprint.
        assert _connection_fingerprint(sse) != _connection_fingerprint(
            sse.model_copy(update={"url": "https://other.example/sse"})
        )
        assert _connection_fingerprint(stdio) != _connection_fingerprint(
            stdio.model_copy(update={"args": ["b"]})
        )


# ── live stdio transport (real anyio scopes) ─────────────────────────────

_ECHO_SERVER_SRC = """
import os
import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")
TAG = sys.argv[1] if len(sys.argv) > 1 else "noarg"


@mcp.tool()
def greet() -> str:
    return f"hello from {os.environ.get('GREETING', 'default')}/{TAG}"


mcp.run()
"""


def _child_env(greeting: str) -> dict[str, str]:
    """Minimal child env: the marker under test plus the platform vars a bare
    Python subprocess needs (StdioServerParameters env REPLACES the default
    environment rather than merging)."""
    import os

    env = {"GREETING": greeting}
    for key in ("PATH", "HOME", "SYSTEMROOT", "USERPROFILE", "TEMP", "TMP"):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    return env


class TestLiveStdioReconnect:
    """End-to-end against a REAL stdio child process — the mocked-transport
    suites cannot see anyio cancel-scope semantics. Regression pin for the
    prepare-first ordering: with the replacement's scopes opened on the
    calling task, closing the old stack afterward is a same-task
    out-of-order scope exit and the old stdio transport's close CANCELS the
    calling task — the CancelledError escapes every except-Exception guard
    and kills the tool call that triggered the reconnect. The fix opens the
    replacement in a child task."""

    async def test_config_change_live_reconnect_same_task(self, tmp_path):
        import sys

        server_py = tmp_path / "echo_server.py"
        server_py.write_text(_ECHO_SERVER_SRC)

        def _cfg(greeting: str, tag: str) -> UpstreamServerConfig:
            return UpstreamServerConfig(
                prefix="echo",
                command=sys.executable,
                args=[str(server_py), tag],
                env=_child_env(greeting),
            )

        cfg_a = _cfg("alpha", "v1")
        cfg_b = _cfg("beta", "v2")
        mgr = _make_manager(tmp_path, {"echo": cfg_a})
        await mgr.start()
        try:
            assert "echo" in mgr._connections, mgr._failed_servers

            r1 = await mgr._fetch_upstream("echo", "greet", {"_trace_id": None}, trace_id=None)
            assert "hello from alpha/v1" in r1.content[0].text

            _reseed(mgr, tmp_path, {"echo": cfg_b})

            # Same task as the connect: before the child-task fix this call
            # died with CancelledError while closing the old stack.
            r2 = await mgr._fetch_upstream("echo", "greet", {"_trace_id": None}, trace_id=None)
            assert "hello from beta/v2" in r2.content[0].text

            conn = mgr._connections["echo"]
            assert conn.reconnect_generation == 1
            assert conn.config is cfg_b

            # The swapped connection must survive further calls (scope stack
            # left consistent), including a SECOND same-task reconnect.
            _reseed(mgr, tmp_path, {"echo": cfg_a})
            r3 = await mgr._fetch_upstream("echo", "greet", {"_trace_id": None}, trace_id=None)
            assert "hello from alpha/v1" in r3.content[0].text
            assert mgr._connections["echo"].reconnect_generation == 2
        finally:
            await mgr.stop()
