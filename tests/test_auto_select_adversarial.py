"""Adversarial boundary-condition tests for auto_select_strategy().

Tests every decision boundary (B1-B8) directly against the function,
without ProxyManager or mocking.  Complements the integration-level
tests in test_auto_compression.py.
"""

from __future__ import annotations

import json

from memtomem_stm.proxy.compression import auto_select_strategy
from memtomem_stm.proxy.config import CompressionStrategy

S = CompressionStrategy  # shorthand


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_json_array(n: int) -> str:
    """JSON array of *n* simple objects, well over any typical budget."""
    return json.dumps([{"id": i, "val": f"item_{i}"} for i in range(n)])


def _make_json_dict_with_array(array_len: int) -> str:
    """JSON dict containing one array of *array_len* items."""
    return json.dumps({"results": [{"k": i} for i in range(array_len)], "total": array_len})


def _make_json_dict_nested(nested_count: int, *, extra_scalars: int = 2) -> str:
    """JSON dict with exactly *nested_count* values that are dicts/lists."""
    data: dict = {}
    for i in range(nested_count):
        data[f"nested_{i}"] = {"inner": i} if i % 2 == 0 else [i]
    for i in range(extra_scalars):
        data[f"scalar_{i}"] = f"value_{i}"
    return json.dumps(data)


def _make_markdown(
    heading_count: int,
    target_chars: int,
    *,
    with_http: bool = False,
) -> str:
    """Build markdown with exact heading count, padded to *target_chars*.

    The target_chars is measured AFTER strip() — the function under test
    strips before measuring.
    """
    sections: list[str] = []
    for i in range(heading_count):
        if with_http and i < 4:
            methods = ["GET", "POST", "PUT", "DELETE"]
            sections.append(f"## {methods[i]} /resource/{i}\n\nEndpoint description.\n")
        else:
            sections.append(f"## Section {i}\n\nContent for section {i}.\n")
    body = "\n".join(sections)
    if len(body) < target_chars:
        body += "x" * (target_chars - len(body))
    return body[:target_chars]


def _make_code_heavy(fence_count: int, target_chars: int) -> str:
    """Content with exactly *fence_count* occurrences of triple-backtick."""
    parts: list[str] = []
    for i in range(fence_count):
        parts.append(f"```\ncode block {i}\n")
    body = "\n".join(parts)
    if len(body) < target_chars:
        body += "x" * (target_chars - len(body))
    return body[:target_chars]


# ── B1/B2: Empty and passthrough ────────────────────────────────────────


class TestEmptyAndPassthrough:
    def test_empty_string(self):
        assert auto_select_strategy("") == S.NONE

    def test_whitespace_only(self):
        assert auto_select_strategy("   \n\t  ") == S.NONE

    def test_content_fits_budget(self):
        assert auto_select_strategy("x" * 100, max_chars=100) == S.NONE

    def test_content_one_over_budget(self):
        result = auto_select_strategy("x" * 101, max_chars=100)
        assert result != S.NONE  # proceeds to TRUNCATE (plain text)
        assert result == S.TRUNCATE

    def test_budget_zero_means_unknown(self):
        # max_chars=0 skips the budget check; plain text → TRUNCATE
        result = auto_select_strategy("x" * 50, max_chars=0)
        assert result == S.TRUNCATE


# ── B3: JSON array boundary (20 items) ─────────────────────────────────


class TestJsonArrayBoundary:
    def test_array_20_items(self):
        assert auto_select_strategy(_make_json_array(20), max_chars=10) == S.SCHEMA_PRUNING

    def test_array_19_items(self):
        assert auto_select_strategy(_make_json_array(19), max_chars=10) == S.TRUNCATE

    def test_array_21_items(self):
        assert auto_select_strategy(_make_json_array(21), max_chars=10) == S.SCHEMA_PRUNING

    def test_empty_array(self):
        # "[]" over budget → list with 0 items → falls through to TRUNCATE
        assert auto_select_strategy("[]", max_chars=1) == S.TRUNCATE

    def test_array_of_scalars_at_20(self):
        # [1,2,...,20] — len check has no type constraint on items
        text = json.dumps(list(range(20)))
        assert auto_select_strategy(text, max_chars=10) == S.SCHEMA_PRUNING


# ── B4: JSON dict with large nested array ──────────────────────────────


class TestJsonDictArrayBoundary:
    def test_dict_with_array_20(self):
        assert (
            auto_select_strategy(_make_json_dict_with_array(20), max_chars=10) == S.SCHEMA_PRUNING
        )

    def test_dict_with_array_19(self):
        # 19 items → no SCHEMA_PRUNING; dict has 2 values, 1 list + 1 int
        # nested count = 1 (only the list) → < 3 → TRUNCATE
        assert auto_select_strategy(_make_json_dict_with_array(19), max_chars=10) == S.TRUNCATE

    def test_dict_multiple_arrays_one_at_20(self):
        data = {"a": [{"k": i} for i in range(20)], "b": [1, 2, 3]}
        assert auto_select_strategy(json.dumps(data), max_chars=10) == S.SCHEMA_PRUNING

    def test_dict_multiple_arrays_all_under_20(self):
        data = {"a": [{"k": i} for i in range(19)], "b": [{"k": i} for i in range(15)]}
        # No array ≥ 20 → skip SCHEMA_PRUNING
        # nested count = 2 (both lists) → < 3 → TRUNCATE
        assert auto_select_strategy(json.dumps(data), max_chars=10) == S.TRUNCATE


# ── B5: JSON dict nested count (3 threshold) ──────────────────────────


class TestJsonDictNestedBoundary:
    def test_3_nested(self):
        assert auto_select_strategy(_make_json_dict_nested(3), max_chars=10) == S.EXTRACT_FIELDS

    def test_2_nested(self):
        assert auto_select_strategy(_make_json_dict_nested(2), max_chars=10) == S.TRUNCATE

    def test_4_nested(self):
        assert auto_select_strategy(_make_json_dict_nested(4), max_chars=10) == S.EXTRACT_FIELDS

    def test_mixed_dict_and_list(self):
        # 1 dict + 2 lists = 3 nested
        data = {"a": {"x": 1}, "b": [1], "c": [2], "d": "scalar"}
        assert auto_select_strategy(json.dumps(data), max_chars=10) == S.EXTRACT_FIELDS

    def test_all_scalars(self):
        data = {"a": 1, "b": "two", "c": True, "d": None}
        assert auto_select_strategy(json.dumps(data), max_chars=1) == S.TRUNCATE

    def test_empty_dict(self):
        assert auto_select_strategy("{}", max_chars=1) == S.TRUNCATE


# ── B6: Skeleton boundary (4 headings + HTTP methods) ──────────────────


class TestSkeletonBoundary:
    def test_4_headings_with_http(self):
        text = _make_markdown(4, 500, with_http=True)
        assert auto_select_strategy(text, max_chars=100) == S.SKELETON

    def test_3_headings_with_http(self):
        text = _make_markdown(3, 500, with_http=True)
        # heading_count < 4 → skip entire markdown block → TRUNCATE
        assert auto_select_strategy(text, max_chars=100) == S.TRUNCATE

    def test_4_headings_without_http(self):
        text = _make_markdown(4, 500, with_http=False)
        # 4 headings, no HTTP, < 5000 chars → no SKELETON, no HYBRID → TRUNCATE
        assert auto_select_strategy(text, max_chars=100) == S.TRUNCATE

    def test_http_without_slash_not_detected(self):
        # "GET users" has no slash → regex (?:POST|GET|...) \s+/ won't match
        text = "## Endpoint 1\n## Endpoint 2\n## Endpoint 3\n## Endpoint 4\n\nGET users\n"
        assert auto_select_strategy(text, max_chars=10) == S.TRUNCATE

    def test_patch_method_detected(self):
        text = (
            "## PATCH /resource\n\n## GET /other\n\n"
            "## POST /create\n\n## DELETE /remove\n\nBody text.\n"
        )
        assert auto_select_strategy(text, max_chars=10) == S.SKELETON

    def test_skeleton_takes_priority_over_hybrid(self):
        # 5+ headings, 5000+ chars, BUT has HTTP methods → SKELETON, not HYBRID
        text = _make_markdown(6, 6000, with_http=True)
        assert auto_select_strategy(text, max_chars=100) == S.SKELETON


# ── B7: Hybrid markdown boundary (5 headings + 5000 chars) ─────────────


class TestHybridMarkdownBoundary:
    def test_5_headings_5000_chars(self):
        text = _make_markdown(5, 5000)
        assert auto_select_strategy(text, max_chars=100) == S.HYBRID

    def test_5_headings_4999_chars(self):
        text = _make_markdown(5, 4999)
        assert auto_select_strategy(text, max_chars=100) == S.TRUNCATE

    def test_4_headings_5000_chars_no_http(self):
        # 4 headings (< 5 for HYBRID), no HTTP (no SKELETON) → TRUNCATE
        text = _make_markdown(4, 5000)
        assert auto_select_strategy(text, max_chars=100) == S.TRUNCATE

    def test_6_headings_5000_chars(self):
        text = _make_markdown(6, 5000)
        assert auto_select_strategy(text, max_chars=100) == S.HYBRID

    def test_5_headings_exactly_at_budget(self):
        # 5 headings, 5000 chars, but fits in budget → NONE
        text = _make_markdown(5, 5000)
        assert auto_select_strategy(text, max_chars=5000) == S.NONE


# ── B8: Hybrid code-heavy boundary (6 fences + 5000 chars) ─────────────


class TestHybridCodeBoundary:
    def test_6_fences_5000_chars(self):
        text = _make_code_heavy(6, 5000)
        assert auto_select_strategy(text, max_chars=100) == S.HYBRID

    def test_5_fences_5000_chars(self):
        text = _make_code_heavy(5, 5000)
        assert auto_select_strategy(text, max_chars=100) == S.TRUNCATE

    def test_6_fences_4999_chars(self):
        text = _make_code_heavy(6, 4999)
        assert auto_select_strategy(text, max_chars=100) == S.TRUNCATE

    def test_7_fences_large(self):
        # Odd fence count (unclosed block) still counts ≥ 6
        text = _make_code_heavy(7, 6000)
        assert auto_select_strategy(text, max_chars=100) == S.HYBRID

    def test_inline_backtick_not_counted(self):
        # Single backticks don't contribute to triple-backtick count
        text = "Use `code` and `more code` inline. " * 200
        assert auto_select_strategy(text, max_chars=100) == S.TRUNCATE


# ── Cross-cutting adversarial cases ─────────────────────────────────────


class TestConfusingContent:
    def test_json_inside_markdown_code_fence(self):
        # Starts with "#", not "{" → JSON detection skipped
        data = json.dumps([{"id": i} for i in range(30)])
        text = f"# API Response\n\n## Schema\n\n```json\n{data}\n```\n\n"
        text += "## Details\n\n## Usage\n\n## Examples\n\n"
        text += "x" * max(0, 5000 - len(text))
        # 5+ headings, 5000+ chars → HYBRID (JSON inside fence is invisible)
        assert auto_select_strategy(text, max_chars=100) == S.HYBRID

    def test_markdown_link_starting_with_bracket(self):
        # "[link](url)" starts with "[" → tries json.loads → fails → falls through
        text = "[Click here](https://example.com)\n\n" + "paragraph. " * 100
        assert auto_select_strategy(text, max_chars=10) == S.TRUNCATE

    def test_malformed_json_with_bracket_prefix(self):
        # "[not valid json" → json.loads fails → markdown/code detection
        text = "[not valid json at all\n" + "x" * 500
        assert auto_select_strategy(text, max_chars=10) == S.TRUNCATE

    def test_json_object_with_markdown_in_values(self):
        # Valid JSON dict with markdown as string values → JSON path runs
        # 1 value (string, not nested) → nested < 3 → TRUNCATE
        data = {"readme": "# Title\n## Sec1\n## Sec2\n## Sec3\n## Sec4\nContent"}
        assert auto_select_strategy(json.dumps(data), max_chars=10) == S.TRUNCATE

    def test_empty_json_object_over_budget(self):
        assert auto_select_strategy("{}", max_chars=1) == S.TRUNCATE

    def test_empty_json_array_over_budget(self):
        assert auto_select_strategy("[]", max_chars=1) == S.TRUNCATE

    def test_headings_inside_html_comment(self):
        # The heading regex matches "# " at start of a line even inside comments
        text = "<!-- \n# H1\n## H2\n## H3\n## H4\n -->\n\nActual content.\n"
        # 4 headings detected (regex doesn't exclude comments) + no HTTP → TRUNCATE
        # (heading_count ≥ 4 but < 5 headings and < 5000 chars)
        result = auto_select_strategy(text, max_chars=10)
        assert result == S.TRUNCATE

    def test_heading_mid_line_not_counted(self):
        # "text ## heading" — ## not at start of line or after \n
        text = "This is text ## not a heading\n" * 20
        assert auto_select_strategy(text, max_chars=10) == S.TRUNCATE

    def test_json_with_leading_whitespace(self):
        # Leading whitespace stripped before "[" check
        text = "   \n  " + _make_json_array(25)
        assert auto_select_strategy(text, max_chars=10) == S.SCHEMA_PRUNING


# ── Golden samples: synthetic LangChain-style docs ─────────────────────
# Modeled on the violation profile from proxy_metrics.db:
#   tool=query_docs_filesystem_docs_by_lang_chain
#   compression_strategy=truncate (auto-selected)
#   original_chars ≈ 23K-28K, ratio ≈ 0.25-0.29
# Original content unrecoverable (cache stores compressed only).


class TestGoldenSamples:
    """Synthetic fixtures modeled on real LangChain docs violation profile.

    proxy_metrics.db shows two truncate violations at ratio 0.25-0.29 from
    query_docs_filesystem_docs_by_lang_chain (23K-28K original chars).
    Original content is unrecoverable (cache stores compressed only).

    Key insight: the auto-selector has two paths to HYBRID — headings (B7)
    and code fences (B8).  Content with code blocks can reach HYBRID even
    with few headings.  Docs that actually got TRUNCATE must have had
    < 5 headings AND < 6 triple-backtick markers.
    """

    @staticmethod
    def _make_prose_heavy_docs(heading_count: int, total_chars: int) -> str:
        """Large docs with headings but NO code fences — prose + tables only."""
        parts = []
        for i in range(heading_count):
            labels = ["Overview", "API Reference", "Parameters", "Examples"]
            parts.append(f"## {labels[i % 4]} {i}\n\n")
            parts.append("This section describes the component in detail. " * 10 + "\n\n")
            parts.append("| Parameter | Type | Description |\n")
            parts.append("|-----------|------|-------------|\n")
            for j in range(8):
                parts.append(f"| param_{j} | str | Description of parameter {j} |\n")
            parts.append("\n")
        body = "".join(parts)
        if len(body) < total_chars:
            body += "Additional documentation content. " * ((total_chars - len(body)) // 36 + 1)
        return body[:total_chars]

    @staticmethod
    def _make_code_heavy_docs(heading_count: int, total_chars: int) -> str:
        """Large docs with headings AND code fences."""
        parts = []
        for i in range(heading_count):
            parts.append(f"## Section {i}\n\n")
            parts.append("```python\n")
            parts.append(f"chain = LLMChain_{i}(llm=llm, prompt=prompt)\n")
            parts.append(f"result = chain.run(input_text_{i})\n")
            parts.append("```\n\n")
            parts.append("Explanation of the above code.\n\n")
        body = "".join(parts)
        if len(body) < total_chars:
            body += "Additional content. " * ((total_chars - len(body)) // 20 + 1)
        return body[:total_chars]

    def test_prose_few_headings_selects_truncate(self):
        """Large prose doc with 3 headings, no code → TRUNCATE.

        Models the actual violation profile: docs with structure but not
        enough headings or code to trigger HYBRID.
        """
        text = self._make_prose_heavy_docs(3, 25000)
        assert text.count("```") == 0  # no code fences
        assert auto_select_strategy(text, max_chars=8000) == S.TRUNCATE

    def test_prose_5_headings_selects_hybrid(self):
        """Same prose doc with 5 headings → HYBRID via heading path."""
        text = self._make_prose_heavy_docs(5, 25000)
        assert auto_select_strategy(text, max_chars=8000) == S.HYBRID

    def test_prose_4_headings_large_selects_truncate(self):
        """4 headings, 25K chars, no code → TRUNCATE.

        Documents the gap: heading_count ≥ 4 enters the markdown block but
        needs ≥ 5 for HYBRID.  Without HTTP methods, SKELETON doesn't fire.
        """
        text = self._make_prose_heavy_docs(4, 25000)
        assert auto_select_strategy(text, max_chars=8000) == S.TRUNCATE

    def test_code_heavy_3_headings_selects_hybrid(self):
        """3 headings + code fences → HYBRID via code-heavy path (B8).

        Each heading section has one code block (2 fence markers).
        3 headings = 6 fences = exactly at the B8 threshold.
        """
        text = self._make_code_heavy_docs(3, 25000)
        fence_count = text.count("```")
        assert fence_count >= 6
        assert auto_select_strategy(text, max_chars=8000) == S.HYBRID

    def test_code_heavy_2_headings_selects_truncate(self):
        """2 headings + 4 fences → below B8 threshold → TRUNCATE."""
        text = self._make_code_heavy_docs(2, 25000)
        fence_count = text.count("```")
        assert fence_count < 6  # 2 headings × 2 fences = 4
        assert auto_select_strategy(text, max_chars=8000) == S.TRUNCATE
