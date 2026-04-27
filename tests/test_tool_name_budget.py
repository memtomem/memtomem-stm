"""Unit tests for ``memtomem_stm.proxy.tool_name_budget``.

The arithmetic itself is small but it underpins the boot-time
overflow skip in ``ProxyManager._connect_server`` and the sanity check
in ``mms add``. A regression here would silently let overflowing tools
register again, which is exactly the issue (#261) the helper exists
to prevent — so pin the math.
"""

from __future__ import annotations

from memtomem_stm.proxy import tool_name_budget


class TestComposedLength:
    """Reference: the user-visible #261 case is
    ``mcp__memtomem-stm__docs_langchain__query_docs_filesystem_docs_by_lang_chain``
    which renders to 75 chars in Claude Code's strict format. We pin
    that here so the helper doesn't drift away from the empirical
    composition the strictest client actually uses."""

    def test_default_overhead_matches_empirical_claude_code(self, monkeypatch):
        # ``memtomem-stm`` server (12 chars) + 9 fixed = 21
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)
        assert tool_name_budget.overhead() == 21

    def test_composed_length_matches_user_facing_repro(self, monkeypatch):
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)
        # The exact tool name + prefix from issue #261's repro
        composed = tool_name_budget.composed_length(
            "docs_langchain", "query_docs_filesystem_docs_by_lang_chain"
        )
        assert composed == 75  # measured against Claude Code's render

    def test_overrides_via_env_var(self, monkeypatch):
        # Registering STM as 'mms' (3 chars) saves 9 bytes and unblocks the
        # exact case above.
        monkeypatch.setenv("MMS_CLIENT_SERVER_NAME", "mms")
        composed = tool_name_budget.composed_length(
            "docs_langchain", "query_docs_filesystem_docs_by_lang_chain"
        )
        assert composed == 75 - 9
        assert tool_name_budget.overhead() == 12  # 9 + len("mms")


class TestOverflowPredicate:
    def test_short_prefix_short_tool_fits(self, monkeypatch):
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)
        assert not tool_name_budget.overflows("ctx7", "resolve_library_id")

    def test_long_tool_with_short_prefix_fits(self, monkeypatch):
        # The issue #261 fix path — prefix 'lc' (2) + 40-char tool = 42
        # bytes for prefix+tool, fits the 43-byte default budget.
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)
        assert not tool_name_budget.overflows("lc", "query_docs_filesystem_docs_by_lang_chain")

    def test_long_tool_with_long_prefix_overflows(self, monkeypatch):
        # The original failing combination (#261's user-facing repro).
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)
        assert tool_name_budget.overflows(
            "docs_langchain", "query_docs_filesystem_docs_by_lang_chain"
        )

    def test_at_64_boundary_does_not_overflow(self, monkeypatch):
        """Exactly 64 chars composed must still pass — the spec is
        ``{1,64}`` (inclusive)."""
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)
        # Need: overhead(21) + len(prefix) + len(tool) == 64 → 43 budget
        prefix = "p" * 21  # warn threshold but not overflow
        tool = "t" * 22  # 21 + 22 = 43
        assert tool_name_budget.composed_length(prefix, tool) == 64
        assert not tool_name_budget.overflows(prefix, tool)

    def test_one_over_64_overflows(self, monkeypatch):
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)
        prefix = "p" * 21
        tool = "t" * 23  # 21 + 23 = 44 → composed 65
        assert tool_name_budget.composed_length(prefix, tool) == 65
        assert tool_name_budget.overflows(prefix, tool)


class TestPrefixThresholds:
    """Sanity bounds used by ``mms add``."""

    def test_hard_limit_is_overhead_minus_room_for_one_char_tool(self, monkeypatch):
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)
        # 64 - 21 - 1 = 42 — leaves exactly 1 char for the tool name
        assert tool_name_budget.prefix_hard_limit() == 42

    def test_hard_limit_loosens_with_short_server_name(self, monkeypatch):
        monkeypatch.setenv("MMS_CLIENT_SERVER_NAME", "mms")
        # 64 - (9 + 3) - 1 = 51 — much more room
        assert tool_name_budget.prefix_hard_limit() == 51

    def test_warn_threshold_is_constant(self, monkeypatch):
        # Empirical heuristic — leaves 22 chars for tool name (median).
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)
        assert tool_name_budget.prefix_warn_threshold() == 21


class TestClientServerNameLookup:
    def test_default_is_package_name(self, monkeypatch):
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)
        assert tool_name_budget.client_server_name() == "memtomem-stm"

    def test_env_var_overrides(self, monkeypatch):
        monkeypatch.setenv("MMS_CLIENT_SERVER_NAME", "custom-stm")
        assert tool_name_budget.client_server_name() == "custom-stm"


class TestSpecConstant:
    def test_tool_name_limit_matches_anthropic_spec(self):
        # Hardcoded reminder — if Anthropic ever raises this, every
        # threshold in this module has to be reconsidered.
        assert tool_name_budget.TOOL_NAME_LIMIT == 64
