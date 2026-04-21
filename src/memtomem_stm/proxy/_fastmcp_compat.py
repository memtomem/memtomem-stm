"""Compatibility layer for registering proxy tools with correct schema in FastMCP.

FastMCP infers tool parameter schemas from the handler's function signature.
Proxy handlers use **kwargs, which produces an incorrect schema (single "kwargs"
param). This module overrides both the schema AND the validation model so that:
  - Claude sees the upstream tool's actual parameter names
  - FastMCP validation passes any arguments through to the handler
  - Tool annotations (readOnlyHint, destructiveHint) are preserved
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata


class _ProxyPassthroughArgs(ArgModelBase):
    """Pydantic model that accepts and forwards any fields."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def model_dump_one_level(self) -> dict[str, Any]:
        result = super().model_dump_one_level()
        if self.__pydantic_extra__:
            result.update(self.__pydantic_extra__)
        return result


_PASSTHROUGH_METADATA = FuncMetadata(
    arg_model=_ProxyPassthroughArgs,
    output_schema=None,
    output_model=None,
    wrap_output=False,
)


def _tag_annotations_title(annotations: Any, server_name: str) -> Any:
    """Prepend ``[server_name]`` to ``annotations.title`` for picker disambiguation.

    MCP clients such as Claude Code's ``/mcp`` picker display ``annotations.title``
    in place of the tool ``name`` when it is set. Upstream servers that populate
    ``title`` (e.g. Playwright's "Close browser") then appear unattributed in the
    picker, while servers that leave it blank fall back to the prefixed ``name``
    (e.g. "Context7__resolve-library-id"). Tagging the title with the source
    server restores a uniform ``[server] original title`` display without
    touching the invocation ``name`` or input schema.

    Returns the original annotations unchanged when:
    - ``annotations`` is ``None`` (clients fall back to the prefixed ``name``),
    - ``title`` is missing or empty (same fallback path),
    - the object is not a pydantic model with ``model_copy`` (unknown shape).
    """
    if annotations is None:
        return None
    title = getattr(annotations, "title", None)
    if not title:
        return annotations
    new_title = f"[{server_name}] {title}"
    model_copy = getattr(annotations, "model_copy", None)
    if callable(model_copy):
        try:
            return model_copy(update={"title": new_title})
        except Exception:
            return annotations
    return annotations


def register_proxy_tool(
    server: FastMCP,
    handler: Any,
    info: Any,  # ProxyToolInfo
) -> None:
    """Register a proxy tool with the upstream's actual schema and annotations."""
    tagged_annotations = _tag_annotations_title(info.annotations, info.server)
    try:
        server.add_tool(
            handler,
            name=info.prefixed_name,
            description=f"[proxied] {info.description}",
            annotations=tagged_annotations,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "Failed to register proxy tool '%s' — FastMCP API may have changed",
            info.prefixed_name,
            exc_info=True,
        )
        return
    try:
        registered = server._tool_manager._tools.get(info.prefixed_name)
    except AttributeError:
        import logging

        logging.getLogger(__name__).warning(
            "Cannot override schema for '%s' — FastMCP internal API changed. "
            "Tool is registered but may show incorrect parameter schema.",
            info.prefixed_name,
        )
        return
    if registered is not None:
        if info.input_schema:
            registered.parameters = info.input_schema
        registered.fn_metadata = _PASSTHROUGH_METADATA
