"""Tests for tool metadata optimization (Phase 2 of gateway improvements)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from memtomem_stm.proxy.config import (
    MIN_DESCRIPTION_CHARS,
    CompressionStrategy,
    HybridConfig,
    ProxyConfig,
    TailMode,
    ToolOverrideConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.tool_metadata import (
    PROXIED_PREFIX,
    convention_suffix,
    truncate_description,
)

SELECTIVE_SUFFIX = " | TOC response: use stm_proxy_select_chunks"


# ── _truncate_description ────────────────────────────────────────────────


class TestTruncateDescription:
    def test_empty_string(self):
        assert ProxyManager._truncate_description("", 100) == ""

    def test_under_limit(self):
        assert ProxyManager._truncate_description("Short desc.", 100) == "Short desc."

    def test_at_limit(self):
        text = "x" * 200
        assert ProxyManager._truncate_description(text, 200) == text

    def test_sentence_boundary(self):
        text = "First sentence. Second sentence. Third sentence that is long."
        result = ProxyManager._truncate_description(text, 35)
        assert result == "First sentence. Second sentence."

    def test_word_boundary_fallback(self):
        text = "one two three four five six seven eight nine ten"
        result = ProxyManager._truncate_description(text, 25)
        assert result.endswith("...")
        # The ellipsis is inside the budget, not added on top of it (#893).
        assert len(result) <= 25

    def test_no_space_fallback(self):
        text = "a" * 300
        result = ProxyManager._truncate_description(text, 100)
        assert result == "a" * 97 + "..."
        assert len(result) == 100

    def test_question_mark_boundary(self):
        text = "What is this? This is a tool. It does things."
        result = ProxyManager._truncate_description(text, 20)
        assert result == "What is this?"

    def test_budget_too_small_for_an_ellipsis(self):
        """Under four chars there is no room for text plus ``...``, so the
        result is a hard slice rather than an over-budget ellipsis (#893)."""
        for max_chars in (0, 1, 3):
            result = ProxyManager._truncate_description("hello world", max_chars)
            assert result == "hello world"[:max_chars]
            assert "..." not in result

    def test_negative_budget_yields_empty(self):
        """A negative budget must not slice from the end of the string."""
        assert ProxyManager._truncate_description("hello world", -5) == ""

    def test_never_exceeds_budget(self):
        """The cap holds for every input shape and every branch (#893)."""
        texts = [
            "one two three four five six seven eight nine ten eleven twelve",
            "a" * 300,
            "First sentence. Second sentence. Third sentence that runs long.",
            "What is this? This is a tool. It does things and more things.",
            "no-spaces-at-all-in-this-quite-long-hyphenated-description-here",
        ]
        for max_chars in (4, 10, 25, 100):
            for text in texts:
                result = ProxyManager._truncate_description(text, max_chars)
                assert len(result) <= max_chars, (max_chars, text, result)


# ── _distill_schema ──────────────────────────────────────────────────────


class TestDistillSchema:
    def test_no_strip(self):
        schema = {"type": "object", "description": "A schema"}
        result = ProxyManager._distill_schema(schema, strip_descriptions=False)
        assert result == schema  # unchanged

    def test_strip_top_level_description(self):
        schema = {"type": "object", "description": "Remove me"}
        result = ProxyManager._distill_schema(schema, strip_descriptions=True)
        assert "description" not in result
        assert result["type"] == "object"

    def test_strip_nested_descriptions(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The name"},
                "age": {"type": "integer", "description": "The age"},
            },
        }
        result = ProxyManager._distill_schema(schema, strip_descriptions=True)
        assert "description" not in result["properties"]["name"]
        assert "description" not in result["properties"]["age"]
        assert result["properties"]["name"]["type"] == "string"

    def test_strip_examples(self):
        schema = {
            "type": "object",
            "examples": [{"name": "foo"}],
            "properties": {"x": {"type": "string", "examples": ["a", "b"]}},
        }
        result = ProxyManager._distill_schema(schema, strip_descriptions=True)
        assert "examples" not in result
        assert "examples" not in result["properties"]["x"]

    def test_preserves_non_description_keys(self):
        schema = {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "enum": ["a", "b"]},
            },
        }
        result = ProxyManager._distill_schema(schema, strip_descriptions=True)
        assert result["required"] == ["name"]
        assert result["properties"]["name"]["enum"] == ["a", "b"]

    def test_list_of_schemas(self):
        schema = {
            "oneOf": [
                {"type": "string", "description": "A string"},
                {"type": "integer", "description": "An int"},
            ],
        }
        result = ProxyManager._distill_schema(schema, strip_descriptions=True)
        assert len(result["oneOf"]) == 2
        assert "description" not in result["oneOf"][0]
        assert "description" not in result["oneOf"][1]


# ── get_proxy_tools integration ──────────────────────────────────────────


def _fake_tool(name: str, description: str = "", schema: dict | None = None):
    return SimpleNamespace(
        name=name,
        description=description,
        input_schema=schema or {"type": "object"},
        annotations=None,
    )


def _make_manager_with_tools(
    tools: list,
    tool_overrides: dict | None = None,
    max_description_chars: int = 200,
    strip_schema_descriptions: bool = False,
    server_max_desc: int = 200,
    server_strip: bool = False,
    compression: CompressionStrategy | None = CompressionStrategy.AUTO,
    hybrid: HybridConfig | None = None,
    advertise_context_query: bool = False,
    default_compression: CompressionStrategy | None = None,
) -> ProxyManager:
    # ``compression=None`` leaves the per-server key OUT of ``model_fields_set``
    # — the configuration that hands resolution to the global default (#924) —
    # as distinct from an operator explicitly typing ``compression: auto``.
    server_kwargs: dict = {
        "prefix": "test",
        "tool_overrides": tool_overrides or {},
        "max_description_chars": server_max_desc,
        "strip_schema_descriptions": server_strip,
        "hybrid": hybrid,
    }
    if compression is not None:
        server_kwargs["compression"] = compression
    server_cfg = UpstreamServerConfig(**server_kwargs)
    proxy_kwargs: dict = {
        "config_path": Path("/tmp/proxy.json"),
        "upstream_servers": {"srv": server_cfg},
        "max_description_chars": max_description_chars,
        "strip_schema_descriptions": strip_schema_descriptions,
        "advertise_context_query": advertise_context_query,
    }
    if default_compression is not None:
        proxy_kwargs["default_compression"] = default_compression
    proxy_cfg = ProxyConfig(**proxy_kwargs)
    mgr = ProxyManager(proxy_cfg, TokenTracker())
    conn = UpstreamConnection(
        name="srv",
        config=server_cfg,
        session=AsyncMock(),
        tools=tools,
    )
    mgr._connections["srv"] = conn
    return mgr


class TestGetProxyToolsFiltering:
    def test_hidden_tool_excluded(self):
        tools = [_fake_tool("visible"), _fake_tool("secret")]
        mgr = _make_manager_with_tools(
            tools,
            tool_overrides={"secret": ToolOverrideConfig(hidden=True)},
        )
        proxy_tools = mgr.get_proxy_tools()
        names = [t.original_name for t in proxy_tools]
        assert "visible" in names
        assert "secret" not in names

    def test_all_tools_visible_by_default(self):
        tools = [_fake_tool("a"), _fake_tool("b"), _fake_tool("c")]
        mgr = _make_manager_with_tools(tools)
        assert len(mgr.get_proxy_tools()) == 3


class TestGetProxyToolsDescription:
    def test_description_truncated(self):
        long_desc = "This is a very detailed description. " * 20
        tools = [_fake_tool("tool", description=long_desc)]
        mgr = _make_manager_with_tools(tools, max_description_chars=50)
        proxy_tools = mgr.get_proxy_tools()
        # The budget the manager hands the client reserves the registration
        # prefix, so what it stores is the cap minus that prefix (#893).
        assert len(proxy_tools[0].description) <= 50 - len(PROXIED_PREFIX)

    def test_description_override(self):
        tools = [_fake_tool("tool", description="Original long description.")]
        mgr = _make_manager_with_tools(
            tools,
            tool_overrides={"tool": ToolOverrideConfig(description_override="Custom desc.")},
        )
        proxy_tools = mgr.get_proxy_tools()
        assert proxy_tools[0].description == "Custom desc."

    def test_server_max_desc_used(self):
        long_desc = "x" * 500
        tools = [_fake_tool("tool", description=long_desc)]
        mgr = _make_manager_with_tools(tools, server_max_desc=100, max_description_chars=300)
        proxy_tools = mgr.get_proxy_tools()
        # min(server=100, global=300) = 100
        assert len(proxy_tools[0].description) <= 100 - len(PROXIED_PREFIX)

    def test_global_max_desc_used_when_it_is_the_stricter(self):
        """The other direction of the ``min`` composition (#893)."""
        tools = [_fake_tool("tool", description="x" * 500)]
        mgr = _make_manager_with_tools(tools, server_max_desc=300, max_description_chars=100)
        assert len(mgr.get_proxy_tools()[0].description) <= 100 - len(PROXIED_PREFIX)

    def test_raising_only_the_global_does_not_widen_the_server_budget(self):
        """``min`` composition, not override: the stricter side still binds."""
        tools = [_fake_tool("tool", description="x" * 500)]
        tight = _make_manager_with_tools(tools, server_max_desc=100, max_description_chars=100)
        raised = _make_manager_with_tools(tools, server_max_desc=100, max_description_chars=500)
        assert (
            len(raised.get_proxy_tools()[0].description)
            == len(tight.get_proxy_tools()[0].description)
            <= 100 - len(PROXIED_PREFIX)
        )


class TestGetProxyToolsSchema:
    def test_context_query_not_advertised_by_default(self):
        mgr = _make_manager_with_tools([_fake_tool("tool")])
        assert "_context_query" not in mgr.get_proxy_tools()[0].input_schema.get("properties", {})

    def test_context_query_advertised_when_opted_in_without_mutating_upstream(self):
        schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "additionalProperties": False,
        }
        mgr = _make_manager_with_tools(
            [_fake_tool("tool", schema=schema)], advertise_context_query=True
        )
        exposed = mgr.get_proxy_tools()[0].input_schema
        assert exposed["properties"]["_context_query"]["type"] == "string"
        assert exposed["additionalProperties"] is False
        assert "_context_query" not in schema["properties"]

    def test_existing_context_query_contract_is_preserved(self):
        existing = {"type": "integer", "description": "upstream owns this"}
        schema = {"type": "object", "properties": {"_context_query": existing}}
        mgr = _make_manager_with_tools(
            [_fake_tool("tool", schema=schema)], advertise_context_query=True
        )
        assert mgr.get_proxy_tools()[0].input_schema["properties"]["_context_query"] == existing

    def test_schema_distilled_global(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The name"},
            },
        }
        tools = [_fake_tool("tool", schema=schema)]
        mgr = _make_manager_with_tools(tools, strip_schema_descriptions=True)
        proxy_tools = mgr.get_proxy_tools()
        assert "description" not in proxy_tools[0].input_schema["properties"]["name"]

    def test_schema_distilled_server(self):
        schema = {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Query"},
            },
        }
        tools = [_fake_tool("tool", schema=schema)]
        mgr = _make_manager_with_tools(tools, server_strip=True)
        proxy_tools = mgr.get_proxy_tools()
        assert "description" not in proxy_tools[0].input_schema["properties"]["q"]

    def test_schema_not_distilled_by_default(self):
        schema = {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Query"},
            },
        }
        tools = [_fake_tool("tool", schema=schema)]
        mgr = _make_manager_with_tools(tools)
        proxy_tools = mgr.get_proxy_tools()
        assert proxy_tools[0].input_schema["properties"]["q"]["description"] == "Query"


class TestTokenSavings:
    def test_50_tools_description_savings(self):
        """50 tools with 500-char descriptions → truncated to 200 saves >50%."""
        tools = [_fake_tool(f"tool_{i}", description="d" * 500) for i in range(50)]
        mgr = _make_manager_with_tools(tools, max_description_chars=200)
        proxy_tools = mgr.get_proxy_tools()
        total_chars = sum(len(t.description) for t in proxy_tools)
        original_chars = 50 * 500
        savings_pct = (1 - total_chars / original_chars) * 100
        assert savings_pct > 50


# ── Convention suffix ──────────────────────────────────────────────────


class TestConventionSuffix:
    def test_selective_suffix(self):
        tools = [_fake_tool("t", description="Fetches a document.")]
        mgr = _make_manager_with_tools(tools, compression=CompressionStrategy.SELECTIVE)
        desc = mgr.get_proxy_tools()[0].description
        assert desc.endswith(" | TOC response: use stm_proxy_select_chunks")

    def test_progressive_suffix(self):
        tools = [_fake_tool("t", description="Fetches a document.")]
        mgr = _make_manager_with_tools(tools, compression=CompressionStrategy.PROGRESSIVE)
        desc = mgr.get_proxy_tools()[0].description
        assert desc.endswith(" | Chunked: use stm_proxy_read_more for more")

    def test_hybrid_toc_suffix(self):
        """hybrid + default HybridConfig (tail_mode=TOC) → suffix appended."""
        tools = [_fake_tool("t", description="Fetches a document.")]
        mgr = _make_manager_with_tools(tools, compression=CompressionStrategy.HYBRID)
        desc = mgr.get_proxy_tools()[0].description
        assert desc.endswith(" | Head+TOC: use stm_proxy_select_chunks")

    def test_hybrid_truncate_no_suffix(self):
        """hybrid + tail_mode=TRUNCATE → no suffix."""
        tools = [_fake_tool("t", description="Fetches a document.")]
        mgr = _make_manager_with_tools(
            tools,
            compression=CompressionStrategy.HYBRID,
            hybrid=HybridConfig(tail_mode=TailMode.TRUNCATE),
        )
        desc = mgr.get_proxy_tools()[0].description
        assert "stm_proxy_select_chunks" not in desc
        assert "stm_proxy_read_more" not in desc

    def test_auto_no_suffix(self):
        tools = [_fake_tool("t", description="Fetches a document.")]
        mgr = _make_manager_with_tools(tools, compression=CompressionStrategy.AUTO)
        desc = mgr.get_proxy_tools()[0].description
        assert desc == "Fetches a document."

    def test_none_no_suffix(self):
        tools = [_fake_tool("t", description="Fetches a document.")]
        mgr = _make_manager_with_tools(tools, compression=CompressionStrategy.NONE)
        desc = mgr.get_proxy_tools()[0].description
        assert desc == "Fetches a document."

    def test_truncate_no_suffix(self):
        tools = [_fake_tool("t", description="Fetches a document.")]
        mgr = _make_manager_with_tools(tools, compression=CompressionStrategy.TRUNCATE)
        desc = mgr.get_proxy_tools()[0].description
        assert desc == "Fetches a document."

    def test_per_tool_override_suffix(self):
        """Server uses AUTO, but one tool overridden to selective → suffix on that tool."""
        tools = [_fake_tool("normal"), _fake_tool("special")]
        mgr = _make_manager_with_tools(
            tools,
            compression=CompressionStrategy.AUTO,
            tool_overrides={
                "special": ToolOverrideConfig(compression=CompressionStrategy.SELECTIVE),
            },
        )
        proxy_tools = {t.original_name: t for t in mgr.get_proxy_tools()}
        assert "stm_proxy_select_chunks" not in proxy_tools["normal"].description
        assert proxy_tools["special"].description.endswith(
            " | TOC response: use stm_proxy_select_chunks"
        )


class TestGlobalDefaultCompressionSuffix:
    """An upstream configured only by ``default_compression`` advertises the
    same suffix it would with the per-server key set (#924).

    The call path resolves compression through the global default whenever the
    operator omitted the per-server key; advertisement read ``cfg.compression``
    directly, so these configurations compressed responses while advertising
    nothing about how to retrieve the rest.
    """

    @pytest.mark.parametrize(
        "strategy,expected_suffix",
        [
            (CompressionStrategy.SELECTIVE, " | TOC response: use stm_proxy_select_chunks"),
            (CompressionStrategy.PROGRESSIVE, " | Chunked: use stm_proxy_read_more for more"),
            (CompressionStrategy.HYBRID, " | Head+TOC: use stm_proxy_select_chunks"),
        ],
    )
    def test_global_default_advertises_the_same_suffix(self, strategy, expected_suffix):
        """Global-only configuration pairs with the per-server form exactly."""
        tools = [_fake_tool("t", description="Reads a file.")]
        global_only = _make_manager_with_tools(
            tools, compression=None, default_compression=strategy
        )
        per_server = _make_manager_with_tools(tools, compression=strategy)
        advertised = global_only.get_proxy_tools()[0].description
        assert advertised.endswith(expected_suffix)
        assert advertised == per_server.get_proxy_tools()[0].description

    def test_explicit_server_auto_beats_the_global_default(self):
        """``compression: auto`` typed by the operator is a choice, not an omission.

        The call path honours it over the global default, so advertisement must
        emit no suffix — matching the response the client will actually get.
        """
        tools = [_fake_tool("t", description="Reads a file.")]
        mgr = _make_manager_with_tools(
            tools,
            compression=CompressionStrategy.AUTO,
            default_compression=CompressionStrategy.PROGRESSIVE,
        )
        assert mgr.get_proxy_tools()[0].description == "Reads a file."

    def test_per_tool_override_beats_the_global_default(self):
        tools = [_fake_tool("normal"), _fake_tool("special")]
        mgr = _make_manager_with_tools(
            tools,
            compression=None,
            default_compression=CompressionStrategy.PROGRESSIVE,
            tool_overrides={
                "special": ToolOverrideConfig(compression=CompressionStrategy.SELECTIVE),
            },
        )
        proxy_tools = {t.original_name: t for t in mgr.get_proxy_tools()}
        assert proxy_tools["normal"].description.endswith(
            " | Chunked: use stm_proxy_read_more for more"
        )
        assert proxy_tools["special"].description.endswith(
            " | TOC response: use stm_proxy_select_chunks"
        )

    def test_omitted_everywhere_stays_auto(self):
        """Both keys omitted → AUTO on both paths → no suffix, as before."""
        tools = [_fake_tool("t", description="Reads a file.")]
        mgr = _make_manager_with_tools(tools, compression=None)
        assert mgr.get_proxy_tools()[0].description == "Reads a file."

    def test_a_rebuild_after_hot_reload_advertises_what_calls_resolve(self):
        """A rebuild resolves the suffix live, not off the connect-time config.

        ``tools/list_changed`` re-advertises without refreshing ``conn.config``,
        so a suffix built from the connect-time server config could name a
        follow-up tool the call would never use: connect with ``compression``
        omitted, then have the operator add an explicit ``compression:
        selective`` while the global default says ``progressive``. Resolving the
        connect-time omission against the live global advertises
        ``stm_proxy_read_more`` while calls resolve ``selective``.
        """
        tools = [_fake_tool("t", description="Reads a file.")]
        mgr = _make_manager_with_tools(
            tools, compression=None, default_compression=CompressionStrategy.PROGRESSIVE
        )
        edited_server = UpstreamServerConfig(
            prefix="test", compression=CompressionStrategy.SELECTIVE
        )
        mgr._config_loader.seed(
            mgr._config.model_copy(update={"upstream_servers": {"srv": edited_server}})
        )

        advertised = mgr.get_proxy_tools()[0].description
        resolved = mgr._resolve_tool_config("srv", "t").compression

        assert resolved == CompressionStrategy.SELECTIVE
        assert advertised.endswith(SELECTIVE_SUFFIX)

    def test_a_rebuild_honours_a_hot_reloaded_per_tool_override(self):
        """The per-tool override feeding the suffix is read live too."""
        tools = [_fake_tool("t", description="Reads a file.")]
        mgr = _make_manager_with_tools(tools, compression=CompressionStrategy.PROGRESSIVE)
        edited_server = UpstreamServerConfig(
            prefix="test",
            compression=CompressionStrategy.PROGRESSIVE,
            tool_overrides={"t": ToolOverrideConfig(compression=CompressionStrategy.SELECTIVE)},
        )
        mgr._config_loader.seed(
            mgr._config.model_copy(update={"upstream_servers": {"srv": edited_server}})
        )

        assert mgr._resolve_tool_config("srv", "t").compression == CompressionStrategy.SELECTIVE
        assert mgr.get_proxy_tools()[0].description.endswith(SELECTIVE_SUFFIX)

    def test_description_override_stays_connect_time(self):
        """Only the suffix goes live — advertised TEXT keeps its lifetime.

        Guards the boundary the fix draws: a hot-reloaded
        ``description_override`` must NOT reach the client without a reconnect,
        which is what docs/configuration.md promises for advertised metadata.
        """
        tools = [_fake_tool("t", description="Reads a file.")]
        mgr = _make_manager_with_tools(tools, compression=None)
        edited_server = UpstreamServerConfig(
            prefix="test",
            tool_overrides={"t": ToolOverrideConfig(description_override="Edited after connect.")},
        )
        mgr._config_loader.seed(
            mgr._config.model_copy(update={"upstream_servers": {"srv": edited_server}})
        )

        assert mgr.get_proxy_tools()[0].description == "Reads a file."


class TestSuffixBudget:
    def test_suffix_within_budget(self):
        """description + suffix stays within max_description_chars."""
        long_desc = "A" * 300
        tools = [_fake_tool("t", description=long_desc)]
        mgr = _make_manager_with_tools(
            tools,
            compression=CompressionStrategy.SELECTIVE,
            max_description_chars=200,
        )
        desc = mgr.get_proxy_tools()[0].description
        assert len(desc) <= 200 - len(PROXIED_PREFIX)
        assert desc.endswith(SELECTIVE_SUFFIX)

    def test_tight_budget_keeps_the_suffix_and_starves_the_body(self):
        """The suffix is functional — it names the follow-up tool — so it wins
        over upstream text whenever it fits inside the cap (#893)."""
        tools = [_fake_tool("t", description="A" * 100)]
        mgr = _make_manager_with_tools(
            tools,
            compression=CompressionStrategy.SELECTIVE,
            max_description_chars=60,
        )
        desc = mgr.get_proxy_tools()[0].description
        assert desc.endswith(SELECTIVE_SUFFIX)
        assert len(desc) <= 60 - len(PROXIED_PREFIX)

    def test_budget_too_small_for_the_suffix_drops_it_whole(self):
        """A suffix that cannot fit is dropped, never truncated — a cut hint is
        incomplete, and may not name its tool at all (#893)."""
        tools = [_fake_tool("t", description="A" * 100)]
        mgr = _make_manager_with_tools(
            tools,
            compression=CompressionStrategy.SELECTIVE,
            max_description_chars=32,
        )
        desc = mgr.get_proxy_tools()[0].description
        # Equality, not absence of the tool name: a regression that cut the
        # suffix mid-string would still hide "stm_proxy_select_chunks" and stay
        # within budget, so only the exact no-suffix result rules it out.
        assert desc == truncate_description("A" * 100, 32 - len(PROXIED_PREFIX))
        assert len(desc) <= 32 - len(PROXIED_PREFIX)

    def test_the_suffix_fits_exactly_at_its_own_boundary(self):
        """The cap that fits the suffix and nothing else keeps it, and one
        character less drops it.

        Straddling caps alone would pass a ``<``-for-``<=`` mutation of the fit
        test; these two pin the boundary itself (#893).
        """
        boundary = len(SELECTIVE_SUFFIX) + len(PROXIED_PREFIX)  # 54
        tools = [_fake_tool("t", description="A" * 100)]

        fits = _make_manager_with_tools(
            tools, compression=CompressionStrategy.SELECTIVE, max_description_chars=boundary
        ).get_proxy_tools()[0]
        assert fits.description == SELECTIVE_SUFFIX  # whole suffix, zero body

        just_short = _make_manager_with_tools(
            tools, compression=CompressionStrategy.SELECTIVE, max_description_chars=boundary - 1
        ).get_proxy_tools()[0]
        assert "stm_proxy_select_chunks" not in just_short.description

    def test_description_override_is_capped_like_upstream_text(self):
        """An explicit override is budgeted on the same path (#893)."""
        tools = [_fake_tool("t", description="short")]
        mgr = _make_manager_with_tools(
            tools,
            compression=CompressionStrategy.SELECTIVE,
            max_description_chars=60,
            tool_overrides={"t": ToolOverrideConfig(description_override="B" * 300)},
        )
        desc = mgr.get_proxy_tools()[0].description
        # The retained body must come from the override, not from the short
        # upstream text — otherwise a length-only assertion passes either way.
        body = desc[: -len(SELECTIVE_SUFFIX)]
        assert body.startswith("B")
        assert "short" not in desc
        assert len(desc) <= 60 - len(PROXIED_PREFIX)

    def test_empty_description_contributes_no_text(self):
        """An upstream that sets no description contributes none.

        With no suffix that leaves the bare prefix once registration adds it —
        not a cap violation, but the shape the floor does NOT prevent, pinned
        so the claim stays honest (polish tracked in #922). Where a suffix
        fits, that is what the client sees instead of a bare prefix.
        """
        bare = _make_manager_with_tools([_fake_tool("t", description="")])
        assert bare.get_proxy_tools()[0].description == ""

        suffixed = _make_manager_with_tools(
            [_fake_tool("t", description="")],
            compression=CompressionStrategy.SELECTIVE,
        )
        assert suffixed.get_proxy_tools()[0].description == SELECTIVE_SUFFIX


# ── Client-visible cap, end to end ─────────────────────────────────────


class TestClientVisibleCap:
    """``max_description_chars`` is an exact cap on what the client receives.

    The manager's own budgeting is only half the story: registration prepends
    ``[proxied] `` afterwards, so the invariant is only real when measured on
    the description a ``ClientSession`` decodes from ``tools/list`` (#893).
    """

    @pytest.mark.parametrize("cap", [32, 53, 54, 60, 200])
    @pytest.mark.parametrize(
        "compression",
        [
            CompressionStrategy.NONE,
            CompressionStrategy.SELECTIVE,
            CompressionStrategy.PROGRESSIVE,
            CompressionStrategy.HYBRID,
        ],
    )
    async def test_listed_description_never_exceeds_the_cap(self, cap, compression):
        from mcp import ClientSession
        from mcp.client._memory import InMemoryTransport
        from mcp.server.mcpserver import MCPServer

        from memtomem_stm.proxy._fastmcp_compat import register_proxy_tool

        # Four shapes, so that across the cap range every composition the
        # advertisement can produce is measured: a sentence-boundary cut, an
        # unbroken run that spends an ellipsis, an override, and an upstream
        # that supplies no description at all. Which branch a given shape takes
        # varies with the cap — that is the point of sweeping caps.
        tools = [
            _fake_tool("sentences", description="A long upstream description. " * 20),
            _fake_tool("unbroken", description="A" * 500),
            _fake_tool("overridden", description="short upstream text"),
            _fake_tool("empty", description=""),
        ]
        mgr = _make_manager_with_tools(
            tools,
            compression=compression,
            max_description_chars=cap,
            server_max_desc=cap,
            tool_overrides={"overridden": ToolOverrideConfig(description_override="B" * 300)},
        )
        server = MCPServer("description-cap-test")
        for info in mgr.get_proxy_tools():

            async def proxy_tool(**kwargs: object) -> str:
                return "ok"

            register_proxy_tool(server, proxy_tool, info)

        async with InMemoryTransport(server) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                listed = await session.list_tools()

        assert len(listed.tools) == 4
        suffix = convention_suffix(compression, None)
        for tool in listed.tools:
            assert tool.description is not None
            assert tool.description.startswith(PROXIED_PREFIX)
            assert len(tool.description) <= cap, (tool.name, tool.description)
            if suffix and len(suffix) <= cap - len(PROXIED_PREFIX):
                assert tool.description.endswith(suffix)
            elif suffix:
                # Dropped whole: not one fragment of the suffix may survive,
                # which absence of "stm_proxy_" alone would not catch. Only the
                # one-character fragment is excluded — it is a bare space, and
                # a prefix-only description legitimately ends with one.
                fragments = [suffix[:n] for n in range(2, len(suffix) + 1)]
                assert not any(tool.description.endswith(f) for f in fragments)
            if tool.name.endswith("overridden"):
                # The override, not the short upstream text, supplied the body.
                assert "short upstream text" not in tool.description


# ── Config validation ──────────────────────────────────────────────────


class TestMaxDescriptionCharsValidation:
    """The cap must leave room for the fixed prefix plus real text (#893)."""

    def test_global_rejects_a_budget_below_the_floor(self):
        with pytest.raises(ValidationError):
            ProxyConfig(
                config_path=Path("/tmp/proxy.json"),
                max_description_chars=MIN_DESCRIPTION_CHARS - 1,
            )

    def test_server_rejects_a_budget_below_the_floor(self):
        with pytest.raises(ValidationError):
            UpstreamServerConfig(prefix="test", max_description_chars=MIN_DESCRIPTION_CHARS - 1)

    def test_floor_itself_is_accepted(self):
        server = UpstreamServerConfig(prefix="test", max_description_chars=MIN_DESCRIPTION_CHARS)
        proxy = ProxyConfig(
            config_path=Path("/tmp/proxy.json"), max_description_chars=MIN_DESCRIPTION_CHARS
        )
        # Assert the stored value, not the model's truthiness: a constructed
        # pydantic model is always truthy, so ``assert Model(...)`` would pass
        # even against a field that silently coerced the value away.
        assert server.max_description_chars == MIN_DESCRIPTION_CHARS
        assert proxy.max_description_chars == MIN_DESCRIPTION_CHARS

    def test_the_floor_leaves_room_for_the_prefix_and_an_ellipsis(self):
        """The floor is a usability choice, but it must at least clear the
        costs the budget charges before any upstream text survives."""
        assert MIN_DESCRIPTION_CHARS > len(PROXIED_PREFIX) + len("...")
