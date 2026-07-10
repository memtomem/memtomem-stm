"""PR ⑦ — hot-reload of upstream connection config via reconnect.

Pins the acceptance criteria of the runtime-lifecycle plan section:
a reconnect applies the CURRENT config snapshot (not the connect-time one),
the replacement connection is prepared before the old one is torn down, and
a failed replacement leaves the previous connection serving.

All transports are mocked (CLAUDE.md: no live upstreams). The config loader
is reseeded via ``ProxyConfigLoader.seed`` with a ``tmp_path``-based config
path — the path never exists on disk, so the mtime probe keeps returning the
seeded snapshot deterministically (a real file at a shared path like
``/tmp/proxy.json`` could shadow the reseed mid-run).
"""

from __future__ import annotations

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
        import asyncio

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
