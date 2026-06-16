"""MCP client adapter for the optional tool-graph eligibility provider (#465).

STM consults a separate, non-proxied tool-graph MCP server for cross-server
authorization / data-flow eligibility facts and feeds them into the
exposure filter as an additional rule source. This module mirrors
``surfacing/mcp_client.py::McpClientSearchAdapter`` (the proven "consult a
separate, non-proxied MCP server" pattern): one ``ClientSession`` over a
launched stdio child, and **zero** Python-level dependency on the external
package — all traffic goes over the MCP protocol.

This module is the transport adapter only: it connects and runs the
``eligible_tools`` consult, returning the raw structured verdict. The
``ProxyManager`` drives it once at startup (beside ``compute_health_flags``),
interprets the verdict (see ``tool_eligibility.interpret_verdict``), feeds it
into ``filter_tools`` as per-candidate ``toolgraph_*`` rejects or a whole-call
withhold, and pins ``graph_generation`` into selection telemetry. Unlike the
surfacing adapter the consult runs once per session at proxy startup (so the
advertised set stays stable), which is why this adapter has no per-request
lazy-start / reconnect machinery: the caller starts it once and maps any
failure onto the configured ``on_*`` knobs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from typing import Any, cast

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent

from memtomem_stm.proxy.config import ToolgraphConfig

logger = logging.getLogger(__name__)

# The upstream MCP tools this adapter consults (toolgraph/server/app.py).
# ``eligible_tools`` is the authoritative hard-filter verdict (rejects);
# ``rank_features`` carries the per-candidate ``risk_score`` STM maps onto the
# relevance ``risk_penalty`` (#493) — a separate, best-effort enrichment.
_ELIGIBLE_TOOLS = "eligible_tools"
_RANK_FEATURES = "rank_features"

# Transport failure modes that mean "graph unreachable" rather than a contract
# mismatch. ``eligible_tools()`` wraps these into ``ToolgraphUnreachableError``;
# ``start()`` re-raises them RAW (its rollback only cleans up), so the manager
# catches this tuple directly to classify a failed launch as unreachable.
# Module-level so the manager need not duplicate it. stdio pipe failures
# surface as OSError / EOFError / BrokenPipeError; a blocked/slow server
# surfaces as ``asyncio.TimeoutError`` (per-consult or via the caller's
# ``wait_for`` around ``start()``).
TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    ConnectionError,
    EOFError,
    BrokenPipeError,
    asyncio.TimeoutError,
)


class ToolgraphConsultError(Exception):
    """Base error for a failed tool-graph consult."""


class ToolgraphUnreachableError(ToolgraphConsultError):
    """The tool-graph server could not be reached, or the consult timed out.

    An *availability* failure at the **transport** layer: the stdio child
    never came up, the pipe broke, or the consult exceeded ``timeout_seconds``.
    The caller maps this onto ``on_unreachable``. Note this is narrower than
    "the graph is unavailable": a backend (e.g. Neo4j) outage where the graph
    *server* stays up surfaces as an ``isError`` tool result, not a transport
    error, and is therefore classified ``ToolgraphProtocolError`` (below) —
    any server-side error envelope is treated as a contract class.
    """


class ToolgraphProtocolError(ToolgraphConsultError):
    """The tool-graph server is reachable but returned an incompatible response.

    A *contract* failure: the ``eligible_tools`` tool is missing, the call
    came back ``isError`` (including a backend/DB outage the graph server
    reports as a tool error), or the structured payload is absent / malformed.
    The caller maps this onto ``on_protocol_error``. Distinct from
    ``ToolgraphUnreachableError`` so a version/contract drift is never
    silently treated as "the graph is down".
    """


class ToolgraphConsultAdapter:
    """Connects to a tool-graph MCP server (stdio) and consults ``eligible_tools``.

    Lifecycle mirrors :class:`~memtomem_stm.surfacing.mcp_client.McpClientSearchAdapter`:
    ``start()`` launches the stdio child and opens one initialized
    ``ClientSession``; ``stop()`` tears it down. ``eligible_tools()`` runs the
    batch consult and returns the raw structured response for the caller to
    interpret.
    """

    # Module-level :data:`TRANSPORT_ERRORS` aliased here for the
    # ``eligible_tools()`` ``except`` clause (kept for call-site readability).
    _TRANSPORT_ERRORS = TRANSPORT_ERRORS

    def __init__(self, config: ToolgraphConfig) -> None:
        self._config = config
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    @property
    def is_started(self) -> bool:
        """True once :meth:`start` has opened a live session."""
        return self._session is not None

    async def start(self) -> None:
        """Launch the stdio tool-graph server and open one ``ClientSession``."""
        stack = AsyncExitStack()
        self._stack = stack
        try:
            # ``env=None`` lets mcp use ``get_default_environment()`` (PATH etc.);
            # a configured ``env`` is merged *over* that default by stdio_client,
            # so setting e.g. NEO4J_* does not strip the inherited PATH.
            params = StdioServerParameters(
                command=self._config.command,
                args=list(self._config.args),
                env=self._config.env,
            )
            streams = await stack.enter_async_context(stdio_client(params))
            self._session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            await self._session.initialize()
            logger.info("Tool-graph consult adapter connected via stdio: %s", self._config.command)
        except BaseException:
            # Roll back the stdio subprocess + session streams so a failed
            # start does not leak file descriptors / child processes across
            # retries (mirrors McpClientSearchAdapter.start()).
            try:
                await stack.aclose()
            except Exception:
                logger.debug("Error during tool-graph adapter start() cleanup", exc_info=True)
            self._stack = None
            self._session = None
            raise

    async def stop(self) -> None:
        """Disconnect from the tool-graph server and reap the child."""
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._session = None

    async def eligible_tools(
        self,
        candidates: list[str],
        *,
        agent: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """Consult the graph's ``eligible_tools`` for *candidates*.

        ``candidates`` are server-qualified ``"<server>::<tool>"`` refs (bare
        names risk ``AMBIGUOUS_TOOL``). ``agent`` / ``profile`` fall back to
        the configured ``agent_id`` / ``query_profile``.

        Returns the raw structured response dict
        ``{agent, agent_found, profile, eligible, rejected, graph_generation}``
        (input order preserved on ``eligible``). The caller interprets it:
        ``agent_found=false`` is a structured *abort* signal (not an empty
        result), and each ``rejected`` row carries a ``reason`` plus optional
        ``paths`` / ``candidates``.

        Raises:
            ToolgraphUnreachableError: transport down / timeout / not started.
            ToolgraphProtocolError: ``isError`` result, missing tool, or a
                missing / non-dict structured payload.
        """
        args: dict[str, Any] = {
            "agent": self._config.agent_id if agent is None else agent,
            "candidates": candidates,
            "profile": self._config.query_profile if profile is None else profile,
        }
        return await self._consult(_ELIGIBLE_TOOLS, args)

    async def rank_features(
        self,
        candidates: list[str],
        *,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Consult the graph's ``rank_features`` for *candidates* (#493).

        Returns the raw structured response
        ``{agent, agent_found, features, graph_generation}`` (``features`` in
        input order, each row carrying a ``candidate`` ref and a ``risk_score``
        in ``[0,1]`` or ``None``). Unlike :meth:`eligible_tools` this is a
        *best-effort enrichment*: the caller maps a positive ``risk_score`` onto
        a relevance ``risk_penalty`` and treats any failure as "no penalty"
        (ranking telemetry only, never exposure). The upstream tool takes no
        ``profile`` — features are profile-independent facts.

        Raises:
            ToolgraphUnreachableError: transport down / timeout / not started.
            ToolgraphProtocolError: ``isError`` result, missing tool, or a
                missing / non-dict structured payload.
        """
        args: dict[str, Any] = {
            "agent": self._config.agent_id if agent is None else agent,
            "candidates": candidates,
        }
        return await self._consult(_RANK_FEATURES, args)

    async def _consult(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Call one upstream tool and return its parsed structured verdict.

        Shared transport/error/parse contract for :meth:`eligible_tools` and
        :meth:`rank_features`: transport faults map to
        :class:`ToolgraphUnreachableError`; an ``isError`` result, an unknown
        tool, or an unparseable payload map to :class:`ToolgraphProtocolError`.
        """
        if self._session is None:
            raise ToolgraphUnreachableError("tool-graph adapter not started")

        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool, args),
                timeout=self._config.timeout_seconds,
            )
        except self._TRANSPORT_ERRORS as exc:
            raise ToolgraphUnreachableError(str(exc)) from exc
        except ToolgraphConsultError:
            raise
        except Exception as exc:
            # Any other call_tool failure (e.g. an MCP error for an unknown
            # tool) is a contract problem, not an availability one.
            raise ToolgraphProtocolError(str(exc)) from exc

        if result.isError:
            raise ToolgraphProtocolError(
                f"{tool} returned an error result: {_result_error_text(result)}"
            )

        # Prefer ``structuredContent`` if the upstream emits it, but mcp 1.27.x
        # does NOT auto-wrap a bare ``-> dict`` tool return — the JSON-serialized
        # verdict arrives as a TextContent instead (toolgraph returns ``dict``),
        # so text-JSON is the actual production path today. ``structuredContent``
        # stays the preferred path in case the upstream switches to a typed
        # (BaseModel / TypedDict) return.
        verdict = result.structuredContent
        if not isinstance(verdict, dict):
            verdict = _parse_text_verdict(result)
        if not isinstance(verdict, dict):
            raise ToolgraphProtocolError(
                f"{tool} response carried no parseable verdict "
                "(neither structuredContent nor a JSON text payload)"
            )
        return verdict


def _parse_text_verdict(result: Any) -> dict[str, Any] | None:
    """Parse the verdict dict from a CallToolResult's text content.

    mcp 1.27.x serializes a bare ``-> dict`` tool return into a single
    ``TextContent`` JSON string rather than ``structuredContent``. Joins the
    text parts and JSON-decodes them; returns ``None`` on a missing / malformed
    / non-dict payload (the caller raises ``ToolgraphProtocolError``).
    """
    parts = [
        cast(TextContent, c).text or ""
        for c in (result.content or [])
        if getattr(c, "type", None) == "text"
    ]
    if not parts:
        return None
    try:
        parsed = json.loads("\n".join(parts))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _result_error_text(result: Any) -> str:
    """Best-effort one-line error text from an ``isError`` CallToolResult.

    Tolerates spec-noncompliant ``content=None`` / ``text=None`` the same way
    the surfacing adapter does (mirrors PR #114 / manager.py).
    """
    parts = [
        cast(TextContent, c).text or ""
        for c in (result.content or [])
        if getattr(c, "type", None) == "text"
    ]
    return " ".join(p for p in parts if p) or "(no error text)"
