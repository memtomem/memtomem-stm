"""STM-native hard filter for proxy tool exposure (#465).

The proxy is the tool-exposure choke point: ``get_proxy_tools()`` decides
which upstream tools the client model ever sees. This module is that
decision, factored into a pure, deterministic pass so it can be tested and
audited in isolation. It absorbs what used to be three scattered exposure
guards (the per-tool ``hidden`` override inline in ``get_proxy_tools``, the
connect-time duplicate-name skip, the connect-time 64-char overflow skip —
``_connect_server`` now keeps every discovered tool and only logs the #261
prefix guidance) and adds profile- and signal-based rules, with every
rejection recorded as a structured reason instead of (at best) a log line.

Rule vocabulary — one reason code per rejected name; the ambiguity
pre-pass (``duplicate_name``) runs first over the whole candidate set,
then the remaining rules apply per tool in this order, first match wins:

``config_hidden``
    ``ToolOverrideConfig.hidden`` — explicit operator intent. Every profile.
``profile_excluded``
    ``expose_in_profiles`` (tool-level wins over upstream-level when both
    set) does not include the active profile. Every profile.
``name_overflow``
    The composed client-side name exceeds the 64-char MCP limit
    (``tool_name_budget``) — clients silently drop such tools (#261), so
    advertising one is strictly worse than rejecting it. Every profile.
    This filter is the only place such tools are excluded; the connect
    path logs the prefix-shortening guidance but keeps the tool in
    ``conn.tools``, so connect and reconnect get identical treatment and
    the verdict reaches telemetry on the normal startup path (codex R2).
``duplicate_name``
    More than one candidate carries the same composed name, so the ENTIRE
    group is withheld (ambiguity pre-pass, evaluated before every other
    rule). Upstream calls route by raw tool name — same-named occurrences
    are one callable entity wearing several metadata claims, so picking a
    "winning" occurrence would advertise metadata that does not bind to
    what executes (codex R3); and two handlers must never race for one
    composed name. Ambiguous names are never auto-exposed, in any
    profile — the original #465 regression criterion, verbatim.
``sensitive_metadata``
    The tool's metadata (original description, advertised description, raw
    schema) matches a credential pattern (``privacy.CREDENTIAL_PATTERNS``
    only — not PII, so an email in a contact-info description never trips
    it). A credential-looking string in tool *metadata* means the upstream
    is misconfigured at best and hostile at worst; advertising it would
    paste the match into the client's context on every ``tools/list``.
    Signal rule: rejects under ``strict``, demotes under ``review``, off
    under ``explore``.
``unhealthy``
    The tool's upstream-attributable error rate (transport / timeout /
    protocol / upstream_error — proxy-internal pipeline failures never
    count against the tool) over the recent metrics window crossed the
    configured threshold with enough samples. Signal rule, like
    ``sensitive_metadata``.

Profile semantics (``ExposureProfile``): ``strict`` enforces signal rules as
hard rejects; ``review`` keeps flagged tools advertised but assigns them a
``risk_penalty`` for tool-relevance telemetry (#466) so the operator can
observe what strict *would* hide; ``explore`` skips signal rules entirely.
Config and structural rules apply in every profile.

Three invariants this module exists to uphold:

- **Ranking may never resurrect a hard reject.** Relevance ranking (#466)
  runs over ``EligibilityResult.eligible`` — the filter's output — so a
  rejected tool is structurally outside the ranker's candidate set.
- **Telemetry never contradicts the advertisement.** ``reject_reasons``
  keys are disjoint from the eligible names: the ambiguity pre-pass
  withholds every multi-occurrence name outright, so no name can be both
  advertised and recorded as withheld (and no advertised metadata can
  diverge from the callable entity behind the name).
- **The advertised set is stable for the session.** Health flags are
  computed once at proxy startup (``compute_health_flags``) from the
  persisted metrics store, not per call: MCP clients are not guaranteed to
  re-list tools, and a mid-session eligibility change would make selection
  telemetry lie about the candidate set the client actually saw. A tool
  hidden for health gets re-evaluated at the next startup; once its
  failures age out of the window it is advertised again (startup-grained
  half-open probing — recovery is possible because hiding stops new
  failures from accruing, and the window forgets old ones).

Reject reasons flow into the selection log's ``reject_reasons`` field
(#467) via ``ProxyManager``; they are reason *codes* only — no tool
metadata, no error text — so the telemetry redaction contract is untouched.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from memtomem_stm.proxy import tool_name_budget
from memtomem_stm.proxy.config import (
    ExposureConfig,
    ExposureProfile,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.metrics import ErrorCategory
from memtomem_stm.proxy.privacy import CREDENTIAL_PATTERNS, contains_sensitive_content

if TYPE_CHECKING:
    from memtomem_stm.proxy.manager import ProxyToolInfo
    from memtomem_stm.proxy.metrics_store import MetricsStore

logger = logging.getLogger(__name__)

# Reject reason codes — the closed vocabulary of the selection log's
# ``reject_reasons`` values. Adding a code is additive (replay tooling
# treats unknown codes as opaque); renaming one is a breaking change to
# recorded history and needs the same care as a schema bump.
REASON_CONFIG_HIDDEN = "config_hidden"
REASON_PROFILE_EXCLUDED = "profile_excluded"
REASON_NAME_OVERFLOW = "name_overflow"
REASON_DUPLICATE_NAME = "duplicate_name"
REASON_SENSITIVE_METADATA = "sensitive_metadata"
REASON_UNHEALTHY = "unhealthy"

# Error categories that count against a TOOL's health. Proxy-side failures
# (programming, internal_error, lock_timeout) are our bugs, not the
# upstream's — hiding a tool because the proxy's own pipeline broke would
# punish the wrong party.
UPSTREAM_ERROR_CATEGORIES: tuple[str, ...] = (
    ErrorCategory.TRANSPORT.value,
    ErrorCategory.TIMEOUT.value,
    ErrorCategory.PROTOCOL.value,
    ErrorCategory.UPSTREAM_ERROR.value,
)


@dataclass(frozen=True, slots=True)
class ExposureCandidate:
    """One tool as it would be advertised, plus the raw artifacts the
    advertisement was derived from.

    ``info`` carries the advertised (post-truncation, post-distill) view;
    ``raw_description`` / ``raw_schema`` are the upstream originals, scanned
    for sensitive metadata because truncation/distillation can only *remove*
    text — a credential at char 250 of a description truncated at 200 is
    still a signal the upstream is compromised.
    """

    info: ProxyToolInfo
    raw_description: str
    raw_schema: dict[str, Any] | None
    server_config: UpstreamServerConfig


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    """Output of one filter pass over an advertisement candidate set.

    ``eligible`` preserves input order (config order, then upstream
    catalogue order). ``reject_reasons`` and ``risk_penalties`` are keyed by
    prefixed name — the same vocabulary as ``candidate_tools`` in selection
    telemetry — and describe NAMES: ``reject_reasons`` only ever names tools
    the client did not get (its key set is disjoint from the eligible names
    — telemetry must never claim a name was both advertised and withheld;
    the ambiguity pre-pass in :func:`filter_tools` withholds every
    multi-occurrence name outright, so each surviving name has exactly one
    candidate and the disjointness is structural), and ``risk_penalties``
    only ever names eligible tools (``review``-profile demotions).
    """

    eligible: list[ProxyToolInfo]
    reject_reasons: dict[str, str]
    risk_penalties: dict[str, float]


def compute_health_flags(
    metrics_store: MetricsStore | None, cfg: ExposureConfig
) -> frozenset[tuple[str, str]]:
    """``(server, raw_tool_name)`` pairs whose recent error rate flags them.

    Reads the persisted metrics store once; call at proxy startup and cache
    — see the module docstring for why health must not drift mid-session.
    No store (metrics disabled, or the standalone manager in tests) means no
    health signal: every tool is presumed healthy. A read failure likewise
    fails open — exposure must not depend on the health of the metrics DB.
    """
    if metrics_store is None:
        return frozenset()
    try:
        stats = metrics_store.get_tool_error_stats(
            cfg.health_window_hours * 3600.0, UPSTREAM_ERROR_CATEGORIES
        )
    except Exception:
        logger.warning(
            "Tool health read failed — exposure proceeds without health signals",
            exc_info=True,
        )
        return frozenset()
    flagged = {
        key
        for key, (calls, errors) in stats.items()
        if calls >= cfg.health_min_calls and (errors / calls) >= cfg.health_error_rate_threshold
    }
    if flagged:
        logger.warning(
            "Tool health flags (window %.1fh, threshold %.0f%%, min %d calls): %s",
            cfg.health_window_hours,
            cfg.health_error_rate_threshold * 100.0,
            cfg.health_min_calls,
            ", ".join(sorted(f"{s}/{t}" for s, t in flagged)),
        )
    return frozenset(flagged)


def _metadata_scan_text(candidate: ExposureCandidate) -> str:
    """Assemble the metadata text scanned for credential patterns.

    Covers the raw upstream description, the advertised description (which
    may originate from an operator ``description_override`` rather than the
    raw text), and the raw schema. Serialization is deterministic
    (sorted keys) so the same candidate always scans the same text.

    Deliberately UNBOUNDED: this is a security gate, not a scoring
    document, and unlike payload telemetry there is no downstream backstop
    for advertisement — whatever passes here goes to the client's context
    verbatim on every ``tools/list``. The scan runs once per advertisement
    (not per call), and the advertisement path already serializes the same
    schema unbounded, so one regex pass over the full text costs nothing
    the upstream couldn't already inflict (codex R2).
    """
    try:
        schema_text = json.dumps(
            candidate.raw_schema or {}, sort_keys=True, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError):  # pragma: no cover - dict from JSON can't cycle
        schema_text = ""
    return "\n".join((candidate.raw_description, candidate.info.description, schema_text))


def filter_tools(
    candidates: list[ExposureCandidate],
    cfg: ExposureConfig,
    unhealthy: frozenset[tuple[str, str]] = frozenset(),
) -> EligibilityResult:
    """Apply the hard-filter rules to one advertisement candidate set.

    Pure and deterministic: same candidates + config + health flags →
    identical result, independent of call count or wall clock. *unhealthy*
    is the cached startup snapshot from :func:`compute_health_flags`.
    """
    # ── ambiguity pre-pass (every profile) ───────────────────────────────
    # A composed name carried by MORE than one candidate is structurally
    # ambiguous and the whole group is withheld: upstream calls route by
    # raw tool name (``session.call_tool(tool)``), so same-named
    # occurrences are one callable entity wearing several metadata claims
    # — advertising the "clean" copy while a sibling copy carries a
    # credential or a hidden flag would advertise metadata that does not
    # bind to what actually executes (codex R3). This also realizes the
    # issue's regression criterion verbatim: ambiguous names are never
    # auto-exposed. Withholding the group (rather than first-wins) costs
    # only the misbehaving-upstream case and removes every
    # occurrence-vs-name ambiguity downstream.
    name_counts = Counter(candidate.info.prefixed_name for candidate in candidates)

    eligible: list[ProxyToolInfo] = []
    reject_reasons: dict[str, str] = {}
    risk_penalties: dict[str, float] = {}

    for candidate in candidates:
        info = candidate.info
        server_cfg = candidate.server_config
        override = server_cfg.tool_overrides.get(info.original_name)

        # ── structural ambiguity (pre-pass verdict, every profile) ───────
        if name_counts[info.prefixed_name] > 1:
            reject_reasons[info.prefixed_name] = REASON_DUPLICATE_NAME
            continue

        # ── config rules (every profile) ─────────────────────────────────
        if override is not None and override.hidden:
            reject_reasons[info.prefixed_name] = REASON_CONFIG_HIDDEN
            continue
        profiles = (
            override.expose_in_profiles
            if override is not None and override.expose_in_profiles is not None
            else server_cfg.expose_in_profiles
        )
        if profiles is not None and cfg.profile not in profiles:
            reject_reasons[info.prefixed_name] = REASON_PROFILE_EXCLUDED
            continue

        # ── structural rules (every profile) ─────────────────────────────
        if tool_name_budget.overflows(server_cfg.prefix, info.original_name):
            reject_reasons[info.prefixed_name] = REASON_NAME_OVERFLOW
            continue

        # ── signal rules (profile-dependent) ─────────────────────────────
        if cfg.profile is not ExposureProfile.EXPLORE:
            flagged_reason: str | None = None
            if contains_sensitive_content(_metadata_scan_text(candidate), CREDENTIAL_PATTERNS):
                flagged_reason = REASON_SENSITIVE_METADATA
            elif (info.server, info.original_name) in unhealthy:
                flagged_reason = REASON_UNHEALTHY
            if flagged_reason is not None:
                if cfg.profile is ExposureProfile.STRICT:
                    reject_reasons[info.prefixed_name] = flagged_reason
                    continue
                # review: advertise, but demote in ranking telemetry.
                risk_penalties[info.prefixed_name] = cfg.review_risk_penalty

        eligible.append(info)

    return EligibilityResult(
        eligible=eligible, reject_reasons=reject_reasons, risk_penalties=risk_penalties
    )
