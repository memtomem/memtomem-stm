"""Progressive surfacing outcomes and deferred source-analysis work."""

from unittest.mock import Mock
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, TextContent

from test_structured_delivery import manager, payload
from memtomem_stm.proxy._fastmcp_compat import to_call_tool_result
from memtomem_stm.proxy.progressive import ProgressiveChunker


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["append", "prepend"])
@pytest.mark.parametrize("format", ["legacy", "structured"])
@pytest.mark.parametrize("outcome", ["missing", "disabled", "success", "failure"])
async def test_progressive_surfacing_metrics_reflect_engine_outcome(
    tmp_path, monkeypatch, mode, format, outcome
):
    proxy, session = manager(tmp_path)
    proxy._config.upstream_servers["srv"].delivery_format = format
    proxy._config.upstream_servers["srv"].surfacing_enabled = outcome != "disabled"
    source = "source words " * 800
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    calls = []

    async def surface(*, response_text, source_response_chars=None, **kwargs):
        calls.append((response_text, source_response_chars, kwargs))
        if outcome == "failure":
            raise RuntimeError("injected failure")
        return response_text + "\nMEMORY_SUPPLEMENT"

    obs = Mock()
    proxy._surfacing_engine = (
        None
        if outcome == "missing"
        else SimpleNamespace(surface=surface, injection_mode=mode, observability=obs)
    )
    record = Mock(wraps=proxy.tracker.record)
    monkeypatch.setattr(proxy.tracker, "record", record)
    result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))
    skipped = outcome in ("missing", "disabled") or (format == "legacy" and mode == "prepend")
    metrics = record.call_args.args[0]
    assert metrics.surfacing_on_progressive_ok is (None if skipped else outcome == "success")
    assert metrics.surface_error == (
        "RuntimeError" if not skipped and outcome == "failure" else None
    )
    assert bool(calls) == (not skipped)
    if format == "structured":
        desc = payload(result)
        assert source.startswith(desc["payload"]["content"])
        if not skipped and outcome == "success":
            assert desc["supplements"]["surfacing"] == "\nMEMORY_SUPPLEMENT"
        else:
            assert "supplements" not in desc
        if calls:
            assert calls[0][0] == ""
            assert calls[0][1] == len(source)
    if outcome == "disabled":
        obs.record_skip.assert_called_once_with("read", "upstream_disabled")


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["legacy", "structured"])
async def test_progressive_detects_large_json_content_type_once(tmp_path, monkeypatch, format):
    proxy, session = manager(tmp_path)
    proxy._config.upstream_servers["srv"].delivery_format = format
    source = '{"body":"' + "x" * 8000 + '"}'
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    detect = Mock(wraps=ProgressiveChunker.detect_content_type)
    monkeypatch.setattr(ProgressiveChunker, "detect_content_type", detect)
    result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))
    assert not result.is_error
    assert detect.call_count == (1 if format == "structured" else 0)
    if format == "structured":
        assert payload(result)["payload"]["source_format"] == "json"
