"""Tests for ContextExtractor — query extraction from tool arguments."""

from __future__ import annotations

from memtomem_stm.surfacing.config import SurfacingConfig, ToolSurfacingConfig
from memtomem_stm.surfacing.context_extractor import ContextExtractor


def _extract(
    tool: str = "read_file",
    arguments: dict | None = None,
    *,
    context_query: str | None = None,
    **config_kwargs,
) -> str | None:
    ext = ContextExtractor()
    cfg = SurfacingConfig(**config_kwargs)
    return ext.extract_query("server", tool, arguments or {}, cfg, context_query=context_query)


class TestQueryTemplate:
    def test_template_substitution(self):
        cfg_kwargs = {
            "context_tools": {
                "read_file": ToolSurfacingConfig(query_template="file path {arg.path}")
            }
        }
        result = _extract("read_file", {"path": "/src/main.py"}, **cfg_kwargs)
        assert result == "file path /src/main.py"

    def test_template_with_server_and_tool(self):
        cfg_kwargs = {
            "context_tools": {
                "search": ToolSurfacingConfig(query_template="{server} {tool_name} {arg.q}")
            }
        }
        result = _extract("search", {"q": "hello"}, **cfg_kwargs)
        assert result == "server search hello"


class TestContextQuery:
    def test_context_query_argument(self):
        result = _extract("any_tool", {"_context_query": "find authentication code", "path": "x"})
        assert result == "find authentication code"

    def test_empty_context_query_falls_through(self):
        result = _extract("any_tool", {"_context_query": "", "path": "/src/main.py is a test file"})
        assert result is not None
        assert "/src/main.py" in result


class TestExplicitContextQueryKwarg:
    """Priority-2 branch: explicit ``context_query`` kwarg from the proxy.

    The proxy strips ``_context_query`` out of ``arguments`` before invoking
    surfacing and forwards it via this kwarg. The legacy in-arguments branch
    stays as a fallback for direct engine callers and tests.
    """

    def test_explicit_kwarg_wins(self):
        result = _extract(
            "any_tool",
            {"path": "/unrelated.py"},
            context_query="find authentication code",
        )
        assert result == "find authentication code"

    def test_explicit_kwarg_wins_over_legacy_in_arguments(self):
        result = _extract(
            "any_tool",
            {"_context_query": "loser", "path": "/unrelated.py"},
            context_query="winner explicit query",
        )
        assert result == "winner explicit query"

    def test_empty_explicit_kwarg_falls_through_to_legacy(self):
        result = _extract(
            "any_tool",
            {"_context_query": "from explicit arguments", "path": "/x.py"},
            context_query="",
        )
        assert result == "from explicit arguments"

    def test_whitespace_explicit_kwarg_falls_through_to_legacy(self):
        result = _extract(
            "any_tool",
            {"_context_query": "from explicit arguments", "path": "/x.py"},
            context_query="   \n\t  ",
        )
        assert result == "from explicit arguments"

    def test_none_explicit_kwarg_falls_through_to_heuristic(self):
        result = _extract(
            "any_tool",
            {"path": "/src/main.py is a test file"},
            context_query=None,
        )
        assert result is not None
        assert "main.py" in result

    def test_template_still_wins_over_explicit_kwarg(self):
        cfg_kwargs = {
            "context_tools": {
                "read_file": ToolSurfacingConfig(query_template="file path {arg.path}")
            }
        }
        result = _extract(
            "read_file",
            {"path": "/src/main.py"},
            context_query="ignored because template wins",
            **cfg_kwargs,
        )
        assert result == "file path /src/main.py"

    def test_explicit_kwarg_is_stripped(self):
        result = _extract(
            "any_tool",
            {"path": "/unrelated.py"},
            context_query="  padded explicit query  ",
        )
        assert result == "padded explicit query"


class TestHeuristicExtraction:
    def test_semantic_string_args(self):
        result = _extract("tool", {"path": "/src/main.py", "query": "search term"})
        # Path is tokenized: /src/main.py → "src main py"
        assert "main" in result
        assert "search term" in result

    def test_skips_internal_args(self):
        result = _extract("tool", {"_internal": "skip", "path": "/src/main.py"})
        assert "_internal" not in (result or "")
        assert "skip" not in (result or "")
        # Path tokenized, so "main" should be present
        if result:
            assert "main" in result

    def test_skips_identifiers(self):
        result = _extract(
            "tool",
            {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "name": "actual content with enough words",
            },
        )
        assert "550e8400" not in (result or "")
        assert "actual content" in result

    def test_skips_hex_strings(self):
        result = _extract("tool", {"hash": "a" * 32, "title": "real query with enough words"})
        assert "real query" in result

    def test_falls_back_to_tool_name(self, surfacing_config):
        # With min_query_tokens=1 to avoid the 3-token minimum filter
        result = _extract("search_repositories", {}, min_query_tokens=1)
        assert result == "search repositories"

    def test_returns_none_for_short_query(self, surfacing_config):
        result = _extract("x", {"a": "ab"}, min_query_tokens=3)
        assert result is None


class TestShortNonEmptyQueryPadsWithToolName:
    """KR-4.2: the tool-name fallback fires whenever the extracted args alone
    fall below ``min_query_tokens``, not only when extraction yields nothing.
    A short-but-non-empty extraction (e.g. ``/etc/hosts`` → "etc hosts",
    2 tokens) previously returned None and surfacing never fired for it."""

    def test_short_path_pads_with_tool_name(self):
        # /etc/hosts → "etc hosts" (2 tokens) < 3; pre-fix returned None.
        result = _extract("read_file", {"path": "/etc/hosts"}, min_query_tokens=3)
        assert result is not None
        assert "etc" in result and "hosts" in result
        # The tool-name token(s) were appended to clear the floor.
        assert "read" in result and "file" in result
        assert len(result.split()) >= 3

    def test_pad_is_appended_after_args_deterministically(self):
        # The tool name comes after the extracted arg tokens (stable order
        # keeps the surfacing cache key deterministic).
        result = _extract("read_file", {"path": "/etc/hosts"}, min_query_tokens=3)
        assert result == "etc hosts read file"

    def test_still_none_when_args_plus_tool_name_below_floor(self):
        # Non-empty short extraction + short tool name still below the floor
        # → None (the final threshold re-check is unchanged). "etc hosts" (2)
        # + "io" (1) = 3 tokens < 5.
        result = _extract("io", {"path": "/etc/hosts"}, min_query_tokens=5)
        assert result is None

    def test_strong_query_not_diluted_by_tool_name(self):
        # A query that already clears the floor must not get the tool name
        # appended — no dilution of an already-good query.
        result = _extract(
            "read_file", {"query": "authentication token refresh flow"}, min_query_tokens=3
        )
        assert result == "authentication token refresh flow"
        assert "read" not in result and "file" not in result


class TestEdgeCases:
    """Edge cases for extract_query — inputs realistic from LLM tool calls."""

    def test_all_underscore_prefixed_keys_falls_back(self):
        """When every key starts with _, heuristic yields nothing → tool name."""
        result = _extract(
            "search_docs",
            {"_context_query": "", "_internal": "data", "_trace": "abc123"},
            min_query_tokens=1,
        )
        assert result == "search docs"

    def test_only_short_values_falls_back(self):
        """Values with len <= 2 are skipped; only tool name remains."""
        result = _extract("read_file", {"q": "ab", "x": "no"}, min_query_tokens=1)
        assert result == "read file"

    def test_non_string_context_query_falls_through(self):
        """_context_query that is not a str should be ignored."""
        result = _extract(
            "search_tool",
            {"_context_query": 42, "topic": "authentication patterns in the codebase"},
        )
        assert "authentication patterns" in result


class TestIsIdentifier:
    def test_uuid(self):
        assert ContextExtractor._is_identifier("550e8400-e29b-41d4-a716-446655440000")

    def test_hex_string(self):
        assert ContextExtractor._is_identifier("a" * 24)

    def test_boolean_literals(self):
        assert ContextExtractor._is_identifier("true")
        assert ContextExtractor._is_identifier("False")
        assert ContextExtractor._is_identifier("null")
        assert ContextExtractor._is_identifier("None")

    def test_normal_string(self):
        assert not ContextExtractor._is_identifier("hello world")


class TestFirstSentence:
    def test_truncates_at_period(self):
        assert ContextExtractor._first_sentence("Hello. World.", max_chars=200) == "Hello."

    def test_respects_max_chars(self):
        result = ContextExtractor._first_sentence("A" * 300, max_chars=100)
        assert len(result) == 100
