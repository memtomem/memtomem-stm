"""Shared staged connection-status vocabulary for upstream probes.

Registration success, transport liveness, MCP handshake success, and tool
discovery are distinct failure modes a first-time user needs to tell
apart, but probe results used to collapse them into one boolean plus a
raw error string. This module is the single source of truth for the
stage ladder — ``configured → transport connected → MCP initialized →
tools discovered`` — and the result type carrying "how far did we get".
The CLI probe (``_probe_one`` in ``cli/proxy.py``) produces it; ``mms
health`` and ``mms doctor`` consume it.

Stage names are deliberately transport-neutral: HTTP/SSE upstreams have
no child process, so child-process liveness is a stdio-only rendering
detail layered on top by consumers, never a stage of its own.

This is result-type sharing only — the runtime connect path
(``ProxyManager._establish_connection``) intentionally keeps its own
execution code and is not wired through this module.

Deliberately dependency-light — stdlib only, no pydantic/click — because
the CLI keeps pydantic imports lazy for startup latency and this module
sits on both sides of that boundary (same contract as ``prefixes.py``).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

__all__ = ["ProbeStage", "StagedProbeResult"]


class ProbeStage(enum.StrEnum):
    """Ordered ladder of upstream-connection stages.

    Enum order is the ladder order — comparisons and "next stage"
    derivation rely on the member sequence, so keep it sorted from
    earliest to latest.
    """

    CONFIGURED = "configured"
    TRANSPORT_CONNECTED = "transport_connected"
    MCP_INITIALIZED = "mcp_initialized"
    TOOLS_DISCOVERED = "tools_discovered"

    def display(self) -> str:
        """Human-readable stage name for status lines."""
        return _DISPLAY[self]


_DISPLAY = {
    ProbeStage.CONFIGURED: "configured",
    ProbeStage.TRANSPORT_CONNECTED: "transport connected",
    ProbeStage.MCP_INITIALIZED: "MCP initialized",
    ProbeStage.TOOLS_DISCOVERED: "tools discovered",
}

_ORDER = list(ProbeStage)


@dataclass(frozen=True)
class StagedProbeResult:
    """Outcome of probing one upstream server, stage-resolved.

    ``stage`` is the last stage that *completed successfully*; when
    ``error`` is set, the failure happened in the stage after it
    (``failed_stage``). ``error`` must already be sanitized — consumers
    render it verbatim into ``health``/``doctor`` output.
    """

    stage: ProbeStage
    transport: str = "stdio"
    error: str | None = None
    tools: int = 0
    overflowing: tuple[str, ...] = ()

    @property
    def connected(self) -> bool:
        """Full success: every stage completed and no failure recorded."""
        return self.error is None and self.stage is ProbeStage.TOOLS_DISCOVERED

    @property
    def failed_stage(self) -> ProbeStage | None:
        """The stage the probe failed in, or ``None`` on full success."""
        if self.error is None:
            return None
        idx = _ORDER.index(self.stage)
        return _ORDER[min(idx + 1, len(_ORDER) - 1)]

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly rendering: legacy probe keys plus additive stage info.

        ``connected``/``tools``/``overflowing``/``error`` keep the shapes
        the pre-staged ``--json`` consumers scripted against; ``stage``,
        ``failed_stage``, and ``transport`` are additive.
        """
        return {
            "connected": self.connected,
            "tools": self.tools,
            "overflowing": list(self.overflowing),
            "error": self.error,
            "stage": self.stage.value,
            "failed_stage": self.failed_stage.value if self.failed_stage else None,
            "transport": self.transport,
        }
