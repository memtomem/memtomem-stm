"""Tests for ProxyManager lifecycle — start, stop, double-start guard."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
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
    _RetiredConnectionResources,
)
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
        instead of a closed instance, whose gate would send every extract() to
        the local heuristic for the rest of the process (#867).

        The #890 cfg stamp is cleared with it: a stamp left behind describes an
        instance that no longer exists, and the rebuild predicate reads both."""
        mgr = _make_manager(servers={})
        mgr._extractor = AsyncMock()
        mgr._extractor_cfg = mgr._config.extraction

        await mgr.stop()

        assert mgr._extractor is None
        assert mgr._extractor_cfg is None

    async def test_stop_closes_retiring_extractors(self):
        """A rebuild whose own close never completed leaves its instance in
        ``_retiring_extractors``; stop() is the retry, and a success drops it."""
        mgr = _make_manager(servers={})
        retiring = AsyncMock()
        mgr._retiring_extractors.add(retiring)

        await mgr.stop()

        retiring.close.assert_awaited_once()
        assert mgr._retiring_extractors == set()

    async def test_stop_keeps_a_retiring_extractor_whose_close_fails(self):
        """A failed retry must NOT drop the entry.

        The set is the manager's last reference to that instance — clearing it
        wholesale strands an open transport with nothing left to retry it. The
        entry stays so the next rebuild or stop() tries again."""
        mgr = _make_manager(servers={})
        stubborn = AsyncMock()
        stubborn.close.side_effect = RuntimeError("transport wedged")
        mgr._retiring_extractors.add(stubborn)

        await mgr.stop()

        stubborn.close.assert_awaited_once()
        assert stubborn in mgr._retiring_extractors, "a failed close dropped the last reference"

    async def test_stop_detaches_under_the_lock_and_drains_outside_it(self):
        """Teardown must not hold ``_extractor_lock`` across an await.

        Every holder of this lock does synchronous work under it, which is what
        lets an ordinary timeout on it mean "stuck holder" rather than "someone
        is shutting down". Teardown keeps that property by transferring the
        installed instance into the retirement set — which is what gives it an
        owner — and closing it after releasing. Pinned by observing the lock
        from inside a slow close."""
        mgr = _make_manager(servers={})
        released = asyncio.Event()
        observed: list[bool] = []

        slow = AsyncMock()

        async def slow_close():
            observed.append(mgr._extractor_lock.locked())
            await released.wait()

        slow.close.side_effect = slow_close
        mgr._extractor = slow
        mgr._extractor_cfg = mgr._config.extraction

        stopping = asyncio.create_task(mgr.stop())
        for _ in range(200):
            if observed:
                break
            await asyncio.sleep(0.01)
        released.set()
        await asyncio.wait_for(stopping, timeout=10)

        assert observed == [False], f"teardown drained while holding the lock: {observed}"
        assert mgr._extractor is None
        assert mgr._extractor_cfg is None
        assert mgr._retiring_extractors == set()

    async def test_overlapping_retirement_passes_close_each_instance_once(self):
        """The rebuild path and ``stop()`` both drain the set, so two passes can
        overlap. Entries are claimed before the await, so neither awaits
        ``close()`` on an instance the other is already tearing down."""
        mgr = _make_manager(servers={})
        released = asyncio.Event()
        entered = asyncio.Event()

        slow = AsyncMock()

        async def slow_close():
            entered.set()
            await released.wait()

        slow.close.side_effect = slow_close
        mgr._retiring_extractors.add(slow)

        first = asyncio.create_task(mgr._close_retiring_extractors())
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(mgr._close_retiring_extractors())
        await asyncio.sleep(0.05)

        released.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=10)

        assert slow.close.await_count == 1, "the same instance was closed by both passes"
        assert mgr._retiring_extractors == set()

    async def test_a_cold_rebuild_after_stop_retries_a_failed_retirement(self):
        """The retry gate is publication, not displacement.

        After stop() the slot is deliberately None while the retirement set may
        still hold an instance whose close failed. The first post-stop lookup
        publishes into an empty slot — displacing nothing — so a gate written as
        "only when this call replaced something" skips cleanup, and every warm
        lookup after it skips too: the transport stays open for the whole
        restarted lifecycle. That entry is the one that has been waiting
        longest, so a cold publication is exactly when to retry it.
        """
        mgr = _make_manager(servers={})
        stubborn = AsyncMock()
        stubborn.close.side_effect = [RuntimeError("transport wedged"), None]
        mgr._extractor = stubborn
        mgr._extractor_cfg = mgr._config.extraction

        await mgr.stop()
        assert stubborn in mgr._retiring_extractors, "the failed close lost its owner"
        assert mgr._extractor is None

        # Cold rebuild: nothing to displace, but something to retry.
        rebuilt = await mgr._get_extractor(cfg_snap=mgr._config)
        try:
            assert rebuilt is not stubborn
            assert stubborn.close.await_count == 2, "the cold rebuild skipped the retry"
            assert mgr._retiring_extractors == set()
        finally:
            await rebuilt.close()

    async def test_a_cancelled_retirement_pass_releases_its_claim(self):
        """Cancellation must not leave an entry claimed forever.

        ``CancelledError`` is a BaseException, so it unwinds past an
        ``except Exception`` — and every other pass skips an entry that is
        marked in flight. Releasing the claim only on the non-cancelled paths
        would therefore strand the instance permanently: still owned, still
        open, and never again eligible for a close. Hence the ``finally``."""
        mgr = _make_manager(servers={})
        entered = asyncio.Event()

        blocked = AsyncMock()

        async def never_finishes():
            entered.set()
            await asyncio.Event().wait()

        blocked.close.side_effect = never_finishes
        mgr._retiring_extractors.add(blocked)

        pass_task = asyncio.create_task(mgr._close_retiring_extractors())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert blocked in mgr._retiring_inflight, "the entry was not claimed"
            assert blocked in mgr._retiring_extractors, "a claim removed the entry from the set"
        finally:
            # In a finally: the close above never returns, so an assertion
            # failure would otherwise leave this task pending into loop teardown.
            pass_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pass_task

        assert blocked in mgr._retiring_extractors, "cancellation consumed the claimed entry"
        assert mgr._retiring_inflight == set(), "the cancelled claim was never released"

    async def test_a_claimed_retirement_survives_stop_in_the_set(self):
        """A claim must never make its instance invisible to a teardown pass.

        Claiming by REMOVAL — the shape this replaced — meant a ``stop()``
        landing while a rebuild's close was in flight saw nothing to drain and
        walked past the instance. The late hand-back then had no retry point
        left: the first post-stop lookup publishes into an empty slot while the
        set still reads empty, and warm lookups never publish at all, so the
        transport stayed open for the whole restarted lifecycle (#904).

        The entry now stays in the set for the length of the close, and the
        skip records the attempt teardown could not make, so the failure is
        collected by the pass that produced it rather than by nothing."""
        mgr = _make_manager(servers={})
        entered = asyncio.Event()
        released = asyncio.Event()

        stubborn = AsyncMock()
        attempts = 0

        async def fails_then_succeeds():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                await released.wait()
                raise RuntimeError("transport wedged")

        stubborn.close.side_effect = fails_then_succeeds
        mgr._retiring_extractors.add(stubborn)

        pass_task = asyncio.create_task(mgr._close_retiring_extractors())
        await asyncio.wait_for(entered.wait(), timeout=5)
        try:
            # stop() must not wait on another task's close — that is the
            # coupling detaching-then-draining exists to avoid — so what has to
            # hold is that it cannot LOSE the instance either.
            await asyncio.wait_for(mgr.stop(), timeout=10)
            assert stubborn in mgr._retiring_extractors, (
                "stop() walked past a claimed entry: it is in neither the set "
                "nor the slot, so nothing will retry its close"
            )
            assert stubborn.close.await_count == 1, "stop() raced the in-flight close"
        finally:
            released.set()
            await asyncio.wait_for(pass_task, timeout=10)

        # stop() was the trigger here, and it spent itself on a skip. The
        # request it left behind is honoured by the pass that releases the
        # claim, so no post-stop lookup is needed at all.
        assert stubborn.close.await_count == 2, "the skipped attempt was not carried forward"
        assert mgr._retiring_extractors == set(), "the retried close did not drop its entry"
        assert mgr._retiring_inflight == set()
        assert mgr._retiring_retry_requested == set()

    async def test_a_trigger_spent_on_a_claimed_entry_is_carried_to_its_failure(self):
        """The exact #904 ordering: the retry trigger arrives DURING the claim.

        A drain runs on a trigger — a publication, or ``stop()`` — and the
        trigger is spent whether or not an attempt was possible, since a pass
        that finds the entry claimed can only skip. So the claim's failure, when
        it comes, lands after the trigger that would have retried it, and warm
        lookups never publish again. Keeping the entry visible is not enough on
        its own: the skip has to carry the attempt forward, and the pass that
        releases the claim without closing has to make it.

        Ordered deliberately: publish while the close is still in flight, and
        make no further publication afterwards."""
        mgr = _make_manager(servers={})
        entered = asyncio.Event()
        released = asyncio.Event()

        stubborn = AsyncMock()
        attempts = 0

        async def fails_then_succeeds():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                await released.wait()
                raise RuntimeError("transport wedged")

        stubborn.close.side_effect = fails_then_succeeds
        mgr._retiring_extractors.add(stubborn)

        pass_task = asyncio.create_task(mgr._close_retiring_extractors())
        await asyncio.wait_for(entered.wait(), timeout=5)

        rebuilt = None
        try:
            # The trigger. Bounded: an implementation that made this wait for
            # the in-flight claim would hang the suite rather than fail.
            rebuilt = await asyncio.wait_for(mgr._get_extractor(cfg_snap=mgr._config), timeout=10)
            assert stubborn in mgr._retiring_extractors, (
                "the publication saw an empty set while the instance was still owned"
            )
            assert stubborn.close.await_count == 1, (
                "the gate closed an instance another pass was already closing"
            )
        finally:
            # Unconditional, so a timeout or a failed assertion above cannot
            # leave ``pass_task`` blocked on ``released`` into loop teardown.
            released.set()
            await asyncio.wait_for(pass_task, timeout=10)
            if rebuilt is not None:
                await asyncio.wait_for(rebuilt.close(), timeout=10)

        assert stubborn.close.await_count == 2, (
            "the failure landed after the only trigger it had, and nothing "
            "carried that attempt forward — the transport stays open"
        )
        assert mgr._retiring_extractors == set(), "the retried close did not drop its entry"
        assert mgr._retiring_inflight == set()
        assert mgr._retiring_retry_requested == set(), "the honoured request was not cleared"

    async def test_a_pass_rechecks_membership_before_closing(self):
        """A pass iterates a snapshot; the set moves under it.

        While the pass awaits one entry, another pass can close a later
        candidate and drop it. Closing it again from the stale snapshot would
        break the protocol's single-close rule — and the concrete extractor
        tolerating a second call is not the guarantee, the recheck is. The
        request recorded for that entry has to go with it, or the registry
        keeps a closed instance referenced."""
        mgr = _make_manager(servers={})
        entered = asyncio.Event()
        released = asyncio.Event()
        parked: list[AsyncMock] = []

        def make_blocking() -> AsyncMock:
            instance = AsyncMock()

            async def close():
                parked.append(instance)
                entered.set()
                await released.wait()

            instance.close.side_effect = close
            return instance

        # Two entries in one snapshot. Which one the pass reaches first depends
        # on set iteration order, so the test drops whichever it did NOT park
        # on rather than assuming an order.
        pair = [make_blocking(), make_blocking()]
        mgr._retiring_extractors.update(pair)
        mgr._retiring_retry_requested.update(pair)

        pass_task = asyncio.create_task(mgr._close_retiring_extractors())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            dropped = next(instance for instance in pair if instance not in parked)
            # Someone else closes it and drops it while the pass is parked on
            # the other one, leaving the pass holding a stale snapshot.
            mgr._retiring_extractors.discard(dropped)
        finally:
            released.set()
            await asyncio.wait_for(pass_task, timeout=10)

        assert dropped.close.await_count == 0, (
            "the pass closed an entry that had already been dropped from the set"
        )
        assert mgr._retiring_retry_requested == set(), "a stale request outlived its entry"

    async def _carried_retry_fixture(self, mgr, owed_close, extra=()):
        """Seed a pass that reaches the catch-up loop with owed candidates.

        The catch-up exists for entries that become honourable only after the
        main loop's snapshot was taken, so the blocker below produces them from
        inside its own close — standing in for the overlapping passes that
        would record and release them in production."""
        owed = AsyncMock()
        owed.close.side_effect = owed_close
        owed_entries = [owed, *extra]

        blocker = AsyncMock()
        entered = asyncio.Event()
        released = asyncio.Event()

        async def blocker_close():
            entered.set()
            await released.wait()
            # The overlapping passes settle here — after this pass took its main
            # loop's snapshot, so these reach it only through the catch-up.
            for instance in owed_entries:
                mgr._retiring_extractors.add(instance)
                mgr._retiring_retry_requested.add(instance)

        blocker.close.side_effect = blocker_close
        mgr._retiring_extractors.add(blocker)
        return owed, entered, released

    async def test_a_carried_retry_is_skipped_once_its_request_is_spent(self):
        """The catch-up list is built before the awaits it then performs.

        While this pass is parked on one candidate, another pass can spend a
        later candidate's request on an attempt of its own. If that attempt
        fails, the entry is still in the set and still unclaimed — so rechecking
        only those two would close it again for a request that no longer exists.
        That is an over-retry, and it lands on the shutdown path when the pass
        holding the stale list is ``stop()``."""
        other = await self._stale_catch_up_candidate(
            lambda mgr, entry: mgr._retiring_retry_requested.discard(entry)
        )
        assert other.close.await_count == 0, (
            "a stale catch-up candidate was retried for a request that had already been spent"
        )

    async def test_a_carried_retry_is_skipped_once_another_pass_claims_it(self):
        """The same stale list, with the candidate CLAIMED rather than spent.

        A third pass can re-record the request while a second one claims the
        entry, so the request and set checks both pass and only the in-flight
        recheck stands between the stale list and two tasks closing one
        instance at the same time."""
        other = await self._stale_catch_up_candidate(
            lambda mgr, entry: mgr._retiring_inflight.add(entry)
        )
        assert other.close.await_count == 0, (
            "a stale catch-up candidate was closed while another pass held its claim"
        )

    async def _stale_catch_up_candidate(self, make_stale):
        """Park a pass inside its catch-up, then stale the candidate it has not
        reached yet with ``make_stale``. Returns that candidate."""
        mgr = _make_manager(servers={})
        entered_retry = asyncio.Event()
        release_retry = asyncio.Event()

        async def owed_close():
            entered_retry.set()
            await release_retry.wait()
            raise RuntimeError("transport wedged")

        # Two catch-up candidates whose closes are interchangeable, so the test
        # can act on whichever one the pass does NOT reach first.
        second = AsyncMock()
        second.close.side_effect = owed_close
        owed, entered, released = await self._carried_retry_fixture(
            mgr, owed_close, extra=(second,)
        )
        pair = [owed, second]

        pass_task = asyncio.create_task(mgr._close_retiring_extractors())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            released.set()
            # The pass is now in its catch-up, parked on one of the pair.
            await asyncio.wait_for(entered_retry.wait(), timeout=5)
            parked_on = next(x for x in pair if x.close.await_count)
            other = next(x for x in pair if x is not parked_on)
            make_stale(mgr, other)
        finally:
            release_retry.set()
            released.set()
            await asyncio.wait_for(pass_task, timeout=10)

        assert other in mgr._retiring_extractors, "the skipped candidate lost its owner"
        return other

    async def test_a_cancelled_carried_retry_keeps_the_attempt_owed(self):
        """Cancellation must not consume the request it was honouring.

        The request is taken before the retry's ``close()`` is awaited, so a
        cancellation there would otherwise leave the entry in the set with no
        record that an attempt is still owed to it — the next trigger would
        treat it as an ordinary entry rather than one whose trigger was already
        spent on a skip."""
        mgr = _make_manager(servers={})

        async def owed_close():
            await asyncio.Event().wait()

        owed, entered, released = await self._carried_retry_fixture(mgr, owed_close)

        pass_task = asyncio.create_task(mgr._close_retiring_extractors())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            released.set()
            # Let the pass finish the blocker and park inside the carried retry,
            # probed by the close actually starting.
            for _ in range(200):
                if owed.close.await_count:
                    break
                await asyncio.sleep(0.01)
            assert owed.close.await_count == 1, "the pass never reached the carried retry"
        finally:
            released.set()
            pass_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pass_task

        assert owed in mgr._retiring_extractors, "the cancelled retry lost its owner"
        assert mgr._retiring_inflight == set(), "the cancelled retry kept its claim"
        assert owed in mgr._retiring_retry_requested, (
            "the cancelled retry consumed the request it could not honour"
        )

    async def test_a_cancelled_claim_leaves_its_request_to_the_next_trigger(self):
        """The documented residual, pinned so it cannot change silently.

        The pass holding a claim is normally the one that honours the requests
        left by passes it made skip. A cancelled task cannot: it unwinds from
        the close. The entry therefore keeps its owner and its outstanding
        request, and waits for the next trigger — the same footing as any close
        that failed with no request pending, which is the baseline this design
        restores rather than exceeds."""
        mgr = _make_manager(servers={})
        entered = asyncio.Event()

        blocked = AsyncMock()

        async def never_finishes():
            entered.set()
            await asyncio.Event().wait()

        blocked.close.side_effect = never_finishes
        mgr._retiring_extractors.add(blocked)

        claim_task = asyncio.create_task(mgr._close_retiring_extractors())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            # A trigger that can only skip, and so records the attempt it owes.
            await asyncio.wait_for(mgr._close_retiring_extractors(), timeout=10)
            assert blocked in mgr._retiring_retry_requested
        finally:
            claim_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await claim_task

        assert blocked in mgr._retiring_extractors, "the cancelled claim lost its owner"
        assert mgr._retiring_inflight == set(), "the cancelled claim was never released"
        assert blocked in mgr._retiring_retry_requested, (
            "the attempt owed to this entry was dropped, leaving the next "
            "trigger unaware that one is outstanding"
        )

        # The next trigger honours it, without the entry having been touched in
        # between: nothing about the residual is lost, only deferred.
        blocked.close.side_effect = None
        await asyncio.wait_for(mgr._close_retiring_extractors(), timeout=10)
        assert mgr._retiring_extractors == set()
        assert mgr._retiring_retry_requested == set()

    async def test_stop_does_not_wait_on_a_wedged_claimed_close(self):
        """A close that never returns must not hold shutdown open.

        ``stop()`` skips what another pass has claimed instead of waiting on it:
        waiting would put an unbounded close belonging to someone else on the
        shutdown path. The instance keeps its owner and stays in the set, and
        its claim — along with the request this skip records — stays live until
        that close resolves or the task holding it is cancelled."""
        mgr = _make_manager(servers={})
        entered = asyncio.Event()

        wedged = AsyncMock()

        async def never_finishes():
            entered.set()
            await asyncio.Event().wait()

        wedged.close.side_effect = never_finishes
        mgr._retiring_extractors.add(wedged)

        pass_task = asyncio.create_task(mgr._close_retiring_extractors())
        await asyncio.wait_for(entered.wait(), timeout=5)

        stop_task = asyncio.create_task(mgr.stop())
        try:
            done, _ = await asyncio.wait({stop_task}, timeout=10.0)
            assert done, "stop() waited on a close claimed by another pass"
            await stop_task
            assert wedged in mgr._retiring_extractors, "the wedged claim lost its owner"
        finally:
            for task in (stop_task, pass_task):
                if not task.done():
                    task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(task, timeout=5.0)

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


class TestConnectionGenerationLeases:
    async def test_reconnect_retires_old_session_after_last_call_releases(self):
        cfg = UpstreamServerConfig(prefix="docs")
        mgr = _make_manager(servers={"docs": cfg})
        old_session = AsyncMock()
        old_stack = AsyncMock()
        conn = UpstreamConnection(
            name="docs",
            config=cfg,
            session=old_session,
            tools=[],
            stack=old_stack,
        )
        mgr._connections["docs"] = conn
        leased_session, generation = mgr._acquire_connection_session(conn)
        new_session = AsyncMock()

        with patch.object(
            mgr,
            "_establish_connection",
            new_callable=AsyncMock,
            return_value=(new_session, None, []),
        ):
            await mgr._reconnect_server("docs", cfg)

        assert leased_session is old_session
        assert conn.session is new_session
        old_stack.aclose.assert_not_awaited()
        assert generation in conn.retired_resources

        mgr._release_connection_session(conn, generation)
        await asyncio.gather(*mgr._background_tasks)

        old_stack.aclose.assert_awaited_once()
        assert generation not in conn.retired_resources

    async def test_stop_closes_retired_generation_whose_retire_task_never_started(self):
        """#952: a retire task cancelled before its first step never discards its
        ``retiring_generations`` marker, and stop() used to skip exactly those
        generations — leaving the retired transport open."""
        cfg = UpstreamServerConfig(prefix="docs")
        mgr = _make_manager(servers={"docs": cfg})
        old_stack = AsyncMock()
        conn = UpstreamConnection(name="docs", config=cfg, session=AsyncMock(), tools=[])
        mgr._connections["docs"] = conn
        # A reconnect retired generation 0 while one call still held a lease.
        conn.retired_resources[0] = _RetiredConnectionResources(
            owner=None, stack=old_stack, config=cfg
        )
        conn.active_calls[0] = 1
        conn.reconnect_generation = 1

        # Releasing the last lease registers the close task…
        mgr._release_connection_session(conn, 0)
        assert set(conn.retiring_tasks) == {0}
        # …and stop() runs before the loop ever steps that task (no await in
        # between), so its drain loop cancels it before the coroutine body runs.
        await mgr.stop()

        old_stack.aclose.assert_awaited_once()
        assert conn.retired_resources == {}

    async def test_stop_closes_retired_generation_despite_an_unrelated_straggler(self):
        """#952: the decision is per generation. An unrelated background task
        that outlives the bounded drain must not keep a cancelled retire task's
        entry standing — that entry is what makes stop() skip the generation,
        and ``_connections`` is cleared right after."""
        cfg = UpstreamServerConfig(prefix="docs")
        mgr = _make_manager(servers={"docs": cfg})
        old_stack = AsyncMock()
        conn = UpstreamConnection(name="docs", config=cfg, session=AsyncMock(), tools=[])
        mgr._connections["docs"] = conn
        conn.retired_resources[0] = _RetiredConnectionResources(
            owner=None, stack=old_stack, config=cfg
        )
        conn.active_calls[0] = 1
        conn.reconnect_generation = 1

        # A background task that swallows cancellation until released, so the
        # drain loop cannot empty ``_background_tasks`` within its budget.
        release = asyncio.Event()

        async def _straggler() -> None:
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    if release.is_set():
                        raise

        straggler = asyncio.create_task(_straggler())
        mgr._background_tasks.add(straggler)
        await asyncio.sleep(0)  # let it reach the sleep, so it can swallow
        try:
            mgr._release_connection_session(conn, 0)
            with patch("memtomem_stm.proxy.manager.BACKGROUND_DRAIN_BUDGET_SECONDS", 0.05):
                await mgr.stop()
            assert not straggler.done()  # it really did outlive the drain
        finally:
            release.set()
            straggler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await straggler

        old_stack.aclose.assert_awaited_once()
        assert conn.retired_resources == {}

    async def test_retired_generation_error_keeps_old_credentials_client_safe(self):
        old_url_token = "old-url-token"
        old_header_token = "old-header-token"
        old_env_token = "old-env-token"
        old_cfg = UpstreamServerConfig(
            prefix="docs",
            transport=TransportType.SSE,
            url=f"https://alice:{old_url_token}@old.example.test/mcp",
            headers={"Authorization": f"Bearer {old_header_token}"},
            env={"UPSTREAM_TOKEN": old_env_token},
            max_retries=0,
        )
        mgr = _make_manager(servers={"docs": old_cfg})
        old_session = AsyncMock()
        entered = asyncio.Event()
        fail = asyncio.Event()

        async def old_call(*_args, **_kwargs):
            entered.set()
            await fail.wait()
            raise ConnectionError(
                f"failed at {old_cfg.url}; auth=Bearer {old_header_token}; env={old_env_token}"
            )

        old_session.call_tool.side_effect = old_call
        old_stack = AsyncMock()
        conn = UpstreamConnection(
            name="docs", config=old_cfg, session=old_session, tools=[], stack=old_stack
        )
        mgr._connections["docs"] = conn

        call = asyncio.create_task(mgr.call_tool("docs", "read", {}))
        await entered.wait()

        new_cfg = old_cfg.model_copy(
            update={
                "url": "https://bob:new-url-token@new.example.test/mcp",
                "headers": {"Authorization": "Bearer new-header-token"},
                "env": {"UPSTREAM_TOKEN": "new-env-token"},
            }
        )
        current = mgr._config.model_copy(update={"upstream_servers": {"docs": new_cfg}})
        mgr._config_loader.seed(current)
        with patch.object(
            mgr,
            "_establish_connection",
            new_callable=AsyncMock,
            return_value=(AsyncMock(), None, []),
        ):
            await mgr._reconnect_server("docs", new_cfg)
        assert 0 in conn.retired_resources

        fail.set()
        with pytest.raises(ConnectionError) as raised:
            await call

        rendered = mgr.safe_upstream_error("docs", raised.value)
        for secret in (old_url_token, old_header_token, old_env_token):
            assert secret not in rendered
        assert "***@old.example.test" in rendered
        await asyncio.gather(*list(mgr._background_tasks), return_exceptions=True)

    async def test_next_dispatch_waits_for_failed_generation_recovery(self):
        cfg = UpstreamServerConfig(prefix="docs", max_retries=0)
        mgr = _make_manager(servers={"docs": cfg})
        old_session = AsyncMock()
        old_session.call_tool.side_effect = ConnectionError("broken session")
        new_session = AsyncMock()
        new_session.call_tool.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="fresh")], is_error=False
        )
        conn = UpstreamConnection(name="docs", config=cfg, session=old_session, tools=[])
        mgr._connections["docs"] = conn
        publish = asyncio.Event()

        async def reconnect(_name, _cfg=None):
            await publish.wait()
            conn.session = new_session
            conn.reconnect_generation += 1

        with patch.object(mgr, "_reconnect_server", side_effect=reconnect):
            with pytest.raises(ConnectionError, match="broken session"):
                await mgr.call_tool("docs", "read", {})

            second = asyncio.create_task(mgr.call_tool("docs", "read", {}))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not second.done()
            assert old_session.call_tool.await_count == 1
            assert new_session.call_tool.await_count == 0

            publish.set()
            assert await second == "fresh"

        assert new_session.call_tool.await_count == 1
        await asyncio.gather(*list(mgr._background_tasks), return_exceptions=True)

    async def test_concurrent_terminal_failures_share_one_recovery_task(self):
        cfg = UpstreamServerConfig(prefix="docs", max_retries=0, circuit_max_failures=0)
        mgr = _make_manager(servers={"docs": cfg})
        session = AsyncMock()
        caller_count = 12
        entered = 0
        all_entered = asyncio.Event()
        fail_calls = asyncio.Event()

        async def failed_call(*_args, **_kwargs):
            nonlocal entered
            entered += 1
            if entered == caller_count:
                all_entered.set()
            await fail_calls.wait()
            raise ConnectionError("shared outage")

        session.call_tool.side_effect = failed_call
        conn = UpstreamConnection(name="docs", config=cfg, session=session, tools=[])
        mgr._connections["docs"] = conn
        recovery_entered = asyncio.Event()
        release_recovery = asyncio.Event()

        async def failed_reconnect(*_args, **_kwargs):
            recovery_entered.set()
            await release_recovery.wait()
            raise ConnectionError("still unavailable")

        with patch.object(mgr, "_reconnect_server", side_effect=failed_reconnect) as reconnect:
            calls = [
                asyncio.create_task(mgr.call_tool("docs", "read", {"caller": index}))
                for index in range(caller_count)
            ]
            await all_entered.wait()
            fail_calls.set()
            results = await asyncio.gather(*calls, return_exceptions=True)
            await recovery_entered.wait()

            assert all(isinstance(result, ConnectionError) for result in results)
            assert session.call_tool.await_count == caller_count
            assert reconnect.await_count == 1
            recovery = conn.recovery_task
            assert recovery is not None

            release_recovery.set()
            assert await recovery is False
            reconnect.assert_awaited_once_with("docs", cfg)


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
        """A failed RPC generation is recovered after its overall deadline;
        a reconnect failure must not leak the credentialed URL."""
        import pytest as _pt

        cfg = self._cfg(
            overall_deadline_seconds=0.05,
            call_timeout_seconds=0.05,
            reconnect_delay_seconds=0.1,
        )
        mgr = _make_manager(servers={"bad": cfg})
        session = self._seed_fetch_conn(mgr, cfg)
        session.call_tool.side_effect = ConnectionError("transport reset")
        mgr._connections["bad"].tools = [
            SimpleNamespace(
                name="t",
                annotations=SimpleNamespace(read_only_hint=True, destructive_hint=False),
            )
        ]

        with caplog.at_level("DEBUG"):
            with patch.object(
                mgr,
                "_reconnect_server",
                new_callable=AsyncMock,
                side_effect=ConnectionError(f"reconnect boom for {self.URL}"),
            ):
                with _pt.raises(asyncio.TimeoutError):
                    await mgr._fetch_upstream(
                        "bad",
                        "t",
                        {"_trace_id": None},
                        trace_id=None,
                        cfg_snap=mgr._config,
                    )
                await asyncio.gather(*mgr._background_tasks)

        self._assert_redacted(
            caplog, "Background reconnect after overall deadline failed for 'bad'"
        )

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
                    await mgr._fetch_upstream(
                        "bad", "t", {"_trace_id": None}, trace_id=None, cfg_snap=mgr._config
                    )
                await asyncio.gather(*mgr._background_tasks)

        self._assert_redacted(caplog, "Background reconnect after protocol error failed for 'bad'")

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
                    await mgr._fetch_upstream(
                        "bad", "t", {"_trace_id": None}, trace_id=None, cfg_snap=mgr._config
                    )
                await asyncio.gather(*mgr._background_tasks)

        self._assert_redacted(
            caplog, "Background reconnect after terminal call failure failed for 'bad'"
        )

    async def test_fetch_mid_loop_reconnect_failure_redacts(self, caplog):
        """#622: the mid-loop reconnect on the retry-continue path (a retryable
        transport error with attempts remaining) re-raises the reconnect error;
        its ERROR log must not leak the credentialed URL. Sibling to the three
        #605 post-error reconnect sites, which this sweep originally missed."""
        import pytest as _pt

        cfg = self._cfg(
            max_retries=1,
            reconnect_delay_seconds=0.0,
            max_reconnect_delay_seconds=0.0,
            # An explicit cache allowlist is affirmative evidence that this
            # tool is safe to replay after an ambiguous transport failure.
            cache=True,
        )
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
                    await mgr._fetch_upstream(
                        "bad", "t", {"_trace_id": None}, trace_id=None, cfg_snap=mgr._config
                    )

        self._assert_redacted(caplog, "Reconnect to 'bad' failed")

    async def test_retry_warning_redacts_header_and_env_values(self, caplog):
        import pytest as _pt

        header_token = "header-secret-token"
        env_token = "env-secret-token"
        cfg = UpstreamServerConfig(
            prefix="bad",
            transport=TransportType.SSE,
            url=self.URL,
            headers={"Authorization": f"Bearer {header_token}"},
            env={"UPSTREAM_TOKEN": env_token},
            cache=True,
            max_retries=1,
            reconnect_delay_seconds=0.0,
            max_reconnect_delay_seconds=0.0,
        )
        mgr = _make_manager(servers={"bad": cfg})
        session = self._seed_fetch_conn(mgr, cfg)
        session.call_tool = AsyncMock(
            side_effect=ConnectionError(
                f"failed for {self.URL}; auth=Bearer {header_token}; env={env_token}"
            )
        )

        with caplog.at_level("WARNING"):
            with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
                with _pt.raises(ConnectionError):
                    await mgr._fetch_upstream("bad", "t", {}, trace_id=None, cfg_snap=mgr._config)
                await asyncio.gather(*mgr._background_tasks)

        assert "Tool call bad/t failed" in caplog.text
        for secret in ("s3cr3t-token", header_token, env_token):
            assert secret not in caplog.text
        assert "***@ltm.example.com" in caplog.text


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


class TestBackgroundTaskBounds:
    """#868: the fire-and-forget set must be capped, and stop() must not leak.

    Background index/extract tasks were added to an unbounded set, so under
    sustained load with ``background=True`` the task count tracked the request
    rate. ``stop()``'s drain loop then gave up after 8 passes, logged "leaking
    them", and cleared the set — abandoning tasks that go on running against
    torn-down resources.
    """

    async def _blocker(self, gate: asyncio.Event) -> None:
        await gate.wait()

    def _fill_to_cap(self, mgr, gate: asyncio.Event) -> list:
        from memtomem_stm.proxy.manager import MAX_BACKGROUND_TASKS

        return [
            mgr._spawn_background(self._blocker(gate), stage="extract", server="s", tool="t")
            for _ in range(MAX_BACKGROUND_TASKS)
        ]

    async def test_spawn_sheds_once_the_cap_is_reached(self, tmp_path, caplog):
        from memtomem_stm.proxy.manager import MAX_BACKGROUND_TASKS

        mgr = _make_manager(tmp_path=tmp_path)
        gate = asyncio.Event()
        try:
            spawned = self._fill_to_cap(mgr, gate)
            assert all(t is not None for t in spawned)
            assert len(mgr._background_tasks) == MAX_BACKGROUND_TASKS

            with caplog.at_level("WARNING", logger="memtomem_stm.proxy.manager"):
                overflow = [
                    mgr._spawn_background(
                        self._blocker(gate), stage="extract", server="s", tool="t"
                    )
                    for _ in range(3)
                ]
            assert overflow == [None, None, None], "spawn past the cap must shed"
            assert len(mgr._background_tasks) == MAX_BACKGROUND_TASKS
            assert mgr._background_shed_total == 3, "every shed must be counted"
            # Warn-ONCE: three sheds, one line, so a sustained overload cannot
            # flood the log.
            shed_warnings = [r for r in caplog.records if "shed" in r.getMessage()]
            assert len(shed_warnings) == 1, [r.getMessage() for r in shed_warnings]
        finally:
            gate.set()
            await asyncio.gather(*mgr._background_tasks, return_exceptions=True)

    async def test_reconnect_bypasses_saturated_enrichment_cap(self, tmp_path):
        from memtomem_stm.proxy.manager import MAX_BACKGROUND_TASKS

        cfg = UpstreamServerConfig(prefix="s")
        mgr = _make_manager(servers={"s": cfg}, tmp_path=tmp_path)
        mgr._connections["s"] = UpstreamConnection(
            name="s", config=cfg, session=AsyncMock(), tools=[]
        )
        gate = asyncio.Event()
        recovery_entered = asyncio.Event()
        recovery_gate = asyncio.Event()
        try:
            self._fill_to_cap(mgr, gate)
            assert len(mgr._background_tasks) == MAX_BACKGROUND_TASKS

            async def reconnect_server(name, reconnect_cfg):
                recovery_entered.set()
                await recovery_gate.wait()
                raise ConnectionError("still down")

            with patch.object(mgr, "_reconnect_server", side_effect=reconnect_server) as reconnect:
                recovery = mgr._schedule_reconnect_for_next_call("s", cfg, "terminal failure")
                assert recovery is not None
                assert len(mgr._background_tasks) == MAX_BACKGROUND_TASKS + 1
                duplicates = [
                    mgr._schedule_reconnect_for_next_call("s", cfg, "terminal failure")
                    for _ in range(100)
                ]
                assert all(task is recovery for task in duplicates)
                assert len(mgr._background_tasks) == MAX_BACKGROUND_TASKS + 1
                await recovery_entered.wait()
                recovery_gate.set()
                assert await recovery is False
                reconnect.assert_awaited_once_with("s", cfg)
        finally:
            gate.set()
            await asyncio.gather(*list(mgr._background_tasks), return_exceptions=True)

    async def test_shed_warning_re_arms_after_pressure_clears(self, tmp_path, caplog):
        # A one-shot latch would report the first burst and stay silent for
        # every later one. The latch must re-arm once the set drains, and the
        # SECOND burst must produce its own warning.
        mgr = _make_manager(tmp_path=tmp_path)
        first_gate = asyncio.Event()
        try:
            self._fill_to_cap(mgr, first_gate)
            mgr._spawn_background(self._blocker(first_gate), stage="extract", server="s", tool="t")
            assert mgr._background_shed_warned is True
        finally:
            # Release even if the assert fails, so the blockers do not leak
            # into the loop teardown as pending tasks.
            first_gate.set()
        await asyncio.gather(*list(mgr._background_tasks), return_exceptions=True)
        await asyncio.sleep(0)
        assert mgr._background_shed_warned is False, "latch never re-armed"

        second_gate = asyncio.Event()
        try:
            self._fill_to_cap(mgr, second_gate)
            caplog.clear()  # else the FIRST burst's record satisfies the assert
            with caplog.at_level("WARNING", logger="memtomem_stm.proxy.manager"):
                mgr._spawn_background(
                    self._blocker(second_gate), stage="extract", server="s", tool="t"
                )
            assert any("shed" in r.getMessage() for r in caplog.records), (
                "the second burst was silent — the latch re-armed but nothing warned"
            )
            assert mgr._background_shed_total == 2
        finally:
            second_gate.set()
            await asyncio.gather(*list(mgr._background_tasks), return_exceptions=True)

    async def test_latch_stays_armed_while_the_backlog_hovers_above_half(self, tmp_path):
        # The re-arm threshold is half the cap, so a backlog that never drains
        # that far keeps the latch set — one warning for one sustained
        # overload, not one per task.
        from memtomem_stm.proxy.manager import MAX_BACKGROUND_TASKS

        mgr = _make_manager(tmp_path=tmp_path)
        gate = asyncio.Event()
        try:
            tasks = self._fill_to_cap(mgr, gate)
            mgr._spawn_background(self._blocker(gate), stage="extract", server="s", tool="t")
            assert mgr._background_shed_warned is True

            # Drain to just above half the cap.
            drain_to = MAX_BACKGROUND_TASKS // 2 + 1
            for task in tasks[: MAX_BACKGROUND_TASKS - drain_to]:
                task.cancel()
            await asyncio.gather(*tasks[: MAX_BACKGROUND_TASKS - drain_to], return_exceptions=True)
            await asyncio.sleep(0)
            assert len(mgr._background_tasks) == drain_to
            assert mgr._background_shed_warned is True, "latch re-armed above the threshold"
        finally:
            gate.set()
            await asyncio.gather(*list(mgr._background_tasks), return_exceptions=True)

    async def test_stop_closes_the_spawn_path_before_draining(self, tmp_path):
        # Setting the flag at the END of stop() would let the drain loop race
        # a producer this manager creates itself. Probe from inside the drain:
        # a task cancelled by the loop tries to spawn while stop() is still
        # running, and must be refused.
        mgr = _make_manager(tmp_path=tmp_path)
        refusals: list[object] = []

        async def probe() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                refusals.append(
                    mgr._spawn_background(
                        asyncio.sleep(3600), stage="extract", server="s", tool="t"
                    )
                )
                raise

        mgr._spawn_background(probe(), stage="extract", server="s", tool="t")
        await asyncio.sleep(0)
        await mgr.stop()

        assert refusals == [None], (
            "a spawn from inside the drain was accepted — the flag is set too late"
        )

    async def test_stop_closes_the_tools_refresh_path_too(self, tmp_path):
        # tools_refresh bypasses the CAP deliberately, but it must not bypass
        # the DRAIN: it adds to the same set, so one scheduled mid-drain lands
        # where stop() has already passed and nothing cancels it. Probe from
        # inside the drain.
        mgr = _make_manager(tmp_path=tmp_path)
        observed: dict[str, object] = {}

        async def probe() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                mgr._schedule_tools_refresh("srv")
                # Snapshot INSIDE the drain: these are the values that decide
                # whether a refresh task was created behind stop()'s back.
                observed["tasks"] = len(mgr._background_tasks)
                observed["running"] = "srv" in mgr._tools_refresh_running
                raise

        first = mgr._spawn_background(probe(), stage="extract", server="s", tool="t")
        assert first is not None
        await asyncio.sleep(0)
        await mgr.stop()

        assert observed["tasks"] == 1, (
            "a refresh was scheduled mid-drain — it would never be cancelled "
            f"(set held {observed['tasks']} task(s))"
        )
        assert observed["running"] is False, (
            "claiming ``running`` without a task would block every later refresh"
        )
        # The refusal is not a permanent block: once stop() has finished, a
        # fresh notification schedules normally.
        task = mgr._schedule_tools_refresh("srv")  # noqa: F841 - state asserted below
        assert len(mgr._background_tasks) == 1
        for pending in list(mgr._background_tasks):
            pending.cancel()
        await asyncio.gather(*list(mgr._background_tasks), return_exceptions=True)

    async def test_stop_reopens_the_spawn_path_when_it_finishes(self, tmp_path):
        # The closure is scoped to the drain, not to "stopped forever": a
        # manager reused after stop() (and the documented "notification after
        # stop reschedules" contract in test_tools_list_changed) must get
        # working background stages back rather than silently skipping them.
        mgr = _make_manager(servers={}, tmp_path=tmp_path)
        await mgr.stop()
        assert mgr._background_closed is False

        task = mgr._spawn_background(asyncio.sleep(0), stage="extract", server="s", tool="t")
        assert task is not None, "stop() left the background spawn path closed"
        await asyncio.gather(task, return_exceptions=True)
        await mgr.stop()

    async def test_stop_cancels_survivors_instead_of_leaking(self, tmp_path, caplog):
        # A task that schedules a replacement as it unwinds outruns any
        # single drain pass: the pass cancels what it snapshotted and a new
        # one appears behind it. Replacements are injected directly into the
        # set (the shape a concurrent producer has), so the chain does not
        # depend on the manager's own spawn refusal. Every generation must end
        # up CANCELLED — the pre-fix code cleared the set instead, abandoning
        # whatever the bounded loop had not reached. (This chain is finite, so
        # it converges inside the budget; the straggler branch is pinned by
        # test_stop_keeps_tracking_a_task_that_never_unwinds.)
        mgr = _make_manager(tmp_path=tmp_path)
        spawned: list[asyncio.Task] = []
        generations = 12  # more chained respawns than a single drain round

        def _respawn() -> None:
            if len(spawned) >= generations:
                return
            replacement = asyncio.create_task(_respawning())
            mgr._background_tasks.add(replacement)
            spawned.append(replacement)

        async def _respawning() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                _respawn()
                raise

        _respawn()
        await asyncio.sleep(0)

        with caplog.at_level("WARNING", logger="memtomem_stm.proxy.manager"):
            await mgr.stop()

        # Several generations prove the chain really did outrun individual
        # drain rounds; the exact count depends on how many rounds the
        # deadline allows.
        assert len(spawned) > 8, (
            f"only {len(spawned)} generations — the respawn chain never outran a "
            "single drain round, so this test would pass without the fix"
        )
        assert all(t.cancelled() for t in spawned), (
            "survivors must be cancelled, not merely done or abandoned: "
            f"{[(t.done(), t.cancelled()) for t in spawned]}"
        )
        # Everything converged inside the budget, so there is nothing to warn
        # about — the point is that no task was left running.
        assert not mgr._background_tasks

    async def test_refresh_straggler_keeps_its_running_claim(self, tmp_path):
        # tools_refresh is cap-EXEMPT, so a straggler that outlives the drain
        # must keep its ``running`` claim: clearing it lets the next
        # notification schedule a second cap-exempt refresh for the same
        # server beside the old one, and every stop/start cycle would stack
        # another — unbounded despite a fixed upstream count.
        #
        # The straggler is registered in the refresh bookkeeping only, NOT in
        # ``_background_tasks``: this pins stop()'s bookkeeping decision, and
        # keeping it out of the drain means the probe can be an ordinary
        # cancellable task instead of one that swallows cancellation (which
        # wedges the event loop at teardown).
        mgr = _make_manager(tmp_path=tmp_path)
        straggler = asyncio.create_task(asyncio.sleep(3600))
        mgr._tools_refresh_tasks["srv"] = straggler
        mgr._tools_refresh_running.add("srv")
        try:
            await mgr.stop()

            assert not straggler.done()  # premise: it really is still alive
            assert "srv" in mgr._tools_refresh_running, (
                "the straggler's claim was cleared — the next notification would "
                "schedule a second cap-exempt refresh beside it"
            )

            mgr._schedule_tools_refresh("srv")
            assert not mgr._background_tasks, (
                "a duplicate refresh was scheduled for a server that already has a live one"
            )
        finally:
            straggler.cancel()
            with contextlib.suppress(BaseException):
                await straggler

    async def test_double_start_cancels_live_refreshes(self, tmp_path):
        # The double-start guard replaces the connections a refresh is running
        # against. Dropping only its ``running`` claim would leave the task
        # alive AND let the next notification schedule a second cap-exempt
        # refresh beside it — one more per start cycle.
        mgr = _make_manager(servers={}, tmp_path=tmp_path)
        await mgr.start()  # first start: _stack is set, so the next one guards

        straggler = asyncio.create_task(asyncio.sleep(3600))
        mgr._tools_refresh_tasks["srv"] = straggler
        mgr._tools_refresh_running.add("srv")
        try:
            await mgr.start()
            # Observe with asyncio.wait, NOT wait_for: wait_for cancels its
            # awaitable on timeout, which would supply the very cancellation
            # this test claims production performed.
            done, _ = await asyncio.wait({straggler}, timeout=5.0)

            assert done, "the straggler was never cancelled by the double-start guard"
            assert straggler.cancelled(), (
                "a refresh task survived the double-start guard and now runs "
                "against replaced connections"
            )
            assert not mgr._tools_refresh_tasks
            assert "srv" not in mgr._tools_refresh_running
        finally:
            if not straggler.done():
                straggler.cancel()
            with contextlib.suppress(BaseException):
                await straggler
            await mgr.stop()

    async def test_shed_background_extract_is_not_reported_as_pending(self, tmp_path):
        # Symmetric to the auto-index shed test in test_auto_index_background:
        # a background extraction that was never scheduled must not leave
        # extract_ok/extract_error at None, which records no outcome and so
        # is indistinguishable from a run that WAS scheduled. The coroutine
        # is closed before it can
        # record its attempt, so the stage records it instead.
        from memtomem_stm.proxy.config import ExtractionConfig, ProxyConfig
        from memtomem_stm.proxy.manager import MAX_BACKGROUND_TASKS

        mgr = _make_manager(tmp_path=tmp_path)
        mgr._index_engine = AsyncMock()
        mgr._connections["srv"] = UpstreamConnection(
            name="srv",
            config=UpstreamServerConfig(prefix="test"),
            session=AsyncMock(),
            tools=[],
        )
        cfg_snap = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={"srv": UpstreamServerConfig(prefix="test")},
            extraction=ExtractionConfig(
                enabled=True, background=True, min_response_chars=0, memory_dir=tmp_path / "f"
            ),
        )
        tool_cfg = replace(
            mgr._resolve_tool_config("srv", "t", cfg_snap),
            extraction_enabled=True,
        )
        gate = asyncio.Event()
        fillers = [
            mgr._spawn_background(self._blocker(gate), stage="extract", server="s", tool="t")
            for _ in range(MAX_BACKGROUND_TASKS)
        ]
        try:
            result = await mgr._run_extract_stage(
                server="srv",
                tool="t",
                upstream_args={},
                tc=tool_cfg,
                cfg_snap=cfg_snap,
                cleaned="x" * 200,
                context_query=None,
            )

            assert result.ok is False, "a shed extract must not look like pending work"
            assert result.error == "background_shed"
            snap = mgr.index_observability.snapshot()
            assert snap["attempts"]["__total__"] == {"extract": 1}
            assert snap["outcomes"]["__total__"] == {"shed": 1}
        finally:
            gate.set()
            await asyncio.gather(*fillers, return_exceptions=True)

    async def test_older_refresh_finishing_does_not_free_the_newer_one(self, tmp_path):
        # Double-start can leave an older refresh task alive while a newer one
        # is registered for the same server. When the older one finishes, its
        # ``finally`` must NOT release the newer task's claim — that would let
        # a third cap-exempt refresh be scheduled beside them, one per cycle.
        # Exercised through the real task/callback path, not a synthetic map
        # entry, since that interaction is what the guard protects.
        mgr = _make_manager(tmp_path=tmp_path)
        session = AsyncMock()
        session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[]))
        mgr._connections["srv"] = UpstreamConnection(
            name="srv", config=UpstreamServerConfig(prefix="srv"), session=session, tools=[]
        )

        mgr._schedule_tools_refresh("srv")
        older = mgr._tools_refresh_tasks["srv"]

        # Simulate the double-start window: a newer task registered for the
        # same server while the older one is still pending.
        newer = asyncio.create_task(asyncio.sleep(3600))
        mgr._tools_refresh_tasks["srv"] = newer
        mgr._background_tasks.add(newer)
        try:
            await asyncio.gather(older, return_exceptions=True)
            await asyncio.sleep(0)

            assert "srv" in mgr._tools_refresh_running, (
                "the older task's finally released the newer task's claim"
            )
            assert mgr._tools_refresh_tasks["srv"] is newer, (
                "the older task pruned the newer map entry"
            )
        finally:
            newer.cancel()
            with contextlib.suppress(BaseException):
                await newer

    async def test_finished_refresh_releases_its_running_claim(self, tmp_path):
        # The other half of the identity guard: a refresh that DID finish must
        # release the claim and leave the map AT COMPLETION — before any
        # stop(). Installing a pre-finished task and calling stop() would pass
        # on stop()'s own filtering even if the coroutine's finally and the
        # done-callback both failed to prune, so this schedules a real refresh
        # and awaits it.
        mgr = _make_manager(tmp_path=tmp_path)
        session = AsyncMock()
        session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[]))
        mgr._connections["srv"] = UpstreamConnection(
            name="srv", config=UpstreamServerConfig(prefix="srv"), session=session, tools=[]
        )

        mgr._schedule_tools_refresh("srv")
        task = mgr._tools_refresh_tasks["srv"]
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)  # let the done-callback run

        assert "srv" not in mgr._tools_refresh_running, (
            "a finished refresh kept its claim — later notifications for this "
            "server would be dropped"
        )
        assert "srv" not in mgr._tools_refresh_tasks, "the finished task lingers in the map"

    async def test_stop_re_cancels_a_task_that_ignores_its_first_cancel(self, tmp_path):
        # ``asyncio.wait`` defaults to ALL_COMPLETED, so waiting the whole
        # budget in one call would let a task that ignores only its FIRST
        # cancellation consume the entire window and never be asked again —
        # it would still be running when resource teardown starts. The drain
        # polls in slices so the second request actually arrives.
        from memtomem_stm.proxy.manager import BACKGROUND_DRAIN_BUDGET_SECONDS

        mgr = _make_manager(tmp_path=tmp_path)
        started = asyncio.Event()
        cancels = 0

        async def stubborn_once() -> None:
            nonlocal cancels
            started.set()
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancels += 1
                    if cancels >= 2:
                        raise  # unwinds on the SECOND request
                    continue

        task = asyncio.create_task(stubborn_once())
        mgr._background_tasks.add(task)
        await started.wait()
        try:
            await asyncio.wait_for(mgr.stop(), timeout=BACKGROUND_DRAIN_BUDGET_SECONDS + 20.0)

            assert cancels >= 2, (
                f"only {cancels} cancellation(s) issued — one full-budget wait swallowed "
                "the window, so a task that ignores its first cancel is never asked again"
            )
            assert task.cancelled()
            assert not mgr._background_tasks
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(task, timeout=5.0)

    async def test_stop_keeps_tracking_a_task_that_never_unwinds(self, tmp_path, caplog):
        # cancel() only REQUESTS cancellation. A task that swallows it must
        # not be dropped from the set — clearing it is what made the old leak
        # invisible — and stop() must still return within its budget.
        from memtomem_stm.proxy.manager import BACKGROUND_DRAIN_BUDGET_SECONDS

        mgr = _make_manager(tmp_path=tmp_path)
        started = asyncio.Event()
        release = asyncio.Event()  # teardown escape hatch, see below

        async def stubborn() -> None:
            started.set()
            while not release.is_set():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    continue  # refuses to unwind

        task = asyncio.create_task(stubborn())
        mgr._background_tasks.add(task)
        await started.wait()
        stop_task = asyncio.create_task(mgr.stop())
        try:
            with caplog.at_level("WARNING", logger="memtomem_stm.proxy.manager"):
                # Observe stop() rather than awaiting it: under the regression
                # this pins against (an unbounded gather), stop() never
                # returns AND cannot be cancelled, because the probe swallows
                # cancellation. ``wait_for`` would then hang the suite instead
                # of failing — the probe has to be released first, which only
                # a non-awaiting observation allows.
                done, _ = await asyncio.wait(
                    {stop_task}, timeout=BACKGROUND_DRAIN_BUDGET_SECONDS + 20.0
                )
            assert done, "stop() did not return within its drain budget"

            assert not task.done()
            assert task in mgr._background_tasks, (
                "a task that never unwound was dropped from the set and is now invisible"
            )
            assert any("stay tracked" in r.getMessage() for r in caplog.records), caplog.text
        finally:
            # MUST run even when an assertion above fails — and before
            # awaiting anything that depends on the probe: it ignores
            # cancellation by construction, so a leaked one wedges the event
            # loop at teardown and the suite HANGS instead of reporting the
            # failure. A test whose job is to go red must be able to go red.
            release.set()
            task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, timeout=5.0)
            stop_task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(stop_task, timeout=5.0)
