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


class _TooDeep(list):  # type: ignore[type-arg]
    """Stands in for a list nested past the C recursion limit: ``str()`` of the
    real thing raises ``RecursionError`` at a depth that varies by platform
    and Python version (5,000 failed on the Windows CI lanes), #1364."""

    def __repr__(self) -> str:
        raise RecursionError("maximum recursion depth exceeded while getting the repr")


class _NeverRendered:
    """An argument the template does not name; rendering it fails the test."""

    def __str__(self) -> str:
        raise AssertionError("an argument the template does not name was rendered")

    __repr__ = __str__


# Static text alone clears the default ``min_query_tokens`` (3), so a ``None``
# below cannot come from the token floor or from rendering the value as "".
_LOOKUP = {
    "context_tools": {
        "lookup": ToolSurfacingConfig(query_template="lookup stored {arg.q} entries here")
    }
}


class TestTemplateRendering:
    def test_unrenderable_named_argument_yields_no_query(self) -> None:
        assert _extract("lookup", {"q": _TooDeep()}, **_LOOKUP) is None

    def test_unrenderable_value_with_a_secret_still_yields_no_query(self) -> None:
        """The raw privacy check returns the raw inputs only in place of a
        query the route yields; an unrenderable template yields none."""
        args = {"q": _TooDeep(), "note": "api_key=demo_secret_value"}
        assert _extract("lookup", args, **_LOOKUP) is None

    def test_argument_the_template_does_not_name_is_not_rendered(self) -> None:
        args = {"blob": _NeverRendered(), "q": "auth tokens", "deep": _TooDeep()}
        assert _extract("lookup", args, **_LOOKUP) == "lookup stored auth tokens entries here"

    def test_named_list_still_renders_as_its_str(self) -> None:
        cfg = {
            "context_tools": {"lookup": ToolSurfacingConfig(query_template="{arg.q}")},
            "min_query_tokens": 1,
        }
        assert _extract("lookup", {"q": ["a", "b"]}, **cfg) == "['a', 'b']"

    def test_placeholder_introduced_by_an_earlier_value_is_still_substituted(self) -> None:
        cfg = {
            "context_tools": {
                "lookup": ToolSurfacingConfig(query_template="find {arg.a} please now")
            }
        }
        args = {"a": "{arg.b} docs", "b": "routing"}
        assert _extract("lookup", args, **cfg) == "find routing docs please now"

    def test_real_deep_list_does_not_raise(self) -> None:
        """A 200,000-level nesting stress test: whether this interpreter can
        render it or not, the template yields a query or none; it never raises."""
        deep: Any = "x"
        for _ in range(200_000):
            deep = [deep]
        result = _extract("lookup", {"q": deep}, **_LOOKUP)
        assert result is None or result.startswith("lookup stored")


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
        path = "/private/tmp/claude-501/-Users-u/my scratchpad/review log.txt"
        assert _builtin("Read", {"file_path": path}) is None

    def test_temp_path_with_spaces_outside_the_claude_root_is_tokenized(self) -> None:
        # Only Claude Code's temp root is a scratchpad for a path argument (#1324).
        q = _builtin("Read", {"file_path": "/tmp/my scratchpad/review log.txt"})
        assert q == "my scratchpad review log"

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
        assert ContextExtractor._builtin_path_tokens(f"/repo/auth/{name}", None, broad=False) == [
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


class TestTempRepositoryFromAnotherCwd:
    """#1324: a path argument under ``/tmp/<dir>/`` is a repository, not a
    scratchpad, even when the call's ``cwd`` is elsewhere. Shell commands keep
    the broad rule."""

    def test_release_worktree_read(self) -> None:
        q = _builtin("Read", {"file_path": "/private/tmp/memtomem-release/CHANGELOG.md"})
        assert q == "memtomem release CHANGELOG"

    def test_nested_package_read(self) -> None:
        path = "/private/tmp/memtomem-lcmm/packages/langchain-memtomem/README.md"
        assert _builtin("Read", {"file_path": path}) == "memtomem lcmm langchain memtomem README"

    def test_repository_named_like_claude_is_not_the_claude_root(self) -> None:
        q = _builtin("Read", {"file_path": "/tmp/claude-tools/src/auth/jwt_handler.py"})
        assert q == "claude tools auth jwt handler"

    def test_grep_path_under_a_temp_repository(self) -> None:
        q = _builtin("Grep", {"path": "/private/tmp/demo-repo/src", "pattern": "jwt_handler"})
        assert q == "demo repo jwt handler"

    @pytest.mark.parametrize(
        "path",
        [
            "/private/tmp/claude-501/-Users-u/ee4d5107-06b0-44f4-b580-9e98d18804c4/notes.md",
            "/tmp/claude-1000/-home-u-repo/ee4d5107-06b0-44f4-b580-9e98d18804c4/notes.md",
            "/var/folders/4n/mt3k/T/pytest-of-u/pytest-1/repo/notes.md",
            "$TMPDIR/review/notes.md",
            "${TMPDIR}/review/notes.md",
        ],
    )
    def test_claude_root_and_tmpdir_reads_stay_scratchpads(self, path: str) -> None:
        assert _builtin("Read", {"file_path": path}) is None

    @pytest.mark.parametrize(
        "path",
        ["/tmp/claude-501", "/tmp/claude-501/", "\\tmp\\claude-501", "/private/tmp/claude-0"],
    )
    def test_claude_root_itself_is_a_scratchpad(self, path: str) -> None:
        # A Grep/Glob ``path`` is often the directory itself; its ``claude``
        # token would otherwise lift a two-token pattern over the floor.
        assert _builtin("Grep", {"path": path, "pattern": "jwt_handler"}) is None

    @pytest.mark.parametrize(
        "path",
        [
            "/tmp//claude-501/session/scratchpad/notes.md",
            "/private/tmp/./claude-501/session/scratchpad/notes.md",
            "/tmp/claude-501//",
        ],
    )
    def test_equivalent_spellings_of_the_claude_root_stay_scratchpads(self, path: str) -> None:
        assert _builtin("Grep", {"path": path, "pattern": "jwt_handler"}) is None

    @pytest.mark.parametrize("cwd", ["/var/folders/ab/hash/T/./repo", "/tmp/claude-501/./repo"])
    def test_cwd_is_compared_in_the_same_canonical_form(self, cwd: str) -> None:
        q = _builtin("Read", {"file_path": f"{cwd}/src/auth/jwt_handler.py"}, cwd=cwd)
        assert q == "auth jwt handler"

    @pytest.mark.parametrize(
        "path", ["/tmp/claude-501/../repo/notes.md", "$TMPDIR/../review/notes.md"]
    )
    def test_dot_dot_is_not_resolved(self, path: str) -> None:
        # Lexical ``..`` resolution can name another file under a symlink, so
        # a path that starts in a scratchpad stays one.
        assert _builtin("Read", {"file_path": path}) is None

    @pytest.mark.parametrize(
        ("path", "cwd"),
        [("/Users/u//tmp/auth/jwt.py", "/repo"), ("/repo//tmp/auth/jwt.py", "/repo")],
    )
    def test_temp_root_is_not_stripped_from_a_remainder(self, path: str, cwd: str) -> None:
        # After the home prefix or cwd is removed, ``/tmp/…`` is an ordinary
        # segment of the original path, not its temp root.
        assert _builtin("Read", {"file_path": path}, cwd=cwd) == "tmp auth jwt"

    def test_command_naming_a_temp_repository_is_still_skipped(self) -> None:
        cmd = "cat /private/tmp/memtomem-release/CHANGELOG.md"
        assert _builtin("Bash", {"command": cmd, "description": "Read the changelog"}) is None

    def test_other_segments_of_that_command_still_contribute(self) -> None:
        cmd = "cat /tmp/repo/CHANGELOG.md; cat src/auth/jwt_handler.py"
        assert _builtin("Bash", {"command": cmd}) == "auth jwt handler"


class TestMacTempDirSpellings:
    """#1334: macOS ``$TMPDIR`` is a scratchpad in both spellings (``/var`` is a
    symlink to ``/private/var``). A path argument includes the directory itself;
    a shell command matches descendants only."""

    _CWD = "/Users/u/repo"

    @pytest.mark.parametrize(
        "path",
        [
            "/private/var/folders/4n/mt3k/T/ctx.8Q5jnO/context.json",
            "/private/var/folders/",
            "/private/var/folders",
            "/var/folders",
        ],
    )
    def test_read_is_a_scratchpad(self, path: str) -> None:
        assert _builtin("Grep", {"path": path, "pattern": "jwt_handler"}, cwd=self._CWD) is None

    @pytest.mark.parametrize(
        "command",
        [
            "ls /private/var/folders/4n/mt3k/T/pytest-of-u/pytest-365/",
            "ls /var/folders/4n/mt3k/T/pytest-of-u/pytest-365/",
        ],
    )
    def test_command_segment_is_skipped(self, command: str) -> None:
        assert _builtin("Bash", {"command": command}, cwd=self._CWD) is None

    @pytest.mark.parametrize(
        ("command", "query"),
        [
            # The path token stops at ``+``/``:``; the rest is not the temp dir.
            ("cat /private/var/folders+archive/auth/jwt_handler.py", "private var folders"),
            ("cat /private/var/folders:archive/auth/jwt_handler.py", "private var folders"),
        ],
    )
    def test_command_root_token_is_not_a_scratchpad(self, command: str, query: str) -> None:
        assert _builtin("Bash", {"command": command}, cwd=self._CWD) == query


class TestShellTempDirWord:
    """#1341: a shell word that is a temp directory itself names no topic. The
    word yields no tokens; the segment is not a scratchpad, so its other words
    and the description still count."""

    _CWD = "/Users/u/repo"
    _GREP = "grep -rn SurfacingConfig {} src/memtomem_stm/surfacing"

    @pytest.mark.parametrize(
        "word",
        [
            "/private/tmp",
            "/private/tmp/",
            "/var/folders",
            "/var/folders/",
            "/private/var/folders",
            "/private/var/folders//",
            "'/private/tmp'",
            '"/private/var/folders/"',
        ],
    )
    def test_temp_dir_word_yields_no_tokens(self, word: str) -> None:
        cmd = self._GREP.format(word)
        assert _builtin("Bash", {"command": cmd}, cwd=self._CWD) == (
            "SurfacingConfig memtomem stm surfacing"
        )

    def test_segment_is_not_a_scratchpad(self) -> None:
        args = {"command": "ls /private/var/folders/", "description": "List the temp dir"}
        assert _builtin("Bash", args, cwd=self._CWD) == "List the temp dir"

    @pytest.mark.parametrize(
        ("word", "tokens"),
        [
            ("/private/var/folders-other", "private var folders other"),
            ("/private/tmpdir", "private tmpdir"),
            ("/private/tmp+x/auth", "private tmp"),
            # Not the bare directory: a literal trailing character, an
            # unmatched quote, or the first fragment of a quoted path with a space.
            ("'/private/var/folders,'", "private var folders"),
            ("'/private/var/folders)'", "private var folders"),
            ("'/private/tmp\"", "private tmp"),
            ("'/private/var/folders other'", "private var folders other"),
        ],
    )
    def test_lookalike_word_keeps_its_tokens(self, word: str, tokens: str) -> None:
        cmd = self._GREP.format(word)
        assert _builtin("Bash", {"command": cmd}, cwd=self._CWD) == (
            f"SurfacingConfig {tokens} memtomem stm surfacing"
        )

    def test_checkout_used_as_cwd_is_still_tokenized(self) -> None:
        root = "/private/var/folders/4n/mt3k/T/pytest-of-u/pytest-1/repo"
        assert _builtin("Read", {"file_path": f"{root}/src/auth/jwt_handler.py"}, cwd=root) == (
            "auth jwt handler"
        )
        cmd = f"sed -n 1,20p {root}/src/auth/jwt_handler.py"
        assert _builtin("Bash", {"command": cmd}, cwd=root) == "auth jwt handler"

    def test_lookalike_directory_is_not_the_temp_root(self) -> None:
        q = _builtin("Read", {"file_path": "/private/var/folders-other/auth/jwt_handler.py"})
        assert q == "private var folders other auth jwt handler"


class TestPathOnlyFloor:
    """#1325: a builtin query made only of path tokens needs one token fewer
    than ``min_query_tokens`` (at least 1); anything else keeps the full floor."""

    @staticmethod
    def _q(tool: str, args: dict[str, Any], floor: int = 3) -> str | None:
        cfg = SurfacingConfig(min_query_tokens=floor)
        return ContextExtractor().extract_query("builtin", tool, args, cfg, cwd=_REPO)

    @pytest.mark.parametrize(
        ("path", "query"),
        [
            (f"{_REPO}/tests/test_server_tools.py", "server tools"),
            ("/Users/u/.claude/settings.json", "claude settings"),
        ],
    )
    def test_two_token_read_clears_the_default_floor(self, path: str, query: str) -> None:
        assert self._q("Read", {"file_path": path, "offset": 1, "limit": 40}) == query

    def test_one_token_read_is_still_skipped(self) -> None:
        assert self._q("Read", {"file_path": f"{_REPO}/CONTRIBUTING.md"}) is None

    def test_floor_follows_min_query_tokens(self) -> None:
        two = {"file_path": f"{_REPO}/tests/test_server_tools.py"}
        three = {"file_path": f"{_REPO}/src/auth/jwt_handler.py"}
        assert self._q("Read", two, floor=4) is None
        assert self._q("Read", three, floor=4) == "auth jwt handler"
        assert self._q("Read", {"file_path": f"{_REPO}/CHANGELOG.md"}, floor=1) == "CHANGELOG"
        # The floor never drops to 0: a Read with no tokens is still skipped.
        scratch = {"file_path": "/tmp/claude-501/session/scratchpad/log.txt"}
        assert self._q("Read", scratch, floor=1) is None

    def test_glob_path_without_pattern_is_path_only(self) -> None:
        assert self._q("Glob", {"path": f"{_REPO}/src/auth/session"}) == "auth session"

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            # A pattern contributes tokens: the full floor applies.
            ("Grep", {"pattern": "jwt", "path": f"{_REPO}/src/auth"}),
            ("Glob", {"pattern": "*.py", "path": f"{_REPO}/src/auth"}),
            # A command keeps the full floor even when it names only a path.
            ("Bash", {"command": f"ls {_REPO}/src/auth/session"}),
            ("Bash", {"command": "ls", "description": "List files"}),
            # A description without a command keeps the full floor too.
            ("Read", {"file_path": f"{_REPO}/session.py", "description": "Inspect"}),
        ],
    )
    def test_other_text_keeps_the_full_floor(self, tool: str, args: dict[str, Any]) -> None:
        assert self._q(tool, args) is None


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
            "/data/ee4d5107-06b0-44f4-b580-9e98d18804c4/deadbeef/runs/summary.json",
            None,
            broad=False,
        )
        assert tokens == ["data", "runs", "summary"]

    def test_scratchpad_read_yields_nothing(self) -> None:
        path = "/private/tmp/claude-501/-Users-u/ee4d5107-06b0-44f4-b580-9e98d18804c4/notes.md"
        assert _builtin("Read", {"file_path": path}) is None

    def test_short_builtin_read_is_not_padded_with_the_tool_name(self) -> None:
        assert _builtin("Read", {"file_path": "/etc/hosts"}) == "etc hosts"
        assert _builtin("Read", {"file_path": f"{_REPO}/CHANGELOG.md"}) is None

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

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            # No segment contributes: ``cd`` and inline Python.
            (
                "Bash",
                {
                    "command": "cd /srv && python3 -c \"print('api_key=demo_secret_value')\"",
                    "description": "Check the key",
                },
            ),
            ("Bash", {"command": "cd /srv/api_key=demo_secret_value"}),
            # Contributes, but below ``min_query_tokens``.
            ("Grep", {"pattern": "a@b.io"}),
        ],
    )
    def test_a_secret_does_not_make_a_query_of_a_call_that_has_none(
        self, tool: str, args: dict[str, Any]
    ) -> None:
        """#1344: the raw values replace a query; they do not create one."""
        from memtomem_stm.proxy.privacy import contains_sensitive_content

        assert any(contains_sensitive_content(v) for v in args.values())
        assert _builtin(tool, args) is None

    def test_sensitive_argv_command_is_returned_raw(self) -> None:
        from memtomem_stm.proxy.privacy import contains_sensitive_content

        argv = ["curl", "-H", "api_key: demo_secret_value", "https://api.example.invalid/status"]
        q = _builtin("shell", {"command": argv})
        assert q is not None and contains_sensitive_content(q)

    def test_argv_sensitive_only_when_joined_is_returned_raw(self) -> None:
        from memtomem_stm.proxy.privacy import contains_sensitive_content

        argv = ["echo", "api_key", "=", "demo_secret_value"]
        # No single element is sensitive, so only the joined-command check
        # in ``_extract_builtin`` can catch it.
        assert not any(contains_sensitive_content(a) for a in argv)
        q = _builtin("shell", {"command": argv, "description": "Probe the endpoint"})
        assert q == "echo api_key = demo_secret_value Probe the endpoint"
        assert contains_sensitive_content(q)

    def test_sensitive_raw_query_is_independent_of_argument_order(self) -> None:
        a = _builtin("Bash", {"command": self._SECRET_CMD, "description": "Probe the API"})
        b = _builtin("Bash", {"description": "Probe the API", "command": self._SECRET_CMD})
        assert a == b


class TestRawInputPrivacyFirst:
    """#1344: every raw input a reshaping route reads (server and tool names,
    the template and every argument string, nested ones included) is checked
    before that route runs. On a match, a route that yields a query returns
    the raw inputs instead, in a fixed order, so the engine's check sees them
    whole; a route that yields none still yields none. An agent-provided query
    is used as given and is not part of this check; a per-tool template wins
    over it."""

    @staticmethod
    def _q(
        args: dict[str, Any],
        *,
        server: str = "srv",
        tool: str = "read_file",
        template: str = "",
    ) -> str | None:
        tools = {tool: ToolSurfacingConfig(query_template=template)} if template else {}
        cfg = SurfacingConfig(context_tools=tools)
        return ContextExtractor().extract_query(server, tool, args, cfg)

    @pytest.mark.parametrize(
        ("kwargs", "args", "query"),
        [
            # Path and pattern tokenizing split the secret apart.
            (
                {},
                {"path": "/srv/cfg/api_key=demo_secret_value/x.txt"},
                "srv read_file /srv/cfg/api_key=demo_secret_value/x.txt",
            ),
            (
                {"tool": "search_code"},
                {"pattern": "api_key=demo_secret_value"},
                "srv search_code api_key=demo_secret_value",
            ),
            (
                {},
                {"file_path": "/home/alice@example.com/notes.md"},
                "srv read_file /home/alice@example.com/notes.md",
            ),
            # Tool-name padding splits the name.
            (
                {"tool": "probe_api_key=demo_secret_value"},
                {},
                "srv probe_api_key=demo_secret_value",
            ),
            # Template interpolation: a glued suffix breaks the word boundary,
            # str() of a list or dict escapes the tab the detector matches.
            (
                {"tool": "t", "template": "lookup {arg.email}_log entries"},
                {"email": "alice@example.com"},
                "srv t lookup {arg.email}_log entries alice@example.com",
            ),
            (
                {"tool": "t", "template": "lookup payload {arg.query}"},
                {"query": ["api_key\t=demo_secret_value"]},
                "srv t lookup payload {arg.query} api_key\t=demo_secret_value",
            ),
            (
                {"tool": "t", "template": "lookup {arg.cfg}"},
                {"cfg": {"auth": {"header": "api_key\t=demo_secret_value"}}},
                "srv t lookup {arg.cfg} auth header api_key\t=demo_secret_value",
            ),
            # A value carrying a placeholder that a later key expands.
            (
                {"tool": "t", "template": "lookup {arg.q} entries"},
                {"q": "{arg.email}_log", "email": "alice@example.com"},
                "srv t lookup {arg.q} entries alice@example.com {arg.email}_log",
            ),
            # The server name, and the template text itself.
            (
                {
                    "server": "alice@example.com",
                    "tool": "t",
                    "template": "lookup {server}_log entries",
                },
                {},
                "alice@example.com t lookup {server}_log entries",
            ),
            (
                {"tool": "t", "template": "lookup alice@example.com{arg.suffix} entries"},
                {"suffix": "_log"},
                "srv t lookup alice@example.com{arg.suffix} entries _log",
            ),
            # A sensitive value the template does not use.
            (
                {"tool": "t", "template": "lookup {arg.topic}"},
                {"topic": "jwt rotation policy", "note": "api_key=demo_secret_value"},
                "srv t lookup {arg.topic} api_key=demo_secret_value jwt rotation policy",
            ),
        ],
    )
    def test_sensitive_raw_input_is_returned_raw(
        self, kwargs: dict[str, str], args: dict[str, Any], query: str
    ) -> None:
        from memtomem_stm.proxy.privacy import contains_sensitive_content

        q = self._q(args, **kwargs)
        assert q == query
        assert contains_sensitive_content(q)

    @pytest.mark.parametrize(
        ("kwargs", "args"),
        [
            ({}, {"path": "/srv/cfg/api_key=demo_secret_value/x.txt"}),
            ({"tool": "search_code"}, {"pattern": "api_key=demo_secret_value"}),
            ({"tool": "probe_api_key=demo_secret_value"}, {}),
            (
                {"tool": "t", "template": "lookup {arg.email}_log entries"},
                {"email": "alice@example.com"},
            ),
            (
                {"tool": "t", "template": "lookup {arg.query}"},
                {"query": ["api_key\t=demo_secret_value"]},
            ),
        ],
    )
    def test_each_route_would_have_hidden_it(
        self, kwargs: dict[str, str], args: dict[str, Any]
    ) -> None:
        """The positive control: without the raw check, each of these routes
        produces a query the engine's check does not flag."""
        from memtomem_stm.proxy.privacy import contains_sensitive_content

        ext = ContextExtractor()
        server, tool = kwargs.get("server", "srv"), kwargs.get("tool", "read_file")
        if "template" in kwargs:
            reshaped = ext._apply_template(kwargs["template"], server, tool, args)
        elif "path" in args:
            reshaped = ext._tokenize_path(args["path"])
        elif "pattern" in args:
            reshaped = ext._tokenize_pattern(args["pattern"])
        else:
            reshaped = tool.replace("_", " ")
        assert not contains_sensitive_content(reshaped)

    @pytest.mark.parametrize(
        ("kwargs", "args"),
        [
            # Each template renders under the floor.
            ({"tool": "t", "template": "{arg.email}_log"}, {"email": "alice@example.com"}),
            ({"server": "alice@example.com", "tool": "t", "template": "{server}_log"}, {}),
            # A one-token render holding a secret is still under the floor.
            ({"tool": "t", "template": "{arg.q}"}, {"q": "alice@example.com"}),
            ({"tool": "t", "template": "{arg.q}"}, {"q": "api_key=demo_secret_value"}),
            # Interpolation assembles the secret; no single input holds it.
            (
                {"tool": "t", "template": "{arg.label}={arg.value}"},
                {"label": "api_key", "value": "demo_secret_value"},
            ),
        ],
    )
    def test_a_route_without_a_query_still_yields_none(
        self, kwargs: dict[str, str], args: dict[str, Any]
    ) -> None:
        """A secret does not turn a call that has no query into one."""
        assert self._q(args, **kwargs) is None

    def test_raw_query_is_independent_of_argument_order(self) -> None:
        a = self._q({"path": "/srv/api_key=demo_secret_value", "owner": "acme"})
        b = self._q({"owner": "acme", "path": "/srv/api_key=demo_secret_value"})
        assert a == b == "srv read_file acme /srv/api_key=demo_secret_value"

    def test_deeply_nested_value_is_walked_without_recursion(self) -> None:
        """The raw check walks nested arguments iteratively and never renders
        them: a container whose ``repr`` raises is still read to the leaf."""

        class _NoRepr(list):
            def __repr__(self) -> str:
                raise AssertionError("nested argument was rendered")

        nested: Any = "api_key=demo_secret_value"
        for _ in range(5000):
            nested = [nested]
        args = {"q": _NoRepr([nested]), "query": "jwt rotation policy"}
        q = self._q(args)
        assert q == "srv read_file api_key=demo_secret_value jwt rotation policy"

    @pytest.mark.parametrize(
        ("kwargs", "args", "query"),
        [
            ({}, {"path": "/srv/cfg/settings.json"}, "srv cfg settings json"),
            (
                {"tool": "t", "template": "lookup {arg.topic}"},
                {"topic": "jwt rotation policy", "note": "see the runbook"},
                "lookup jwt rotation policy",
            ),
        ],
    )
    def test_non_sensitive_query_is_unchanged(
        self, kwargs: dict[str, str], args: dict[str, Any], query: str
    ) -> None:
        assert self._q(args, **kwargs) == query


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
