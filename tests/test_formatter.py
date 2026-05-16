"""Tests for SurfacingFormatter — memory injection into tool responses."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.formatter import SurfacingFormatter


@dataclass
class FakeChunkMeta:
    source_file: Path = Path("/notes/test.md")
    namespace: str = "default"


@dataclass
class FakeChunk:
    id: str = ""
    content: str = "memory content here"
    metadata: FakeChunkMeta | None = None

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid4())
        if self.metadata is None:
            self.metadata = FakeChunkMeta()


@dataclass
class FakeResult:
    chunk: FakeChunk
    score: float


class TestFormatterInjection:
    def test_prepend_mode(self):
        fmt = SurfacingFormatter(SurfacingConfig(injection_mode="prepend"))
        results = [FakeResult(FakeChunk(content="remember this"), 0.5)]
        output = fmt.inject("original response", results, "query", surfacing_id="abc123")
        assert output.startswith("<surfaced-memories>")
        assert "## Relevant Memories" in output
        assert "original response" in output
        assert output.index("remember this") < output.index("original response")

    def test_append_mode(self):
        fmt = SurfacingFormatter(SurfacingConfig(injection_mode="append"))
        results = [FakeResult(FakeChunk(content="appended memory"), 0.5)]
        output = fmt.inject("original response", results, "query")
        assert output.startswith("original response")
        assert "appended memory" in output

    def test_section_mode(self):
        fmt = SurfacingFormatter(SurfacingConfig(injection_mode="section"))
        results = [FakeResult(FakeChunk(content="section memory"), 0.5)]
        output = fmt.inject("original response", results, "query")
        assert "## Relevant Memories" in output

    def test_empty_results_returns_original(self):
        fmt = SurfacingFormatter(SurfacingConfig())
        output = fmt.inject("original", [], "query")
        assert output == "original"

    def test_source_badge_format(self):
        fmt = SurfacingFormatter(SurfacingConfig())
        chunk = FakeChunk(content="test content")
        chunk.metadata = FakeChunkMeta(source_file=Path("/notes/deploy.md"), namespace="work")
        results = [FakeResult(chunk, 0.42)]
        output = fmt.inject("response", results, "query")
        assert "deploy.md" in output
        assert "[work]" in output
        assert "0.42" in output

    def test_surfacing_id_included(self):
        fmt = SurfacingFormatter(SurfacingConfig())
        results = [FakeResult(FakeChunk(), 0.5)]
        output = fmt.inject("response", results, "query", surfacing_id="xyz789")
        assert "xyz789" in output
        assert "stm_surfacing_feedback" in output

    def test_scratch_items_included(self):
        fmt = SurfacingFormatter(SurfacingConfig())
        results = [FakeResult(FakeChunk(), 0.5)]
        scratch = [{"key": "current_task", "value": "testing memtomem"}]
        output = fmt.inject("response", results, "query", scratch_items=scratch)
        assert "Working Memory" in output
        assert "current_task" in output
        assert "testing memtomem" in output

    def test_injection_size_capped(self):
        """Memory block is truncated when it exceeds max_injection_chars."""
        config = SurfacingConfig(max_injection_chars=100)
        fmt = SurfacingFormatter(config)
        # Create results with very long content
        results = [FakeResult(FakeChunk(content="x" * 500), 0.5)]
        output = fmt.inject("response", results, "query")
        # The memory block should be capped
        assert "truncated" in output

    def test_custom_section_header(self):
        fmt = SurfacingFormatter(SurfacingConfig(section_header="## 관련 기억"))
        results = [FakeResult(FakeChunk(), 0.5)]
        output = fmt.inject("response", results, "query")
        assert "## 관련 기억" in output


class TestPreviewMaxCharsKnob:
    """F3 — ``preview_max_chars`` controls the per-result preview slice.

    Default (300) preserves prior behavior; operators can shorten or
    lengthen the preview per deployment.
    """

    def test_default_preview_caps_at_300(self):
        """Regression guard: existing 300-cap behavior must survive when
        ``preview_max_chars`` is not overridden."""
        fmt = SurfacingFormatter(SurfacingConfig())
        long_content = "a" * 800
        results = [FakeResult(FakeChunk(content=long_content), 0.5)]
        output = fmt.inject("response", results, "query")
        # The preview is the slice that follows the "score=X.XX): " marker
        marker = "score=0.50): "
        idx = output.index(marker) + len(marker)
        preview_line = output[idx:].split("\n", 1)[0]
        assert len(preview_line) <= 300
        assert "a" * 250 in preview_line  # well within the cap

    def test_lower_preview_max_chars_truncates(self):
        fmt = SurfacingFormatter(SurfacingConfig(preview_max_chars=50))
        results = [FakeResult(FakeChunk(content="b" * 800), 0.5)]
        output = fmt.inject("response", results, "query")
        marker = "score=0.50): "
        idx = output.index(marker) + len(marker)
        preview_line = output[idx:].split("\n", 1)[0]
        assert len(preview_line) <= 50
        # Tighter check: exactly 50 'b's then nothing more on that line
        assert preview_line.startswith("b" * 50)
        assert "b" * 51 not in preview_line

    def test_higher_preview_max_chars_allows_longer_preview(self):
        fmt = SurfacingFormatter(SurfacingConfig(preview_max_chars=600, max_injection_chars=10000))
        results = [FakeResult(FakeChunk(content="c" * 800), 0.5)]
        output = fmt.inject("response", results, "query")
        marker = "score=0.50): "
        idx = output.index(marker) + len(marker)
        preview_line = output[idx:].split("\n", 1)[0]
        assert len(preview_line) >= 600
        assert "c" * 600 in preview_line
