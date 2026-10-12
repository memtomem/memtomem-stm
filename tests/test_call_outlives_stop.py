"""A call that ``stop()`` overtakes fails as a stopping manager.

``stop()`` clears the connection registry while a call may be awaiting its
upstream. The call then refuses with ``ManagerStoppingError`` — recorded like
any other in-pipeline failure — instead of raising ``KeyError`` from whichever
stage first reads the registry, and without rebuilding the stores ``stop()``
has just closed (#1119). A call already past its fetch that waits out teardown
on ``_selective_lock`` refuses the same way (#1125).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import Annotations, TextContent

from memtomem_stm.proxy.config import CompressionStrategy, ProxyConfig, UpstreamServerConfig
from memtomem_stm.proxy.manager import ManagerStoppingError, ProxyManager
from memtomem_stm.proxy.metrics_store import MetricsStore
from helpers import fake_tool_result, wire_proxy_manager


def _build(
    tmp_path: Path, compression: CompressionStrategy = CompressionStrategy.TRUNCATE
) -> tuple[ProxyManager, MetricsStore, ProxyConfig]:
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=compression,
        max_result_chars=500,
        max_retries=0,
        reconnect_delay_seconds=0.0,
    )
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json", upstream_servers={"srv": server_cfg}
    )
    mgr, store, _ = wire_proxy_manager(proxy_cfg, server_cfg, tmp_path)
    return mgr, store, proxy_cfg


async def _call_across_stop(mgr: ProxyManager, reply: object) -> None:
    """Run ``call_tool`` and land ``stop()`` while the upstream reply is pending."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _upstream(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return reply

    mgr._connections["srv"].session.call_tool.side_effect = _upstream
    call = asyncio.create_task(mgr.call_tool("srv", "tool", {}))
    await asyncio.wait_for(entered.wait(), timeout=5)
    await mgr.stop()
    assert "srv" not in mgr._connections
    release.set()
    await asyncio.wait_for(call, timeout=5)


def _annotated_reply() -> SimpleNamespace:
    first = TextContent(
        type="text", text="for the user", annotations=Annotations(audience=["user"])
    )
    second = TextContent(type="text", text="for the model")
    return SimpleNamespace(
        content=[first, second], is_error=False, structured_content=None, meta=None
    )


def _empty_reply() -> SimpleNamespace:
    return SimpleNamespace(content=[], is_error=False, structured_content=None, meta=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(lambda: fake_tool_result("hello from upstream"), id="text"),
        pytest.param(_annotated_reply, id="annotated-multi-block"),
        pytest.param(lambda: fake_tool_result("boom", is_error=True), id="upstream-error"),
        pytest.param(_empty_reply, id="empty"),
    ],
)
async def test_reply_after_stop_is_refused_and_recorded(tmp_path, reply):
    mgr, store, _ = _build(tmp_path)
    try:
        with pytest.raises(ManagerStoppingError):
            await _call_across_stop(mgr, reply())
    finally:
        store.close()
    with sqlite3.connect(tmp_path / "metrics.db") as db:
        rows = db.execute(
            "SELECT is_error, error_category, error_message FROM proxy_metrics"
        ).fetchall()
    assert rows == [
        (
            1,
            "internal_error",
            "ManagerStoppingError: proxy manager stopped while a call to 'srv' was in flight",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("compression", [CompressionStrategy.SELECTIVE, CompressionStrategy.HYBRID])
async def test_reply_after_stop_does_not_reopen_the_selective_store(tmp_path, compression):
    mgr, store, _ = _build(tmp_path, compression)
    text = "\n\n".join(f"## Section {i}\n" + "word " * 200 for i in range(10))
    try:
        with pytest.raises(ManagerStoppingError):
            await _call_across_stop(mgr, fake_tool_result(text))
    finally:
        store.close()
    assert mgr._selective_compressor is None


def test_superseded_publication_after_stop_raises_stopping(tmp_path):
    """A pin superseded by a reload resolves the live generation's settings
    through the registry; after ``stop()`` that is a stopping refusal, not a
    ``KeyError`` (the window between the post-fetch check and a later stage)."""
    mgr, store, proxy_cfg = _build(tmp_path, CompressionStrategy.SELECTIVE)
    stale_pin = proxy_cfg.model_copy()
    try:
        assert not mgr._pin_is_live_generation(stale_pin)
        mgr._connections.clear()
        with pytest.raises(ManagerStoppingError):
            mgr._publication_pair("srv", "tool", None, stale_pin)
    finally:
        store.close()


async def _call_across_stop_at_selective_lock(mgr: ProxyManager, reply: object) -> None:
    """Hold ``_selective_lock`` so the call parks there after its fetch, then
    run ``stop()`` to completion before letting the call take the lock."""
    mgr._connections["srv"].session.call_tool.return_value = reply
    lock = mgr._selective_lock
    await lock.acquire()
    try:
        call = asyncio.create_task(mgr.call_tool("srv", "tool", {}))
        for _ in range(1000):
            if lock._waiters:
                break
            await asyncio.sleep(0)
        assert lock._waiters, "the call never reached the selective lock"
        await mgr.stop()
        assert "srv" not in mgr._connections
    finally:
        lock.release()
    await asyncio.wait_for(call, timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("compression", [CompressionStrategy.SELECTIVE, CompressionStrategy.HYBRID])
async def test_call_parked_on_the_selective_lock_during_stop_does_not_rebuild(
    tmp_path, compression
):
    """A live pin's publication never read the registry, so a call that passed
    the post-fetch check and waited out ``stop()`` on the lock rebuilt the
    selective compressor and its store after teardown had closed them."""
    mgr, store, _ = _build(tmp_path, compression)
    text = "\n\n".join(f"## Section {i}\n" + "word " * 200 for i in range(10))
    try:
        with pytest.raises(ManagerStoppingError):
            await _call_across_stop_at_selective_lock(mgr, fake_tool_result(text))
    finally:
        store.close()
    assert mgr._selective_compressor is None


@pytest.mark.asyncio
async def test_live_publication_refuses_from_stop_until_start(tmp_path):
    """The live-pin branch refuses too: it is where SELECTIVE, HYBRID and the
    progressive store all decide what to (re)build. A manager that was never
    started publishes as before, and ``start()`` lifts the refusal."""
    mgr, store, proxy_cfg = _build(tmp_path, CompressionStrategy.SELECTIVE)
    try:
        assert mgr._pin_is_live_generation(proxy_cfg)
        assert mgr._publication_pair("srv", "tool", None, proxy_cfg) == (proxy_cfg, None)
        await mgr.stop()
        with pytest.raises(ManagerStoppingError):
            mgr._publication_pair("srv", "tool", None, proxy_cfg)
        await mgr.start()
        assert mgr._publication_pair("srv", "tool", None, proxy_cfg) == (proxy_cfg, None)
    finally:
        await mgr.stop()
        store.close()
