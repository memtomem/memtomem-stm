"""Tests for ContextExtractor — query extraction from tool arguments."""

from __future__ import annotations

from typing import Any

import pytest

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
    def test_short_semantic_string_counts_toward_the_query(self):
        # A semantic-keyed string too short for the free-text branch still joins
        # the query; dropping it left "read file" (2 tokens) under the 3-token
        # floor, so surfacing silently stopped firing for the call.
        assert _extract("read_file", {"path": "y"}) == "y read file"
        assert _extract("read_file", {"name": "42"}) == "42 read file"
        assert _extract("read_file", {"title": 7}) == "7 read file"

    def test_semantic_container_values_are_not_stringified(self):
        result = _extract("read_file", {"query": {"text": "nested payload words"}})
        assert "nested" not in (result or "")
        assert "{" not in (result or "")

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


# --- Native-tool (``server == "builtin"``) extraction ---------------------------

_REPO = "/Users/u/Work/repo"


def _builtin(tool: str, arguments: dict[str, Any], *, cwd: str | None = _REPO) -> str | None:
    return ContextExtractor().extract_query("builtin", tool, arguments, SurfacingConfig(), cwd=cwd)


class TestBuiltinBashCommands:
    """One case per command class measured in the surfacing replay."""

    def test_inspect_slice_keeps_the_path_topic(self) -> None:
        q = _builtin(
            "Bash",
            {"command": "sed -n 189,300p src/memtomem_stm/proxy/progressive_reads_store.py"},
        )
        assert q == "memtomem stm proxy progressive reads store"

    def test_cd_prefix_redirect_and_pipe_are_dropped(self) -> None:
        q = _builtin(
            "Bash",
            {
                "command": f"cd {_REPO} && sed -n 1,40p src/auth/jwt_handler.py 2>/dev/null "
                "| head -5",
                "description": "Read the handler",
            },
        )
        assert q == "Read the handler auth jwt handler"

    def test_pipe_glued_to_a_path_ends_the_path(self) -> None:
        q = _builtin(
            "Bash",
            {
                "command": "cat src/auth/jwt_handler.py|head -n5",
                "description": "Show the JWT handler",
            },
        )
        assert q == "Show the JWT handler auth jwt handler"

    def test_scratchpad_read_is_skipped_even_with_a_description(self) -> None:
        cmd = "cat /private/tmp/claude-501/-Users-u/ee4d5107-06b0-44f4-b580-9e98d18804c4/x.log"
        assert _builtin("Bash", {"command": cmd, "description": "Read the review log"}) is None

    @pytest.mark.parametrize(
        "command",
        ["cat $TMPDIR/report.txt", "cat ${TMPDIR}/report.txt", "tail -n 20 /tmp/build.log"],
    )
    def test_scratchpad_variants(self, command: str) -> None:
        assert _builtin("Bash", {"command": command, "description": "Read the report"}) is None

    def test_inline_python_heredoc_is_skipped_without_lexing_the_body(self) -> None:
        cmd = "python3 - <<'EOF'\nprint(\"it's\")\nEOF"
        assert _builtin("Bash", {"command": cmd, "description": "Run inline analysis"}) is None

    def test_inline_python_dash_c_does_not_swallow_the_next_line(self) -> None:
        cmd = "python3 -c 'print(6/2)'\ncat src/auth/handler.py"
        assert _builtin("Bash", {"command": cmd, "description": "Check"}) == "Check auth handler"

    def test_uv_run_python_dash_c_is_inline(self) -> None:
        cmd = "uv run python -c 'import x; print(x.__file__)'"
        assert _builtin("Bash", {"command": cmd, "description": "Locate module x"}) is None

    def test_git_plumbing_keeps_the_description_and_non_verb_subcommands(self) -> None:
        q = _builtin(
            "Bash",
            {"command": "git status --short && git diff --stat", "description": "Show tree state"},
        )
        assert q == "Show tree state status"

    def test_gh_keeps_subcommands(self) -> None:
        q = _builtin(
            "Bash",
            {"command": "gh pr view 1300 --json state,title", "description": "Check PR state"},
        )
        assert q == "Check PR state pr view"

    def test_test_command_keeps_the_test_topic(self) -> None:
        q = _builtin(
            "Bash",
            {
                "command": "uv run pytest tests/test_surfacing_engine.py -k cooldown -q",
                "description": "Run cooldown tests",
            },
        )
        assert q == "Run cooldown tests pytest surfacing engine cooldown"

    def test_variable_path_segments_are_dropped(self) -> None:
        cmd = "S=/private/tmp/claude-501/x/scratchpad; cat $S/out.txt"
        assert ContextExtractor._command_tokens(cmd, _REPO) == ["out"]

    def test_bare_file_name_is_a_path(self) -> None:
        q = _builtin("Bash", {"command": "cat CLAUDE.md", "description": "Read repo notes"})
        assert q == "Read repo notes CLAUDE"

    def test_list_command_is_joined(self) -> None:
        q = _builtin("shell", {"command": ["sed", "-n", "1,20p", "src/auth/jwt_handler.py"]})
        assert q == "auth jwt handler"

    def test_command_only_cd_is_skipped(self) -> None:
        assert _builtin("Bash", {"command": f"cd {_REPO}", "description": "Go to repo"}) is None


class TestBuiltinReviewRegressions:
    """Cases found by implementation review; each failed before its fix."""

    def test_fd_duplication_does_not_swallow_the_next_word(self) -> None:
        assert _builtin("Bash", {"command": "rg 2>&1 jwt src/auth/handler.py"}) == (
            "jwt auth handler"
        )

    def test_scratchpad_inside_a_flag_still_skips_the_segment(self) -> None:
        cmd = "rg --file=/tmp/patterns.txt src/auth/jwt_handler.py"
        assert _builtin("Bash", {"command": cmd, "description": "Search the handler"}) is None

    def test_quoted_search_term_is_kept(self) -> None:
        q = _builtin("Bash", {"command": 'grep -rn "SurfacingConfig" src/memtomem_stm'})
        assert q == "SurfacingConfig memtomem stm"

    def test_path_argument_without_a_slash_is_tokenized(self) -> None:
        assert _builtin("Read", {"file_path": "progressive_reads_store.py"}) == (
            "progressive reads store"
        )

    def test_scratchpad_path_with_spaces_is_dropped(self) -> None:
        assert _builtin("Read", {"file_path": "/tmp/my scratchpad/review log.txt"}) is None

    def test_path_with_spaces_is_tokenized(self) -> None:
        q = _builtin("Read", {"file_path": "/Users/u/My Docs/design notes.md"})
        assert q == "My Docs design notes"


class TestBuiltinRound2Regressions:
    def test_long_command_extraction_is_bounded(self) -> None:
        import time

        for filler in ("-", "1"):
            started = time.perf_counter()
            ContextExtractor._command_tokens("echo " + filler * 50_000, _REPO)
            assert time.perf_counter() - started < 0.2

    def test_only_the_command_head_is_read(self) -> None:
        cmd = "cat src/auth/jwt_handler.py" + " -v" * 400 + " src/billing/invoice_store.py"
        assert _builtin("Bash", {"command": cmd}) == "auth jwt handler"

    def test_argv_inline_python_with_a_semicolon_is_skipped(self) -> None:
        argv = ["python3", "-c", 'import os; print("three noisy words")']
        assert _builtin("shell", {"command": argv, "description": "Run inline analysis"}) is None

    @pytest.mark.parametrize("command", ["cat </tmp/build.log", "pytest -q > /tmp/out.txt"])
    def test_scratchpad_redirection_skips_the_segment(self, command: str) -> None:
        assert _builtin("Bash", {"command": command, "description": "Read the build log"}) is None

    @pytest.mark.parametrize(
        "name", ["1234567890abcdef1234567890.json", "550e8400-e29b-41d4-a716-446655440000.json"]
    )
    def test_identifier_file_stems_are_dropped(self, name: str) -> None:
        assert ContextExtractor._builtin_path_tokens(f"/repo/auth/{name}", None) == [
            "repo",
            "auth",
        ]


class TestRepositoryUnderTmp:
    """A repository checked out under a temp root is the work, not a scratchpad."""

    _ROOT = "/tmp/pytest-of-runner/pytest-0/repo"

    def test_read_inside_cwd_under_tmp_is_tokenized(self) -> None:
        q = _builtin("Read", {"file_path": f"{self._ROOT}/src/auth/jwt_handler.py"}, cwd=self._ROOT)
        assert q == "auth jwt handler"

    def test_command_path_inside_cwd_under_tmp_is_not_a_scratchpad(self) -> None:
        cmd = f"sed -n 1,20p {self._ROOT}/src/auth/jwt_handler.py"
        assert _builtin("Bash", {"command": cmd}, cwd=self._ROOT) == "auth jwt handler"

    def test_tmp_path_outside_cwd_is_still_a_scratchpad(self) -> None:
        cmd = "cat /tmp/other/build.log"
        assert _builtin("Bash", {"command": cmd, "description": "Read log"}, cwd=self._ROOT) is None


class TestBuiltinPaths:
    def test_read_relative_to_cwd(self) -> None:
        q = _builtin(
            "Read", {"file_path": f"{_REPO}/src/memtomem_stm/surfacing/context_extractor.py"}
        )
        assert q == "memtomem stm surfacing context extractor"

    def test_home_prefix_stripped_outside_cwd(self) -> None:
        q = _builtin("Read", {"file_path": "/Users/u/Other/proj/docs/design/caching.md"}, cwd=_REPO)
        assert q == "Other proj docs design caching"

    def test_uuid_and_hex_segments_dropped(self) -> None:
        tokens = ContextExtractor._builtin_path_tokens(
            "/data/ee4d5107-06b0-44f4-b580-9e98d18804c4/deadbeef/runs/summary.json", None
        )
        assert tokens == ["data", "runs", "summary"]

    def test_scratchpad_read_yields_nothing(self) -> None:
        path = "/private/tmp/claude-501/-Users-u/ee4d5107-06b0-44f4-b580-9e98d18804c4/notes.md"
        assert _builtin("Read", {"file_path": path}) is None

    def test_short_builtin_read_is_not_padded_with_the_tool_name(self) -> None:
        assert _builtin("Read", {"file_path": "/etc/hosts"}) is None

    def test_short_non_builtin_read_is_still_padded(self) -> None:
        cfg = SurfacingConfig()
        q = ContextExtractor().extract_query("srv", "read_file", {"path": "/etc/hosts"}, cfg)
        assert q == "etc hosts read file"


class TestBuiltinPrivacyFirst:
    _SECRET_CMD = "curl 'https://api.example.invalid/?api_key=demo_secret_value'"

    def test_sensitive_command_is_returned_raw_for_the_engine_to_digest(self) -> None:
        from memtomem_stm.proxy.privacy import contains_sensitive_content

        q = _builtin("Bash", {"command": self._SECRET_CMD, "description": "Probe the API"})
        assert q is not None
        assert contains_sensitive_content(q)
        # Tokenizing first would have hidden it: the lossy form is not sensitive.
        assert not contains_sensitive_content(
            " ".join(ContextExtractor._command_tokens(self._SECRET_CMD, _REPO) or [])
        )

    def test_sensitive_argv_command_is_returned_raw(self) -> None:
        from memtomem_stm.proxy.privacy import contains_sensitive_content

        argv = ["curl", "-H", "api_key: demo_secret_value", "https://api.example.invalid/status"]
        q = _builtin("shell", {"command": argv})
        assert q is not None and contains_sensitive_content(q)

    def test_sensitive_raw_query_is_independent_of_argument_order(self) -> None:
        a = _builtin("Bash", {"command": self._SECRET_CMD, "description": "Probe the API"})
        b = _builtin("Bash", {"description": "Probe the API", "command": self._SECRET_CMD})
        assert a == b


# Expected values were produced by the extractor on ``main`` before native-tool
# extraction existed; non-builtin tools must keep returning exactly these.
_NON_BUILTIN_BASELINE: list[tuple[str, str, dict[str, Any], str | None]] = [
    (
        "github",
        "get_file_contents",
        {
            "owner": "memtomem",
            "repo": "memtomem-stm",
            "path": "src/memtomem_stm/surfacing/engine.py",
        },
        "memtomem src memtomem stm surfacing engine py memtomem-stm",
    ),
    ("filesystem", "read_file", {"path": "/etc/hosts"}, "etc hosts read file"),
    (
        "filesystem",
        "read_file",
        {"path": "/Users/u/Work/repo/src/auth/jwt_handler.py"},
        "Users Work repo src auth jwt handler py",
    ),
    (
        "context7",
        "query-docs",
        {"libraryId": "/vercel/next.js", "query": "How do middleware matchers work?"},
        "/vercel/next.js How do middleware matchers work?",
    ),
    (
        "lc",
        "search_docs_by_lang_chain",
        {"query": "LangGraph checkpointer postgres"},
        "LangGraph checkpointer postgres",
    ),
    (
        "srv",
        "run_command",
        {"command": "cat src/a.py | head -n5", "description": "peek"},
        "cat src/a.py | head -n5 peek",
    ),
    (
        "srv",
        "grep",
        {"pattern": "auth.*token_handler", "path": "src/auth"},
        "src auth auth token handler",
    ),
    ("srv", "search_docs", {"_context_query": "", "_internal": "data"}, None),
    ("srv", "tool", {"name": "550e8400-e29b-41d4-a716-446655440000"}, None),
    (
        "srv",
        "fetch",
        {"url": "https://example.invalid/docs/getting-started", "max_length": 5000},
        None,
    ),
]


@pytest.mark.parametrize(("server", "tool", "arguments", "expected"), _NON_BUILTIN_BASELINE)
def test_non_builtin_extraction_is_unchanged(
    server: str, tool: str, arguments: dict[str, Any], expected: str | None
) -> None:
    got = ContextExtractor().extract_query(server, tool, arguments, SurfacingConfig(), cwd=_REPO)
    assert got == expected
