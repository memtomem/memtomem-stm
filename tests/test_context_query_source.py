"""``proxy_metrics.context_query_source``: whether a call carried ``_context_query``.

The column answers one question — did the caller supply a usable query — for
the two MCP success rows that carry a ``compression_accounting`` stamp. Every
other row stays ``NULL`` and out of the count. The query's text is never stored.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import types as mcp_types

from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.config import (
    CacheConfig,
    ProgressiveConfig,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import (
    METADATA_BYPASS_STRATEGY,
    STRUCTURED_BYPASS_STRATEGY,
    CallMetrics,
    TokenTracker,
    context_query_source,
)
from memtomem_stm.proxy.metrics_store import MetricsStore, read_compression_summary

QUERY = "how to configure the zebra retention floor"


def _text_result(text: str = "payload " * 40):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], is_error=False)


def _metadata_bypass_result():
    """Two text blocks, one with block-level metadata: the metadata bypass."""
    return SimpleNamespace(
        content=[
            mcp_types.TextContent(type="text", text="first", _meta={"audience": "a"}),
            mcp_types.TextContent(type="text", text="second"),
        ],
        is_error=False,
    )


@pytest.fixture
async def build(tmp_path: Path):
    opened: list[tuple[ProxyManager, MetricsStore, ProxyCache | None]] = []

    def _factory(*, with_cache: bool = False, tools=(), **server):
        store = MetricsStore(tmp_path / "m.db")
        store.initialize()
        server_cfg = UpstreamServerConfig(
            prefix="t", max_retries=0, reconnect_delay_seconds=0.0, **server
        )
        proxy_cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={"srv": server_cfg},
            cache=CacheConfig(default_ttl_seconds=3600.0),
        )
        mgr = ProxyManager(proxy_cfg, TokenTracker(metrics_store=store))
        mgr._connections["srv"] = UpstreamConnection(
            name="srv", config=server_cfg, session=AsyncMock(), tools=list(tools)
        )
        cache: ProxyCache | None = None
        if with_cache:
            cache = ProxyCache(tmp_path / "c.db", max_entries=100)
            cache.initialize()
            mgr._cache = cache
        opened.append((mgr, store, cache))
        return mgr, store

    yield _factory
    for mgr, store, cache in opened:
        await mgr.stop()
        store.close()
        if cache is not None:
            cache.close()


def _rows(store: MetricsStore, tool: str) -> list[tuple]:
    return store._db.execute(
        "SELECT context_query_source, compression_accounting, compression_strategy, is_error "
        "FROM proxy_metrics WHERE tool = ? ORDER BY id",
        (tool,),
    ).fetchall()


class TestHelper:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(QUERY, "agent"), ("  x  ", "agent"), (None, "none"), ("", "none"), ("  \n\t", "none")],
    )
    def test_value(self, value, expected):
        assert context_query_source(value) == expected

    def test_a_row_built_without_it_is_unobserved(self):
        row = CallMetrics(server="s", tool="t", original_chars=1, compressed_chars=1)
        assert row.context_query_source is None


class TestStampedRows:
    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            ({"q": "a", "_context_query": QUERY}, "agent"),
            ({"q": "a"}, "none"),
            ({"q": "a", "_context_query": "   "}, "none"),
            ({"q": "a", "_context_query": 123}, "none"),
        ],
    )
    async def test_normal_path(self, build, arguments, expected):
        mgr, store = build()
        mgr._connections["srv"].session.call_tool.return_value = _text_result()

        await mgr.call_tool("srv", "read", arguments)

        ((source, accounting, _strategy, is_error),) = _rows(store, "read")
        assert (source, is_error) == (expected, 0)
        assert accounting is not None

    async def test_an_empty_text_block_is_stamped(self, build):
        """The stamp follows the upstream result's text blocks, not their
        text: one empty block is observed, no block is not (see
        ``TestUnstampedRows.test_response_without_text``)."""
        mgr, store = build()
        mgr._connections["srv"].session.call_tool.return_value = _text_result("")

        await mgr.call_tool("srv", "read", {"_context_query": QUERY})

        ((source, accounting, _strategy, is_error),) = _rows(store, "read")
        assert (source, is_error) == ("agent", 0)
        assert accounting is not None
        summary = read_compression_summary(store._db_path)
        assert summary["context_query"] == {"observed": 1, "agent": 1}

    async def test_structured_delivery_frame(self, build):
        mgr, store = build(
            compression="progressive",
            delivery_format="structured",
            max_result_chars=1600,
            progressive=ProgressiveConfig(chunk_size=1000),
        )
        mgr._connections["srv"].session.call_tool.return_value = _text_result("word " * 2000)

        await mgr.call_tool("srv", "read", {"_context_query": QUERY})

        ((source, accounting, _strategy, _is_error),) = _rows(store, "read")
        assert accounting == "initial_structured_v1"  # a structured frame was delivered
        assert source == "agent"
        # …and the reader counts it: structured rows are sized apart from text
        # rows, but they are observed like any other stamped response.
        summary = read_compression_summary(store._db_path)
        assert summary["structured_delivery"]["calls"] == 1
        assert summary["context_query"] == {"observed": 1, "agent": 1}

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [({"_context_query": QUERY}, "agent"), ({}, "none")],
    )
    async def test_bypass_path(self, build, arguments, expected):
        mgr, store = build()
        mgr._connections["srv"].session.call_tool.return_value = _metadata_bypass_result()

        await mgr.call_tool("srv", "meta", arguments)

        ((source, _accounting, strategy, _is_error),) = _rows(store, "meta")
        # The row came from the bypass, not from the normal path.
        assert strategy == METADATA_BYPASS_STRATEGY
        assert source == expected

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [({"_context_query": QUERY}, "agent"), ({}, "none")],
    )
    async def test_native_structured_bypass(self, build, arguments, expected):
        mgr, store = build(delivery_format="structured")
        mgr._connections["srv"].session.call_tool.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="rows follow")],
            is_error=False,
            structured_content={"rows": [1, 2]},
        )

        await mgr.call_tool("srv", "table", arguments)

        ((source, accounting, strategy, _is_error),) = _rows(store, "table")
        assert (strategy, accounting) == (STRUCTURED_BYPASS_STRATEGY, "initial_response_v1")
        assert source == expected


class TestUnstampedRows:
    """A valid query is supplied in each case, so ``NULL`` is a decision about
    the row and not the absence of a query."""

    async def test_upstream_error(self, build):
        mgr, store = build()
        mgr._connections["srv"].session.call_tool.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="boom")], is_error=True
        )

        await mgr.call_tool("srv", "read", {"_context_query": QUERY})

        ((source, _accounting, _strategy, is_error),) = _rows(store, "read")
        assert (source, is_error) == (None, 1)

    @pytest.mark.parametrize(
        "result",
        [
            SimpleNamespace(content=[], is_error=False),
            SimpleNamespace(content=[], is_error=False, structured_content={"rows": [1, 2]}),
            SimpleNamespace(
                content=[SimpleNamespace(type="image", data="aGk=", mimeType="image/png")],
                is_error=False,
            ),
        ],
        ids=["empty", "structured-only", "image-only"],
    )
    async def test_response_without_text(self, build, result):
        mgr, store = build()
        mgr._connections["srv"].session.call_tool.return_value = result

        await mgr.call_tool("srv", "read", {"_context_query": QUERY})

        ((source, accounting, _strategy, is_error),) = _rows(store, "read")
        assert (source, accounting, is_error) == (None, None, 0)


class TestCacheHit:
    async def test_a_hit_adds_no_row(self, build):
        tool = SimpleNamespace(
            name="read",
            annotations=SimpleNamespace(read_only_hint=True, destructive_hint=None),
        )
        mgr, store = build(with_cache=True, tools=[tool])
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result()
        arguments = {"q": "a", "_context_query": QUERY}

        await mgr.call_tool("srv", "read", dict(arguments))
        await mgr.call_tool("srv", "read", dict(arguments))

        # The second call was served from the cache…
        assert session.call_tool.call_count == 1
        # …and wrote no row, stamped or not.
        rows = store._db.execute(
            "SELECT context_query_source FROM proxy_metrics WHERE tool = 'read'"
        ).fetchall()
        assert rows == [("agent",)]

    async def test_a_different_query_misses_the_cache_and_is_counted(self, build):
        tool = SimpleNamespace(
            name="read",
            annotations=SimpleNamespace(read_only_hint=True, destructive_hint=None),
        )
        mgr, store = build(with_cache=True, tools=[tool])
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result()

        await mgr.call_tool("srv", "read", {"q": "a", "_context_query": QUERY})
        await mgr.call_tool("srv", "read", {"q": "a", "_context_query": QUERY + " again"})

        assert session.call_tool.call_count == 2
        rows = store._db.execute(
            "SELECT context_query_source FROM proxy_metrics WHERE tool = 'read'"
        ).fetchall()
        assert rows == [("agent",), ("agent",)]


class TestStorage:
    async def test_the_query_text_is_not_stored(self, build):
        mgr, store = build()
        mgr._connections["srv"].session.call_tool.return_value = _text_result()

        await mgr.call_tool("srv", "read", {"_context_query": QUERY})

        (row,) = store._db.execute("SELECT * FROM proxy_metrics").fetchall()
        assert "agent" in row  # the positive control: the stamp is in this row
        assert not any(isinstance(cell, str) and "zebra" in cell for cell in row)

    def test_an_older_db_is_migrated_and_its_rows_stay_unobserved(self, tmp_path):
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE proxy_metrics ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "server TEXT NOT NULL, tool TEXT NOT NULL, "
            "original_chars INTEGER NOT NULL, compressed_chars INTEGER NOT NULL, "
            "cleaned_chars INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO proxy_metrics (server, tool, original_chars, compressed_chars, "
            "created_at) VALUES ('srv', 'old', 10, 10, 1.0)"
        )
        conn.commit()
        conn.close()

        store = MetricsStore(path)
        store.initialize()
        try:
            store.record(
                CallMetrics(
                    server="srv",
                    tool="new",
                    original_chars=10,
                    compressed_chars=10,
                    context_query_source="agent",
                )
            )
            rows = store._db.execute(
                "SELECT tool, context_query_source FROM proxy_metrics ORDER BY id"
            ).fetchall()
        finally:
            store.close()
        assert rows == [("old", None), ("new", "agent")]


def _record(store: MetricsStore, tool: str, value: str | None, *, source: str = "mcp") -> None:
    store.record(
        CallMetrics(
            server="builtin:claude" if source == "hook" else "srv",
            tool=tool,
            original_chars=100,
            compressed_chars=60,
            source=source,
            context_query_source=value,
        )
    )


class TestSummary:
    def _seed(self, path: Path) -> None:
        store = MetricsStore(path)
        store.initialize()
        try:
            _record(store, "read", "agent")
            _record(store, "read", "none")
            _record(store, "read", "none")
            _record(store, "read", None)  # an error or no-text row: unobserved
            _record(store, "other", "agent")  # another tool's call
            # ``mms hook`` never stamps the column; a hook row that carries a
            # value anyway, on the selected tool's name, is still not a proxied
            # call.
            _record(store, "read", "agent", source="hook")
        finally:
            store.close()

    def test_counts_only_stamped_mcp_rows(self, tmp_path):
        path = tmp_path / "m.db"
        self._seed(path)

        assert read_compression_summary(path)["context_query"] == {"observed": 4, "agent": 2}
        assert read_compression_summary(path, source="mcp")["context_query"] == {
            "observed": 4,
            "agent": 2,
        }

    def test_tool_filter(self, tmp_path):
        path = tmp_path / "m.db"
        self._seed(path)

        summary = read_compression_summary(path, tool="read")

        assert summary["context_query"] == {"observed": 3, "agent": 1}

    def test_a_hook_summary_has_no_figure(self, tmp_path):
        path = tmp_path / "m.db"
        self._seed(path)

        summary = read_compression_summary(path, source="hook")

        assert summary["total_calls"] == 1  # the stamped hook row was read…
        assert summary["context_query"] is None  # …and reports no figure

    def test_all_time_has_no_figure(self, tmp_path):
        from memtomem_stm.proxy.metrics_store import read_compression_all_time

        path = tmp_path / "m.db"
        self._seed(path)

        summary = read_compression_all_time(path)

        assert summary["total_calls"] == 6
        assert summary["context_query"] is None

    @pytest.mark.parametrize(
        "filters",
        [{}, {"source": "mcp"}, {"tool": "read"}],
        ids=["unfiltered", "source-mcp", "tool"],
    )
    def test_the_counts_and_the_totals_see_the_same_rows(self, tmp_path, monkeypatch, filters):
        """A row is written each time the reader starts a statement. Every row
        here is a stamped ``agent`` row, so a reader that takes the counts and
        the totals from one snapshot reports the same number three times,
        however many rows landed first; one that reads them apart does not."""
        path = tmp_path / "m.db"
        store = MetricsStore(path)
        store.initialize()
        try:
            for _ in range(3):
                _record(store, "read", "agent")
            real = sqlite3.connect

            def _writer_on_every_statement(*args, **kwargs):
                db = real(*args, **kwargs)
                db.set_trace_callback(lambda _sql: _record(store, "read", "agent"))
                return db

            monkeypatch.setattr(sqlite3, "connect", _writer_on_every_statement)
            summary = read_compression_summary(path, **filters)
        finally:
            store.close()

        seen = summary["total_calls"]
        assert summary["context_query"] == {"observed": seen, "agent": seen}
        # Rows did land while the reader ran: it saw more than were seeded.
        assert seen > 3

    def test_a_db_without_the_column_reads_zero_and_is_left_alone(self, tmp_path):
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE proxy_metrics ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "server TEXT NOT NULL, tool TEXT NOT NULL, "
            "original_chars INTEGER NOT NULL, compressed_chars INTEGER NOT NULL, "
            "cleaned_chars INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO proxy_metrics (server, tool, original_chars, compressed_chars, "
            "created_at) VALUES ('srv', 'old', 10, 10, 1.0)"
        )
        conn.commit()
        conn.close()
        before = path.read_bytes()

        summary = read_compression_summary(path)

        assert summary["total_calls"] == 1
        assert summary["context_query"] == {"observed": 0, "agent": 0}
        assert path.read_bytes() == before
