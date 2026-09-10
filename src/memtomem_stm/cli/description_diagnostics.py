"""Read-only advice about the configured advertised-description budget (#1015).

This module must not construct a manager, open a store, or connect to an
upstream. It reads the effective configuration and the tool *lengths* a staged
probe already collected, and answers the three questions the knob leaves an
operator no way to ask: is the cap cutting descriptions, which of the two
levels is actually binding, and can the configured strategy's convention suffix
fit at all.

The arithmetic itself is not restated here -- it comes from
``proxy.tool_metadata``, which is the same rule the advertisement composes
with, so the advice cannot drift from what a restart would do (#926).
"""

from __future__ import annotations

from collections.abc import Mapping

from memtomem_stm.proxy.config import ProxyConfig, effective_compression_pair
from memtomem_stm.proxy.staged_status import StagedProbeResult
from memtomem_stm.proxy.tool_metadata import (
    PROXIED_PREFIX,
    DescriptionBudget,
    convention_suffix,
)

DoctorCheck = tuple[str, str, str, str, str | None]

_SCOPE = (
    "Computed from the config file as the proxy would advertise it at its next start; a "
    "running proxy keeps the per-server budget it connected with until then. A host may cut "
    "descriptions again on its own side, which is a separate limit and is not measured here "
    "(#1014)."
)


def _level(cap: int, stated: bool) -> str:
    return f"{cap}" if stated else f"{cap} (default)"


def _binding_clause(budget: DescriptionBudget) -> str:
    if budget.binding == "server":
        return "the server value binds, so raising only the global is a no-op"
    if budget.binding == "global":
        return "the global value binds"
    return "both levels bind equally, so raising either alone changes nothing"


def _next_action(budget: DescriptionBudget, name: str, need: int, path_hint: str) -> str:
    # Edit-this-file instructions lead with ``#``: a pasted ``next:`` line must
    # never *do* anything (same rule as doctor's other config hints).
    tail = "restart the proxy to apply"
    if budget.binding == "server":
        return (
            f'# set "max_description_chars": {need} on upstream_servers.{name} in {path_hint}'
            f"  (the global {budget.global_cap} is not the limit; {tail})"
        )
    if budget.binding == "global":
        return (
            f'# set the top-level "max_description_chars": {need} in {path_hint}'
            f"  (server '{name}' allows {budget.server_cap}; {tail})"
        )
    return (
        f'# set "max_description_chars": {need} at top level and on '
        f"upstream_servers.{name} in {path_hint}  (the budget is min(server, global); {tail})"
    )


def _unmeasured_reason(probe: StagedProbeResult | None, name: str) -> str:
    """Say which of the several silences left the per-tool half unmeasured.

    Collapsing them would put a claim in the report that the run did not make:
    an upstream that is up and advertises nothing is not an upstream that could
    not be reached.
    """
    if probe is None:
        return "this upstream was not probed in this run"
    if not probe.connected:
        return f"upstream not reachable in this run — see upstream: {name}"
    if not probe.tools:
        return "this upstream advertises no tools"
    return "the probe reported no per-tool description lengths"


def description_budget_doctor_checks(
    config: ProxyConfig,
    probes: Mapping[str, StagedProbeResult],
    *,
    path_hint: str,
) -> list[DoctorCheck]:
    """One advisory per configured upstream. WARN or PASS, never FAIL.

    A cap that truncates text, or that cannot carry the resolved strategy's
    convention suffix, is a WARN. Which level binds is reported either way: it
    is not itself a fault -- a deliberately lower per-server cap is a valid
    choice -- but it decides where an edit has to land, and getting that wrong
    is the silent failure this check exists for.
    """
    checks: list[DoctorCheck] = []
    global_cap = config.max_description_chars
    global_stated = "max_description_chars" in config.model_fields_set

    for name, server_cfg in config.upstream_servers.items():
        server_cap = server_cfg.max_description_chars
        server_stated = "max_description_chars" in server_cfg.model_fields_set
        server_budget = DescriptionBudget(
            server_cap,
            global_cap,
            convention_suffix(*effective_compression_pair(server_cfg, None, config)),
        )

        probe = probes.get(name)
        rows = probe.description_chars if probe is not None and probe.connected else ()

        truncated: list[tuple[str, int, int]] = []  # (tool, overflow, source chars)
        needs: list[int] = []
        dropped_tools: list[str] = []
        dropped_suffix = ""
        assessed = 0
        longest = 0

        for tool, chars in rows:
            override = server_cfg.tool_overrides.get(tool)
            if override is not None and override.hidden:
                # Never advertised, so its length spends no budget.
                continue
            assessed += 1
            longest = max(longest, chars)
            budget = DescriptionBudget(
                server_cap,
                global_cap,
                convention_suffix(*effective_compression_pair(server_cfg, override, config)),
            )
            overflow = budget.overflow(chars)
            if overflow:
                truncated.append((tool, overflow, chars))
                needs.append(budget.cap_to_fit(chars))
            if budget.suffix and not budget.suffix_fits:
                dropped_tools.append(tool)
                dropped_suffix = budget.suffix

        # With no probed tools the suffix is still a config fact, so a dead or
        # unprobed upstream does not hide it.
        if not rows and server_budget.suffix and not server_budget.suffix_fits:
            dropped_suffix = server_budget.suffix

        parts = [
            f"cap {server_budget.cap} = min("
            f"server {_level(server_cap, server_stated)}, "
            f"global {_level(global_cap, global_stated)}) — {_binding_clause(server_budget)}",
            f"{server_budget.total} chars for text after the '{PROXIED_PREFIX}' prefix",
        ]
        if server_budget.suffix_fits:
            parts[-1] += (
                f" ({server_budget.body} beside the {len(server_budget.suffix)}-char "
                "convention suffix)"
            )

        if not rows:
            parts.append(
                f"per-tool truncation was not assessed ({_unmeasured_reason(probe, name)})"
            )
        elif truncated:
            tool, overflow, chars = max(truncated, key=lambda row: row[1])
            parts.append(
                f"{len(truncated)} of {assessed} descriptions truncated, longest by "
                f"{overflow} chars ('{tool}', {chars} chars); a cap of {max(needs)} would "
                "advertise every description whole"
            )
        else:
            parts.append(f"all {assessed} descriptions fit whole (longest {longest} chars)")

        if dropped_suffix:
            where = f"{len(dropped_tools)} tool(s)" if dropped_tools else "this server"
            parts.append(
                f"the convention suffix '{dropped_suffix.strip()}' "
                f"({len(dropped_suffix)} chars) is dropped on {where}: only "
                f"{server_budget.total} chars remain after the prefix, so the client is not "
                "told which follow-up tool to call before it calls"
            )

        detail = "; ".join(parts) + ". " + _SCOPE
        if truncated or dropped_suffix:
            need = max(
                needs + ([len(dropped_suffix) + len(PROXIED_PREFIX)] if dropped_suffix else [])
            )
            action: str | None = _next_action(server_budget, name, need, path_hint)
            status = "WARN"
        else:
            action = None
            status = "PASS"
        checks.append(
            (f"description_budget:{name}", f"description budget: {name}", status, detail, action)
        )

    return checks
