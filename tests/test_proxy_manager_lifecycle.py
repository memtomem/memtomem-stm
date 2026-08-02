"""Tests for ProxyManager lifecycle — start, stop, double-start guard."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from memtomem_stm.proxy.config import (
    ProxyConfig,
    TransportType,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_manager(
    servers: dict[str, UpstreamServerConfig] | None = None,
    tmp_path: Path | None = None,
) -> ProxyManager:
    """Create a ProxyManager with configurable upstream servers."""
    if servers is None:
        servers = {
            "srv": UpstreamServerConfig(prefix="test"),
        }
    config_path = (tmp_path / "proxy.json") if tmp_path else Path("/tmp/proxy.json")
    proxy_cfg = ProxyConfig(config_path=config_path, upstream_servers=servers)
    return ProxyManager(proxy_cfg, TokenTracker())


# ── start() ──────────────────────────────────────────────────────────────


class TestStart:
    async def test_start_connects_to_servers(self):
        """start() calls _connect_server for each configured upstream server."""
        mgr = _make_manager(
            servers={
                "a": UpstreamServerConfig(prefix="a"),
                "b": UpstreamServerConfig(prefix="b"),
            }
        )
        with patch.object(mgr, "_connect_server", new_callable=AsyncMock) as mock_conn:
            await mgr.start()

        called_names = [call.args[0] for call in mock_conn.call_args_list]
        assert sorted(called_names) == ["a", "b"]

    async def test_start_empty_servers_loads_file(self, tmp_path):
        """When upstream_servers is empty, start() falls back to load_from_file."""
        mgr = _make_manager(servers={}, tmp_path=tmp_path)
        loaded_cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={"file_srv": UpstreamServerConfig(prefix="fs")},
        )
        with (
            patch.object(ProxyConfig, "load_from_file", return_value=loaded_cfg),
            patch.object(mgr, "_connect_server", new_callable=AsyncMock) as mock_conn,
        ):
            await mgr.start()

        assert mock_conn.call_count == 1
        assert mock_conn.call_args_list[0].args[0] == "file_srv"

    async def test_start_fallback_load_does_not_duplicate_advisory_warnings(self, tmp_path, caplog):
        """codex review of #611: the empty-upstreams fallback re-loads a file
        the server startup path already loaded and warned about — start()
        must not emit the advisory unknown-key / permissive-mode warnings a
        second time."""
        import json
        import logging

        cfg_file = tmp_path / "proxy.json"
        cfg_file.write_text(json.dumps({"enabled": True, "max_result_char": 1}))
        cfg_file.chmod(0o644)
        mgr = _make_manager(servers={}, tmp_path=tmp_path)
        with (
            caplog.at_level(logging.WARNING),
            patch.object(mgr, "_connect_server", new_callable=AsyncMock),
        ):
            await mgr.start()
        assert not [
            r
            for r in caplog.records
            if "unknown key" in r.getMessage() or "permissive mode" in r.getMessage()
        ]
        await mgr.stop()

    async def test_start_empty_servers_no_file_noop(self, tmp_path):
        """No servers configured and no file — start() completes without error."""
        mgr = _make_manager(servers={}, tmp_path=tmp_path)
        with (
            patch.object(ProxyConfig, "load_from_file", return_value=None),
            patch.object(mgr, "_connect_server", new_callable=AsyncMock) as mock_conn,
        ):
            await mgr.start()

        assert mock_conn.call_count == 0

    async def test_start_server_failure_logged(self, caplog):
        """If _connect_server raises, start() logs and continues."""
        mgr = _make_manager(
            servers={
                "ok": UpstreamServerConfig(prefix="ok"),
                "bad": UpstreamServerConfig(prefix="bad"),
            }
        )

        async def _conditional_connect(name, cfg):
            if name == "bad":
                raise ConnectionError("unreachable")

        with patch.object(mgr, "_connect_server", side_effect=_conditional_connect):
            await mgr.start()

        assert "Failed to connect to upstream server 'bad'" in caplog.text
        # #580: the failed server is recorded so it stays visible in health,
        # instead of vanishing (no _connections entry is created on failure).
        assert "bad" in mgr._failed_servers
        assert "unreachable" in mgr._failed_servers["bad"]

    async def test_startup_failed_server_appears_in_health(self):
        """#580: a configured-but-unconnected server surfaces in
        get_upstream_health with connected=False and its connect error,
        making the DISCONNECTED rendering reachable."""
        mgr = _make_manager(
            servers={
                "ok": UpstreamServerConfig(prefix="ok"),
                "bad": UpstreamServerConfig(prefix="bad"),
            }
        )

        async def _conditional_connect(name, cfg):
            if name == "bad":
                raise ConnectionError("unreachable")
            # The 'ok' server: register a live connection so it reports healthy.
            mgr._connections[name] = UpstreamConnection(
                name=name, config=cfg, session=AsyncMock(), tools=[]
            )

        with patch.object(mgr, "_connect_server", side_effect=_conditional_connect):
            await mgr.start()

        health = mgr.get_upstream_health()
        assert health["bad"]["connected"] is False
        assert "unreachable" in health["bad"]["error"]
        assert health["ok"]["connected"] is True
        assert "error" not in health["ok"]

    async def test_health_prefers_live_connection_over_stale_failed_entry(self):
        """If a name is somehow in both maps (a live connection and a stale
        failed record), get_upstream_health reports it connected — the live
        connection wins and no error line leaks (#580 guard)."""
        mgr = _make_manager(servers={"srv": UpstreamServerConfig(prefix="srv")})
        mgr._connections["srv"] = UpstreamConnection(
            name="srv", config=UpstreamServerConfig(prefix="srv"), session=AsyncMock(), tools=[]
        )
        mgr._failed_servers["srv"] = "stale error"

        health = mgr.get_upstream_health()
        assert health["srv"]["connected"] is True
        assert "error" not in health["srv"]

    async def test_startup_failure_redacts_credentialed_url(self):
        """#580: a startup connect error whose message embeds a credentialed
        URL (as httpx exceptions do) must be scrubbed before it lands in
        _failed_servers / stm_proxy_health — otherwise the token leaks to the
        MCP client/model through the health tool."""
        url = "https://alice:s3cr3t-token@ltm.example.com/mcp"
        mgr = _make_manager(
            servers={
                "web": UpstreamServerConfig(prefix="web", transport=TransportType.SSE, url=url),
            }
        )

        async def _leaky_connect(name, cfg):
            # httpx-style message that embeds the full request URL, userinfo
            # included.
            raise ConnectionError(f"All connection attempts failed for {url}")

        with patch.object(mgr, "_connect_server", side_effect=_leaky_connect):
            await mgr.start()

        recorded = mgr._failed_servers["web"]
        health_error = mgr.get_upstream_health()["web"]["error"]
        for blob in (recorded, health_error):
            assert "s3cr3t-token" not in blob
            assert "alice:s3cr3t-token" not in blob
            assert "***@ltm.example.com" in blob

    async def test_startup_failure_redacts_long_credential_past_cap(self):
        """#580: redaction runs on the FULL message before the 500-char cap, so
        a credential long enough that ``@host`` falls past the cap is still
        scrubbed. Capping first (as format_error_message_from_exc does) would
        truncate the token mid-string, leaving a partial that redact can no
        longer match against the configured URL."""
        from memtomem_stm.proxy.metrics import MAX_ERROR_MESSAGE_CHARS

        token = "t" * (MAX_ERROR_MESSAGE_CHARS + 300)  # pushes @host past the cap
        url = f"https://user:{token}@ltm.example.com/mcp"
        mgr = _make_manager(
            servers={
                "web": UpstreamServerConfig(prefix="web", transport=TransportType.SSE, url=url),
            }
        )

        async def _leaky_connect(name, cfg):
            raise ConnectionError(f"All connection attempts failed for {url}")

        with patch.object(mgr, "_connect_server", side_effect=_leaky_connect):
            await mgr.start()

        recorded = mgr._failed_servers["web"]
        health_error = mgr.get_upstream_health()["web"]["error"]
        for blob in (recorded, health_error):
            # Not even a long partial run of the token may survive the cap.
            assert token not in blob
            assert "t" * 100 not in blob
            assert "***@ltm.example.com" in blob

    async def test_startup_failure_log_line_redacts_credential(self, caplog):
        """#580: the operator LOG for a failed credentialed connect must also be
        scrubbed. The failure is logged as a redacted message, not via
        logger.exception whose traceback tail repeats the raw exception string
        (URL included)."""
        url = "https://alice:s3cr3t-token@ltm.example.com/mcp"
        mgr = _make_manager(
            servers={
                "web": UpstreamServerConfig(prefix="web", transport=TransportType.SSE, url=url),
            }
        )

        async def _leaky_connect(name, cfg):
            raise ConnectionError(f"All connection attempts failed for {url}")

        with caplog.at_level("ERROR"):
            with patch.object(mgr, "_connect_server", side_effect=_leaky_connect):
                await mgr.start()

        # caplog.text includes any exc_info traceback, so this also fails if the
        # code regresses to logger.exception.
        assert "s3cr3t-token" not in caplog.text
        assert "alice:s3cr3t-token" not in caplog.text
        # The failure is still logged, redacted.
        assert "Failed to connect to upstream server 'web'" in caplog.text
        assert "***@ltm.example.com" in caplog.text

    async def test_url_less_network_upstream_recorded_in_health(self):
        """#580: a non-stdio upstream configured without a url is skipped by
        _connect_server with a warning + early return (no exception), so it
        must be recorded in _failed_servers itself — otherwise start()'s except
        never fires and the misconfigured server stays false-green in health."""
        mgr = _make_manager(
            servers={
                "web": UpstreamServerConfig(prefix="web", transport=TransportType.SSE, url=""),
            }
        )

        await mgr.start()

        assert "web" in mgr._failed_servers
        assert "configuration error" in mgr._failed_servers["web"]
        health = mgr.get_upstream_health()
        assert health["web"]["connected"] is False
        assert "configuration error" in health["web"]["error"]

    async def test_double_start_clears_stale_failed_servers(self):
        """#580: a manager reused across start() calls must not keep reporting
        a previous session's failed upstream — the double-start reset clears
        _failed_servers alongside _connections."""
        mgr = _make_manager(servers={})
        with patch.object(ProxyConfig, "load_from_file", return_value=None):
            await mgr.start()
        mgr._failed_servers["gone"] = "stale connect error"

        with patch.object(ProxyConfig, "load_from_file", return_value=None):
            await mgr.start()

        assert mgr._failed_servers == {}
        assert "gone" not in mgr.get_upstream_health()

    async def test_double_start_closes_previous(self):
        """Calling start() twice closes the previous AsyncExitStack."""
        mgr = _make_manager(servers={})
        with patch.object(ProxyConfig, "load_from_file", return_value=None):
            await mgr.start()

        first_stack = mgr._stack
        assert first_stack is not None

        with (
            patch.object(ProxyConfig, "load_from_file", return_value=None),
            patch.object(first_stack, "aclose", new_callable=AsyncMock) as mock_close,
        ):
            await mgr.start()

        mock_close.assert_awaited_once()
        assert mgr._stack is not first_stack

    async def test_double_start_closes_existing_connection_stacks(self):
        """Calling start() twice closes per-connection stacks before clearing them."""
        mgr = _make_manager(servers={})
        with patch.object(ProxyConfig, "load_from_file", return_value=None):
            await mgr.start()

        mock_stack = AsyncMock()
        mgr._connections["srv"] = UpstreamConnection(
            name="srv",
            config=UpstreamServerConfig(prefix="test"),
            session=AsyncMock(),
            tools=[],
            stack=mock_stack,
        )

        with patch.object(ProxyConfig, "load_from_file", return_value=None):
            await mgr.start()

        mock_stack.aclose.assert_awaited_once()
        assert mgr._connections == {}


# ── stop() ────────────────────────────────────────────────────────────────


class TestStop:
    async def test_stop_cancels_background_tasks(self):
        """stop() cancels all background tasks and gathers them."""
        mgr = _make_manager(servers={})

        async def _forever():
            await asyncio.sleep(999)

        task = asyncio.create_task(_forever())
        mgr._background_tasks.add(task)

        await mgr.stop()

        assert task.cancelled()
        assert len(mgr._background_tasks) == 0

    async def test_stop_closes_extractor(self):
        """stop() calls close() on the extractor if present."""
        mgr = _make_manager(servers={})
        mock_ext = AsyncMock()
        mgr._extractor = mock_ext

        await mgr.stop()

        mock_ext.close.assert_awaited_once()

    async def test_stop_clears_failed_servers(self):
        """#580: stop() clears startup-failure records so a stopped manager
        reports no upstreams — mirrors the double-start reset."""
        mgr = _make_manager(servers={})
        mgr._failed_servers["bad"] = "connect error"

        await mgr.stop()

        assert mgr._failed_servers == {}

    async def test_stop_nulls_extractor_so_restart_rebuilds(self):
        """stop() nulls _extractor (like _llm_compressor) — _get_extractor()
        rebuilds on None, so a stop->start cycle gets a fresh httpx client
        instead of the closed instance whose extract() would AssertionError."""
        mgr = _make_manager(servers={})
        mgr._extractor = AsyncMock()

        await mgr.stop()

        assert mgr._extractor is None

    async def test_stop_closes_connection_stacks(self):
        """stop() closes per-connection stacks and clears _connections."""
        mgr = _make_manager(servers={})

        mock_stack = AsyncMock()
        conn = UpstreamConnection(
            name="srv",
            config=UpstreamServerConfig(prefix="test"),
            session=AsyncMock(),
            tools=[],
            stack=mock_stack,
        )
        mgr._connections["srv"] = conn

        await mgr.stop()

        mock_stack.aclose.assert_awaited_once()
        assert len(mgr._connections) == 0


# ── connect timeout ─────────────────────────────────────────────────────


class TestConnectTimeout:
    async def test_connect_server_times_out_on_slow_initialize(self):
        """_connect_server raises TimeoutError when session.initialize() exceeds timeout."""
        cfg = UpstreamServerConfig(prefix="slow", connect_timeout_seconds=0.05)
        mgr = _make_manager(servers={"slow": cfg})

        # Initialize _stack without actually connecting
        with patch.object(mgr, "_connect_server", new_callable=AsyncMock):
            await mgr.start()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        async def _slow_init():
            await asyncio.sleep(10)

        mock_session.initialize = _slow_init

        mock_transport = AsyncMock()
        mock_transport.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
        mock_transport.__aexit__ = AsyncMock(return_value=False)

        import pytest as _pt

        with (
            patch.object(mgr, "_open_transport", return_value=mock_transport),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
        ):
            with _pt.raises(asyncio.TimeoutError):
                await mgr._connect_server("slow", cfg)

    async def test_start_logs_timeout_and_continues(self, caplog):
        """start() catches TimeoutError from _connect_server and continues."""
        mgr = _make_manager(
            servers={
                "ok": UpstreamServerConfig(prefix="ok"),
                "slow": UpstreamServerConfig(prefix="slow"),
            }
        )

        async def _conditional_connect(name, cfg):
            if name == "slow":
                raise asyncio.TimeoutError()

        with patch.object(mgr, "_connect_server", side_effect=_conditional_connect):
            await mgr.start()

        assert "Failed to connect to upstream server 'slow'" in caplog.text


class TestConnectDeadlineEndToEnd:
    """PR ⑦ timeout contract: ``connect_timeout_seconds`` is ONE end-to-end
    budget over transport entry + initialize + tools/list, applied identically
    at first connect and reconnect. Previously only ``initialize()`` was
    bounded — a hung TCP connect or a stalled ``tools/list`` blocked forever."""

    @staticmethod
    def _mocks(*, slow_transport=False, slow_list_tools=False):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.initialize = AsyncMock()
        if slow_list_tools:

            async def _slow_list():
                await asyncio.sleep(10)

            mock_session.list_tools = _slow_list
        else:
            mock_session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[]))

        mock_transport = AsyncMock()
        if slow_transport:

            async def _slow_enter(*_args):
                await asyncio.sleep(10)

            mock_transport.__aenter__ = AsyncMock(side_effect=_slow_enter)
        else:
            mock_transport.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
        mock_transport.__aexit__ = AsyncMock(return_value=False)
        return mock_session, mock_transport

    async def test_connect_times_out_on_slow_transport_entry(self):
        import pytest as _pt

        cfg = UpstreamServerConfig(prefix="slow", connect_timeout_seconds=0.05)
        mgr = _make_manager(servers={"slow": cfg})
        with patch.object(mgr, "_connect_server", new_callable=AsyncMock):
            await mgr.start()
        mock_session, mock_transport = self._mocks(slow_transport=True)

        with (
            patch.object(mgr, "_open_transport", return_value=mock_transport),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
        ):
            with _pt.raises(asyncio.TimeoutError):
                await mgr._connect_server("slow", cfg)

        assert "slow" not in mgr._connections

    async def test_connect_times_out_on_slow_list_tools(self):
        import pytest as _pt

        cfg = UpstreamServerConfig(prefix="slow", connect_timeout_seconds=0.05)
        mgr = _make_manager(servers={"slow": cfg})
        with patch.object(mgr, "_connect_server", new_callable=AsyncMock):
            await mgr.start()
        mock_session, mock_transport = self._mocks(slow_list_tools=True)

        with (
            patch.object(mgr, "_open_transport", return_value=mock_transport),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
        ):
            with _pt.raises(asyncio.TimeoutError):
                await mgr._connect_server("slow", cfg)

        # Partial unwind: both entered contexts are rolled back.
        assert "slow" not in mgr._connections
        mock_session.__aexit__.assert_awaited_once()
        mock_transport.__aexit__.assert_awaited_once()

    async def test_reconnect_times_out_on_slow_transport_entry(self):
        import pytest as _pt

        cfg = UpstreamServerConfig(prefix="srv", connect_timeout_seconds=0.05)
        mgr = _make_manager(servers={"srv": cfg})
        old_session = AsyncMock()
        mgr._connections["srv"] = UpstreamConnection(
            name="srv", config=cfg, session=old_session, tools=[], stack=AsyncMock()
        )
        mock_session, mock_transport = self._mocks(slow_transport=True)

        with (
            patch.object(mgr, "_open_transport", return_value=mock_transport),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
        ):
            with _pt.raises(asyncio.TimeoutError):
                await mgr._reconnect_server("srv")

        conn = mgr._connections["srv"]
        assert conn.session is old_session
        assert conn.reconnect_generation == 0

    async def test_reconnect_times_out_on_slow_list_tools(self):
        import pytest as _pt

        cfg = UpstreamServerConfig(prefix="srv", connect_timeout_seconds=0.05)
        mgr = _make_manager(servers={"srv": cfg})
        old_session = AsyncMock()
        mgr._connections["srv"] = UpstreamConnection(
            name="srv", config=cfg, session=old_session, tools=[], stack=AsyncMock()
        )
        mock_session, mock_transport = self._mocks(slow_list_tools=True)

        with (
            patch.object(mgr, "_open_transport", return_value=mock_transport),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
        ):
            with _pt.raises(asyncio.TimeoutError):
                await mgr._reconnect_server("srv")

        conn = mgr._connections["srv"]
        assert conn.session is old_session
        assert conn.reconnect_generation == 0
        # The partial NEW stack is rolled back on failure.
        mock_session.__aexit__.assert_awaited_once()
        mock_transport.__aexit__.assert_awaited_once()

    async def test_deadline_is_shared_across_phases(self):
        """The budget is one deadline, not a fresh timeout per phase."""
        cfg = UpstreamServerConfig(prefix="srv", connect_timeout_seconds=0.5)
        mgr = _make_manager(servers={"srv": cfg})
        with patch.object(mgr, "_connect_server", new_callable=AsyncMock):
            await mgr.start()

        mock_session, mock_transport = self._mocks()

        async def _consuming_enter(*_args):
            await asyncio.sleep(0.3)
            return (AsyncMock(), AsyncMock())

        async def _consuming_initialize():
            await asyncio.sleep(0.3)

        mock_transport.__aenter__ = AsyncMock(side_effect=_consuming_enter)
        mock_session.initialize = AsyncMock(side_effect=_consuming_initialize)

        import pytest as _pt

        with (
            patch.object(mgr, "_open_transport", return_value=mock_transport),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
        ):
            with _pt.raises(asyncio.TimeoutError):
                await mgr._connect_server("srv", cfg)

        # Each phase is individually below 0.5s, but their sum is not. A
        # per-phase reset would connect successfully; one shared deadline
        # times out during initialize and rolls the partial connection back.
        assert "srv" not in mgr._connections
        mock_session.__aexit__.assert_awaited_once()
        mock_transport.__aexit__.assert_awaited_once()

    async def test_connect_server_closes_partial_stack_when_list_tools_fails(self):
        """Failed initial connection must not leave transport/session cleanup
        deferred until ProxyManager.stop().
        """
        cfg = UpstreamServerConfig(prefix="bad")
        mgr = _make_manager(servers={"bad": cfg})

        with patch.object(mgr, "_connect_server", new_callable=AsyncMock):
            await mgr.start()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.initialize = AsyncMock()
        mock_session.list_tools = AsyncMock(side_effect=RuntimeError("catalog failed"))

        mock_transport = AsyncMock()
        mock_transport.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
        mock_transport.__aexit__ = AsyncMock(return_value=False)

        import pytest as _pt

        with (
            patch.object(mgr, "_open_transport", return_value=mock_transport),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
        ):
            with _pt.raises(RuntimeError, match="catalog failed"):
                await mgr._connect_server("bad", cfg)

        assert "bad" not in mgr._connections
        mock_session.__aexit__.assert_awaited_once()
        mock_transport.__aexit__.assert_awaited_once()

    async def test_cleanup_failure_log_redacts_credential(self, caplog):
        """#580: if the rollback aclose() ALSO raises for a credentialed network
        upstream, the DEBUG cleanup log must not leak the token — the message is
        redacted and no exc_info traceback (whose tail repeats the raw exception
        string) is emitted."""
        import pytest as _pt

        url = "https://alice:s3cr3t-token@ltm.example.com/mcp"
        cfg = UpstreamServerConfig(prefix="bad", transport=TransportType.SSE, url=url)
        mgr = _make_manager(servers={"bad": cfg})

        with patch.object(mgr, "_connect_server", new_callable=AsyncMock):
            await mgr.start()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.initialize = AsyncMock()
        mock_session.list_tools = AsyncMock(side_effect=RuntimeError("catalog failed"))

        mock_transport = AsyncMock()
        mock_transport.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
        # The rollback close itself raises an httpx-style error embedding the URL.
        mock_transport.__aexit__ = AsyncMock(
            side_effect=ConnectionError(f"cleanup failed for {url}")
        )

        with caplog.at_level("DEBUG"):
            with (
                patch.object(mgr, "_open_transport", return_value=mock_transport),
                patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
            ):
                with _pt.raises(RuntimeError, match="catalog failed"):
                    await mgr._connect_server("bad", cfg)

        assert "Error during connection cleanup for 'bad'" in caplog.text
        assert "s3cr3t-token" not in caplog.text
        assert "alice:s3cr3t-token" not in caplog.text
        assert "***@ltm.example.com" in caplog.text


class TestConcurrentReconnect:
    """#586 — two concurrent _reconnect_server calls for one server must
    collapse into a single transport spawn; the loser skips (generation
    advanced) instead of building a second AsyncExitStack that gets orphaned."""

    async def test_concurrent_reconnect_spawns_one_transport(self):
        cfg = UpstreamServerConfig(prefix="srv", connect_timeout_seconds=5.0)
        mgr = _make_manager(servers={"srv": cfg})

        # Seed a live connection with an existing (closeable) stack.
        old_stack = AsyncMock()
        mgr._connections["srv"] = UpstreamConnection(
            name="srv",
            config=cfg,
            session=AsyncMock(),
            tools=[],
            stack=old_stack,
        )

        transport_opens = 0

        def _open_transport(_cfg):
            nonlocal transport_opens
            transport_opens += 1
            t = AsyncMock()

            async def _delayed_streams(*_args):
                # Yield control so the second reconnect reaches the lock while
                # the first is mid-setup — the interleaving the guard defends.
                await asyncio.sleep(0.01)
                return (AsyncMock(), AsyncMock())

            t.__aenter__ = AsyncMock(side_effect=_delayed_streams)
            t.__aexit__ = AsyncMock(return_value=False)
            return t

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.initialize = AsyncMock()
        mock_session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[]))

        with (
            patch.object(mgr, "_open_transport", side_effect=_open_transport),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
        ):
            await asyncio.gather(
                mgr._reconnect_server("srv"),
                mgr._reconnect_server("srv"),
            )

        # Exactly one reconnect actually ran: one transport spawn, one
        # generation bump, and the old stack closed once (not twice).
        assert transport_opens == 1
        assert mgr._connections["srv"].reconnect_generation == 1
        old_stack.aclose.assert_awaited_once()
        assert mgr._connections["srv"].session is mock_session


class TestOwnedUpstreamLifecycle:
    """MCP async contexts stay task-affine across connect/reconnect/stop."""

    async def test_contexts_enter_and_exit_in_each_connection_owner(self):
        from contextlib import AsyncExitStack

        cfg = UpstreamServerConfig(prefix="srv", connect_timeout_seconds=5.0)
        mgr = _make_manager(servers={"srv": cfg})
        mgr._stack = AsyncExitStack()
        records: list[dict] = []
        events: list[tuple[str, int]] = []

        class TrackingTransport:
            def __init__(self, record, connection_id):
                self.record = record
                self.connection_id = connection_id

            async def __aenter__(self):
                self.record["transport_enter_task"] = asyncio.current_task()
                events.append(("transport_enter", self.connection_id))
                return (object(), object())

            async def __aexit__(self, *_args):
                self.record["transport_exit_task"] = asyncio.current_task()
                events.append(("transport_exit", self.connection_id))

        class TrackingSession:
            def __init__(self, record, connection_id):
                self.record = record
                self.connection_id = connection_id

            async def __aenter__(self):
                self.record["session_enter_task"] = asyncio.current_task()
                events.append(("session_enter", self.connection_id))
                return self

            async def __aexit__(self, *_args):
                self.record["session_exit_task"] = asyncio.current_task()
                events.append(("session_exit", self.connection_id))

            async def initialize(self):
                events.append(("initialize", self.connection_id))

            async def list_tools(self):
                events.append(("list_tools", self.connection_id))
                return SimpleNamespace(tools=[])

        def open_transport(_cfg):
            record: dict = {}
            records.append(record)
            return TrackingTransport(record, len(records))

        def make_session(*_args, **_kwargs):
            return TrackingSession(records[-1], len(records))

        caller_task = asyncio.current_task()
        with (
            patch.object(mgr, "_open_transport", side_effect=open_transport),
            patch("memtomem_stm.proxy.manager.ClientSession", side_effect=make_session),
        ):
            await mgr._connect_server("srv", cfg)
            first_owner = mgr._connections["srv"].owner
            assert first_owner is not None
            assert records[0]["transport_enter_task"] is first_owner.task
            assert records[0]["session_enter_task"] is first_owner.task
            assert first_owner.task is not caller_task

            await mgr._reconnect_server("srv")
            second_owner = mgr._connections["srv"].owner
            assert second_owner is not None and second_owner is not first_owner

            # Prepare-first remains intact: the replacement discovers its
            # tools before the old owner begins unwinding.
            assert events.index(("list_tools", 2)) < events.index(("session_exit", 1))
            assert records[0]["session_exit_task"] is records[0]["session_enter_task"]
            assert records[0]["transport_exit_task"] is records[0]["transport_enter_task"]

            stop_task = asyncio.create_task(mgr.stop())
            await stop_task

        assert records[1]["session_exit_task"] is records[1]["session_enter_task"]
        assert records[1]["transport_exit_task"] is records[1]["transport_enter_task"]
        assert records[1]["transport_exit_task"] is not stop_task

    async def test_failed_setup_rolls_back_in_owner_task(self):
        from contextlib import AsyncExitStack

        cfg = UpstreamServerConfig(prefix="bad", connect_timeout_seconds=5.0)
        mgr = _make_manager(servers={"bad": cfg})
        mgr._stack = AsyncExitStack()
        record: dict = {}

        class TrackingTransport:
            async def __aenter__(self):
                record["transport_enter_task"] = asyncio.current_task()
                return (object(), object())

            async def __aexit__(self, *_args):
                record["transport_exit_task"] = asyncio.current_task()

        class FailingSession:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                record["session_enter_task"] = asyncio.current_task()
                return self

            async def __aexit__(self, *_args):
                record["session_exit_task"] = asyncio.current_task()

            async def initialize(self):
                raise ConnectionError("initialize failed")

        import pytest as _pt

        with (
            patch.object(mgr, "_open_transport", return_value=TrackingTransport()),
            patch("memtomem_stm.proxy.manager.ClientSession", FailingSession),
        ):
            with _pt.raises(ConnectionError, match="initialize failed"):
                await mgr._connect_server("bad", cfg)

        assert record["session_exit_task"] is record["session_enter_task"]
        assert record["transport_exit_task"] is record["transport_enter_task"]
        assert "bad" not in mgr._connections


# ── tool name overflow (#261 → exposure-time enforcement via #465) ──────


class TestConnectServerOverflowSkip:
    """When an upstream returns a tool whose composed name
    (`mcp__<server>__<prefix>__<tool>`) would exceed the 64-char MCP regex,
    only *that one tool* is withheld and the rest still register — one bad
    name shouldn't make every other tool from the same upstream invisible
    (#261). Since #465 the enforcement point is the exposure-time
    eligibility filter (reason ``name_overflow``, visible to telemetry):
    ``_connect_server`` keeps every discovered tool in ``conn.tools`` and
    only logs the prefix-shortening guidance.
    """

    async def _stub_session(self, tool_names: list[str]) -> AsyncMock:
        """Build a fake ClientSession returning the given tool names."""
        from mcp.types import Tool

        tools = [
            Tool(
                name=n,
                description=f"upstream tool {n}",
                input_schema={"type": "object", "properties": {}},
            )
            for n in tool_names
        ]
        list_result = AsyncMock()
        list_result.tools = tools

        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.initialize = AsyncMock()
        session.list_tools = AsyncMock(return_value=list_result)
        return session

    async def _stub_transport(self) -> AsyncMock:
        transport = AsyncMock()
        transport.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
        transport.__aexit__ = AsyncMock(return_value=False)
        return transport

    async def test_overflowing_tool_is_withheld_others_register(self, caplog, monkeypatch) -> None:
        """Mixed catalogue: a 40-char tool with the original ``docs_langchain``
        prefix (14 chars) overflows the 64-char limit, while a short tool from
        the same upstream fits. Expect: both tools kept in ``conn.tools``,
        guidance logged at connect, and the long tool withheld at exposure
        with a ``name_overflow`` reject on the normal startup path."""
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)

        cfg = UpstreamServerConfig(prefix="docs_langchain")
        mgr = _make_manager(servers={"docs": cfg})

        with patch.object(mgr, "_connect_server", new_callable=AsyncMock):
            await mgr.start()  # initialize internal _stack

        session = await self._stub_session(
            tool_names=[
                "search",  # composed = mcp__memtomem-stm__docs_langchain__search = 41, fits
                "query_docs_filesystem_docs_by_lang_chain",  # composed = 75, overflow
            ]
        )
        transport = await self._stub_transport()

        import logging

        with (
            patch.object(mgr, "_open_transport", return_value=transport),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=session),
            caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"),
        ):
            await mgr._connect_server("docs", cfg)

        # Discovery keeps the full catalogue; exclusion is the filter's job.
        discovered = [t.name for t in mgr._connections["docs"].tools]
        assert discovered == ["search", "query_docs_filesystem_docs_by_lang_chain"]
        # The first advertisement withholds the overflowing tool, with the
        # verdict recorded for selection telemetry (#465 / codex R2: the
        # structural reject must be observable on the NORMAL startup path).
        advertised = [info.prefixed_name for info in mgr.get_proxy_tools()]
        assert advertised == ["docs_langchain__search"]
        assert mgr._advertised_reject_reasons == {
            "docs_langchain__query_docs_filesystem_docs_by_lang_chain": "name_overflow"
        }
        # Warning identifies the overflowing tool by name + composed length.
        warning_text = caplog.text
        assert "query_docs_filesystem_docs_by_lang_chain" in warning_text
        assert "75" in warning_text  # composed length
        assert "64" in warning_text  # spec limit
        # Hint surfaces both fix paths user can take.
        assert "Shorten the" in warning_text  # → narrow the prefix
        assert "mms" in warning_text  # → shorter client server name alternative

    async def test_short_prefix_lets_long_tool_through(self, caplog, monkeypatch) -> None:
        """With the recommended ``lc`` prefix the same long tool fits
        (composed = 61 chars), so neither the guidance warning nor the
        exposure reject fires."""
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)

        cfg = UpstreamServerConfig(prefix="lc")
        mgr = _make_manager(servers={"docs": cfg})

        with patch.object(mgr, "_connect_server", new_callable=AsyncMock):
            await mgr.start()

        session = await self._stub_session(tool_names=["query_docs_filesystem_docs_by_lang_chain"])
        transport = await self._stub_transport()

        import logging

        with (
            patch.object(mgr, "_open_transport", return_value=transport),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=session),
            caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"),
        ):
            await mgr._connect_server("docs", cfg)

        advertised = [info.prefixed_name for info in mgr.get_proxy_tools()]
        assert advertised == ["lc__query_docs_filesystem_docs_by_lang_chain"]
        assert mgr._advertised_reject_reasons == {}
        # No overflow guidance in the log — no overflow happened.
        assert "will not be advertised" not in caplog.text


class TestCleanupLogCredentialRedaction:
    """#605 (follow-up to #580/#593): the connection-lifecycle cleanup and
    reconnect DEBUG logs close or reopen transports opened with the
    credentialed ``cfg.url``. httpx transport exceptions embed the request
    URL, so a close/reconnect failure routed through ``logger.debug(...,
    exc_info=True)`` would repeat the token in the traceback tail. Every such
    site must instead render the exception through ``_redacted_error`` with no
    ``exc_info`` — the same guarantee #593 gave the startup connect path.

    One regression per in-scope site: the two ``_reconnect_server`` closes, the
    ``stop()`` and double-start-guard connection-stack closes, and the three
    ``_fetch_upstream`` post-error reconnect logs.
    """

    URL = "https://alice:s3cr3t-token@ltm.example.com/mcp"

    def _assert_redacted(self, caplog, expect_msg: str) -> None:
        assert expect_msg in caplog.text
        assert "s3cr3t-token" not in caplog.text
        assert "alice:s3cr3t-token" not in caplog.text
        # The redacted rendering still identifies the host for operators.
        assert "***@ltm.example.com" in caplog.text

    def _cfg(self, **overrides) -> UpstreamServerConfig:
        return UpstreamServerConfig(
            prefix="bad", transport=TransportType.SSE, url=self.URL, **overrides
        )

    async def test_double_start_guard_conn_stack_close_redacts(self, caplog):
        """start() re-entry closes each live connection's stack; a close failure
        for a credentialed upstream must not leak the token."""
        from contextlib import AsyncExitStack

        cfg = self._cfg()
        mgr = _make_manager(servers={"bad": cfg})
        failing_stack = AsyncMock()
        failing_stack.aclose = AsyncMock(
            side_effect=ConnectionError(f"close failed for {self.URL}")
        )
        mgr._connections["bad"] = UpstreamConnection(
            name="bad", config=cfg, session=AsyncMock(), tools=[], stack=failing_stack
        )
        mgr._stack = AsyncExitStack()  # non-None → double-start branch runs

        with caplog.at_level("DEBUG"):
            with patch.object(mgr, "_connect_server", new_callable=AsyncMock):
                await mgr.start()

        self._assert_redacted(
            caplog, "Failed to close connection stack for 'bad' in double-start guard"
        )

    async def test_stop_conn_stack_close_redacts(self, caplog):
        """stop() closes every connection stack; a close failure for a
        credentialed upstream must not leak the token."""
        from contextlib import AsyncExitStack

        cfg = self._cfg()
        mgr = _make_manager(servers={"bad": cfg})
        failing_stack = AsyncMock()
        failing_stack.aclose = AsyncMock(
            side_effect=ConnectionError(f"close failed for {self.URL}")
        )
        mgr._connections["bad"] = UpstreamConnection(
            name="bad", config=cfg, session=AsyncMock(), tools=[], stack=failing_stack
        )
        mgr._stack = AsyncExitStack()

        with caplog.at_level("DEBUG"):
            await mgr.stop()

        self._assert_redacted(caplog, "Failed to close connection stack for 'bad'")

    async def test_reconnect_previous_stack_close_redacts(self, caplog):
        """_reconnect_server closes the previous stack before reopening; a close
        failure must not leak the credentialed URL."""
        cfg = self._cfg(connect_timeout_seconds=5.0)
        mgr = _make_manager(servers={"bad": cfg})
        failing_stack = AsyncMock()
        failing_stack.aclose = AsyncMock(
            side_effect=ConnectionError(f"prev close failed for {self.URL}")
        )
        mgr._connections["bad"] = UpstreamConnection(
            name="bad", config=cfg, session=AsyncMock(), tools=[], stack=failing_stack
        )

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.initialize = AsyncMock()
        mock_session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[]))
        mock_transport = AsyncMock()
        mock_transport.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
        mock_transport.__aexit__ = AsyncMock(return_value=False)

        with caplog.at_level("DEBUG"):
            with (
                patch.object(mgr, "_open_transport", return_value=mock_transport),
                patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
            ):
                await mgr._reconnect_server("bad")

        self._assert_redacted(caplog, "Failed to close previous stack for 'bad'")

    async def test_reconnect_rollback_cleanup_redacts(self, caplog):
        """When a reconnect's list_tools fails, the rollback aclose() of the new
        stack may itself raise for a credentialed transport; the cleanup log
        must not leak the token."""
        import pytest as _pt

        cfg = self._cfg(connect_timeout_seconds=5.0)
        mgr = _make_manager(servers={"bad": cfg})
        mgr._connections["bad"] = UpstreamConnection(
            name="bad", config=cfg, session=AsyncMock(), tools=[], stack=None
        )

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.initialize = AsyncMock()
        mock_session.list_tools = AsyncMock(side_effect=RuntimeError("catalog failed"))
        mock_transport = AsyncMock()
        mock_transport.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
        mock_transport.__aexit__ = AsyncMock(
            side_effect=ConnectionError(f"rollback failed for {self.URL}")
        )

        with caplog.at_level("DEBUG"):
            with (
                patch.object(mgr, "_open_transport", return_value=mock_transport),
                patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
            ):
                with _pt.raises(RuntimeError, match="catalog failed"):
                    await mgr._reconnect_server("bad")

        self._assert_redacted(caplog, "Error during connection cleanup for 'bad'")

    def _seed_fetch_conn(self, mgr, cfg):
        """A connection whose session.call_tool is a controllable AsyncMock."""
        session = AsyncMock()
        mgr._connections["bad"] = UpstreamConnection(
            name="bad", config=cfg, session=session, tools=[], stack=AsyncMock()
        )
        return session

    async def test_fetch_post_deadline_reconnect_redacts(self, caplog):
        """_fetch_upstream best-effort reconnect after the overall deadline is
        blown; a reconnect failure must not leak the credentialed URL."""
        import pytest as _pt

        cfg = self._cfg(overall_deadline_seconds=1.0, call_timeout_seconds=1.0)
        mgr = _make_manager(servers={"bad": cfg})
        self._seed_fetch_conn(mgr, cfg)

        with caplog.at_level("DEBUG"):
            with (
                # _call_started_at, then the in-loop deadline check reads a
                # monotonic far in the future → remaining_deadline <= 0.
                patch(
                    "memtomem_stm.proxy.manager._time.monotonic",
                    side_effect=[100.0, 200.0, 200.0, 200.0],
                ),
                patch.object(
                    mgr,
                    "_reconnect_server",
                    new_callable=AsyncMock,
                    side_effect=ConnectionError(f"reconnect boom for {self.URL}"),
                ),
            ):
                with _pt.raises(asyncio.TimeoutError):
                    await mgr._fetch_upstream("bad", "t", {"_trace_id": None}, trace_id=None)

        self._assert_redacted(caplog, "Post-deadline reconnect failed for 'bad'")

    async def test_fetch_post_protocol_error_reconnect_redacts(self, caplog):
        """_fetch_upstream reconnects after a no-retry protocol error; a
        reconnect failure must not leak the credentialed URL."""
        import pytest as _pt

        cfg = self._cfg()
        mgr = _make_manager(servers={"bad": cfg})
        session = self._seed_fetch_conn(mgr, cfg)

        class _ProtocolError(Exception):
            def __init__(self):
                super().__init__("protocol boom")
                self.error = SimpleNamespace(code=-32601)  # METHOD_NOT_FOUND

        session.call_tool = AsyncMock(side_effect=_ProtocolError())

        with caplog.at_level("DEBUG"):
            with patch.object(
                mgr,
                "_reconnect_server",
                new_callable=AsyncMock,
                side_effect=ConnectionError(f"reconnect boom for {self.URL}"),
            ):
                with _pt.raises(_ProtocolError):
                    await mgr._fetch_upstream("bad", "t", {"_trace_id": None}, trace_id=None)

        self._assert_redacted(caplog, "Post-protocol-error reconnect failed for 'bad'")

    async def test_fetch_post_failure_reconnect_redacts(self, caplog):
        """_fetch_upstream reconnects after exhausting retries on a transport
        error; a reconnect failure must not leak the credentialed URL."""
        import pytest as _pt

        cfg = self._cfg(max_retries=0)
        mgr = _make_manager(servers={"bad": cfg})
        session = self._seed_fetch_conn(mgr, cfg)
        # URL-free upstream error so the ONLY token-bearing string is the
        # reconnect failure we assert is redacted.
        session.call_tool = AsyncMock(side_effect=ConnectionError("upstream boom"))

        with caplog.at_level("DEBUG"):
            with patch.object(
                mgr,
                "_reconnect_server",
                new_callable=AsyncMock,
                side_effect=ConnectionError(f"reconnect boom for {self.URL}"),
            ):
                with _pt.raises(ConnectionError, match="upstream boom"):
                    await mgr._fetch_upstream("bad", "t", {"_trace_id": None}, trace_id=None)

        self._assert_redacted(caplog, "Post-failure reconnect failed for 'bad'")

    async def test_fetch_mid_loop_reconnect_failure_redacts(self, caplog):
        """#622: the mid-loop reconnect on the retry-continue path (a retryable
        transport error with attempts remaining) re-raises the reconnect error;
        its ERROR log must not leak the credentialed URL. Sibling to the three
        #605 post-error reconnect sites, which this sweep originally missed."""
        import pytest as _pt

        cfg = self._cfg(max_retries=1, reconnect_delay_seconds=0.0, max_reconnect_delay_seconds=0.0)
        mgr = _make_manager(servers={"bad": cfg})
        session = self._seed_fetch_conn(mgr, cfg)
        # URL-free upstream error so the ONLY token-bearing string is the
        # mid-loop reconnect failure we assert is redacted.
        session.call_tool = AsyncMock(side_effect=ConnectionError("upstream boom"))

        with caplog.at_level("DEBUG"):
            with patch.object(
                mgr,
                "_reconnect_server",
                new_callable=AsyncMock,
                side_effect=ConnectionError(f"reconnect boom for {self.URL}"),
            ):
                with _pt.raises(ConnectionError, match="reconnect boom"):
                    await mgr._fetch_upstream("bad", "t", {"_trace_id": None}, trace_id=None)

        self._assert_redacted(caplog, "Reconnect to 'bad' failed")


# ── _open_transport ──────────────────────────────────────────────────────


class TestOpenTransportHeaders:
    """Pins that the runtime transport passes configured HTTP headers and the
    connect budget to the SDK clients — the last leg of the headers-plumbing
    chain (CLI persist → probe → runtime). ``timeout=`` is the transport-socket
    leg of the timeout contract; ``sse_read_timeout`` must stay at the SDK
    default (long-lived streams don't inherit the connect budget), so the fake
    factories deliberately do NOT accept it."""

    def test_sse_passes_url_headers_and_timeout(self, monkeypatch):
        from memtomem_stm.proxy import manager as mod

        captured = {}
        sentinel = object()

        def fake_sse_client(url, *, headers=None, timeout=5):
            captured.update({"url": url, "headers": headers, "timeout": timeout})
            return sentinel

        monkeypatch.setattr(mod, "sse_client", fake_sse_client)

        cfg = UpstreamServerConfig(
            prefix="api",
            transport=TransportType.SSE,
            url="https://up.example/sse",
            headers={"Authorization": "Bearer t"},
            connect_timeout_seconds=7.5,
        )
        mgr = _make_manager(servers={"api": cfg})

        assert mgr._open_transport(cfg) is sentinel
        assert captured == {
            "url": "https://up.example/sse",
            "headers": {"Authorization": "Bearer t"},
            "timeout": 7.5,
        }

    def test_streamable_http_passes_url_headers_and_timeout(self, monkeypatch):
        from memtomem_stm.proxy import manager as mod

        captured = {}
        sentinel = object()

        def fake_streamable_http_transport(url, *, headers=None, timeout=None):
            captured.update({"url": url, "headers": headers, "timeout": timeout})
            return sentinel

        monkeypatch.setattr(mod, "streamable_http_transport", fake_streamable_http_transport)

        cfg = UpstreamServerConfig(
            prefix="api",
            transport=TransportType.STREAMABLE_HTTP,
            url="https://up.example/mcp",
            headers={"X-Project": "stm"},
            connect_timeout_seconds=12.0,
        )
        mgr = _make_manager(servers={"api": cfg})

        assert mgr._open_transport(cfg) is sentinel
        assert captured["url"] == "https://up.example/mcp"
        assert captured["headers"] == {"X-Project": "stm"}
        # mcp 2.0 carries the timeouts on an httpx2 client rather than as
        # ``timeout=`` / ``sse_read_timeout=`` kwargs. All four legs are pinned:
        # the connect budget applies to connect/write/pool, while the read leg
        # keeps the SDK's long default so a live stream is not killed by it.
        timeout = captured["timeout"]
        assert timeout.connect == 12.0
        assert timeout.write == 12.0
        assert timeout.pool == 12.0
        assert timeout.read == 300.0
