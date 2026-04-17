"""Tests for progressive (cursor-based) delivery."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from memtomem_stm.proxy.compression import PendingSelection
from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProgressiveConfig,
)
from memtomem_stm.proxy.pending_store import InMemoryPendingStore
from memtomem_stm.proxy.progressive import (
    PROGRESSIVE_FOOTER_TOKEN,
    ProgressiveChunker,
    ProgressiveResponse,
    ProgressiveStoreAdapter,
)
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.formatter import SurfacingFormatter


@dataclass
class _FakeChunkMeta:
    source_file: Path = Path("/notes/memory.md")
    namespace: str = "default"


@dataclass
class _FakeChunk:
    content: str = "a relevant memory"
    metadata: _FakeChunkMeta = field(default_factory=_FakeChunkMeta)


@dataclass
class _FakeResult:
    chunk: _FakeChunk
    score: float = 0.5


# ---------------------------------------------------------------------------
# ProgressiveChunker — boundary detection
# ---------------------------------------------------------------------------


class TestProgressiveChunkerBoundary:
    def test_short_content_passthrough(self):
        chunker = ProgressiveChunker(chunk_size=4000)
        text = "short content"
        result = chunker.first_chunk(text, "key1")
        # Content should be fully included + footer with has_more=False
        assert "short content" in result
        assert "has_more=False" in result

    def test_boundary_prefers_line(self):
        """Should cut at line boundary, not mid-word."""
        lines = ["Line " + str(i) + " " + "x" * 50 for i in range(100)]
        text = "\n".join(lines)
        chunker = ProgressiveChunker(chunk_size=200)
        result = chunker.first_chunk(text, "key1")
        # The content portion (before footer) should end at a line boundary
        content_part = result.split(PROGRESSIVE_FOOTER_TOKEN)[0]
        assert content_part.endswith("\n") or content_part == text[:200]

    def test_boundary_prefers_paragraph(self):
        """Should prefer paragraph boundary (\\n\\n) when available."""
        text = "First paragraph content.\n\nSecond paragraph content.\n\nThird paragraph."
        chunker = ProgressiveChunker(chunk_size=30)
        result = chunker.first_chunk(text, "key1")
        content_part = result.split(PROGRESSIVE_FOOTER_TOKEN)[0]
        # Should cut at the paragraph boundary
        assert content_part.strip().endswith("content.")

    def test_hard_cut_for_long_single_line(self):
        """Falls back to hard cut when no natural boundary exists."""
        text = "a" * 10000  # No spaces, no newlines
        chunker = ProgressiveChunker(chunk_size=4000)
        result = chunker.first_chunk(text, "key1")
        # Should still produce a result without error
        assert "has_more=True" in result

    def test_find_boundary_small_span_detects_paragraph_at_target_minus_one(self):
        """Regression for #69: span<=4 collapsed floor to target, skipping \\n\\n
        at target-1. The search now has at least 1 step of backward room."""
        # target=3, floor_offset=0, span=3. \n\n starts at index 2.
        text = "ab\n\ncd"
        assert ProgressiveChunker._find_boundary(text, 3, floor_offset=0) == 2

    def test_find_boundary_small_span_does_not_regress(self):
        """span<=4 should still find line/word boundaries and fall back cleanly."""
        # No natural boundary within the span — hard cut at target.
        assert ProgressiveChunker._find_boundary("abcdef", 4, floor_offset=0) == 4
        # span=0 (target == floor_offset) is a no-op and must not raise.
        assert ProgressiveChunker._find_boundary("abc", 0, floor_offset=0) == 0

    def test_small_chunk_size_sequential_integrity(self):
        """Regression for #69: chunk_size=10 progressive reads must reproduce
        the full content. Previously the small-span boundary no-op could cause
        overshoots that broke offset arithmetic."""
        text = "First.\n\nSecond.\n\nThird.\n\nFourth.\n\nFifth."
        chunker = ProgressiveChunker(chunk_size=10)

        parts: list[str] = []
        result = chunker.first_chunk(text, "key1")
        content = result.split(PROGRESSIVE_FOOTER_TOKEN)[0]
        parts.append(content)
        offset = len(content)

        for _ in range(50):  # safety bound
            result = chunker.read_chunk(text, offset)
            if "no more content" in result:
                break
            content = result.split(PROGRESSIVE_FOOTER_TOKEN)[0]
            parts.append(content)
            offset += len(content)
            if "has_more=False" in result:
                break

        assert "".join(parts) == text


# ---------------------------------------------------------------------------
# ProgressiveChunker — first_chunk metadata
# ---------------------------------------------------------------------------


class TestProgressiveChunkerFirstChunk:
    def test_first_chunk_metadata(self):
        text = "x" * 10000
        chunker = ProgressiveChunker(chunk_size=4000)
        result = chunker.first_chunk(text, "mykey")
        assert "has_more=True" in result
        assert "mykey" in result
        assert "stm_proxy_read_more" in result
        assert "/10000" in result

    def test_first_chunk_includes_remaining_headings(self):
        sections = [f"## Section {i}\n\nContent for section {i}.\n" for i in range(10)]
        text = "\n".join(sections)
        chunker = ProgressiveChunker(chunk_size=100, include_hint=True)
        result = chunker.first_chunk(text, "key1")
        # Should include heading hints for remaining content
        assert "Remaining:" in result or "Section" in result

    def test_no_hint_when_disabled(self):
        sections = [f"## Section {i}\n\nContent for section {i}.\n" for i in range(10)]
        text = "\n".join(sections)
        chunker = ProgressiveChunker(chunk_size=100, include_hint=False)
        result = chunker.first_chunk(text, "key1")
        assert "Remaining:" not in result


# ---------------------------------------------------------------------------
# ProgressiveChunker — read_chunk
# ---------------------------------------------------------------------------


class TestProgressiveChunkerReadChunk:
    def test_read_chunk_at_offset(self):
        text = "A" * 4000 + "\n" + "B" * 4000 + "\n" + "C" * 2000
        chunker = ProgressiveChunker(chunk_size=4000)
        result = chunker.read_chunk(text, offset=4001)
        assert "B" in result
        assert "has_more=" in result

    def test_read_chunk_past_end(self):
        text = "short"
        chunker = ProgressiveChunker(chunk_size=4000)
        result = chunker.read_chunk(text, offset=1000)
        assert "no more content" in result

    def test_last_chunk_has_more_false(self):
        text = "x" * 100
        chunker = ProgressiveChunker(chunk_size=4000)
        result = chunker.read_chunk(text, offset=0)
        assert "has_more=False" in result

    def test_custom_limit(self):
        text = "x" * 10000
        chunker = ProgressiveChunker(chunk_size=4000)
        result = chunker.read_chunk(text, offset=0, limit=500)
        # Content portion should be approximately 500 chars
        content_part = result.split(PROGRESSIVE_FOOTER_TOKEN)[0]
        assert len(content_part) <= 600  # boundary detection may add a bit


# ---------------------------------------------------------------------------
# ProgressiveChunker — content integrity
# ---------------------------------------------------------------------------


class TestProgressiveContentIntegrity:
    def test_sequential_read_covers_all_content(self):
        """Reading first + all continuations must reproduce the full original text."""
        text = "Line {i}: " + "x" * 80 + "\n"
        text = "".join(f"Line {i}: {'x' * 80}\n" for i in range(200))
        chunker = ProgressiveChunker(chunk_size=500)

        # Collect all content portions (before footer)
        parts: list[str] = []
        offset = 0

        # First chunk
        result = chunker.first_chunk(text, "key1")
        content = result.split(PROGRESSIVE_FOOTER_TOKEN)[0]
        parts.append(content)
        offset = len(content)

        # Read remaining chunks
        for _ in range(100):  # safety limit
            result = chunker.read_chunk(text, offset)
            if "no more content" in result:
                break
            content = result.split(PROGRESSIVE_FOOTER_TOKEN)[0]
            parts.append(content)
            offset += len(content)
            if "has_more=False" in result:
                break

        reassembled = "".join(parts)
        assert reassembled == text

    def test_single_chunk_integrity(self):
        """Short content: first_chunk returns it all with has_more=False."""
        text = "Hello, world!"
        chunker = ProgressiveChunker(chunk_size=4000)
        result = chunker.first_chunk(text, "key1")
        assert "Hello, world!" in result
        assert "has_more=False" in result

    @pytest.mark.parametrize(
        "mode,expected_pass",
        [
            ("append", True),
            ("section", True),
            ("prepend", False),
        ],
    )
    def test_concat_invariant_under_surfacing(self, mode, expected_pass):
        """Per-injection-mode offset invariant when surfacing wraps a progressive
        first chunk. `append` and `section` inject AFTER the chunker footer so
        ``split(PROGRESSIVE_FOOTER_TOKEN)[0]`` concat still recovers the
        original content; `prepend` injects BEFORE and breaks the invariant.

        This pins the per-mode safety so a future reader cannot lift the
        `prepend` bypass in ``ProxyManager`` without updating this assertion.
        """
        text = "".join(f"Line {i}: {'x' * 80}\n" for i in range(200))
        chunker = ProgressiveChunker(chunk_size=500)
        formatter = SurfacingFormatter(SurfacingConfig(injection_mode=mode))
        results = [_FakeResult(_FakeChunk(content="a relevant memory"))]

        first_response = chunker.first_chunk(text, "key1")
        surfaced_first = formatter.inject(first_response, results, query="q")

        parts: list[str] = [surfaced_first.split(PROGRESSIVE_FOOTER_TOKEN)[0]]
        offset = len(parts[0])

        for _ in range(100):  # safety bound
            result = chunker.read_chunk(text, offset)
            if "no more content" in result:
                break
            chunk_content = result.split(PROGRESSIVE_FOOTER_TOKEN)[0]
            parts.append(chunk_content)
            offset += len(chunk_content)
            if "has_more=False" in result:
                break

        concat_matches = "".join(parts) == text
        assert concat_matches is expected_pass, (
            f"injection_mode={mode!r}: expected concat=={expected_pass}, "
            f"got concat=={concat_matches}"
        )

    @pytest.mark.parametrize(
        "content_label,text",
        [
            (
                "markdown_horizontal_rule",
                "Intro paragraph.\n\n---\n\nAfter the rule.\n\n"
                + "".join(f"Line {i}: filler text here\n" for i in range(80)),
            ),
            (
                "yaml_frontmatter",
                "---\ntitle: Doc\nauthor: X\n---\n\nBody paragraph.\n\n"
                + "".join(f"Line {i}: filler text here\n" for i in range(80)),
            ),
            (
                "triple_dash_bracket_not_progressive",
                "Start.\n\n---\n[note: an annotation block]\n\nMiddle.\n\n"
                + "".join(f"Line {i}: filler text here\n" for i in range(80)),
            ),
            (
                # Forces a trailing ``\n---\n`` in content so reassembly sees
                # two consecutive ``\n---\n`` sequences (content's HR + the
                # footer). Canonical split must land on the footer, not the
                # trailing content HR.
                "content_ending_in_triple_dash_before_footer",
                "".join(f"Line {i}: filler text here\n" for i in range(80))
                + "\n---\n",
            ),
        ],
    )
    def test_dangerous_content_reassembles_with_canonical_token(
        self, content_label, text
    ):
        """Content embedding ``\\n---\\n`` sequences (markdown HR, YAML
        frontmatter, lookalike brackets, trailing HR) must still round-trip
        byte-for-byte when agents split on :data:`PROGRESSIVE_FOOTER_TOKEN`.

        Regression for issue #160: the older ``split("\\n---\\n")[0]`` rule
        cut inside content on the first embedded ``\\n---\\n`` and silently
        dropped the rest of the chunk.
        """
        chunker = ProgressiveChunker(chunk_size=250)

        parts: list[str] = []
        result = chunker.first_chunk(text, f"key-{content_label}")
        content = result.split(PROGRESSIVE_FOOTER_TOKEN)[0]
        parts.append(content)
        offset = len(content)

        for _ in range(200):  # safety bound
            result = chunker.read_chunk(text, offset)
            if "no more content" in result:
                break
            chunk_content = result.split(PROGRESSIVE_FOOTER_TOKEN)[0]
            parts.append(chunk_content)
            offset += len(chunk_content)
            if "has_more=False" in result:
                break

        assert "".join(parts) == text, (
            f"{content_label}: reassembled content does not match original"
        )

    def test_legacy_split_fails_on_embedded_triple_dash(self):
        """Pins the issue #160 failure mode so a future reader cannot
        reintroduce the weaker ``split("\\n---\\n")[0]`` convention without
        noticing: on content containing a markdown horizontal rule, the
        legacy rule drops bytes while the canonical rule preserves them.
        """
        text = (
            "Intro paragraph.\n\n---\n\nContent after the rule.\n\n"
            + "x" * 500
        )
        chunker = ProgressiveChunker(chunk_size=4000)
        result = chunker.first_chunk(text, "key-legacy")

        legacy_content = result.split("\n---\n")[0]
        canonical_content = result.split(PROGRESSIVE_FOOTER_TOKEN)[0]

        # Legacy rule cuts at the first ``\n---\n`` — the markdown HR inside
        # the content — and drops everything after it, including the
        # "Content after the rule." paragraph and the 500-char body.
        assert legacy_content == "Intro paragraph.\n"
        # Canonical rule preserves the full chunk content intact.
        assert canonical_content.startswith("Intro paragraph.\n\n---\n\n")
        assert "Content after the rule." in canonical_content
        assert len(canonical_content) > len(legacy_content) + 500


# ---------------------------------------------------------------------------
# ProgressiveStoreAdapter
# ---------------------------------------------------------------------------


class TestProgressiveStoreAdapter:
    def test_put_get_roundtrip(self):
        store = ProgressiveStoreAdapter(InMemoryPendingStore())
        resp = ProgressiveResponse(
            content="hello world",
            total_chars=11,
            total_lines=1,
            content_type="text",
            structure_hint="1 lines",
            created_at=time.monotonic(),
        )
        store.put("key1", resp)
        got = store.get("key1")
        assert got is not None
        assert got.content == "hello world"
        assert got.total_chars == 11
        assert got.content_type == "text"

    def test_missing_key_returns_none(self):
        store = ProgressiveStoreAdapter(InMemoryPendingStore())
        assert store.get("nonexistent") is None

    def test_missing_content_key_returns_none(self, caplog):
        """Entry without __content__ should be treated as miss, not crash."""
        backing = InMemoryPendingStore()
        # Inject a progressive-format entry missing __content__
        backing.put(
            "broken",
            PendingSelection(
                chunks={"__meta__": "{}"},  # no __content__
                format="progressive",
                created_at=time.monotonic(),
                total_chars=0,
            ),
        )
        store = ProgressiveStoreAdapter(backing)
        with caplog.at_level("WARNING"):
            assert store.get("broken") is None
        assert any("missing __content__" in r.message for r in caplog.records)

    def test_corrupted_meta_uses_defaults(self, caplog):
        """Corrupted __meta__ JSON should log and fall back to defaults."""
        backing = InMemoryPendingStore()
        backing.put(
            "bad_meta",
            PendingSelection(
                chunks={"__content__": "hello", "__meta__": "{not json"},
                format="progressive",
                created_at=time.monotonic(),
                total_chars=5,
            ),
        )
        store = ProgressiveStoreAdapter(backing)
        with caplog.at_level("WARNING"):
            got = store.get("bad_meta")
        assert got is not None
        assert got.content == "hello"
        assert got.content_type == "text"  # default
        assert any("Corrupted __meta__ JSON" in r.message for r in caplog.records)

    def test_touch_does_not_error(self):
        store = ProgressiveStoreAdapter(InMemoryPendingStore())
        resp = ProgressiveResponse(
            content="x",
            total_chars=1,
            total_lines=1,
            content_type="text",
            structure_hint="",
            created_at=time.monotonic(),
        )
        store.put("key1", resp)
        store.touch("key1")  # should not raise

    def test_delete(self):
        store = ProgressiveStoreAdapter(InMemoryPendingStore())
        resp = ProgressiveResponse(
            content="x",
            total_chars=1,
            total_lines=1,
            content_type="text",
            structure_hint="",
            created_at=time.monotonic(),
        )
        store.put("key1", resp)
        store.delete("key1")
        assert store.get("key1") is None

    def test_does_not_interfere_with_selective(self):
        """Progressive and selective entries can coexist in the same store."""
        backend = InMemoryPendingStore()
        adapter = ProgressiveStoreAdapter(backend)

        # Store a progressive entry
        adapter.put(
            "prog1",
            ProgressiveResponse(
                content="progressive",
                total_chars=11,
                total_lines=1,
                content_type="text",
                structure_hint="",
                created_at=time.monotonic(),
            ),
        )

        # Store a selective entry directly
        backend.put(
            "sel1",
            PendingSelection(
                chunks={"intro": "Intro content"},
                format="markdown",
                created_at=time.monotonic(),
                total_chars=100,
            ),
        )

        # Progressive retrieval works
        assert adapter.get("prog1") is not None
        assert adapter.get("prog1").content == "progressive"

        # Selective entry is ignored by adapter (format != "progressive")
        assert adapter.get("sel1") is None

        # But still accessible via backend
        assert backend.get("sel1") is not None


# ---------------------------------------------------------------------------
# Content type detection
# ---------------------------------------------------------------------------


class TestContentTypeDetection:
    def test_json(self):
        assert ProgressiveChunker.detect_content_type('{"key": "value"}') == "json"

    def test_json_array(self):
        assert ProgressiveChunker.detect_content_type("[1, 2, 3]") == "json"

    def test_markdown(self):
        assert ProgressiveChunker.detect_content_type("# Title\nContent") == "markdown"

    def test_code(self):
        assert ProgressiveChunker.detect_content_type("def foo():\n    pass") == "code"

    def test_plain_text(self):
        assert ProgressiveChunker.detect_content_type("Just some plain text.") == "text"


# ---------------------------------------------------------------------------
# Structure hint
# ---------------------------------------------------------------------------


class TestStructureHint:
    def test_markdown_headings_counted(self):
        text = "# H1\n## H2\n### H3\nContent"
        hint = ProgressiveChunker.structure_hint(text)
        assert "3 headings" in hint

    def test_code_blocks_counted(self):
        text = "```python\ncode\n```\nmore\n```js\ncode\n```"
        hint = ProgressiveChunker.structure_hint(text)
        assert "2 code blocks" in hint

    def test_line_count(self):
        text = "line1\nline2\nline3"
        hint = ProgressiveChunker.structure_hint(text)
        assert "3 lines" in hint


# ---------------------------------------------------------------------------
# ProgressiveConfig
# ---------------------------------------------------------------------------


class TestProgressiveConfig:
    def test_defaults(self):
        cfg = ProgressiveConfig()
        assert cfg.chunk_size == 4000
        assert cfg.max_stored == 200
        assert cfg.ttl_seconds == 1800.0
        assert cfg.include_structure_hint is True

    def test_strategy_enum_includes_progressive(self):
        assert "progressive" in set(CompressionStrategy)


# ---------------------------------------------------------------------------
# TTL exposure in footer
# ---------------------------------------------------------------------------


class TestProgressiveTTL:
    def test_first_chunk_includes_ttl(self):
        """First chunk footer must expose TTL when provided."""
        chunker = ProgressiveChunker(chunk_size=100)
        text = "x" * 500
        result = chunker.first_chunk(text, "key1", ttl_seconds=300.0)
        assert "ttl=300s" in result

    def test_read_chunk_includes_ttl(self):
        """Continuation chunk footer must expose TTL when provided."""
        chunker = ProgressiveChunker(chunk_size=100)
        text = "x" * 500
        result = chunker.read_chunk(text, offset=0, key="key1", ttl_seconds=1800.0)
        assert "ttl=1800s" in result

    def test_ttl_omitted_when_none(self):
        """Footer must not include ttl field when ttl_seconds is None."""
        chunker = ProgressiveChunker(chunk_size=100)
        text = "x" * 500
        result = chunker.first_chunk(text, "key1")
        assert "ttl=" not in result

    def test_ttl_omitted_on_last_chunk(self):
        """Last chunk (has_more=False) should not show TTL — nothing left to retrieve."""
        chunker = ProgressiveChunker(chunk_size=4000)
        text = "short"
        result = chunker.first_chunk(text, "key1", ttl_seconds=300.0)
        assert "has_more=False" in result
        assert "ttl=" not in result

    def test_store_adapter_preserves_ttl(self):
        """ProgressiveStoreAdapter must round-trip ttl_seconds."""
        store = ProgressiveStoreAdapter(InMemoryPendingStore())
        resp = ProgressiveResponse(
            content="hello",
            total_chars=5,
            total_lines=1,
            content_type="text",
            structure_hint="1 lines",
            created_at=time.monotonic(),
            ttl_seconds=600.0,
        )
        store.put("key1", resp)
        got = store.get("key1")
        assert got is not None
        assert got.ttl_seconds == 600.0


# ---------------------------------------------------------------------------
# Remaining headings hint
# ---------------------------------------------------------------------------


class TestRemainingHeadings:
    def test_shows_up_to_5_headings(self):
        headings = [f"## Heading {i}\n\nContent\n" for i in range(10)]
        text = "\n".join(headings)
        result = ProgressiveChunker._remaining_headings(text, 0)
        assert "Heading 0" in result
        assert "+5 more" in result

    def test_empty_when_no_headings(self):
        assert ProgressiveChunker._remaining_headings("plain text only", 0) == ""
