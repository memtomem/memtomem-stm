"""A response nested too deeply to parse must still come back to the caller.

Valid JSON can be deeper than the interpreter recurses: ``json.loads`` gives
out past a few thousand levels, the recursive ``_sanitize_nonfinite`` walk near
``sys.getrecursionlimit()``, and the tiers' own tree walks (pretty-printing,
pruning, field extraction) a few levels after a tree that only just parsed —
where exactly moves with the Python version and the caller's stack.
``RecursionError`` used to escape all of them, so ``call_tool`` raised instead
of returning the upstream's successful response. ``_mm_json_loads`` now refuses
anything deeper than ``_MAX_JSON_DEPTH`` with ``ValueError``, which every caller
already treats as "not JSON": the response is compressed as plain text.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.compression import _MAX_JSON_DEPTH, _mm_json_loads
from memtomem_stm.proxy.config import CompressionStrategy, ProxyConfig, UpstreamServerConfig
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker

# Past the recursion limit, so the ``_sanitize_nonfinite`` walk fails on every
# supported Python even when ``json.loads`` itself succeeds.
_WALK_DEPTH = 1200
# Past the C-level limit of ``json.loads`` itself.
_PARSE_DEPTH = 100_000


def _call(tmp_path: Path, strategy: CompressionStrategy, text: str):
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=strategy,
        max_result_chars=500,
        max_retries=0,
        reconnect_delay_seconds=0.0,
    )
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": server_cfg},
    )
    mgr = ProxyManager(proxy_cfg, TokenTracker())
    session = AsyncMock()
    session.call_tool.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], is_error=False
    )
    mgr._connections["srv"] = UpstreamConnection(
        name="srv", config=server_cfg, session=session, tools=[]
    )
    return mgr.call_tool("srv", "tool", {})


def _deep_list(depth: int) -> str:
    return "[" * depth + "1" + "]" * depth


def _deep_dict(depth: int) -> str:
    return '{"a":' * depth + "1" + "}" * depth


def _deep_config(depth: int) -> str:
    # Config-shaped (two dict-valued keys), so TRUNCATE takes its key-aware
    # JSON path instead of treating a singleton container as text.
    return '{"y":{},"x":' + _deep_dict(depth - 1) + "}"


@pytest.mark.parametrize("depth", [_WALK_DEPTH, _PARSE_DEPTH])
@pytest.mark.parametrize("make", [_deep_list, _deep_dict, _deep_config])
def test_mm_json_loads_reports_too_deep_as_value_error(make, depth):
    with pytest.raises(ValueError, match="JSON nested"):
        _mm_json_loads(make(depth))


@pytest.mark.parametrize("make", [_deep_list, _deep_dict, _deep_config])
def test_mm_json_loads_depth_cap_boundary(make):
    assert _mm_json_loads(make(_MAX_JSON_DEPTH)) == json.loads(make(_MAX_JSON_DEPTH))
    with pytest.raises(ValueError, match=f"deeper than {_MAX_JSON_DEPTH}"):
        _mm_json_loads(make(_MAX_JSON_DEPTH + 1))


def test_mm_json_loads_depth_counts_nesting_not_size():
    wide = json.dumps({"rows": [{"id": i, "tags": [i, [i]]} for i in range(5000)]})
    assert _mm_json_loads(wide) == json.loads(wide)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strategy",
    [s for s in CompressionStrategy if s is not CompressionStrategy.LLM_SUMMARY],
)
@pytest.mark.parametrize("make", [_deep_list, _deep_dict, _deep_config])
@pytest.mark.parametrize("depth", [_MAX_JSON_DEPTH, _MAX_JSON_DEPTH + 1, _WALK_DEPTH])
async def test_call_tool_returns_deep_json_response(tmp_path, strategy, make, depth):
    result = await _call(tmp_path, strategy, make(depth))

    assert isinstance(result, str)
    assert result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strategy",
    [s for s in CompressionStrategy if s is not CompressionStrategy.LLM_SUMMARY],
)
@pytest.mark.parametrize("make", [_deep_list, _deep_dict, _deep_config])
async def test_cap_deep_tree_survives_a_deeper_caller_stack(tmp_path, strategy, make):
    """The cap is only a fix if a tree AT the cap can be walked as structure
    with stack to spare: the tiers' walks run a few levels deeper than the parse,
    and a real caller (MCP server, middleware, task group) sits on more frames
    than this test does."""

    text = make(_MAX_JSON_DEPTH)

    async def nested(frames: int):
        if frames:
            return await nested(frames - 1)
        # Still parsed as structure here, not refused as too deep — otherwise
        # the call below would pass on the text path and prove nothing.
        try:
            _mm_json_loads(text)
        except ValueError:
            pytest.fail("a tree at the cap was refused under a deeper stack", pytrace=False)
        return await _call(tmp_path, strategy, text)

    result = await nested(300)

    assert isinstance(result, str)
    assert result


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", [CompressionStrategy.SELECTIVE, CompressionStrategy.HYBRID])
async def test_call_tool_returns_response_json_loads_cannot_parse(tmp_path, strategy):
    result = await _call(tmp_path, strategy, _deep_dict(_PARSE_DEPTH))

    assert isinstance(result, str)
    assert result
