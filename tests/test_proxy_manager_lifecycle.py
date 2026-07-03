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


class TestConnectServerCleanup:
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
                inputSchema={"type": "object", "properties": {}},
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
