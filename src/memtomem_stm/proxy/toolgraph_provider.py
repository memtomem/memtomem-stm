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
from memtomem_stm.utils.json_out import has_lone_surrogate

logger = logging.getLogger(__name__)

# The upstream MCP tools this adapter consults (toolgraph/server/app.py).
# ``eligible_tools`` is the authoritative hard-filter verdict (rejects);
# ``rank_features`` carries the per-candidate ``risk_score`` STM maps onto the
# relevance ``risk_penalty`` (#493) — a separate, best-effort enrichment.
_ELIGIBLE_TOOLS = "eligible_tools"
_RANK_FEATURES = "rank_features"
_BACKEND_UNAVAILABLE_KIND = "backend_unavailable"

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

    An *availability* failure: either the stdio child never came up, the pipe
    broke, the consult exceeded ``timeout_seconds``, or a compatible Toolgraph
    server returned its typed ``backend_unavailable`` MCP error envelope. The
    caller maps all of these onto ``on_unreachable``.
    """


class ToolgraphProtocolError(ToolgraphConsultError):
    """The tool-graph server is reachable but returned an incompatible response.

    A *contract* failure: the ``eligible_tools`` tool is missing, the call
    returned an untyped/unknown/malformed ``isError``, or the structured
    verdict is absent / malformed. The caller maps this onto
    ``on_protocol_error``. Distinct from ``ToolgraphUnreachableError`` so a
    version/contract drift is never silently treated as "the graph is down".
    """


def validate_toolgraph_identifier(value: str, path: str) -> None:
    """Refuse an unencodable Toolgraph identifier without rewriting its identity.

    Toolgraph refs are exact-matched by ``ProxyManager`` and persisted in the
    consult cache.  ``escape_lone_surrogates`` is deliberately not used here:
    it is non-injective and would make a graph rejection disappear when only
    one side of that exact match was escaped (#783).

    ``path`` is an ASCII schema path supplied by the caller.  The offending
    value is never included in the error because identifiers can originate in
    external metadata and the error is later exposed in health diagnostics.
    """
    if has_lone_surrogate(value):
        raise ToolgraphProtocolError(f"{path} is not a UTF-8-encodable identifier")


def _validate_verdict_identifiers(tool: str, verdict: dict[str, Any]) -> None:
    """Validate identity-bearing fields in a successful Toolgraph verdict.

    Deliberately whole-payload rather than scoped to what the caller reads
    today. ``interpret_verdict`` consumes only ``rejected[].candidate``, and
    ``parse_risk_scores`` only ``features[].candidate`` — but ``eligible`` and
    ``tool_key`` are identities too, and a provider that cannot encode one of
    them is emitting identities STM cannot round-trip anywhere. Scoping the
    check to today's readers would also let the next field to become
    load-bearing arrive unvalidated.

    Note the two tools differ in consequence, which is a property of their
    callers rather than of this check: an ``eligible_tools`` failure rides
    ``on_protocol_error`` (``fail_start`` by default, so it refuses startup),
    while a ``rank_features`` failure is caught by
    ``ProxyManager._fetch_graph_facts`` and degrades to no risk penalties and
    no candidate facts, preserving ``parse_graph_features``'s never-raises
    contract.
    """

    for field in ("agent", "profile"):
        value = verdict.get(field)
        if isinstance(value, str):
            validate_toolgraph_identifier(value, f"{tool} response {field}")

    eligible = verdict.get("eligible")
    if isinstance(eligible, list):
        for index, value in enumerate(eligible):
            if isinstance(value, str):
                validate_toolgraph_identifier(value, f"{tool} response eligible[{index}]")

    rows_field = "rejected" if tool == _ELIGIBLE_TOOLS else "features"
    rows = verdict.get(rows_field)
    if not isinstance(rows, list):
        return
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        for field in ("candidate", "tool_key"):
            value = row.get(field)
            if isinstance(value, str):
                validate_toolgraph_identifier(
                    value, f"{tool} response {rows_field}[{row_index}].{field}"
                )
        candidates = row.get("candidates")
        if isinstance(candidates, list):
            for candidate_index, value in enumerate(candidates):
                if isinstance(value, str):
                    validate_toolgraph_identifier(
                        value,
                        f"{tool} response {rows_field}[{row_index}].candidates[{candidate_index}]",
                    )


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
            ToolgraphUnreachableError: transport down / timeout / not started,
                or a typed backend-unavailable result.
            ToolgraphProtocolError: untyped/unknown ``isError``, missing tool,
                or a missing / non-dict structured payload.
        """
        resolved_agent = self._config.agent_id if agent is None else agent
        resolved_profile = self._config.query_profile if profile is None else profile
        validate_toolgraph_identifier(resolved_agent, "eligible_tools request agent")
        validate_toolgraph_identifier(resolved_profile, "eligible_tools request profile")
        for index, candidate in enumerate(candidates):
            validate_toolgraph_identifier(candidate, f"eligible_tools request candidates[{index}]")
        args: dict[str, Any] = {
            "agent": resolved_agent,
            "candidates": candidates,
            "profile": resolved_profile,
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
            ToolgraphUnreachableError: transport down / timeout / not started,
                or a typed backend-unavailable result.
            ToolgraphProtocolError: untyped/unknown ``isError``, missing tool,
                or a missing / non-dict structured payload.
        """
        resolved_agent = self._config.agent_id if agent is None else agent
        validate_toolgraph_identifier(resolved_agent, "rank_features request agent")
        for index, candidate in enumerate(candidates):
            validate_toolgraph_identifier(candidate, f"rank_features request candidates[{index}]")
        args: dict[str, Any] = {
            "agent": resolved_agent,
            "candidates": candidates,
        }
        return await self._consult(_RANK_FEATURES, args)

    async def _consult(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Call one upstream tool and return its parsed structured verdict.

        Shared transport/error/parse contract for :meth:`eligible_tools` and
        :meth:`rank_features`: transport faults map to
        :class:`ToolgraphUnreachableError`. A producer-declared backend outage
        maps there too, but only from the exact structured discriminator; every
        other ``isError``, unknown tool, or unparseable payload maps to
        :class:`ToolgraphProtocolError`.
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

        if result.is_error:
            error = getattr(result, "structured_content", None)
            if (
                isinstance(error, dict)
                and error.get("error_kind") == _BACKEND_UNAVAILABLE_KIND
                and error.get("retryable") is True
            ):
                raise ToolgraphUnreachableError(
                    f"{tool} reported that the Toolgraph backend is temporarily unavailable"
                )
            raise ToolgraphProtocolError(
                f"{tool} returned an error result: {_result_error_text(result)}"
            )

        # Prefer ``structuredContent`` if the upstream emits it, but mcp 1.27.x
        # does NOT auto-wrap a bare ``-> dict`` tool return — the JSON-serialized
        # verdict arrives as a TextContent instead (toolgraph returns ``dict``),
        # so text-JSON is the actual production path today. ``structuredContent``
        # stays the preferred path in case the upstream switches to a typed
        # (BaseModel / TypedDict) return.
        verdict = result.structured_content
        if not isinstance(verdict, dict):
            verdict = _parse_text_verdict(result)
        if not isinstance(verdict, dict):
            raise ToolgraphProtocolError(
                f"{tool} response carried no parseable verdict "
                "(neither structuredContent nor a JSON text payload)"
            )
        _validate_verdict_identifiers(tool, verdict)
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
