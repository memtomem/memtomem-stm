"""Shared cross-platform test helpers.

Kept deliberately small — fixtures live in ``conftest.py``; this module
holds plain functions that are easier to grep for and call inline.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from memtomem_stm.proxy.cache import ProxyCache
    from memtomem_stm.proxy.config import ProxyConfig, UpstreamServerConfig
    from memtomem_stm.proxy.manager import ProxyManager
    from memtomem_stm.proxy.metrics_store import MetricsStore


def set_home(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Pin the user's home directory to ``path`` on every platform.

    ``Path.home()`` and ``os.path.expanduser("~")`` consult ``USERPROFILE``
    before ``HOME`` on Windows. Tests that monkeypatch only ``HOME`` therefore
    leak the developer's real profile into temp-dir fixtures on Windows
    runners. Patching both keeps the sandbox hermetic.
    """
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))


def fake_tool_result(text: str, *, is_error: bool = False) -> SimpleNamespace:
    """The upstream ``CallToolResult`` shape the manager reads.

    One definition on purpose: the pipeline reads ``content`` / ``is_error`` /
    ``structured_content`` / ``meta``, and a per-file copy of this shape drifts
    silently — the copy that is missing a field keeps passing against whatever
    the manager stopped reading.
    """
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        is_error=is_error,
        structured_content=None,
        meta=None,
    )


class FakeSurfacingEngine:
    """Minimal surfacing engine: the manager only reads ``injection_mode`` /
    ``observability`` and awaits ``surface(...)``.

    ``surface`` APPENDS its suffix so a progressive first chunk's read-more
    footer survives — a fake that replaced the text could report success while
    silently breaking the progressive concat invariant.
    """

    def __init__(
        self,
        mode: str = "append",
        *,
        suffix: str = " [mem]",
        raises: Exception | None = None,
    ) -> None:
        self.injection_mode = mode
        self.observability = None
        self._suffix = suffix
        self._raises = raises

    async def surface(
        self,
        *,
        server: str = "",
        tool: str = "",
        arguments: dict | None = None,
        response_text: str,
        trace_id: Any = None,
        context_query: Any = None,
    ) -> str:
        if self._raises is not None:
            raise self._raises
        return response_text + self._suffix


def count_loader_reads(manager: ProxyManager) -> list[int]:
    """Wrap the manager's config loader so every ``get()`` bumps a counter.

    Returns the one-element list holding the count, so a caller can reset it
    between phases. Each ``get()`` is a ``Path.stat()`` — the syscall #871 is
    about — so this is the measurement those read-count ceilings are pinned on.
    """
    calls = [0]
    real_get = manager._config_loader.get

    def counting_get():
        calls[0] += 1
        return real_get()

    manager._config_loader.get = counting_get  # type: ignore[method-assign]
    return calls


def wire_proxy_manager(
    proxy_cfg: ProxyConfig,
    server_cfg: UpstreamServerConfig,
    tmp_path: Path,
    *,
    server: str = "srv",
    index_engine: object | None = None,
    with_cache: bool = False,
    upstream_text: str | None = None,
) -> tuple[ProxyManager, MetricsStore, ProxyCache | None]:
    """Build a ``ProxyManager`` over a real MetricsStore and a mocked upstream.

    The manager/connection/cache wiring only — the CONFIG is the caller's, so
    each suite keeps its own intent (file-backed vs in-memory, which stages are
    on) without also re-deriving this seam. A second copy of it drifts the
    moment the constructor or ``UpstreamConnection`` shape changes: one file
    gets fixed, the other keeps passing against a stale contract.
    """
    from memtomem_stm.proxy.cache import ProxyCache
    from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
    from memtomem_stm.proxy.metrics import TokenTracker
    from memtomem_stm.proxy.metrics_store import MetricsStore

    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    mgr = ProxyManager(proxy_cfg, TokenTracker(metrics_store=store), index_engine=index_engine)
    session = AsyncMock()
    if upstream_text is not None:
        session.call_tool.return_value = fake_tool_result(upstream_text)
    mgr._connections[server] = UpstreamConnection(
        name=server, config=server_cfg, session=session, tools=[]
    )
    cache: ProxyCache | None = None
    if with_cache:
        cache = ProxyCache(tmp_path / "cache.db", max_entries=100)
        cache.initialize()
        mgr._cache = cache
    return mgr, store, cache
