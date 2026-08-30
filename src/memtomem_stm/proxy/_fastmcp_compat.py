"""Compatibility layer for registering proxy tools with the correct schema.

The SDK's ``MCPServer`` (``FastMCP`` before mcp 2.0 — hence this module's
name) infers tool parameter schemas from the handler's function signature.
Proxy handlers use **kwargs, which produces an incorrect schema (single
"kwargs" param). This module overrides both the schema AND the validation
model so that:
  - Claude sees the upstream tool's actual parameter names
  - SDK validation passes any arguments through to the handler
  - Tool annotations (read_only_hint, destructive_hint) are preserved
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.types import CallToolResult, TextContent

from memtomem_stm.proxy.tool_metadata import PROXIED_PREFIX, tag_annotations_title


def to_call_tool_result(result: str | list | CallToolResult) -> CallToolResult:
    """Normalize a ``ProxyManager.call_tool`` return into a ``CallToolResult``.

    The proxy handler always returns a full ``CallToolResult`` to the SDK:
    both ``FuncMetadata.convert_result`` and the lowlevel ``call_tool`` handler
    pass a ``CallToolResult`` through verbatim, which is what preserves
    ``structuredContent``/``_meta`` on the wire and the content-block order end
    to end. The ``str``/``list`` shapes wrap into exactly the envelope the
    lowlevel server would synthesize for them (single ``TextContent`` / the
    blocks, no structured content, not an error) — wire-identical to the
    pre-envelope behavior.
    """
    if isinstance(result, CallToolResult):
        return result
    if isinstance(result, str):
        return CallToolResult(content=[TextContent(type="text", text=result)])
    return CallToolResult(content=list(result))


class _ProxyPassthroughArgs(ArgModelBase):
    """Pydantic model that accepts and forwards any fields."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def model_dump_one_level(self) -> dict[str, Any]:
        result = super().model_dump_one_level()
        if self.__pydantic_extra__:
            result.update(self.__pydantic_extra__)
        return result


class _ProxyFuncMetadata(FuncMetadata):
    """FuncMetadata that may CARRY an upstream ``outputSchema`` (the SDK's
    ``Tool.output_schema`` cached_property reads ``fn_metadata.output_schema``,
    which is how the schema reaches tools/list) but never VALIDATES results
    against it: we cannot build a pydantic output model from an arbitrary
    upstream JSON schema, the upstream server already validated its own
    result, and the base ``convert_result`` asserts ``output_model`` is set
    whenever ``output_schema`` is — which would turn every proxied call into
    an AssertionError. The proxy handler always returns a ``CallToolResult``
    (see ``to_call_tool_result``), so the ``super()`` tail is defensive-only.

    ``InputRequiredResult`` (mcp 2.0's multi-round flow) is not special-cased
    here on purpose: it fails the ``CallToolResult`` check and reaches
    ``super()``, which passes it through before touching ``output_model``.
    """

    def convert_result(self, result: Any) -> Any:
        if isinstance(result, CallToolResult):
            return result
        return super().convert_result(result)


_PASSTHROUGH_METADATA = _ProxyFuncMetadata(
    arg_model=_ProxyPassthroughArgs,
    output_schema=None,
    output_model=None,
    wrap_output=False,
)


def _passthrough_metadata(output_schema: dict[str, Any] | None) -> _ProxyFuncMetadata:
    """Passthrough metadata for one proxied tool.

    Returns the shared singleton when the upstream declared no
    ``outputSchema`` (the overwhelmingly common case — keeps object identity
    for existing pins) and a per-tool instance carrying the schema otherwise.
    """
    if output_schema is None:
        return _PASSTHROUGH_METADATA
    return _ProxyFuncMetadata(
        arg_model=_ProxyPassthroughArgs,
        output_schema=output_schema,
        output_model=None,
        wrap_output=False,
    )


def register_proxy_tool(
    server: MCPServer,
    handler: Any,
    info: Any,  # ProxyToolInfo
) -> bool:
    """Register a proxy tool with the upstream's actual schema and annotations.

    Returns whether this call took ownership of the name; the caller removes
    exactly the names that returned True at teardown (#891). False when
    ``add_tool`` fails or the name is already occupied — note that the schema
    overrides further down degrade without giving up ownership, since the tool
    is registered either way. True means the name was free immediately before a
    successful ``add_tool`` — or, when the SDK internals needed to establish
    that are absent, that ``add_tool`` reported success and ownership had to be
    assumed. That fallback is the pre-#891 posture: in an SDK where the name
    cannot be probed, claiming risks removing a tool that was already there,
    while declining would leak every proxied registration in a host that
    outlives the session. Neither is provable, and the leak is the larger of
    the two.
    """
    tagged_annotations = tag_annotations_title(info.annotations, info.server)
    # ``getattr`` + dict narrowing (not attribute access) so a pre-envelope
    # ``ProxyToolInfo`` shape — the degradation tests' SimpleNamespace and
    # MagicMock stand-ins included — still registers rather than aborting
    # before ``add_tool`` or feeding a non-dict into Tool validation.
    raw_output_schema = getattr(info, "output_schema", None)
    raw_meta = getattr(info, "meta", None)
    output_schema = raw_output_schema if isinstance(raw_output_schema, dict) else None
    tool_meta = raw_meta if isinstance(raw_meta, dict) else None
    # No ``execution`` kwarg, deliberately: the proxy serves every tool
    # synchronously, so the advertised tool declares no task support (#892).
    # Upstream tools that *require* it are withheld one layer up, in
    # ``tool_eligibility``; the rest advertise as plain sync tools. The pinned
    # SDK offers no seam to forward it through even deliberately — ``add_tool``
    # takes no such kwarg and the server-side ``Tool`` model has no such field
    # — so this comment guards the intent, and
    # ``test_optional_task_upstream_reaches_the_client_without_execution``
    # guards what a client receives if a future SDK grows one.
    add_tool_kwargs: dict[str, Any] = {
        "name": info.prefixed_name,
        # The description arrives already budgeted for this prefix (#893).
        "description": f"{PROXIED_PREFIX}{info.description}",
        "annotations": tagged_annotations,
    }
    if tool_meta is not None:
        # ``meta=`` is public ``add_tool`` API and flows to
        # tools/list as ``_meta``. Passed only when the upstream set it, so
        # meta-less tools produce exactly the pre-envelope call — and an SDK
        # that drops the kwarg fails only meta-bearing tools into the
        # version-drift warning below instead of all of them.
        add_tool_kwargs["meta"] = tool_meta
    # The SDK treats a duplicate ``add_tool`` as a successful no-op: it returns
    # the tool already under that name without inserting the new handler, and
    # ``MCPServer.add_tool`` discards that return value, so a collision is
    # indistinguishable from a fresh registration at the call site. Claiming it
    # would mean overwriting a caller's schema and then deleting their tool at
    # teardown — the embedded/library reuse case #891 is about. Check first and
    # decline the name instead. Guarded like the two version-drift accesses
    # below: an SDK without this internal falls through to ``add_tool``, which
    # is exactly the pre-#891 behavior. When the probe DOES run, reaching it
    # and then a non-raising ``add_tool`` is what makes the ``True`` returned
    # below a fact rather than an assumption — and it stays a fact even if the
    # schema lookup further down fails or comes back empty, because nothing
    # between the two can hand the name to someone else.
    try:
        already_registered = info.prefixed_name in server._tool_manager._tools
    except AttributeError:
        already_registered = False
    if already_registered:
        import logging

        logging.getLogger(__name__).warning(
            "Not registering proxy tool '%s' — a tool is already registered "
            "under that name; leaving the existing one untouched",
            info.prefixed_name,
        )
        return False
    try:
        server.add_tool(handler, **add_tool_kwargs)
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "Failed to register proxy tool '%s' — MCPServer API may have changed",
            info.prefixed_name,
            exc_info=True,
        )
        return False
    try:
        registered = server._tool_manager._tools.get(info.prefixed_name)
    except AttributeError:
        import logging

        logging.getLogger(__name__).warning(
            "Cannot override schema for '%s' — MCPServer internal API changed. "
            "Tool is registered but may show incorrect parameter schema.",
            info.prefixed_name,
        )
        return True
    if registered is not None:
        # Same version-drift posture as the two guards above: a future
        # fastmcp that renames/removes these pydantic fields raises on
        # assignment (pydantic v2 rejects setattr to a non-field), and the
        # caller loops register_proxy_tool with no per-tool guard — one
        # renamed field would otherwise abort registration of every
        # remaining proxied tool and fail server startup.
        #
        # Order matters for a partial failure: fn_metadata (permissive
        # passthrough validation) is written FIRST, so if the parameters
        # write then fails the tool merely advertises a stale default
        # schema while accepting real args. The reverse partial state —
        # advertising the upstream schema while still validating with the
        # original signature-derived model — would reject the very args
        # the advertised schema invites.
        try:
            registered.fn_metadata = _passthrough_metadata(output_schema)
            # ``Tool.output_schema`` is a functools.cached_property over
            # ``fn_metadata.output_schema``; drop any value cached before the
            # overwrite so tools/list reads the upstream schema, not a stale
            # signature-derived one. dict.pop on a pydantic v2 model's
            # ``__dict__`` cannot raise.
            registered.__dict__.pop("output_schema", None)
            if info.input_schema:
                registered.parameters = info.input_schema
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "Cannot override schema for '%s' — MCPServer internal API changed. "
                "Tool is registered but may show incorrect parameter schema.",
                info.prefixed_name,
                exc_info=True,
            )
    return True
