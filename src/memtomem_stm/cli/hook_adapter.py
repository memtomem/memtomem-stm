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

Capability flag — ``can_replace_output``. Per the B0 host-contract
verification, output *replacement* (the compression channel,
``updatedToolOutput``) ports to **no** non-Claude host (Cursor's field is
MCP-only, Kimi has none, Codex's is parsed-but-unsupported). Only Claude can
rewrite a native tool's output, so compression is gated on this flag; a B2
adapter sets it ``False`` and the orchestrator skips compression for that host.
*Surfacing* (context injection) is the universal channel and flows through
``render`` for every host.

Scope B1 does NOT cover (deferred to B2, per the daemon-boundary decision): the
shared surfacing core still reads the raw payload, and the daemon still receives
the raw payload over the wire (``PROTOCOL_VERSION`` unchanged) — it already
treats that payload opaquely, so a Claude-only ship needs no wire change. The
:class:`CanonicalHookCall` is the normalized contract a B2 wire change will send
and have the core consume; in B1 it is produced by :meth:`parse` and drives
adapter dispatch, the compression gate, native-tool metrics, and :meth:`render`.
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
    """

    event_type: str
    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_response: Any = None
    tool_response_text: str = ""
    host_tag: str = "claude"


class HostHookAdapter(ABC):
    """The host-shaped seam: parse a host payload in, render a host output out.

    Subclasses are the *only* place that knows a host's payload keys and output
    shape. ``host_tag`` identifies the host (dispatch + provenance);
    ``can_replace_output`` declares whether the host honors output replacement
    (compression) — see the module docstring.
    """

    host_tag: ClassVar[str]
    can_replace_output: ClassVar[bool]

    @abstractmethod
    def parse(self, payload: dict[str, Any]) -> CanonicalHookCall | None:
        """Normalize the host's stdin payload into a :class:`CanonicalHookCall`.

        Returns ``None`` for an unusable payload (e.g. not a dict) — the caller
        treats that as a clean no-op, mirroring ``_read_payload``. **Never
        raises.** Permissive by design: eligibility gating (PostToolUse, the
        surface allowlist) stays in the shared core, not here.
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

    def parse(self, payload: dict[str, Any]) -> CanonicalHookCall | None:
        if not isinstance(payload, dict):
            return None
        # Lazy import: avoid a cycle (hook_cmd imports this module at top level)
        # and keep ``mms hook --help`` off the surfacing import cost.
        from memtomem_stm.cli.hook_cmd import _tool_response_to_text

        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input")
        tool_response = payload.get("tool_response")
        return CanonicalHookCall(
            event_type=payload.get("hook_event_name") or "PostToolUse",
            tool_name=tool_name if isinstance(tool_name, str) else "",
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


# Single-entry registry in B1; B2 adds Cursor/Kimi/Codex keyed by host_tag.
_ADAPTERS: dict[str, HostHookAdapter] = {ClaudeHookAdapter.host_tag: ClaudeHookAdapter()}


def get_adapter(host_tag: str = "claude") -> HostHookAdapter:
    """Return the adapter for ``host_tag`` (falls back to Claude).

    B1 only registers Claude; the indirection is the B2 dispatch seam.
    """
    return _ADAPTERS.get(host_tag) or _ADAPTERS["claude"]
