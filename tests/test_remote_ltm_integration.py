"""End-to-end: SurfacingEngine talks to a remote MCP server via stdio.

After the move to remote-only LTM access, STM's surfacing engine reaches the
LTM exclusively through `McpClientSearchAdapter`, which spawns (or connects
to) a memtomem MCP server over stdio. These tests exercise that path against
a tiny fake MCP server (_fake_memtomem_server.py) so the integration runs in
under a second and doesn't require memtomem core to be installed.

What this proves:
1. McpClientSearchAdapter can spawn and initialise a child stdio MCP process.
2. SurfacingEngine routes search through the adapter only — there is no
   in-process SearchPipeline fallback path anymore.
3. The canned hits returned by the child server flow back through the
   adapter, the surfacing pipeline, and into the injected response.
4. ``include_session_context`` fetches scratchpad entries via the same
   adapter (``mem_do(action="scratch_get")``) and injects them alongside
   the LTM hits.

The ``McpClientSearchAdapter._parse_scratch_list`` unit tests at the bottom
exercise the text-shape edge cases (empty, expired, promoted) that would
otherwise need a more elaborate fake server fixture per scenario.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

_FAKE_SERVER = Path(__file__).parent / "_fake_memtomem_server.py"

LONG_RESPONSE = "x" * 200
VALID_ARGS = {"path": "src/auth.py", "_context_query": "JWT authentication"}


def _stdio_config(*, include_session_context: bool = False) -> SurfacingConfig:
    return SurfacingConfig(
        enabled=True,
        min_response_chars=10,
        cooldown_seconds=0,
        max_surfacings_per_minute=1000,
        auto_tune_enabled=False,
        include_session_context=include_session_context,
        fire_webhook=False,
        feedback_enabled=False,
        ltm_mcp_command=sys.executable,
        ltm_mcp_args=[str(_FAKE_SERVER)],
    )


@pytest.mark.asyncio
async def test_surfacing_via_remote_stdio_mcp_server():
    config = _stdio_config()

    adapter = McpClientSearchAdapter(config)
    await adapter.start()
    try:
        engine = SurfacingEngine(config, mcp_adapter=adapter)
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
    finally:
        await adapter.stop()

    assert "Relevant Memories" in output, output
    # Canned content from the fake server should land in the surfaced response
    assert "JWT authentication" in output


@pytest.mark.asyncio
async def test_session_context_via_remote_stdio_mcp_server():
    """include_session_context fetches scratch entries via mem_do(scratch_get)."""
    config = _stdio_config(include_session_context=True)

    adapter = McpClientSearchAdapter(config)
    await adapter.start()
    try:
        engine = SurfacingEngine(config, mcp_adapter=adapter)
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
    finally:
        await adapter.stop()

    assert "Relevant Memories" in output, output
    assert "JWT authentication" in output  # LTM injection still works
    assert "Working Memory" in output  # session context surfaced alongside
    assert "current_task" in output
    assert "drafting follow-up 4" in output


# ── McpClientSearchAdapter._parse_scratch_list unit tests ──────────────


def test_parse_scratch_list_empty_message():
    text = "Working memory is empty."
    assert McpClientSearchAdapter._parse_scratch_list(text) == []


def test_parse_scratch_list_blank_text():
    assert McpClientSearchAdapter._parse_scratch_list("") == []


def test_parse_scratch_list_single_entry():
    text = (
        "Working memory: 1 entries\n"
        "\n"
        "  current_task: drafting follow-up 4 implementation plan..."
    )
    entries = McpClientSearchAdapter._parse_scratch_list(text)
    assert entries == [
        {"key": "current_task", "value": "drafting follow-up 4 implementation plan"}
    ]


def test_parse_scratch_list_with_expiry_and_promotion():
    text = (
        "Working memory: 2 entries\n"
        "\n"
        "  api_key: hunter2... (expires: 2026-04-09T12:00:00) [promoted]\n"
        "  recent_branch: feat/stm-session-context-restore..."
    )
    entries = McpClientSearchAdapter._parse_scratch_list(text)
    assert entries == [
        {
            "key": "api_key",
            "value": "hunter2",
            "expires_at": "2026-04-09T12:00:00",
            "promoted": True,
        },
        {
            "key": "recent_branch",
            "value": "feat/stm-session-context-restore",
        },
    ]


def test_parse_scratch_list_skips_non_entry_lines():
    text = (
        "Working memory: 1 entries\n"
        "header line\n"
        "  current_task: actual entry...\n"
        "trailing footer"
    )
    entries = McpClientSearchAdapter._parse_scratch_list(text)
    assert entries == [{"key": "current_task", "value": "actual entry"}]
