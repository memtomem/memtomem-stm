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

The whole pipeline consumes the :class:`CanonicalHookCall` (not the raw
payload): :meth:`parse` produces it; it drives adapter dispatch, the compression
gate, the surfacing core, native-tool metrics, and :meth:`render`; and it
crosses the hook↔daemon wire via :meth:`CanonicalHookCall.to_wire` /
:meth:`~CanonicalHookCall.from_wire` (``PROTOCOL_VERSION`` 2), so the daemon
consumes a host-agnostic call and needs no host knowledge. The hook process
parses once, compresses (it needs the original ``tool_response``), and ships the
canonical; the daemon rehydrates and surfaces.
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

    :meth:`to_wire` / :meth:`from_wire` serialize this for the hook↔daemon link:
    the hook parses the raw host payload and sends the *canonical* over the wire,
    so the daemon needs no host knowledge. ``tool_response`` (the original
    object) is **not** transmitted — compression already ran in the hook process
    before the wire, and surfacing needs only ``tool_response_text``.
    """

    event_type: str
    tool_name: str
    canonical_tool: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_response: Any = None
    tool_response_text: str = ""
    host_tag: str = "claude"

    def to_wire(self) -> dict[str, Any]:
        """Serialize for the daemon ``surface`` request (drops ``tool_response``).

        Only the fields the daemon-side surfacing core consumes are sent; the
        original ``tool_response`` object is omitted (compression is already done
        and it may not be JSON-serializable)."""
        return {
            "event_type": self.event_type,
            "tool_name": self.tool_name,
            "canonical_tool": self.canonical_tool,
            "tool_input": self.tool_input,
            "tool_response_text": self.tool_response_text,
            "host_tag": self.host_tag,
        }

    @classmethod
    def from_wire(cls, data: Any) -> "CanonicalHookCall | None":
        """Rebuild from a :meth:`to_wire` dict; ``None`` for a non-dict.

        Permissive (mirrors :meth:`HostHookAdapter.parse`): missing/wrong-typed
        fields coerce to safe defaults. ``tool_response`` is always ``None`` —
        it is never transmitted — which is fine because the daemon path is
        surfacing-only and reads ``tool_response_text``."""
        if not isinstance(data, dict):
            return None
        tool_input = data.get("tool_input")
        return cls(
            event_type=str(data.get("event_type") or "PostToolUse"),
            tool_name=str(data.get("tool_name") or ""),
            canonical_tool=str(data.get("canonical_tool") or ""),
            tool_input=tool_input if isinstance(tool_input, dict) else {},
            tool_response=None,
            tool_response_text=str(data.get("tool_response_text") or ""),
            host_tag=str(data.get("host_tag") or "claude"),
        )


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


class CodexHookAdapter(HostHookAdapter):
    """Codex CLI PostToolUse adapter — surfacing-only (B0: no output replace).

    Codex's hook payload shares Claude's shape: snake_case keys with the tool
    output under ``tool_response`` and ``hook_event_name`` = ``PostToolUse``. Its
    surfacing channel is the *same* ``hookSpecificOutput.additionalContext``
    envelope as Claude (doc-verified, B0), so :meth:`render` delegates to the one
    shared ``_build_hook_output`` helper rather than re-spelling the envelope —
    the Claude shape then lives in exactly one place.

    Two host facts (B0) shape it:

    * **Compression does not port.** Codex's ``updatedMCPToolOutput`` is "parsed
      but not supported yet" *and* MCP-only — native Bash/apply_patch has no
      rewrite field — so ``can_replace_output`` is ``False`` and the orchestrator
      skips compression for Codex (surfacing still flows through ``render``).
    * **Only Bash + apply_patch fire a native PostToolUse hook.** Codex has no
      separate Read/Grep/Glob/WebFetch built-ins (it shells out; WebSearch is not
      intercepted), so ``native_tool_map`` maps just those two; every other name
      canonicalizes to ``""`` (never surfaced/compressed). In practice only
      ``Bash`` (→ ``shell``) lands in the read-like surface allowlist.

    Rollout caveat (not an adapter-shape concern): the *standalone* exit-0
    ``additionalContext`` shape — without ``decision:"block"`` — is undocumented
    (every official example nests it under a block), so a runtime check should
    confirm Codex honors it before the host-selection step enables Codex
    surfacing by default. The adapter still emits the documented field.
    """

    host_tag: ClassVar[str] = "codex"
    can_replace_output: ClassVar[bool] = False
    # Codex's only native (non-MCP) tools that fire PostToolUse. ``apply_patch``
    # is a diff/patch apply → canonical ``edit``; ``Bash`` → ``shell``. No native
    # Read/Grep/Glob/WebFetch (Codex shells out), so they are absent here and
    # canonicalize to ``""``.
    native_tool_map: ClassVar[dict[str, str]] = {
        "Bash": "shell",
        "apply_patch": "edit",
    }

    def parse(self, payload: dict[str, Any]) -> CanonicalHookCall | None:
        if not isinstance(payload, dict):
            return None
        raw_name = payload.get("tool_name")
        tool_name = raw_name if isinstance(raw_name, str) else ""
        if tool_name.startswith("mcp__"):
            return None  # proxied MCP tool — already runs through the pipeline
        # Lazy import (as in ClaudeHookAdapter): avoid the hook_cmd import cycle
        # and keep ``mms hook --help`` off the surfacing import cost. Codex shares
        # Claude's snake_case / ``tool_response`` payload shape — only the
        # native_tool_map and host_tag differ — so this mirrors Claude's parse.
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
        # Codex's surfacing envelope is identical to Claude's — delegate to the
        # single shared helper so the output shape lives in exactly one place.
        # ``updated_tool_output`` is always ``None`` for Codex
        # (can_replace_output=False) but is honored generically.
        from memtomem_stm.cli.hook_cmd import _build_hook_output

        return _build_hook_output(updated_tool_output, additional_context)


# Registry keyed by host_tag. B1 shipped Claude; B2 step3 adds the non-Claude
# adapters one host at a time (Codex first). Cursor/Kimi follow; Antigravity is
# deferred (unfixturable — see tests/fixtures/hooks/README.md).
_ADAPTERS: dict[str, HostHookAdapter] = {
    ClaudeHookAdapter.host_tag: ClaudeHookAdapter(),
    CodexHookAdapter.host_tag: CodexHookAdapter(),
}

# STM's canonical built-in tool vocabulary — the codomain of every adapter's
# ``native_tool_map``. The surface allowlist + compression gate are expressed in
# these names so one gate works across hosts.
CANONICAL_TOOLS: frozenset[str] = frozenset(
    {"read", "grep", "glob", "shell", "web_fetch", "write", "edit"}
)


def get_adapter(host_tag: str = "claude") -> HostHookAdapter:
    """Return the adapter for ``host_tag`` (falls back to Claude).

    Claude and Codex are registered (B2 step3 adds the rest); an unknown
    ``host_tag`` falls back to Claude. Host selection (which tag the live hook
    passes) is wired in a later step — today every caller uses the default.
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
