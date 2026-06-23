"""Host-shaped hook adapters — the parse/render seam for ``mms hook``.

``mms hook`` (see :mod:`memtomem_stm.cli.hook_cmd`) bridges a host's built-in
tool calls into STM's surfacing/compression pipeline. The *pipeline* is
host-agnostic; only two things are host-shaped:

* **parse** — read the host's PostToolUse payload keys into a normalized
  :class:`CanonicalHookCall`.
* **render** — turn the pipeline's ``(updated_tool_output, additional_context)``
  result into the host's expected stdout shape.

Isolating those two means adding a host (B2: Cursor/Kimi/Codex) is one adapter,
leaving the shared core untouched. B1 ships only :class:`ClaudeHookAdapter`; its
parse/render delegate to the existing helpers in :mod:`hook_cmd` (imported
lazily to avoid an import cycle, since ``hook_cmd`` imports this module), so
behavior is byte-identical to the pre-adapter code.

Tool-name normalization (B2). Every host names the same built-in differently
(``Bash`` / ``Shell``; ``Read`` / ``ReadFile``), so :meth:`parse` maps each
host's native name through :attr:`HostHookAdapter.native_tool_map` into STM's
**canonical vocabulary** ``{read, grep, glob, shell, web_fetch, write, edit}``
and records it on :attr:`CanonicalHookCall.canonical_tool`. The shared core then
gates on the canonical name — one surface allowlist (``{read, grep, glob,
shell}``) and one compression gate (``shell``) work for every host, instead of
each host's PascalCase spelling. A native name absent from the map canonicalizes
to ``""`` (never surfaced/compressed — it isn't one of the read-like built-ins
STM bridges). The surfacing *engine* still receives the host-native
:attr:`CanonicalHookCall.tool_name` for query extraction (so behavior is
unchanged), so the canonical name is purely a host-agnostic gating key.

Capability flag — ``can_replace_output``. Per the B0 host-contract
verification, output *replacement* (the compression channel,
``updatedToolOutput``) ports to **no** non-Claude host (Cursor's field is
MCP-only, Kimi has none, Codex's is parsed-but-unsupported). Only Claude can
rewrite a native tool's output, so compression is gated on this flag; a B2
adapter sets it ``False`` and the orchestrator skips compression for that host.
*Surfacing* (context injection) is the universal channel and flows through
``render`` for every host.

The shared surfacing core now consumes the :class:`CanonicalHookCall` (not the
raw payload): :meth:`parse` produces it and it drives adapter dispatch, the
compression gate, the surfacing core, native-tool metrics, and :meth:`render`.
Still deferred (the daemon-boundary step): the daemon receives the *raw* payload
over the wire (``PROTOCOL_VERSION`` unchanged) and re-parses it server-side with
the Claude adapter — it already treats the payload opaquely, so a Claude-only
ship needs no wire change. Sending the :class:`CanonicalHookCall` itself over the
wire (so the daemon needs no host knowledge) is the next step.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass(frozen=True, slots=True)
class CanonicalHookCall:
    """A host's PostToolUse tool call, normalized into a host-agnostic shape.

    Produced by :meth:`HostHookAdapter.parse`. Fields are the union of what the
    surfacing query (``tool_input``), the size gate / metrics
    (``tool_response_text``), and the compression channel (``tool_response`` —
    the *original* object, so a Bash result's ``stdout`` can be replaced while
    ``stderr`` / exit / ``interrupted`` survive verbatim) need, so a consumer
    never has to re-touch the raw payload.

    ``tool_name`` is the host's *native* tool name (e.g. ``Bash``), passed to the
    surfacing engine for query extraction. ``canonical_tool`` is the
    host-agnostic name (e.g. ``shell``) from STM's canonical vocabulary that the
    allowlist + compression gates key on; ``""`` for a tool outside that
    vocabulary (never surfaced/compressed).
    """

    event_type: str
    tool_name: str
    canonical_tool: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_response: Any = None
    tool_response_text: str = ""
    host_tag: str = "claude"


class HostHookAdapter(ABC):
    """The host-shaped seam: parse a host payload in, render a host output out.

    Subclasses are the *only* place that knows a host's payload keys and output
    shape. ``host_tag`` identifies the host (dispatch + provenance);
    ``can_replace_output`` declares whether the host honors output replacement
    (compression) — see the module docstring. ``native_tool_map`` maps the
    host's native PostToolUse tool names to STM's canonical vocabulary (see the
    module docstring); a native name absent from it canonicalizes to ``""``.
    """

    host_tag: ClassVar[str]
    can_replace_output: ClassVar[bool]
    native_tool_map: ClassVar[dict[str, str]]

    @abstractmethod
    def parse(self, payload: dict[str, Any]) -> CanonicalHookCall | None:
        """Normalize the host's stdin payload into a :class:`CanonicalHookCall`.

        Returns ``None`` for an unusable payload (e.g. not a dict) or an
        ``mcp__``-prefixed tool (those already flow through the MCP proxy, never
        the native-tool hook) — the caller treats that as a clean no-op,
        mirroring ``_read_payload``. **Never raises.** Permissive by design:
        eligibility gating (PostToolUse, the surface allowlist) stays in the
        shared core, not here.
        """

    @abstractmethod
    def render(
        self, *, updated_tool_output: Any | None, additional_context: str | None
    ) -> dict[str, Any]:
        """Build the host's hook output from the pipeline's two optional halves.

        ``updated_tool_output`` is the compression result (or ``None``);
        ``additional_context`` is the surfaced-memories block (or ``None``).
        Returns ``{}`` when both are absent (the tool output passes through).
        """


class ClaudeHookAdapter(HostHookAdapter):
    """Claude Code PostToolUse adapter — the code-verified baseline.

    ``parse`` reads Claude's snake_case payload keys; ``render`` emits the
    ``hookSpecificOutput`` envelope. Both delegate to the existing
    :mod:`hook_cmd` helpers (lazy import) so the refactor is behavior-preserving.
    Claude is the one host that can replace native tool output, so
    ``can_replace_output`` is ``True``.
    """

    host_tag: ClassVar[str] = "claude"
    can_replace_output: ClassVar[bool] = True
    # Claude Code's PascalCase built-in tool names → STM's canonical vocabulary.
    # MultiEdit/Edit both map to ``edit``; tools outside this map (Task,
    # TodoWrite, …) canonicalize to ``""`` and never surface/compress.
    native_tool_map: ClassVar[dict[str, str]] = {
        "Read": "read",
        "Grep": "grep",
        "Glob": "glob",
        "Bash": "shell",
        "WebFetch": "web_fetch",
        "Write": "write",
        "Edit": "edit",
        "MultiEdit": "edit",
    }

    def parse(self, payload: dict[str, Any]) -> CanonicalHookCall | None:
        if not isinstance(payload, dict):
            return None
        raw_name = payload.get("tool_name")
        tool_name = raw_name if isinstance(raw_name, str) else ""
        if tool_name.startswith("mcp__"):
            return None  # proxied MCP tool — already runs through the pipeline
        # Lazy import: avoid a cycle (hook_cmd imports this module at top level)
        # and keep ``mms hook --help`` off the surfacing import cost.
        from memtomem_stm.cli.hook_cmd import _tool_response_to_text

        tool_input = payload.get("tool_input")
        tool_response = payload.get("tool_response")
        return CanonicalHookCall(
            event_type=payload.get("hook_event_name") or "PostToolUse",
            tool_name=tool_name,
            canonical_tool=self.native_tool_map.get(tool_name, ""),
            tool_input=tool_input if isinstance(tool_input, dict) else {},
            tool_response=tool_response,
            tool_response_text=_tool_response_to_text(tool_response),
            host_tag=self.host_tag,
        )

    def render(
        self, *, updated_tool_output: Any | None, additional_context: str | None
    ) -> dict[str, Any]:
        from memtomem_stm.cli.hook_cmd import _build_hook_output

        return _build_hook_output(updated_tool_output, additional_context)


# Single-entry registry today; B2 adds Cursor/Kimi/Codex keyed by host_tag.
_ADAPTERS: dict[str, HostHookAdapter] = {ClaudeHookAdapter.host_tag: ClaudeHookAdapter()}

# STM's canonical built-in tool vocabulary — the codomain of every adapter's
# ``native_tool_map``. The surface allowlist + compression gate are expressed in
# these names so one gate works across hosts.
CANONICAL_TOOLS: frozenset[str] = frozenset(
    {"read", "grep", "glob", "shell", "web_fetch", "write", "edit"}
)


def get_adapter(host_tag: str = "claude") -> HostHookAdapter:
    """Return the adapter for ``host_tag`` (falls back to Claude).

    Only Claude is registered today; the indirection is the B2 dispatch seam.
    """
    return _ADAPTERS.get(host_tag) or _ADAPTERS["claude"]


def canonicalize_tool_token(token: str) -> str | None:
    """Resolve a surface-allowlist token to a canonical tool name, or ``None``.

    Accepts a canonical name verbatim, or a known host-native name (back-compat
    for the pre-canonical ``MEMTOMEM_STM_HOOK_SURFACE_TOOLS`` spelling — e.g.
    Claude's ``Read`` / ``Bash`` → ``read`` / ``shell`` — and any registered
    host's native names). Returns ``None`` for an unrecognized token so the
    caller can warn + drop it instead of silently building an allowlist that
    matches nothing. Case-sensitive: native names are PascalCase, canonical are
    lowercase, so there is no ambiguity."""
    if token in CANONICAL_TOOLS:
        return token
    for adapter in _ADAPTERS.values():
        mapped = adapter.native_tool_map.get(token)
        if mapped:
            return mapped
    return None
