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


def _preview_line_after_bucket(output: str, bucket: str = "related") -> str:
    marker = f"[{bucket}]: "
    idx = output.index(marker) + len(marker)
    return output[idx:].split("\n", 1)[0]


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
        assert "[related]" in output
        assert "score=" not in output
        assert "0.42" not in output

    def test_relevance_bucket_labels_replace_raw_scores(self):
        fmt = SurfacingFormatter(SurfacingConfig())
        results = [
            FakeResult(FakeChunk(content="near floor"), 0.04),
            FakeResult(FakeChunk(content="middle band"), 0.50),
            FakeResult(FakeChunk(content="top band"), 0.95),
        ]
        output = fmt.inject("response", results, "query")
        assert "- **notes/test.md** [default] [weak]: near floor" in output
        assert "- **notes/test.md** [default] [related]: middle band" in output
        assert "- **notes/test.md** [default] [strong]: top band" in output
        assert "score=" not in output
        assert "0.04" not in output
        assert "0.50" not in output
        assert "0.95" not in output

    def test_relevance_bucket_boundaries_follow_min_score(self):
        fmt = SurfacingFormatter(SurfacingConfig(min_score=0.4))
        results = [
            FakeResult(FakeChunk(content="custom weak"), 0.55),
            FakeResult(FakeChunk(content="custom related"), 0.70),
            FakeResult(FakeChunk(content="custom strong"), 0.90),
        ]
        output = fmt.inject("response", results, "query")
        assert "- **notes/test.md** [default] [weak]: custom weak" in output
        assert "- **notes/test.md** [default] [related]: custom related" in output
        assert "- **notes/test.md** [default] [strong]: custom strong" in output

    def test_source_renders_parent_and_basename(self):
        fmt = SurfacingFormatter(SurfacingConfig())
        chunk = FakeChunk(content="ambiguous auth note")
        chunk.metadata = FakeChunkMeta(
            source_file=Path("/notes/2026-q1/auth.md"),
            namespace="default",
        )
        results = [FakeResult(chunk, 0.42)]

        output = fmt.inject("response", results, "query")

        assert "**2026-q1/auth.md**" in output
        assert "**auth.md**" not in output
        assert "notes/2026-q1/auth.md" not in output

    def test_top_level_source_renders_basename_only(self):
        fmt = SurfacingFormatter(SurfacingConfig())
        chunk = FakeChunk(content="top level auth note")
        chunk.metadata = FakeChunkMeta(source_file=Path("/auth.md"), namespace="default")
        results = [FakeResult(chunk, 0.42)]

        output = fmt.inject("response", results, "query")

        assert "**auth.md**" in output
        assert "**/auth.md**" not in output

    def test_default_namespace_none_renders_default_badge(self):
        fmt = SurfacingFormatter(SurfacingConfig(default_namespace=None))
        results = [FakeResult(FakeChunk(), 0.5)]

        output = fmt.inject("response", results, "query")

        assert "[default]" in output

    def test_configured_default_namespace_suppresses_matching_badge(self):
        fmt = SurfacingFormatter(SurfacingConfig(default_namespace="work"))
        chunk = FakeChunk(content="work namespace note")
        chunk.metadata = FakeChunkMeta(source_file=Path("/notes/work.md"), namespace="work")
        results = [FakeResult(chunk, 0.5)]

        output = fmt.inject("response", results, "query")

        assert "[work]" not in output

    def test_configured_default_namespace_keeps_other_badges(self):
        fmt = SurfacingFormatter(SurfacingConfig(default_namespace="work"))
        results = [FakeResult(FakeChunk(), 0.5)]

        output = fmt.inject("response", results, "query")

        assert "[default]" in output

    def test_surfacing_id_included(self):
        fmt = SurfacingFormatter(SurfacingConfig())
        results = [FakeResult(FakeChunk(), 0.5)]
        output = fmt.inject("response", results, "query", surfacing_id="xyz789")
        assert "xyz789" in output
        assert "stm_surfacing_feedback" in output

    def test_rating_spec_enumerates_all_valid_ratings(self):
        """#350 part 3: the agent-facing rating spec must enumerate every
        value the server-side validator accepts. The previous italic hint
        only said "call ``stm_surfacing_feedback`` to rate" — agents (Claude
        / GPT) routinely guessed ``"good"`` / ``"useful"`` and the resulting
        feedback was rejected by ``FeedbackTracker.record_feedback``, so the
        AutoTuner never saw the signal. The values come from
        ``feedback.VALID_RATINGS`` (the same source the validator reads) so
        the two cannot drift."""
        from memtomem_stm.surfacing.feedback import VALID_RATINGS

        fmt = SurfacingFormatter(SurfacingConfig())
        results = [FakeResult(FakeChunk(), 0.5)]
        output = fmt.inject("response", results, "query", surfacing_id="abc123")

        for rating in VALID_RATINGS:
            assert f'"{rating}"' in output, (
                f"rating spec must enumerate {rating!r} so the agent does not "
                f"guess an invalid value; got: {output!r}"
            )
        # The spec is formatted as a callable so the LLM treats it as a tool
        # invocation pattern, not prose — ``stm_surfacing_feedback(...)``.
        assert "stm_surfacing_feedback(surfacing_id=" in output

    def test_rendered_callable_is_a_single_valid_call(self):
        """Reviewer pin: when the agent copies the rendered ``Rate:`` call
        verbatim, the resulting MCP call must be parseable and accepted by
        ``FeedbackTracker.record_feedback``. A pipe-alternation inside
        ``rating=`` (``rating="helpful" | "not_relevant" | ...``) parses as
        ``BinOp(BitOr)`` of strings, which is not a string the validator
        recognises — copied as-is it would drop the very signal this PR
        is meant to preserve. So the callable example must carry a single
        valid rating literal, and the alternatives belong outside the
        argument."""
        import ast
        import re

        from memtomem_stm.surfacing.feedback import VALID_RATINGS

        fmt = SurfacingFormatter(SurfacingConfig())
        results = [FakeResult(FakeChunk(), 0.5)]
        output = fmt.inject("response", results, "query", surfacing_id="abc123")

        # Pull the callable out of its backticked code span.
        m = re.search(r"`(stm_surfacing_feedback\([^`]+\))`", output)
        assert m, f"could not find backticked Rate callable in output: {output!r}"
        call_src = m.group(1)

        # 1) Must parse as a single Python call expression. Pipe-alternation
        #    in the rating= argument would parse as a ``BinOp(BitOr)``
        #    rather than a single literal — this check catches that.
        expr = ast.parse(call_src, mode="eval")
        assert isinstance(expr.body, ast.Call), f"not a Call: {ast.dump(expr.body)}"

        # 2) The rating= kwarg must resolve to exactly one valid string
        #    literal — what the validator actually accepts.
        rating_kw = next((k for k in expr.body.keywords if k.arg == "rating"), None)
        assert rating_kw is not None, f"no rating= keyword in {call_src!r}"
        assert isinstance(rating_kw.value, ast.Constant), (
            f"rating= must be a single literal string for the call to be "
            f"copy-pasteable; got {ast.dump(rating_kw.value)} — likely a "
            f"BinOp(BitOr) from a pipe-alternation regression"
        )
        assert rating_kw.value.value in VALID_RATINGS, (
            f"rating= literal {rating_kw.value.value!r} must be a valid "
            f"value the server-side validator accepts"
        )

    def test_surfacing_id_survives_truncation(self):
        """#350 part 4: the surfacing_id used to be appended at the end of
        ``lines`` and could be cut off when the memory block exceeded
        ``effective_max_injection_chars`` — the agent saw memories but had
        no way to rate them, silently breaking the feedback loop on the
        largest, most expensive surfacings. The ID now lives directly after
        the section header so it survives any truncation that leaves the
        header intact."""
        # ~50 chars caps below the bullet list but above the header + ID
        # line; the regression would put the surfacing_id below the cut.
        config = SurfacingConfig(max_injection_chars=200)
        fmt = SurfacingFormatter(config)
        results = [FakeResult(FakeChunk(content="x" * 5000), 0.5)]
        output = fmt.inject("response", results, "query", surfacing_id="trunc-survivor-id")
        assert "truncated" in output, "precondition: the block must actually be truncated"
        assert "trunc-survivor-id" in output, (
            "surfacing_id must survive truncation so the feedback loop "
            "remains usable on large surfacings"
        )

    def test_no_trailing_surfacing_id_line(self):
        """Regression guard: the old trailing ``_Surfacing ID: ... — call
        ... to rate_`` line is gone. Parsers that scraped the bottom line
        need to know the location moved (CHANGELOG flags this as a behavior
        change)."""
        fmt = SurfacingFormatter(SurfacingConfig())
        results = [FakeResult(FakeChunk(), 0.5)]
        output = fmt.inject("response", results, "query", surfacing_id="abc123")
        assert "to rate_" not in output
        assert "Surfacing ID:" not in output  # old capitalized form gone

    def test_no_rating_spec_when_surfacing_id_absent(self):
        """The rating spec is gated on ``surfacing_id`` — callers that
        intentionally suppress the ID (e.g. callers without feedback
        tracking enabled) should not see a hint pointing at a tool that
        cannot accept their None ID."""
        fmt = SurfacingFormatter(SurfacingConfig())
        results = [FakeResult(FakeChunk(), 0.5)]
        output = fmt.inject("response", results, "query", surfacing_id=None)
        assert "stm_surfacing_feedback" not in output
        assert "surfacing_id" not in output

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
        # The preview is the slice that follows the relevance bucket marker.
        preview_line = _preview_line_after_bucket(output)
        assert len(preview_line) <= 300
        assert "a" * 250 in preview_line  # well within the cap

    def test_lower_preview_max_chars_truncates(self):
        fmt = SurfacingFormatter(SurfacingConfig(preview_max_chars=50))
        results = [FakeResult(FakeChunk(content="b" * 800), 0.5)]
        output = fmt.inject("response", results, "query")
        preview_line = _preview_line_after_bucket(output)
        assert len(preview_line) <= 50
        # Tighter check: exactly 50 'b's then nothing more on that line
        assert preview_line.startswith("b" * 50)
        assert "b" * 51 not in preview_line

    def test_higher_preview_max_chars_allows_longer_preview(self):
        fmt = SurfacingFormatter(SurfacingConfig(preview_max_chars=600, max_injection_chars=10000))
        results = [FakeResult(FakeChunk(content="c" * 800), 0.5)]
        output = fmt.inject("response", results, "query")
        preview_line = _preview_line_after_bucket(output)
        assert len(preview_line) >= 600
        assert "c" * 600 in preview_line

    def test_cap_bounds_assembled_preview_with_context_windows(self):
        """Regression: when ``context_window_size > 0`` the formatter joins
        ±150-char window snippets around the matched chunk. The cap must
        apply to the **assembled** preview AND must preserve the matched
        chunk — a front-slice can drop the hit entirely when ``window_before``
        exceeds the cap."""

        @dataclass
        class _FakeWindowChunk:
            content: str

        @dataclass
        class _FakeContext:
            window_before: list
            window_after: list

        @dataclass
        class _FakeResultWithCtx:
            chunk: FakeChunk
            score: float
            context: _FakeContext

        result = _FakeResultWithCtx(
            chunk=FakeChunk(content="m" * 800),
            score=0.5,
            context=_FakeContext(
                window_before=[_FakeWindowChunk(content="b" * 400)],
                window_after=[_FakeWindowChunk(content="a" * 400)],
            ),
        )
        fmt = SurfacingFormatter(SurfacingConfig(preview_max_chars=50))
        output = fmt.inject("response", [result], "query")
        preview_line = _preview_line_after_bucket(output)
        assert len(preview_line) <= 50, (
            f"preview_max_chars=50 must bound the assembled preview, "
            f"got len={len(preview_line)}: {preview_line!r}"
        )
        # The matched chunk MUST appear in the preview; the cap is hit-first,
        # so when window_before+chunk would exceed the cap, window_before is
        # trimmed or dropped rather than the chunk.
        assert "m" in preview_line, (
            f"matched chunk content (the hit) must be preserved, got: {preview_line!r}"
        )

    def test_default_cap_with_context_windows_keeps_chunk_and_trims_windows(self):
        """At default ``preview_max_chars=300`` with context windows, the
        chunk fills the cap budget first and any leftover goes to windows.
        No silent overflow past the documented per-result cap."""

        @dataclass
        class _FakeWindowChunk:
            content: str

        @dataclass
        class _FakeContext:
            window_before: list
            window_after: list

        @dataclass
        class _FakeResultWithCtx:
            chunk: FakeChunk
            score: float
            context: _FakeContext

        result = _FakeResultWithCtx(
            chunk=FakeChunk(content="m" * 80),  # short chunk leaves room for windows
            score=0.5,
            context=_FakeContext(
                window_before=[_FakeWindowChunk(content="b" * 400)],
                window_after=[_FakeWindowChunk(content="a" * 400)],
            ),
        )
        fmt = SurfacingFormatter(SurfacingConfig(preview_max_chars=300))
        output = fmt.inject("response", [result], "query")
        preview_line = _preview_line_after_bucket(output)
        assert len(preview_line) <= 300
        assert "m" * 80 in preview_line  # full chunk fits
        assert "b" in preview_line  # window_before included
        assert "a" in preview_line  # window_after included

    def test_tiny_cap_drops_windows_keeps_chunk(self):
        """When the cap is smaller than the chunk slice, windows are dropped
        entirely (no budget left for them) but the chunk is still rendered."""

        @dataclass
        class _FakeWindowChunk:
            content: str

        @dataclass
        class _FakeContext:
            window_before: list
            window_after: list

        @dataclass
        class _FakeResultWithCtx:
            chunk: FakeChunk
            score: float
            context: _FakeContext

        result = _FakeResultWithCtx(
            chunk=FakeChunk(content="m" * 800),
            score=0.5,
            context=_FakeContext(
                window_before=[_FakeWindowChunk(content="b" * 400)],
                window_after=[_FakeWindowChunk(content="a" * 400)],
            ),
        )
        fmt = SurfacingFormatter(SurfacingConfig(preview_max_chars=20))
        output = fmt.inject("response", [result], "query")
        preview_line = _preview_line_after_bucket(output)
        assert preview_line == "m" * 20, (
            f"tiny cap must yield chunk-only preview, got: {preview_line!r}"
        )
