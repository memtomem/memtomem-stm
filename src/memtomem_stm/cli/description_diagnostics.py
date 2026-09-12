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
    "running proxy keeps the per-server budget it connected with until then. The host cap "
    "is operator-supplied, not measured; when unset, host-only cuts may lose the recovery "
    "hint (#1014). Counts cover the tools this upstream reported, minus any hidden by config; "
    "exposure filtering — profiles, the tool-name budget, collisions — can withhold more."
)


def _level(cap: int, stated: bool) -> str:
    return f"{cap}" if stated else f"{cap} (default)"


def _binding_clause(budget: DescriptionBudget) -> str:
    if budget.binding == "host":
        return "the configured host limit binds; STM settings cannot raise the host limit"
    if budget.binding == "server":
        return "the server value binds, so raising only the global is a no-op"
    if budget.binding == "global":
        return "the global value binds"
    return "both levels bind equally, so raising either alone changes nothing"


def _next_action(budget: DescriptionBudget, name: str, need: int, path_hint: str) -> str:
    """Name every level that has to move, not just the one binding today.

    The cap is ``min(server, global)``, so raising the binding level alone
    stops at whichever level is next. Recommending only that one is the same
    mistake this check exists to catch, one step further along.
    """
    if budget.host_cap is not None and budget.host_cap < need:
        return (
            "# use stm_proxy_describe_tool with the advertised tool name for full metadata; "
            "verify host_description_cap against the host if the recovery hint cannot fit; "
            "raising max_description_chars alone cannot widen the host limit"
        )
    # Edit-this-file instructions lead with ``#``: a pasted ``next:`` line must
    # never *do* anything (same rule as doctor's other config hints).
    tail = "restart the proxy to apply"
    below = []
    if budget.server_cap < need:
        below.append(f"upstream_servers.{name}")
    if budget.global_cap < need:
        below.append("the top level")
    if not below:
        # Nothing is under the requirement, so the finding is not about the
        # numbers: it is a suffix that no valid cap change would restore.
        return f"# review the compression strategy for upstream_servers.{name} in {path_hint}"
    where = " and ".join(below)
    return (
        f'# set "max_description_chars": {need} on {where} in {path_hint}'
        f"  (the budget is min(server, global), so every level below {need} has to move; {tail})"
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
        stage = probe.failed_stage
        reached = f" — failed at '{stage.display()}'" if stage is not None else ""
        return f"tool discovery did not complete{reached}; see upstream: {name}"
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
            config.host_description_cap,
        )

        probe = probes.get(name)
        rows = probe.description_chars if probe is not None and probe.connected else ()

        truncated: list[tuple[str, int, int]] = []  # (tool, overflow, source chars)
        needs: list[int] = []
        recovery_dropped = 0
        dropped: list[str] = []  # suffixes dropped, one entry per tool
        assessed = 0
        longest = 0

        for tool, chars in rows:
            override = server_cfg.tool_overrides.get(tool)
            if override is not None and override.hidden:
                # Hidden tools are advertised nowhere, so their length spends
                # no budget. Other exposure rules are NOT applied here -- see
                # the scope note, which is why the counts below are worded as
                # discovered rather than advertised.
                continue
            assessed += 1
            longest = max(longest, chars)
            budget = DescriptionBudget(
                server_cap,
                global_cap,
                convention_suffix(*effective_compression_pair(server_cfg, override, config)),
                config.host_description_cap,
            ).for_source(
                chars,
                schema_removed=(
                    (server_cfg.strip_schema_descriptions or config.strip_schema_descriptions)
                    and probe is not None
                    and tool in probe.schema_description_tools
                ),
            )
            overflow = budget.overflow(chars)
            suffix_lost = bool(budget.suffix) and not budget.suffix_fits
            if overflow:
                truncated.append((tool, overflow, chars))
            if suffix_lost:
                dropped.append(budget.suffix)
            recovery_lost = budget.recovery_required and not budget.recovery_fits
            if recovery_lost:
                recovery_dropped += 1
            if overflow or suffix_lost or recovery_lost:
                # The cap that loses nothing for THIS tool: its own body plus
                # the prefix plus its own resolved suffix. A cap that merely
                # readmits the suffix would starve the body it displaces, and
                # a per-tool strategy makes the requirement per-tool too, so
                # the recommendation is the maximum over the findings rather
                # than an arithmetic minimum computed once.
                needs.append(budget.cap_to_fit(chars))

        # With no probed tools the suffix is still a config fact, so a dead or
        # unprobed upstream does not hide it. Nothing is known about body
        # lengths there, so the requirement covers the suffix alone.
        if not rows and server_budget.suffix and not server_budget.suffix_fits:
            dropped.append(server_budget.suffix)
            needs.append(server_budget.cap_to_fit(0))

        parts = [
            f"cap {server_budget.cap} = min("
            f"server {_level(server_cap, server_stated)}, "
            f"global {_level(global_cap, global_stated)}"
            + (
                f", host {config.host_description_cap}"
                if config.host_description_cap is not None
                else ""
            )
            + f") — {_binding_clause(server_budget)}",
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
                f"{len(truncated)} of {assessed} discovered descriptions exceed the text "
                f"budget, largest by {overflow} chars over it ('{tool}', {chars} chars) — the "
                "cut itself drops at least that much, and more when it retreats to a word or "
                "sentence boundary"
            )
        else:
            parts.append(
                f"all {assessed} discovered descriptions fit whole (longest {longest} chars)"
            )

        if dropped:
            # Strategies can differ per tool, so report the longest dropped
            # suffix: it is the one that sets the requirement, and picking any
            # other would make the line depend on the order rows arrived in.
            worst = max(dropped, key=len)
            parts.append(
                f"the convention suffix '{worst.strip()}' ({len(worst)} chars) is dropped on "
                f"{len(dropped)} tool(s): only {server_budget.total} chars remain after the "
                "prefix, so the client is not told which follow-up tool to call before it calls"
            )

        if recovery_dropped:
            parts.append(
                f"the full-metadata recovery hint is dropped on {recovery_dropped} tool(s); "
                "existing compression hints take priority, and hints are never cut mid-name"
            )
        if needs:
            parts.append(
                f"a cap of {max(needs)} would carry every discovered description and its hint whole"
            )

        detail = "; ".join(parts) + ". " + _SCOPE
        if needs:
            action: str | None = _next_action(server_budget, name, max(needs), path_hint)
            status = "WARN"
        else:
            action = None
            status = "PASS"
        checks.append(
            (f"description_budget:{name}", f"description budget: {name}", status, detail, action)
        )

    return checks
