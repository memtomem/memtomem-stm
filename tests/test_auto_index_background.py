"""Background auto-indexing — opt-in via AutoIndexConfig.background=True (F4).

Validates:

- Background flag schedules an asyncio task and returns the
  ``[Indexing…] · scheduled`` placeholder footer immediately.
- ``stop()`` cancels in-flight background indexing tasks (re-uses the
  existing extraction drain loop, no new infrastructure).
- Metrics row records ``index_ok IS NULL`` / ``chunks_indexed = 0`` —
  tri-state matches background extraction so dashboards distinguish
  sync/background with ``index_ok IS NULL``.
- ``background=False`` (the default) keeps the pre-F4 synchronous
  contract verbatim — regression guard for the ``compose_index_footer``
  refactor in commit 3bb800e.
- Background indexing failure stays captured inside the task; the
  agent response is unaffected.
- Latency: with ``index_file`` simulating 500ms LTM latency, the
  background path returns < 300ms absolute AND < sync/3 relative.
  Dual assertion guards CI runner jitter (absolute alone is flaky)
  AND global regression (ratio alone misses proportional slowdown).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from memtomem_stm.proxy.config import (
    AutoIndexConfig,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker


def _bg_manager(
    tmp_path: Path,
    *,
    background: bool,
    sleep_ms: int = 0,
    raise_on_index: bool = False,
) -> tuple[ProxyManager, AsyncMock]:
    """Build a manager with auto_index enabled and a fake indexer.

    ``sleep_ms`` slows ``index_file`` to simulate LTM latency (used by
    the latency-delta assertion). ``raise_on_index`` makes the indexer
    raise inside the background task so we can verify error isolation.
    """
    indexer = AsyncMock()
    if raise_on_index:
        indexer.index_file.side_effect = RuntimeError("simulated index failure")
    elif sleep_ms:

        async def _slow_index(*_args, **_kwargs):
            await asyncio.sleep(sleep_ms / 1000)
            return SimpleNamespace(indexed_chunks=2)

        indexer.index_file.side_effect = _slow_index
    else:
        indexer.index_file.return_value = SimpleNamespace(indexed_chunks=2)

    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": UpstreamServerConfig(prefix="test")},
        auto_index=AutoIndexConfig(
            enabled=True,
            background=background,
            min_chars=10,
            memory_dir=tmp_path / "idx",
        ),
    )
    tracker = TokenTracker()
    mgr = ProxyManager(proxy_cfg, tracker, index_engine=indexer)

    # Inject a mocked upstream that returns enough text to clear ``min_chars``.
    session = AsyncMock()
    session.call_tool.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="upstream content " * 100)],
        is_error=False,
    )
    mgr._connections["srv"] = UpstreamConnection(
        name="srv",
        config=UpstreamServerConfig(prefix="test"),
        session=session,
        tools=[],
    )
    return mgr, indexer


class TestBackgroundAutoIndex:
    async def test_background_flag_schedules_task(self, tmp_path):
        """``background=True`` schedules indexing via ``asyncio.create_task``
        and returns the ``[Indexing…] · scheduled`` placeholder footer
        immediately — the agent does not block on the indexer."""
        mgr, indexer = _bg_manager(tmp_path, background=True)
        try:
            result = await mgr.call_tool("srv", "some_tool", {})

            assert "[Indexing…]" in result
            assert "· scheduled." in result
            # Placeholder intentionally drops the namespace — final ns isn't
            # known until the background task completes.
            assert "namespace" not in result
            assert len(mgr._background_tasks) == 1

            # Drain so the indexer call lands before assertion.
            await asyncio.gather(*mgr._background_tasks, return_exceptions=True)
            indexer.index_file.assert_awaited_once()
        finally:
            await mgr.stop()

    async def test_background_privacy_skip_returns_unfootered_response(self, tmp_path):
        """A secret-bearing response must not get the ``[Indexing…] ·
        scheduled`` placeholder: the task's privacy gate would decline, so
        the footer would promise an indexing run that never happens (#453).
        The manager pre-checks with the same predicate the task uses and
        returns the un-footered response without scheduling anything."""
        mgr, indexer = _bg_manager(tmp_path, background=True)
        mgr._connections["srv"].session.call_tool.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="config dump: password=hunter2 " * 10)],
            is_error=False,
        )
        try:
            result = await mgr.call_tool("srv", "some_tool", {})

            assert "[Indexing…]" not in result
            assert "scheduled" not in result
            assert mgr._background_tasks == set()
            indexer.index_file.assert_not_awaited()
            snap = mgr.index_observability.snapshot()
            assert snap["attempts"]["__total__"] == {"auto_index": 1}
            assert snap["outcomes"]["__total__"] == {"privacy_skip": 1}
        finally:
            await mgr.stop()

    async def test_shed_background_index_returns_unfootered_response(self, tmp_path):
        """A shed spawn must not ship ``[Indexing…] · scheduled`` (#868).

        Same contract the #453 privacy pre-check enforces one branch up: the
        placeholder promises an indexing run, so it must not go out when the
        backlog was at capacity and nothing was scheduled. The metrics row
        must say so too — leaving ``index_ok=None`` would read as "background
        work still pending" on every dashboard that filters that way.
        """
        mgr, indexer = _bg_manager(tmp_path, background=True)
        record_spy = MagicMock()
        mgr.tracker.record = record_spy
        # Fill the set to the cap so the auto-index spawn is shed.
        gate = asyncio.Event()

        async def _blocker() -> None:
            await gate.wait()

        from memtomem_stm.proxy.manager import MAX_BACKGROUND_TASKS

        fillers = [
            mgr._spawn_background(_blocker(), stage="extract", server="s", tool="t")
            for _ in range(MAX_BACKGROUND_TASKS)
        ]
        try:
            result = await mgr.call_tool("srv", "some_tool", {})

            assert "[Indexing…]" not in result
            assert "scheduled" not in result
            indexer.index_file.assert_not_awaited()
            snap = mgr.index_observability.snapshot()
            assert snap["attempts"]["__total__"] == {"auto_index": 1}
            assert snap["outcomes"]["__total__"] == {"shed": 1}
            metrics = record_spy.call_args.args[0]
            assert metrics.index_ok is False, "a shed stage must not look like pending work"
            assert metrics.index_error == "background_shed"
        finally:
            gate.set()
            await asyncio.gather(*fillers, return_exceptions=True)
            await mgr.stop()

    async def test_background_drains_on_stop(self, tmp_path):
        """``stop()`` cancels in-flight background indexing tasks via the
        existing extraction drain loop — no new infrastructure."""
        mgr, _ = _bg_manager(tmp_path, background=True, sleep_ms=2000)
        await mgr.call_tool("srv", "some_tool", {})
        assert len(mgr._background_tasks) == 1
        task = next(iter(mgr._background_tasks))

        await mgr.stop()

        assert task.done()
        assert len(mgr._background_tasks) == 0

    async def test_background_metrics_tri_state(self, tmp_path):
        """Background path leaves ``index_ok=None`` / ``index_error=None`` /
        ``chunks_indexed=0`` in the recorded metrics row — same tri-state
        as background extraction. Dashboards distinguish sync vs background
        with ``WHERE index_ok IS NULL``."""
        mgr, _ = _bg_manager(tmp_path, background=True)
        record_spy = MagicMock()
        mgr.tracker.record = record_spy
        try:
            await mgr.call_tool("srv", "some_tool", {})
            await asyncio.gather(*mgr._background_tasks, return_exceptions=True)

            record_spy.assert_called_once()
            metrics = record_spy.call_args.args[0]
            assert metrics.index_ok is None
            assert metrics.index_error is None
            assert metrics.chunks_indexed == 0
        finally:
            await mgr.stop()

    async def test_sync_default_unchanged(self, tmp_path):
        """``background=False`` (default) keeps the synchronous contract:
        ``[Indexed] ... K chunks`` footer + ``index_ok=True`` in metrics.
        Regression guard for the helper extraction in commit 3bb800e."""
        mgr, indexer = _bg_manager(tmp_path, background=False)
        record_spy = MagicMock()
        mgr.tracker.record = record_spy
        try:
            result = await mgr.call_tool("srv", "some_tool", {})

            assert "[Indexed]" in result
            assert "2 chunks" in result
            assert "· scheduled" not in result
            indexer.index_file.assert_awaited_once()
            assert len(mgr._background_tasks) == 0

            metrics = record_spy.call_args.args[0]
            assert metrics.index_ok is True
            assert metrics.chunks_indexed == 2
        finally:
            await mgr.stop()

    async def test_background_failure_does_not_break_response(self, tmp_path, caplog):
        """Background indexing exception stays captured inside the task as
        ``outcome.ok=False`` (logged WARNING). The agent already received
        the placeholder footer synchronously — the exception cannot break
        the in-flight response."""
        mgr, _ = _bg_manager(tmp_path, background=True, raise_on_index=True)
        try:
            with caplog.at_level("WARNING", logger="memtomem_stm.proxy.memory_ops"):
                result = await mgr.call_tool("srv", "some_tool", {})
                # Drain so the WARNING is emitted before assertion.
                await asyncio.gather(*mgr._background_tasks, return_exceptions=True)

            assert "[Indexing…]" in result
            assert any("Auto-index failed" in r.message for r in caplog.records)
        finally:
            await mgr.stop()

    async def test_background_escaped_exception_surfaces_via_callback(self, tmp_path, caplog):
        """An exception that ESCAPES the inner ``auto_index_response`` handler
        (e.g. mkdir / atomic-write failure) is surfaced by the background
        done-callback as a structured WARNING on the manager logger — not left
        to a non-deterministic gc-time "Task exception was never retrieved".
        Regression guard for the background-task exception consumer."""
        mgr, _ = _bg_manager(tmp_path, background=True)
        # Replace the whole coroutine so the raise bypasses the inner handler
        # and propagates to the task — the residual-escape path the callback
        # exists to catch (the inner handler only covers ``index_file`` errors).
        mgr._auto_index_response = AsyncMock(side_effect=RuntimeError("escaped past inner handler"))
        try:
            with caplog.at_level("WARNING", logger="memtomem_stm.proxy.manager"):
                result = await mgr.call_tool("srv", "some_tool", {})
                assert "[Indexing…]" in result
                assert len(mgr._background_tasks) == 1
                task = next(iter(mgr._background_tasks))
                await asyncio.gather(task, return_exceptions=True)
                # Let the done-callback (scheduled via call_soon) run.
                await asyncio.sleep(0)

            # The callback dropped the finished task from the tracking set ...
            assert mgr._background_tasks == set()
            # ... and surfaced the escaped exception as a structured WARNING
            # (with traceback) instead of a gc-time un-retrieved warning.
            matching = [
                r
                for r in caplog.records
                if "Background auto_index task failed for srv/some_tool" in r.getMessage()
            ]
            assert matching, "escaped background exception was not logged"
            assert "escaped past inner handler" in matching[0].getMessage()
            assert matching[0].exc_info is not None
        finally:
            await mgr.stop()


class TestBackgroundLatency:
    async def test_background_avoids_indexer_latency(self, tmp_path):
        """With ``index_file`` simulating 500ms LTM latency:

        - sync (``background=False``) blocks: response latency ≈ 500ms+.
        - background (``background=True``) returns near-instantly: < 300ms
          absolute AND < sync/3 relative.

        Both assertions must pass. Absolute alone is flaky on shared CI
        runners (50–100ms jitter); ratio alone misses a global regression
        where both paths slow proportionally.
        """
        sleep_ms = 500
        sync_mgr, _ = _bg_manager(tmp_path, background=False, sleep_ms=sleep_ms)
        bg_mgr, _ = _bg_manager(tmp_path, background=True, sleep_ms=sleep_ms)
        try:
            t0 = time.monotonic()
            await sync_mgr.call_tool("srv", "some_tool", {})
            sync_ms = (time.monotonic() - t0) * 1000

            t0 = time.monotonic()
            await bg_mgr.call_tool("srv", "some_tool", {})
            bg_ms = (time.monotonic() - t0) * 1000

            assert bg_ms < 300, f"background latency {bg_ms:.0f}ms exceeds 300ms cap"
            assert bg_ms < sync_ms / 3, (
                f"background latency {bg_ms:.0f}ms not < sync/3 ({sync_ms / 3:.0f}ms)"
            )
        finally:
            await bg_mgr.stop()
            await sync_mgr.stop()
