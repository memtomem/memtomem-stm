"""Tests for ``proxy.memory_ops`` — auto-index and fact extraction.

Zero dedicated tests before this file (issue #42). The module does file
I/O, frontmatter generation, namespace formatting, dedup-via-indexer,
and swallows several exception classes — all of which are silent-failure
paths waiting to regress.

Uses stubbed FileIndexer / FactExtractor so no real LLM or indexer is
touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from memtomem_stm.proxy.config import AutoIndexConfig, ExtractionConfig
from memtomem_stm.proxy.extraction import ExtractedFact
from memtomem_stm.proxy.memory_ops import (
    AutoIndexOutcome,
    ExtractOutcome,
    auto_index_response,
    extract_and_store,
    format_fact_md,
)
from memtomem_stm.proxy.protocols import IndexResult


# ── Stubs ────────────────────────────────────────────────────────────────


@dataclass
class FakeIndexer:
    """Records index_file() calls; optionally raises; supports dedup flag."""

    chunks_per_file: int = 3
    raise_on_index: bool = False
    raise_on_dedup: bool = False
    dedup_positive_substrings: tuple[str, ...] = ()
    indexed_paths: list[tuple[Path, str | None]] = field(default_factory=list)
    dedup_calls: list[tuple[str, str | None, float]] = field(default_factory=list)

    async def index_file(
        self, path: Path, *, force: bool = False, namespace: str | None = None
    ) -> IndexResult:
        if self.raise_on_index:
            raise RuntimeError("simulated indexing failure")
        self.indexed_paths.append((path, namespace))
        return IndexResult(indexed_chunks=self.chunks_per_file)

    async def is_duplicate(
        self, text: str, *, namespace: str | None = None, threshold: float = 0.92
    ) -> bool:
        self.dedup_calls.append((text, namespace, threshold))
        if self.raise_on_dedup:
            raise RuntimeError("simulated dedup failure")
        return any(s in text for s in self.dedup_positive_substrings)


class FakeExtractor:
    def __init__(self, facts: list[ExtractedFact] | None = None, *, raises: bool = False):
        self._facts = facts or []
        self._raises = raises
        self.calls: list[tuple[str, str, str]] = []

    async def extract(self, text: str, *, server: str, tool: str) -> list[ExtractedFact]:
        self.calls.append((text, server, tool))
        if self._raises:
            raise RuntimeError("simulated extraction failure")
        return self._facts


# ── auto_index_response ──────────────────────────────────────────────────


class TestAutoIndexResponse:
    @pytest.fixture
    def config(self, tmp_path) -> AutoIndexConfig:
        return AutoIndexConfig(
            enabled=True,
            memory_dir=tmp_path / "proxy_index",
            namespace="proxy-{server}",
        )

    async def test_writes_markdown_with_frontmatter(self, config):
        indexer = FakeIndexer()
        outcome = await auto_index_response(
            indexer,
            config,
            server="gh",
            tool="read_file",
            arguments={"path": "src/app.py"},
            text="file body contents",
            agent_summary="1 paragraph summary",
            compression_strategy="selective",
            original_chars=12000,
            compressed_chars=3000,
            context_query="what does app.py do?",
        )

        assert isinstance(outcome, AutoIndexOutcome)
        # Indexer was called with the file path under memory_dir and the
        # namespace formatted from the template.
        assert len(indexer.indexed_paths) == 1
        written_path, ns = indexer.indexed_paths[0]
        assert ns == "proxy-gh"
        assert written_path.parent == config.memory_dir.expanduser().resolve()
        assert written_path.exists()

        body = written_path.read_text(encoding="utf-8")
        # Frontmatter present with the compression metadata the call passed in.
        assert body.startswith("---\n")
        assert "source: proxy/gh/read_file" in body
        assert "compression: selective" in body
        assert "original_chars: 12000" in body
        assert "compressed_chars: 3000" in body
        # Context query rendered as a blockquote in the Agent Intent section.
        assert "## Agent Intent" in body
        assert "> what does app.py do?" in body
        assert "file body contents" in body

        # Summary includes compression headline and wraps the agent summary.
        assert "[Indexed]" in outcome.summary
        assert "gh/read_file" in outcome.summary
        assert "3 chunks" in outcome.summary
        assert "1 paragraph summary" in outcome.summary
        # Status fields record the successful index call.
        assert outcome.ok is True
        assert outcome.chunks_indexed == 3
        assert outcome.error is None

    async def test_omits_optional_frontmatter_fields(self, config):
        indexer = FakeIndexer()
        await auto_index_response(
            indexer,
            config,
            server="s",
            tool="t",
            arguments={},
            text="body",
            agent_summary="sum",
        )
        body = indexer.indexed_paths[0][0].read_text(encoding="utf-8")
        assert "compression:" not in body
        assert "original_chars:" not in body
        assert "## Agent Intent" not in body

    async def test_index_failure_returns_zero_chunks(self, config, caplog):
        """Indexer raising must not propagate — log a warning, return
        summary with 0 chunks, and report ``ok=False`` with a populated
        ``error`` so the operator metrics dashboard sees the failure
        (previously swallowed silently)."""
        indexer = FakeIndexer(raise_on_index=True)
        with caplog.at_level("WARNING", logger="memtomem_stm.proxy.memory_ops"):
            outcome = await auto_index_response(
                indexer,
                config,
                server="s",
                tool="t",
                arguments={},
                text="body",
                agent_summary="sum",
            )
        assert "0 chunks" in outcome.summary
        assert outcome.ok is False
        assert outcome.chunks_indexed == 0
        assert outcome.error is not None
        # Error string carries the exception class + message so dashboards
        # can distinguish categories (Timeout vs Permission vs …).
        assert "RuntimeError" in outcome.error
        assert "simulated indexing failure" in outcome.error
        assert any("Auto-index failed" in r.message for r in caplog.records)
        rec = next(r for r in caplog.records if "Auto-index failed" in r.message)
        assert rec.exc_info is not None, "exc_info required for traceback diagnosis"

    async def test_creates_memory_dir_when_missing(self, tmp_path):
        """``memory_dir`` is created — passing a nested path that doesn't
        exist yet must not error."""
        config = AutoIndexConfig(memory_dir=tmp_path / "deep" / "nested" / "dir")
        indexer = FakeIndexer()
        await auto_index_response(
            indexer,
            config,
            server="s",
            tool="t",
            arguments={},
            text="body",
            agent_summary="sum",
        )
        assert config.memory_dir.expanduser().resolve().exists()

    async def test_tool_with_slash_is_sanitized_in_filename(self, config):
        """Tool names with ``/`` are legal in MCP; filenames aren't. The
        sanitization replaces ``/`` with ``_``. A regression here would
        create paths like ``server__sub/tool__ts.md`` that fail on Windows."""
        indexer = FakeIndexer()
        await auto_index_response(
            indexer,
            config,
            server="s",
            tool="ns/inner",
            arguments={},
            text="body",
            agent_summary="sum",
        )
        written_path, _ = indexer.indexed_paths[0]
        assert "/" not in written_path.name
        assert "ns_inner" in written_path.name


# ── extract_and_store ────────────────────────────────────────────────────


class TestExtractAndStore:
    @pytest.fixture
    def config(self, tmp_path) -> ExtractionConfig:
        return ExtractionConfig(
            enabled=True,
            memory_dir=tmp_path / "facts",
            namespace="facts-{server}",
            max_facts=10,
            dedup_threshold=0.92,
        )

    def _fact(self, content: str, *, category: str = "technical", tags: list[str] | None = None):
        return ExtractedFact(
            content=content,
            category=category,
            confidence=0.8,
            tags=tags or [],
        )

    async def test_writes_individual_fact_files_and_indexes(self, config):
        facts = [
            self._fact("Fact A about flask"),
            self._fact("Fact B about redis"),
            self._fact("Fact C about postgres"),
        ]
        extractor = FakeExtractor(facts)
        indexer = FakeIndexer()

        await extract_and_store(
            indexer,
            extractor,
            config,
            server="gh",
            tool="read_file",
            arguments={"path": "src/app.py"},
            text="long response text",
        )

        assert len(indexer.indexed_paths) == 3
        # Each fact was indexed under the formatted namespace.
        assert all(ns == "facts-gh" for _, ns in indexer.indexed_paths)
        # Each fact file exists on disk with its content.
        for path, _ in indexer.indexed_paths:
            content = path.read_text(encoding="utf-8")
            assert any(fact.content in content for fact in facts)

    async def test_dedup_skips_duplicates(self, config):
        facts = [
            self._fact("original insight"),
            self._fact("already seen fact"),
            self._fact("another original insight"),
        ]
        extractor = FakeExtractor(facts)
        indexer = FakeIndexer(dedup_positive_substrings=("already seen",))

        await extract_and_store(
            indexer,
            extractor,
            config,
            server="s",
            tool="t",
            arguments={},
            text="body",
        )

        # 3 dedup calls, 2 indexed (the seen one is skipped)
        assert len(indexer.dedup_calls) == 3
        assert len(indexer.indexed_paths) == 2

    async def test_max_facts_limit_respected(self, config):
        """Only the first ``max_facts`` facts are written to disk."""
        config.max_facts = 2
        facts = [self._fact(f"fact-{i}") for i in range(5)]
        extractor = FakeExtractor(facts)
        indexer = FakeIndexer()

        await extract_and_store(
            indexer,
            extractor,
            config,
            server="s",
            tool="t",
            arguments={},
            text="body",
        )
        assert len(indexer.indexed_paths) == 2

    async def test_dedup_failure_proceeds_with_indexing(self, config):
        """``is_duplicate`` raising is swallowed — the fact is still written
        and indexed. If dedup is flaky, we must not drop facts entirely."""
        facts = [self._fact("will be indexed despite dedup crash")]
        extractor = FakeExtractor(facts)
        indexer = FakeIndexer(raise_on_dedup=True)

        await extract_and_store(
            indexer,
            extractor,
            config,
            server="s",
            tool="t",
            arguments={},
            text="body",
        )
        assert len(indexer.indexed_paths) == 1

    async def test_no_indexer_writes_files_only(self, config):
        """``index_engine=None`` → write fact files to disk but skip indexing.
        This is the 'preserve for later ingestion' mode."""
        facts = [self._fact("standalone fact")]
        extractor = FakeExtractor(facts)

        await extract_and_store(
            None,
            extractor,
            config,
            server="s",
            tool="t",
            arguments={},
            text="body",
        )
        written = list(config.memory_dir.expanduser().resolve().glob("*.md"))
        assert len(written) == 1
        assert "standalone fact" in written[0].read_text(encoding="utf-8")

    async def test_extraction_failure_is_swallowed(self, config, caplog):
        """``extractor.extract`` raising must be caught — a broken LLM path
        cannot break the tool response flow."""
        extractor = FakeExtractor(raises=True)
        indexer = FakeIndexer()

        with caplog.at_level("WARNING", logger="memtomem_stm.proxy.memory_ops"):
            await extract_and_store(
                indexer,
                extractor,
                config,
                server="s",
                tool="t",
                arguments={},
                text="body",
            )

        assert len(indexer.indexed_paths) == 0
        assert any("Fact extraction failed" in r.message for r in caplog.records)

    async def test_fact_indexing_failure_logs_traceback(self, config, caplog):
        """Fact indexing failure must include exc_info for root-cause diagnosis."""
        facts = [self._fact("will fail to index")]
        extractor = FakeExtractor(facts)
        indexer = FakeIndexer(raise_on_index=True)

        with caplog.at_level("WARNING", logger="memtomem_stm.proxy.memory_ops"):
            await extract_and_store(
                indexer,
                extractor,
                config,
                server="s",
                tool="t",
                arguments={},
                text="body",
            )

        rec = next(r for r in caplog.records if "Fact indexing failed" in r.message)
        assert rec.exc_info is not None, "exc_info required for traceback diagnosis"

    async def test_empty_facts_does_nothing(self, config):
        extractor = FakeExtractor([])
        indexer = FakeIndexer()
        await extract_and_store(
            indexer,
            extractor,
            config,
            server="s",
            tool="t",
            arguments={},
            text="body",
        )
        assert len(indexer.indexed_paths) == 0
        assert not config.memory_dir.expanduser().resolve().exists() or not list(
            config.memory_dir.expanduser().resolve().glob("*.md")
        )


# ── ExtractOutcome contract ──────────────────────────────────────────────


class TestExtractOutcome:
    """Outcomes exist so operators see extraction status in proxy_metrics.db.

    Without these, a broken LLM or disk full would silently drop facts
    while the metrics row stayed healthy — the gap F2 is closing.
    """

    @pytest.fixture
    def config(self, tmp_path) -> ExtractionConfig:
        return ExtractionConfig(
            enabled=True,
            memory_dir=tmp_path / "facts",
            namespace="facts-{server}",
            max_facts=10,
            dedup_threshold=0.92,
        )

    def _fact(self, content: str):
        return ExtractedFact(content=content, category="c", confidence=0.8, tags=[])

    async def test_success_reports_ok_and_count(self, config):
        facts = [self._fact("a"), self._fact("b")]
        outcome = await extract_and_store(
            FakeIndexer(),
            FakeExtractor(facts),
            config,
            server="s",
            tool="t",
            arguments={},
            text="body",
        )
        assert isinstance(outcome, ExtractOutcome)
        assert outcome.ok is True
        assert outcome.facts_stored == 2
        assert outcome.error is None

    async def test_extractor_failure_reports_not_ok_with_error(self, config):
        outcome = await extract_and_store(
            FakeIndexer(),
            FakeExtractor(raises=True),
            config,
            server="s",
            tool="t",
            arguments={},
            text="body",
        )
        assert outcome.ok is False
        assert outcome.facts_stored == 0
        assert outcome.error is not None
        # Error string carries class + message for dashboard categorization.
        assert "RuntimeError" in outcome.error
        assert "simulated extraction failure" in outcome.error

    async def test_partial_indexing_failure_stays_ok(self, config):
        """Per-fact indexing failures reduce ``facts_stored`` but don't
        flip ``ok`` — the pre-F2 contract was "partial failure returns
        normally" and we preserve that."""
        facts = [self._fact("a"), self._fact("b")]
        outcome = await extract_and_store(
            FakeIndexer(raise_on_index=True),
            FakeExtractor(facts),
            config,
            server="s",
            tool="t",
            arguments={},
            text="body",
        )
        assert outcome.ok is True  # outer phase completed
        assert outcome.facts_stored == 0  # every index_file call failed
        assert outcome.error is None

    async def test_empty_facts_reports_ok_zero(self, config):
        outcome = await extract_and_store(
            FakeIndexer(),
            FakeExtractor([]),
            config,
            server="s",
            tool="t",
            arguments={},
            text="body",
        )
        assert outcome.ok is True
        assert outcome.facts_stored == 0


# ── format_fact_md ───────────────────────────────────────────────────────


class TestFormatFactMd:
    def test_full_fact_with_tags(self):
        fact = ExtractedFact(
            content="Redis is used as a cache layer with LRU eviction.",
            category="technical",
            confidence=0.85,
            tags=["redis", "cache"],
        )
        md = format_fact_md(fact, "s", "t", {"key": "val"})
        assert "category: technical" in md
        assert "confidence: 0.85" in md
        assert "tags: [redis, cache]" in md
        assert "Redis is used as a cache layer" in md
        assert "extracted_from: s/t(key='val')" in md

    def test_empty_tags_omits_tags_line(self):
        fact = ExtractedFact(content="Fact", category="c", confidence=0.5, tags=[])
        md = format_fact_md(fact, "s", "t", {})
        assert "tags:" not in md

    def test_title_truncates_at_80_chars(self):
        """Titles longer than 80 chars are truncated; the truncation is
        deterministic so generated filenames and cross-links stay stable."""
        long_content = "A" * 120
        fact = ExtractedFact(content=long_content, category="c", confidence=0.5)
        md = format_fact_md(fact, "s", "t", {})
        title_line = next(ln for ln in md.splitlines() if ln.startswith("## "))
        # "## " + 80 chars = 83 — trailing period (if any) is stripped too.
        assert len(title_line) <= 83
        assert title_line.startswith("## " + "A" * 80)

    def test_no_arguments_renders_none_marker(self):
        fact = ExtractedFact(content="Fact", category="c", confidence=0.5)
        md = format_fact_md(fact, "s", "t", {})
        assert "extracted_from: s/t((none))" in md
