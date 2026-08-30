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
``task_required``
    The upstream declared ``execution.taskSupport: "required"`` (MCP
    revision 2025-11-25) — the tool runs only as an async task, and this
    proxy's call path is synchronous-only, so every call against it would
    fail. Advertising one is strictly worse than rejecting it, the same
    posture as ``name_overflow``, and unlike the signal rules there is
    nothing for ``review`` to observe: the failures are certain, not
    heuristic, and would only accrue against the tool's own health. Every
    profile. ``optional`` is the deliberate inverse — advertised WITHOUT
    ``execution``, i.e. as a plain synchronous tool; ``ProxyToolInfo``
    carries no ``execution`` field, so the downgrade is structural rather
    than a strip. ``forbidden`` and an absent ``execution`` are unchanged.
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
    The tool's metadata matches a credential pattern. Scanned: the composed
    name, the advertised description and the raw one behind it, the raw
    input schema behind the advertised one, the forwarded ``output_schema``
    / ``meta``, and ``annotations`` in their tagged (client-visible) form.
    The two raw artifacts are scanned even though the client may never see
    them, because truncation and distillation only ever REMOVE text — a
    credential at char 250 of a description truncated at 200 is still a
    signal about the upstream. A field this gate cannot serialize counts as
    a match, since a value it cannot read is one it cannot clear.
    Credential patterns only (``privacy.CREDENTIAL_PATTERNS`` — not PII, so
    an email in a contact-info description never trips it). A match usually
    means the upstream is misconfigured at best and hostile at worst; in the
    two fields the OPERATOR owns (the name prefix, and the server name
    inside the annotations tag) it means the proxy's own config carries one.
    Either way, advertising it would paste the match into the client's
    context on every ``tools/list``.
    Signal rule: rejects under ``strict``, demotes under ``review``, off
    under ``explore``.
``unhealthy``
    The tool's upstream-attributable error rate (transport / timeout /
    protocol / upstream_error — proxy-internal pipeline failures never
    count against the tool) over the recent metrics window crossed the
    configured threshold with enough samples. Signal rule, like
    ``sensitive_metadata``.
``toolgraph_*``
    The optional external tool-graph eligibility provider (#465) rejected
    this candidate — one ``toolgraph_<reason>`` code per upstream verdict
    (NOT_GRANTED / DENY_VIOLATION / DENY_GOVERNED / DRIFTED / AMBIGUOUS /
    UNMAPPED / TOOL_NOT_FOUND, plus a generic ``toolgraph_rejected``
    fallback for any upstream reason STM does not recognize). Passed
    in pre-resolved via ``external_rejects`` (the manager runs the consult
    once at startup and maps upstream reasons to codes). Signal rule, ranked
    above ``unhealthy`` but below ``sensitive_metadata``.

    ``toolgraph_unconsulted`` is the same family but the absence of a verdict
    rather than one: the stdio consult runs once per session, so a candidate
    that appeared afterwards (an upstream added a tool and #917 rebuilt the
    advertisement) was never put to the graph, and must not read the same as
    one it approved (#918).

A whole-call ``toolgraph_*`` outcome (``toolgraph_unreachable`` /
``toolgraph_agent_not_found`` / ``toolgraph_protocol_error``) is a SEPARATE
mechanism: when the consult itself fails under a ``closed`` knob, the manager
passes ``withhold_all`` and EVERY candidate is withheld under that one code,
profile-INDEPENDENTLY (explore included) — the operator's explicit "no graph,
no tools" posture, distinct from the per-candidate signal rules above.

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
- **STM's own signals are stable for the session; the upstream catalogue is
  not.** Health flags are computed once at proxy startup
  (``compute_health_flags``) from the persisted metrics store, not per call,
  so a tool does not slide in and out of the advertisement as calls fail. A
  tool hidden for health gets re-evaluated at the next startup; once its
  failures age out of the window it is advertised again (startup-grained
  half-open probing — recovery is possible because hiding stops new failures
  from accruing, and the window forgets old ones).

  What the upstream declares is a different matter, and until #917 it was
  treated the same way: the bundled server registered this verdict once at
  startup, while an upstream can replace its catalogue at any time (a
  reconnect, or ``tools/list_changed``). A tool that only THEN earned a
  rejection kept whatever advertisement it already had — general to every
  rule here, and worst for ``task_required``, where the client is left
  holding a tool it can see and can never successfully call. So a catalogue
  change now re-runs this filter and reconciles the registration
  (``ProxyManager.set_advertisement_listener`` → the lifespan's
  re-advertisement in ``server.py``, which asks the clients it can reach to
  re-list whenever that changed the registry).
  The verdict is still a pure function of its inputs; what changed is that
  the inputs are re-read when the upstream moves them, rather than only at
  startup.

Reject reasons flow into the selection log's ``reject_reasons`` field
(#467) via ``ProxyManager``; they are reason *codes* only — no tool
metadata, no error text — so the telemetry redaction contract is untouched.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from memtomem_stm.proxy import tool_name_budget
from memtomem_stm.proxy.config import (
    ExposureConfig,
    ExposureProfile,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.metrics import ErrorCategory
from memtomem_stm.proxy.privacy import CREDENTIAL_PATTERNS, contains_sensitive_content
from memtomem_stm.proxy.tool_metadata import tag_annotations_title
from memtomem_stm.proxy.toolgraph_provider import ToolgraphProtocolError
from memtomem_stm.utils.numeric import finite_number

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
REASON_TASK_REQUIRED = "task_required"
REASON_DUPLICATE_NAME = "duplicate_name"
REASON_SENSITIVE_METADATA = "sensitive_metadata"
REASON_UNHEALTHY = "unhealthy"

# Assigned AFTER the filter, by the manager, for a candidate this filter found
# eligible that the server then could not register — a failed ``add_tool`` or a
# prefixed name already owned by something else in an embedding host (#908).
# It is the one reason code that is not an exposure decision: the tool passed
# every gate here and the registration layer declined it, not policy.
REASON_REGISTRATION_DECLINED = "registration_declined"

# External tool-graph eligibility provider codes (#465). Two families:
#
# PER-CANDIDATE — a successful consult's ``rejected`` rows, one STM code per
# upstream reason. These ride the profile-gated signal block (like
# ``unhealthy``): strict rejects, review demotes, explore ignores. They are
# the value side of ``filter_tools(external_rejects=...)`` — the manager has
# already mapped the upstream reason to one of these before the filter runs.
REASON_TOOLGRAPH_NOT_GRANTED = "toolgraph_not_granted"
REASON_TOOLGRAPH_DENY_VIOLATION = "toolgraph_deny_violation"
REASON_TOOLGRAPH_DENY_GOVERNED = "toolgraph_deny_governed"
REASON_TOOLGRAPH_DRIFTED = "toolgraph_drifted"
REASON_TOOLGRAPH_AMBIGUOUS = "toolgraph_ambiguous"
REASON_TOOLGRAPH_UNMAPPED = "toolgraph_unmapped"
REASON_TOOLGRAPH_TOOL_NOT_FOUND = "toolgraph_tool_not_found"
# Forward-compatible fallback: an upstream ``rejected`` reason STM does not
# recognize still results in a withhold (never a silent advertise of a tool
# the graph wanted blocked), just under a generic code.
REASON_TOOLGRAPH_REJECTED = "toolgraph_rejected"
# Not a verdict but the absence of one: the stdio consult runs once per session
# and this candidate appeared afterwards (an upstream added a tool, and #917
# re-advertised), so the graph was never asked about it — which must not read
# the same as "consulted and allowed" to a policy gateway. A signal rule like
# the rest of the family: ``strict`` withholds, ``review`` demotes, ``explore``
# ignores (#918).
REASON_TOOLGRAPH_UNCONSULTED = "toolgraph_unconsulted"

# WHOLE-CALL — the consult itself failed (or aborted) and the operator's
# ``on_*`` knob resolved to ``closed``. These are profile-INDEPENDENT and
# withhold EVERY candidate at once (``filter_tools(withhold_all=...)``); they
# are STM-owned, never derived from an upstream row.
REASON_TOOLGRAPH_UNREACHABLE = "toolgraph_unreachable"
REASON_TOOLGRAPH_AGENT_NOT_FOUND = "toolgraph_agent_not_found"
REASON_TOOLGRAPH_PROTOCOL_ERROR = "toolgraph_protocol_error"

# Upstream ``eligible_tools`` reject reason → STM per-candidate code. The graph
# owns its own reason vocabulary (selector.py); this is the single 1:1
# translation boundary shared by stdio verdicts and portable bundles.
# ``TOOL_NOT_FOUND`` is still gated separately by the stdio caller's
# ``on_tool_not_found`` knob; mapping it here only centralizes its stable code.
# An unknown reason maps to the generic ``REASON_TOOLGRAPH_REJECTED``
# (forward-compatible withhold).
_TOOLGRAPH_REASON_MAP: dict[str, str] = {
    "TOOL_NOT_FOUND": REASON_TOOLGRAPH_TOOL_NOT_FOUND,
    "NOT_GRANTED": REASON_TOOLGRAPH_NOT_GRANTED,
    "DENY_VIOLATION": REASON_TOOLGRAPH_DENY_VIOLATION,
    "DENY_GOVERNED": REASON_TOOLGRAPH_DENY_GOVERNED,
    "DRIFTED": REASON_TOOLGRAPH_DRIFTED,
    "AMBIGUOUS_TOOL": REASON_TOOLGRAPH_AMBIGUOUS,
    "UNMAPPED": REASON_TOOLGRAPH_UNMAPPED,
}
# The upstream reason string for an uncrawled candidate (the graph's blind
# spot), gated separately by ``on_tool_not_found``.
_TOOLGRAPH_TOOL_NOT_FOUND = "TOOL_NOT_FOUND"


def toolgraph_reject_code(reason: str) -> str:
    """Translate a Toolgraph reason into STM's stable rejection vocabulary."""
    return _TOOLGRAPH_REASON_MAP.get(reason, REASON_TOOLGRAPH_REJECTED)


# Error categories that count against a TOOL's health. Proxy-side failures
# (programming, internal_error, lock_timeout) are our bugs, not the
# upstream's — hiding a tool because the proxy's own pipeline broke would
# punish the wrong party.
UPSTREAM_ERROR_CATEGORIES: tuple[str, ...] = (
    ErrorCategory.TRANSPORT.value,
    ErrorCategory.TIMEOUT.value,
    ErrorCategory.PROTOCOL.value,
    ErrorCategory.UPSTREAM_ERROR.value,
    # Breaker fast-fails (#608) are upstream-attributable by construction —
    # the breaker only opens after consecutive transport/timeout failures.
    # Excluding them would let fast-fail rows inflate the call-count
    # denominator in ``get_tool_error_stats`` and *dilute* per-tool error
    # rates during exactly the window the upstream is down.
    ErrorCategory.CIRCUIT_OPEN.value,
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
    # Normalized ``Tool.execution.task_support`` ("forbidden" / "optional" /
    # "required"; ``None`` — an absent ``execution`` — means forbidden, per
    # spec). A raw artifact, never advertisement data: ``ProxyToolInfo`` has
    # no ``execution`` field, so the proxy structurally cannot forward task
    # support it has no call path for (#892).
    raw_task_support: str | None = None


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


def _plain(value: Any) -> Any:
    """One WHOLE metadata field reduced to JSON types, or an exception.

    Only the outermost value of a field takes this path; anything nested
    that is not already JSON-native is unrenderable by construction (see
    :func:`_scan_texts`). The ladder, in order:

    - JSON-native — returned as is;
    - a pydantic model — dumped through ``BaseModel.model_dump`` *unbound*.
      An instance or subclass override decides only what this gate sees,
      while MCP serializes the real field, so the override is precisely the
      narration the scan must not accept; the base method also brings in
      computed fields, which reading attributes misses (codex R3);
    - anything else carrying pydantic serialization machinery — REFUSED.
      Its wire form is produced by a serializer this function does not run
      (a ``field_serializer`` on a pydantic dataclass can emit a credential
      that no attribute holds, codex R4), so its state is not evidence
      about what the client receives;
    - a duck-typed ``model_dump`` (the ``SimpleNamespace`` / ``MagicMock``
      stand-ins on the degradation paths, which are not models) — its own
      dump is all there is;
    - a plain object — ``vars(obj)``, the state it holds, re-encoded by
      these same rules and never its ``__str__`` / ``__repr__``, which the
      object itself controls.

    Anything else raises, and the caller turns that into a flag.
    """
    if value is None or isinstance(value, (str, int, float, bool, dict, list, tuple)):
        return value
    if isinstance(value, BaseModel):
        return BaseModel.model_dump(value, mode="json", by_alias=True, exclude_none=True)
    if hasattr(value, "__pydantic_serializer__"):
        raise TypeError(f"unscannable pydantic value of type {type(value).__name__}")
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", by_alias=True, exclude_none=True)
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict):
        return data
    raise TypeError(f"unrenderable metadata value of type {type(value).__name__}")


def _string_leaves(value: Any, out: list[str]) -> None:
    """Collect every DECODED string inside ``value`` (keys included).

    The serialized form alone is not enough: ``json.dumps`` escapes a
    newline inside a string to a literal ``\\n``, and the credential labels
    match on real whitespace before their ``:``, so ``{"note": "api_key\\n:
    hunter2"}`` scanned clean while the client received the decoded text
    (codex R3). Leaves restore what the client actually reads; the
    serialized form stays in the scan for the quoted-key patterns, which
    only exist in JSON rendering.
    """
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                out.append(key)
            _string_leaves(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _string_leaves(item, out)
    # Nothing else can appear: :func:`_scan_texts` serializes with no
    # ``default`` hook, so a nested non-JSON-native value has already failed
    # the field closed before this walk runs.


def _scan_texts(value: Any) -> list[str] | None:
    """Every text one field contributes, or ``None`` if it is unrenderable.

    Deterministic (sorted keys) so the same candidate always scans the same
    way. Any failure returns ``None`` rather than an approximate rendering,
    and the caller treats ``None`` as a match: a value this gate cannot read
    is a value it cannot clear (codex R1). The whole body sits inside the
    barrier, attribute lookup included — a raising ``model_dump`` *property*
    would otherwise propagate out and abort the entire filter pass.

    ``json.dumps`` runs with NO ``default`` hook, so the only value that may
    be non-JSON-native is the field itself (:func:`_plain` handles that one).
    A nested one fails the field closed instead of being rendered from its
    attributes: for anything embedded, its wire form is produced by the
    SDK's own pydantic serialization, which this function does not run, so
    its Python state is not evidence about what the client receives
    (codex R4). Genuine upstream metadata arrives as wire JSON and never
    takes that path.
    """
    if value is None:
        return []
    try:
        plain = _plain(value)
        text = json.dumps(plain, sort_keys=True, separators=(",", ":"))
        texts = [text]
        _string_leaves(plain, texts)
        return texts
    except Exception:
        return None


def _flags_sensitive_metadata(candidate: ExposureCandidate) -> bool:
    """Whether this candidate's metadata trips the credential scan.

    Covers the whole surface one successful registration puts in front of
    the client, not a sample of it (codex R1):

    - the composed ``name`` — the upstream's raw name is embedded verbatim,
      and MCP's name charset admits the prefix-anchored token patterns;
    - the raw upstream description and the advertised one (which may
      originate from an operator ``description_override``);
    - the raw input schema — distillation only ever *removes* content;
    - ``output_schema`` and ``meta`` (advertised as ``_meta``), forwarded
      with no truncation or distillation at all;
    - ``annotations`` as REGISTRATION WILL SEND THEM, i.e. through
      :func:`tag_annotations_title`, the same function the registration
      path calls. That tagging prepends ``info.server`` to a non-empty
      ``title``, so the untagged value plus an unconditional server scan is
      not equivalent: it rejects a clean, annotation-less tool whose server
      name merely looks credential-shaped, since nothing would have carried
      that name to the client (codex R2). Calling the shared function is
      also the only form that cannot drift from what registration does.

    ``title``, ``icons`` and ``execution`` are deliberately absent: the
    proxy forwards none of them today (#892, #895). Add them here in the
    same breath as any change that starts forwarding them.

    Every text is scanned SEPARATELY. Scanning one joined blob let a
    pattern bridge a field boundary: the credential labels admit whitespace
    before their ``:``, and a newline is whitespace, so a description
    ending in ``api_key`` matched a next field opening with ``:`` although
    neither held a credential (codex R2). The three string fields above
    contribute themselves; each structured field contributes two kinds of
    text — its deterministic JSON serialization, which the quoted-key
    patterns need, and every decoded string inside it, which the
    whitespace-sensitive label patterns need (:func:`_string_leaves`). A
    field that cannot be rendered at all flags the candidate rather than
    dropping out of the scan — see :func:`_scan_texts`.

    Deliberately UNBOUNDED: this is a security gate, not a scoring
    document, and unlike payload telemetry there is no downstream backstop
    for advertisement — whatever passes here goes to the client's context
    verbatim on every ``tools/list``. The cost is a full pattern pass per
    text, so a schema of many tiny strings pays per leaf rather than once
    over one blob; that is bounded by what the upstream already made the
    advertisement path serialize, it runs once per advertisement rather
    than per call, and the alternative — capping the scan — is what would
    let a credential through (codex R2, R4).
    """
    info = candidate.info
    texts = [candidate.raw_description, info.description, info.prefixed_name]
    try:
        tagged_annotations = tag_annotations_title(info.annotations, info.server)
    except Exception:
        return True
    for value in (
        candidate.raw_schema or {},
        tagged_annotations,
        info.output_schema,
        info.meta,
    ):
        rendered = _scan_texts(value)
        if rendered is None:
            return True
        texts.extend(rendered)
    return any(contains_sensitive_content(text, CREDENTIAL_PATTERNS) for text in texts)


@dataclass(frozen=True, slots=True)
class InterpretedVerdict:
    """Structured, policy-free read of one ``eligible_tools`` consult response.

    Pure parse of the upstream wire shape into what the manager needs, with no
    knowledge of the ``on_*`` knobs (the caller applies those):

    - ``agent_found`` — ``False`` is the upstream's structured *abort* signal
      (the configured ``agent_id`` is unknown to the graph), distinct from an
      empty result; the caller maps it onto ``on_agent_not_found``.
    - ``rejects`` — graph candidate ref → STM per-candidate reason code, for
      *every* ``rejected`` row INCLUDING ``TOOL_NOT_FOUND`` (mapped to
      :data:`REASON_TOOLGRAPH_TOOL_NOT_FOUND`). The caller drops the
      tool-not-found entries when ``on_tool_not_found`` is ``open``.
    - ``tool_not_found_refs`` — the subset of refs the graph never crawled,
      surfaced separately so the manager can run the server-name-mismatch
      heuristic regardless of the ``on_tool_not_found`` posture.
    - ``graph_generation`` — the replay/cache key (#468); always a real ``int``
      because the upstream stamps every response (the ``agent_found=False``
      abort included), so a missing one is contract drift.
    """

    agent_found: bool
    rejects: dict[str, str]
    tool_not_found_refs: frozenset[str]
    graph_generation: int


def interpret_verdict(verdict: Mapping[str, Any]) -> InterpretedVerdict:
    """Validate the ``eligible_tools`` verdict shape and parse it (no policy).

    Raises :class:`ToolgraphProtocolError` on a malformed payload — a reachable
    graph returning a shape STM cannot trust is a *contract* failure (the
    caller maps it onto ``on_protocol_error``), never silently treated as an
    empty/clean result. ``graph_generation`` must be a real ``int`` on EVERY
    path (the abort included — the server stamps it unconditionally); ``bool``
    is rejected as it is an ``int`` subclass but never a valid generation.
    """
    agent_found = verdict.get("agent_found")
    if not isinstance(agent_found, bool):
        raise ToolgraphProtocolError(
            f"eligible_tools verdict has a non-boolean 'agent_found': {agent_found!r}"
        )

    gen = verdict.get("graph_generation")
    if isinstance(gen, bool) or not isinstance(gen, int):
        raise ToolgraphProtocolError(
            f"eligible_tools verdict has a non-integer 'graph_generation': {gen!r}"
        )
    graph_generation = gen

    if not agent_found:
        return InterpretedVerdict(
            agent_found=False,
            rejects={},
            tool_not_found_refs=frozenset(),
            graph_generation=graph_generation,
        )

    rows = verdict.get("rejected")
    if not isinstance(rows, list):
        raise ToolgraphProtocolError(
            f"eligible_tools verdict 'rejected' is not a list: {type(rows).__name__}"
        )

    rejects: dict[str, str] = {}
    tool_not_found: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ToolgraphProtocolError(f"eligible_tools 'rejected' row is not an object: {row!r}")
        ref = row.get("candidate")
        reason = row.get("reason")
        if not isinstance(ref, str) or not isinstance(reason, str):
            raise ToolgraphProtocolError(
                f"eligible_tools 'rejected' row missing string candidate/reason: {row!r}"
            )
        if reason == _TOOLGRAPH_TOOL_NOT_FOUND:
            tool_not_found.add(ref)
        # Unknown upstream reasons still withhold (fail-safe) under a generic
        # code rather than silently advertising a blocked tool.
        rejects[ref] = toolgraph_reject_code(reason)

    return InterpretedVerdict(
        agent_found=True,
        rejects=rejects,
        tool_not_found_refs=frozenset(tool_not_found),
        graph_generation=graph_generation,
    )


# ── rank_features per-candidate facts (#469 data readiness) ────────────────
#
# ``parse_risk_scores`` folds a whole ``rank_features`` row into one scalar and
# drops the ``0.0``/``None`` rows, which is right for a ranking PENALTY (a
# missing ref means "no demotion") but leaves telemetry unable to tell a clean
# ALLOW from an unresolved candidate, and loses every fact the score was
# derived from. #469 needs those facts logged per ELIGIBLE candidate, so the
# functions below keep a dense, sanitized view of the same response.

# The graph's own verdict vocabulary (toolgraph ``selector.rank_features``).
# Recorded verbatim only when it is one of these; see
# ``GRAPH_VALUE_UNRECOGNIZED``.
GRAPH_VERDICTS: frozenset[str] = frozenset(
    {"ALLOW", "DENY", "NOT_GRANTED", "TOOL_NOT_FOUND", "AMBIGUOUS_TOOL"}
)

# Worst-case DENY-path classification, same source.
GRAPH_CLASSIFICATIONS: frozenset[str] = frozenset({"violation", "authorized_but_governed"})

# Stand-in for a verdict/classification string this STM version does not know.
# Forward-compatible in both directions that matter: an upstream that adds a
# member stays *visible* as a fact (the row is not silently dropped), and no
# upstream-authored string ever reaches the telemetry file — the selection
# log's redaction contract is structural, so a free-form value must not ride
# in on an enum-shaped field. Same posture as ``toolgraph_reject_code``'s
# generic fallback.
GRAPH_VALUE_UNRECOGNIZED = "other"

# Upper bound on a recorded ``deny_path_count``. The count is DENY-evidence
# paths for ONE tool, so a value beyond this is not a large answer but a
# corrupt one — and an unbounded integer is not portable as a learning feature
# (it has no float, and no fixed-width column). Above the bound the fact
# records as unknown rather than as a number nothing can use.
MAX_DENY_PATH_COUNT = 10_000

# Boolean facts copied through as-is. ``None`` (upstream's own "not knowable
# for this row") is preserved and distinguished from ``False``.
_GRAPH_FACT_FLAGS: tuple[str, ...] = (
    "found",
    "ambiguous",
    "permitted",
    "is_drifted",
    "is_unmapped",
    "has_unbacked_edges",
    "read_only_hint",
    "destructive_hint",
    "idempotent_hint",
    "open_world_hint",
)

# Every sanitized row carries exactly these keys, always, so a replay reader
# never has to distinguish "field absent" from "fact unknown" (the latter is
# an explicit ``None``).
GRAPH_FACT_KEYS: tuple[str, ...] = (
    *_GRAPH_FACT_FLAGS,
    "verdict",
    "classification",
    "deny_path_count",
    "risk_score",
)


def _graph_enum(value: Any, allowed: frozenset[str]) -> str | None:
    """Closed-vocabulary passthrough: member, sentinel, or ``None``."""
    if not isinstance(value, str):
        return None
    return value if value in allowed else GRAPH_VALUE_UNRECOGNIZED


def sanitize_graph_facts_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce one ``rank_features`` row to the facts telemetry may persist.

    The upstream row also carries ``tool_key``, ``deny_paths`` and (when
    ambiguous) ``candidates``: graph-authored identifiers and policy-evidence
    paths, i.e. exactly the free-form text the selection log's structural
    redaction forbids. They are dropped here rather than screened later —
    ``deny_paths`` survives only as ``deny_path_count`` (``None`` when the row
    reported no list at all, ``0`` when it reported an empty one). The
    candidate ref itself is not part of the row: callers key by it and the log
    records STM's own prefixed name.

    Lenient by construction — a wrong-typed field becomes ``None``, never an
    exception — because these facts are ranking telemetry, never exposure.
    """
    facts: dict[str, Any] = {}
    for flag in _GRAPH_FACT_FLAGS:
        value = row.get(flag)
        facts[flag] = value if isinstance(value, bool) else None
    facts["verdict"] = _graph_enum(row.get("verdict"), GRAPH_VERDICTS)
    facts["classification"] = _graph_enum(row.get("classification"), GRAPH_CLASSIFICATIONS)
    facts["deny_path_count"] = _deny_path_count(row)
    facts["risk_score"] = finite_risk_score(row.get("risk_score"))
    return facts


def _deny_path_count(row: Mapping[str, Any]) -> int | None:
    """DENY-evidence count from either an upstream row or a sanitized one.

    Sanitized rows carry the count and NOT the paths it came from, so a
    paths-only rule silently erased it whenever a row was sanitized twice —
    which the consult cache does on every write and read, making a warm start
    disagree with the cold one that filled it. Being idempotent is part of this
    function's contract, not an optimization.
    """
    deny_paths = row.get("deny_paths")
    count: Any = len(deny_paths) if isinstance(deny_paths, list) else row.get("deny_path_count")
    # The bound applies however the count was reported: a row carrying that
    # many real paths is as unusable a feature as a stored integer claiming it.
    if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= MAX_DENY_PATH_COUNT:
        return count
    return None


def finite_risk_score(score: Any) -> float | None:
    """A recordable risk score, or ``None``.

    Total by construction. ``bool`` is an ``int`` subclass but never a valid
    score. ``float()`` is not total either: a large enough JSON integer raises
    ``OverflowError``, which would escape an enrichment path documented as
    best-effort and abort startup. Non-finite values are refused as well —
    ``NaN``/``Infinity`` are not JSON, so one would travel into the telemetry
    file and the consult cache as a token strict readers reject, and as a
    penalty ``inf`` makes every score ``-inf``.

    Out-of-range but finite values are kept: the graph's ``[0, 1]`` promise is
    the graph's to keep, and a fact it reported is worth recording as reported.
    The penalty path clamps and filters separately.

    The predicate itself is :func:`~memtomem_stm.utils.numeric.finite_number`,
    shared with every other reader of an externally authored number; this name
    is the risk-score domain's use of it.
    """
    return finite_number(score)


def parse_graph_facts(verdict: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract per-candidate facts from a ``rank_features`` verdict (#469).

    Returns ``{candidate_ref: sanitized_row}`` for **every** row naming a
    candidate — including the clean (``risk_score`` ``0.0``) and unresolved
    (``None``) rows :func:`parse_risk_scores` omits, which is the whole point:
    a ranker trained on these needs "the graph looked and found nothing wrong"
    to be distinguishable from "the graph could not look".

    Lenient and never raising, for the same reason as
    :func:`parse_risk_scores`: a malformed payload yields an empty map and the
    caller degrades to logging no facts. A later row wins on a duplicated ref,
    which is where these two views deliberately diverge — the penalty map keeps
    the last POSITIVE score instead. See :func:`parse_graph_features`.
    """
    return parse_graph_features(verdict)[0]


def parse_graph_features(
    verdict: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
    """One pass, two products: per-candidate facts and the penalty map.

    Callers that need both — the consult, which logs the facts and penalizes
    from the scores — take them from here, so "one response, one traversal" is
    true at the call site rather than equal by coincidence.

    They differ on ONE point, and only for a payload that repeats a candidate
    (an upstream contract violation): the facts follow the last row for that
    ref, because "the row for this candidate" is what they are, while the
    penalty keeps the last POSITIVE score. That asymmetry is inherited, not
    invented — the pre-#469 parser assigned on a positive score and skipped
    otherwise, so a later ``0.0``/``None``/malformed row never deleted an
    existing penalty, and a candidate must not lose its demotion to a repeat
    row the graph should not have sent.
    """
    rows = verdict.get("features")
    if not isinstance(rows, list):
        return {}, {}
    facts: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ref = row.get("candidate")
        if not isinstance(ref, str):
            continue
        sanitized = sanitize_graph_facts_row(row)
        facts[ref] = sanitized
        score = sanitized["risk_score"]
        if score is not None and score > 0.0:
            scores[ref] = score
    return facts, scores


def parse_risk_scores(verdict: Mapping[str, Any]) -> dict[str, float]:
    """Extract per-candidate ``risk_score`` from a ``rank_features`` verdict (#493).

    Returns ``{candidate_ref: risk_score}`` for every resolved row carrying a
    *positive* numeric ``risk_score`` (the graph's rule-based data-flow/DENY
    risk, ``[0,1]``). Rows with ``risk_score`` ``None`` (unresolved / ambiguous)
    or ``0.0`` (clean) are omitted so the map stays sparse — a missing ref means
    "no penalty", which is exactly the ranker's default.

    Unlike :func:`interpret_verdict` this is **lenient and never raises**: the
    risk signal is a best-effort ranking-telemetry enrichment (#466/#468), never
    an exposure input, so a malformed payload yields an empty map (the caller
    degrades to no penalties) rather than a contract failure. ``bool`` is
    rejected as a score — it is an ``int`` subclass but never a valid risk.

    Produced by the same walk as :func:`parse_graph_facts` (#469) rather than
    a second pass: the penalty map and the logged facts are two views of one
    response, and two independent walks would be free to disagree about which
    rows count. See :func:`parse_graph_features` for the one place they deliberately
    differ.
    """
    return parse_graph_features(verdict)[1]


def filter_tools(
    candidates: list[ExposureCandidate],
    cfg: ExposureConfig,
    unhealthy: frozenset[tuple[str, str]] = frozenset(),
    *,
    external_rejects: Mapping[tuple[str, str], str] | None = None,
    withhold_all: str | None = None,
) -> EligibilityResult:
    """Apply the hard-filter rules to one advertisement candidate set.

    Pure and deterministic: same candidates + config + health flags +
    external verdict → identical result, independent of call count or wall
    clock. *unhealthy* is the cached startup snapshot from
    :func:`compute_health_flags`.

    *external_rejects* maps ``(server, original_name)`` → an already-resolved
    ``toolgraph_*`` code (the optional tool-graph provider's per-candidate
    verdict, #465). It is a SIGNAL rule: it rides the profile ladder exactly
    like ``unhealthy`` (strict rejects, review demotes, explore ignores), and
    ranks above ``unhealthy`` but below ``sensitive_metadata`` — an explicit
    graph policy verdict outweighs a heuristic error-rate flag, but a
    credential in tool metadata (upstream compromise) outweighs both.

    *withhold_all*, when set, is the whole-call fail-closed code: the consult
    failed under a ``closed`` knob, so EVERY candidate is withheld under that
    one STM-owned code, profile-INDEPENDENTLY (explore included). This is the
    operator's explicit "no graph, no tools" posture and short-circuits every
    per-candidate rule below.
    """
    if withhold_all is not None:
        return EligibilityResult(
            eligible=[],
            reject_reasons={candidate.info.prefixed_name: withhold_all for candidate in candidates},
            risk_penalties={},
        )
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
        # A task-required tool cannot be served by the synchronous call path,
        # so advertising it promises a call that always fails — withheld in
        # every profile, ``optional`` deliberately downgraded (#892).
        if candidate.raw_task_support == "required":
            reject_reasons[info.prefixed_name] = REASON_TASK_REQUIRED
            continue

        # ── signal rules (profile-dependent) ─────────────────────────────
        if cfg.profile is not ExposureProfile.EXPLORE:
            flagged_reason: str | None = None
            external_reason = (
                external_rejects.get((info.server, info.original_name))
                if external_rejects
                else None
            )
            # Precedence: upstream-compromise (credential in metadata) >
            # explicit graph policy verdict > heuristic error-rate health.
            if _flags_sensitive_metadata(candidate):
                flagged_reason = REASON_SENSITIVE_METADATA
            elif external_reason is not None:
                flagged_reason = external_reason
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
