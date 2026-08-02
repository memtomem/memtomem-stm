"""Portable Toolgraph policy-bundle consumer.

This module deliberately imports no Toolgraph Python package.  The JSON
artifact is the compatibility boundary between Toolgraph's control plane and
STM's enforcement plane.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat as stat_module
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memtomem_stm.proxy.tool_eligibility import (
    toolgraph_reject_code,
)
from memtomem_stm.utils.json_out import dumps as _json_dumps

SCHEMA_VERSION = 1
KIND = "toolgraph.policy-bundle"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_TOOL_KEY_RE = re.compile(r"^[^:]+::[^:]+$")


class PolicyBundleError(ValueError):
    """The artifact cannot be trusted as an enforcement snapshot."""


def bundle_provenance_warnings(path: Path) -> list[str]:
    """Advisory: name the ways *path* could be swapped for someone else's policy.

    The bundle is the gateway's entire enforcement authority and carries no
    signature — ``bundle_digest`` identifies bytes, it does not authenticate
    them. Whoever can write the file, or rename any directory above it, decides
    what this proxy will and will not expose. Nothing else stands in the way.

    This asks only "who can *replace* this?", never "who can read it?". A
    world-readable ``0644`` bundle is perfectly fine — it holds no secrets. That
    is why ``config.py``'s ``_permissive_mode`` is deliberately not reused here:
    it is a secrecy check (it flags any group/world READ bit), so it would both
    cry wolf over ``0644`` and miss a world-writable parent directory.

    **Scope: traditional Unix owner and mode bits only. Silence is not an
    assurance.** Extended ACLs are not evaluated, so a ``0644`` file carrying
    ``everyone allow write`` reads as clean here. That is a deliberate limit, not
    an oversight: the stdlib cannot see ACLs on macOS at all (no ``os.listxattr``
    there), and mere ACL *presence* is not a signal — a stock macOS home carries
    ``group:everyone deny delete``, which is restrictive, so warning on presence
    would fire for every default install. Evaluating ACL entries properly means
    platform-specific principal resolution, i.e. the same class of failure this
    diagnostic must never introduce. Findings reflect real mode and ownership
    state, though they can be conservative (see the ``01770`` case below); what
    they cannot see is listed here.

    **Advisory only.** Findings are logged; the bundle is still adopted. Turning
    an insecure bundle into a startup refusal under ``strict`` is a separate
    decision with its own blast radius — it would fail a proxy closed over a
    directory mode, which may be an intentional deployment property — so it waits
    on evidence that this fires on real installs (#707).

    POSIX-only. Windows reports permission bits that do not mean this and a
    constant ``st_uid``, so every check below would be vacuous or misfire.
    Returns ``[]`` there, and on any stat failure — a bundle we cannot inspect
    is the reload path's problem to report, not this diagnostic's.
    """
    if sys.platform == "win32":
        return []
    euid = os.geteuid()
    try:
        expanded = path.expanduser()
        # Absolute but NOT lexically normalised. ``bundle_path`` may be relative
        # (nothing forces otherwise) and ``Path.parents`` of a relative path
        # stops at ``.``, so it must be anchored — but ``os.path.abspath`` would
        # collapse ``..`` textually, and the kernel does not: in ``link/..`` the
        # ``..`` applies to the link's TARGET. Keeping the components lets the
        # lstat walk resolve each prefix the way the loader will.
        configured = expanded if expanded.is_absolute() else Path.cwd() / expanded
        # Analyse what is actually opened. Inspecting a path the loader does not
        # read is worse than not looking: it reports all-clear about a file
        # nothing enforces.
        target = Path(os.path.realpath(configured))
    except (OSError, RuntimeError):
        # RuntimeError: expanduser() raises it, not OSError, when a home cannot
        # be resolved (``~nosuchuser/bundle.json``). This function promises to
        # never be the reason anything breaks, so the promise cannot be
        # conditional on which exception type a stdlib call happens to pick.
        return []
    findings: list[str] = _redirectable_symlinks(configured, euid)
    try:
        info = os.stat(target)
    except OSError:
        return findings
    if info.st_mode & 0o022:
        findings.append(
            f"{target} is writable by group/other (mode {stat_module.S_IMODE(info.st_mode):04o})"
        )
    if info.st_uid not in (euid, 0):
        findings.append(f"{target} is owned by uid {info.st_uid}, not by this process or root")
    findings.extend(_replaceable_ancestors(target, euid))
    return findings


def _substituters(info: os.stat_result, euid: int) -> str:
    """Who besides us can swap the entries of a directory with this stat."""
    renamers = _entry_renamers(info.st_mode)
    if renamers:
        return renamers
    if info.st_uid not in (euid, 0):
        return f"uid {info.st_uid}"
    return ""


def _link_finding(
    link: Path, info: os.stat_result, parent_info: os.stat_result, euid: int
) -> str | None:
    """Report *link* when someone else can replace that directory entry.

    A link redirects the gateway to a policy of someone's choosing without ever
    touching the file we validated — but only if they can *replace the link*.
    POSIX gives no way to edit a symlink's target in place; it must be unlinked
    and recreated, which needs write and search on the directory holding it. So
    a link in a directory only we can write is not a vector, and reporting one
    would be exactly the noise this check cannot afford.
    """
    who = _substituters(parent_info, euid)
    if not who and info.st_uid not in (euid, 0):
        # The sticky exemption protects an entry from everyone *except its own
        # owner*. A link we do not own, in a directory its owner can write and
        # search (``/tmp`` at 01777 being the whole point), is theirs to unlink
        # and recreate whenever they like — and the resolved chain we analyse
        # can be perfectly secure meanwhile.
        #
        # Deliberately conservative for a group-only directory (``01770``):
        # proving the owner is in that group would mean resolving a foreign uid
        # through pwd/grp, which fails or stalls exactly where it would matter
        # (LDAP, containers, a uid with no local passwd entry) — a new failure
        # mode for a diagnostic that must never be the reason anything breaks.
        # So this accepts a false positive when current membership cannot be
        # established, and lets a human judge.
        if _write_search_classes(parent_info.st_mode):
            who = f"its owner uid {info.st_uid}"
    if not who:
        return None
    return (
        f"{link} is a symlink and {link.parent} lets {who} replace it, "
        f"so the policy this proxy loads can be re-pointed"
    )


# The kernel gives up after ~40 links (ELOOP); matching it bounds a symlink
# cycle without inventing a limit of our own.
_MAX_SYMLINK_HOPS = 40


def _redirectable_symlinks(configured: Path, euid: int) -> list[str]:
    """Every symlink the loader traverses that someone else could re-point.

    This walk exists because the two obvious ones both miss hops: the ancestor
    analysis follows the *resolved* chain, which need not contain any link at
    all, and walking only the configured components stops at the first one —
    ``a/bundle -> b/link2 -> c/real`` puts ``b/link2`` on neither, so an
    exposed ``b`` would go unreported while anyone there redirects what loads.

    So each link is followed the way the kernel follows it: prefix by prefix
    (an ``lstat`` per step, which is what makes a ``..`` component behave here
    exactly as at load time), then into the link's target with any remaining
    components appended, recursively, bounded by ``_MAX_SYMLINK_HOPS``.
    """
    findings: list[str] = []
    reported: set[tuple[int, int, str]] = set()
    _scan_for_links(configured, euid, findings, reported, [_MAX_SYMLINK_HOPS])
    return findings


def _scan_for_links(
    path: Path,
    euid: int,
    findings: list[str],
    reported: set[tuple[int, int, str]],
    budget: list[int],
) -> None:
    """Resolve *path* one component at a time, reporting re-pointable links."""
    current = Path(path.anchor)
    parts = list(path.relative_to(path.anchor).parts)
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except OSError:
            return
        if not stat_module.S_ISLNK(info.st_mode):
            continue
        if budget[0] <= 0:
            return
        # Budget burns per traversal, dedup keys on the entry: one link reached
        # by two spellings (``link/../link/x``) is one fact to report but two
        # hops the kernel really walks.
        budget[0] -= 1
        parent_info: os.stat_result | None
        try:
            parent_info = os.stat(current.parent)
        except OSError:
            # Judging THIS entry needs its parent, but the hops beyond it do
            # not: keep following the link rather than abandoning the scan.
            parent_info = None
        if parent_info is not None:
            # Identity is the DIRECTORY ENTRY — parent dir plus name — not the
            # inode: symlinks can be hard-linked, so two entries under parents
            # with different permissions share one inode, and keying on it
            # would let a secure sighting suppress the exposed twin. Stat'ing
            # the parent also collapses spellings (``sub/..`` and ``.``) onto
            # one identity.
            entry = (parent_info.st_dev, parent_info.st_ino, current.name)
            if entry not in reported:
                reported.add(entry)
                finding = _link_finding(current, info, parent_info, euid)
                if finding:
                    findings.append(finding)
        try:
            target = Path(os.readlink(current))
        except OSError:
            return
        if not target.is_absolute():
            # A relative target resolves against the link's own directory.
            target = current.parent / target
        # Whatever followed the link resolves through the target, so hand the
        # rest over and let the same walk apply to the hop.
        rest = parts[index + 1 :]
        _scan_for_links(target.joinpath(*rest), euid, findings, reported, budget)
        return


def _write_search_classes(mode: int) -> str:
    """Classes that may create and remove entries in a dir of *mode*, sticky aside.

    Renaming an entry needs **write and search** together: a ``0720`` parent
    hands group the write bit but no ``x``, so group cannot resolve a name
    inside it and cannot swap the bundle. Masking on write alone would warn
    about such a directory, and a diagnostic nobody trusts is worse than none.
    """
    classes = []
    if mode & 0o020 and mode & 0o010:
        classes.append("group")
    if mode & 0o002 and mode & 0o001:
        classes.append("other")
    return "/".join(classes)


def _entry_renamers(mode: int) -> str:
    """Which classes can replace *someone else's* entry in a dir of *mode*.

    The sticky bit clears everyone — ``/tmp`` is world-writable precisely so
    this is safe, because it lets only an entry's own owner remove it. That
    exemption is about entries we own; a *foreign-owned* entry in a sticky
    directory is still replaceable by its owner, which
    :func:`_redirectable_symlinks` handles via :func:`_write_search_classes`.
    """
    if mode & stat_module.S_ISVTX:
        return ""
    return _write_search_classes(mode)


def _replaceable_ancestors(target: Path, euid: int) -> list[str]:
    """Directories above *target* that let a third party substitute the bundle.

    Walked to the root, not just the immediate parent: renaming *any* ancestor
    is enough to put a different file at the same path.
    """
    findings: list[str] = []
    for parent in target.parents:
        try:
            info = os.stat(parent)
        except OSError:
            break
        renamers = _entry_renamers(info.st_mode)
        if renamers:
            findings.append(
                f"{parent} lets {renamers} rename its entries "
                f"(mode {stat_module.S_IMODE(info.st_mode):04o}), so the bundle can be replaced"
            )
        if info.st_uid not in (euid, 0):
            findings.append(f"{parent} is owned by uid {info.st_uid}, not by this process or root")
    return findings


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    tool_key: str
    contract_digest: str
    decision: str
    reason: str | None
    risk_score: float | None

    @property
    def reject_code(self) -> str | None:
        if self.decision == "eligible":
            return None
        return toolgraph_reject_code(self.reason or "")


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    bundle_digest: str
    instance_id: str
    generation: int
    governance_digest: str
    catalog_digest: str
    agent: str
    profile: str
    decisions: Mapping[str, PolicyDecision]


def canonical_json_bytes(value: object) -> bytes:
    """Match Toolgraph's canonical JSON encoding for contract fingerprints.

    Byte-for-byte the producer's encoding for every input the producer can
    encode. It diverges on exactly the inputs the producer *cannot*: its own
    ``canonical_json_bytes`` raises ``UnicodeEncodeError`` on a lone surrogate,
    so no published bundle can hold a digest for surrogate-bearing metadata.

    Ours must not raise, because it runs against **live** ``tools/list``
    metadata rather than a crawl the producer controls, inside the bind loop
    over every tool of every connection — and neither call site guards it. A
    tool crawled clean and later serving a surrogate in its description binds by
    name, reaches this, and took down every ``tools/list`` and ``tools/call`` in
    bundle mode rather than drifting alone (#761). Escaping makes it total; the
    resulting digest cannot match one no producer could have published, so such
    a tool is rejected as DRIFTED, which is the fail-closed outcome the digest
    exists to produce.
    """
    return (
        _json_dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def tool_contract_digest(
    *,
    server: str,
    name: str,
    description: str | None,
    input_schema: dict[str, Any] | None,
    annotations: Any,
) -> str:
    """Fingerprint live MCP metadata using the producer's canonical fields."""
    contract = {
        "server": server,
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "read_only_hint": getattr(annotations, "read_only_hint", None),
        "destructive_hint": getattr(annotations, "destructive_hint", None),
        "idempotent_hint": getattr(annotations, "idempotent_hint", None),
        "open_world_hint": getattr(annotations, "open_world_hint", None),
    }
    return _sha256(canonical_json_bytes(contract))


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyBundleError(f"{field} must be an object")
    return value


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PolicyBundleError(f"{field} must be a non-empty string")
    return value


def _digest(value: Any, field: str) -> str:
    text = _non_empty_string(value, field)
    if _SHA256_RE.fullmatch(text) is None:
        raise PolicyBundleError(f"{field} must be a lowercase SHA-256 digest")
    return text


def parse_policy_bundle(
    payload: bytes,
    *,
    expected_agent: str,
    expected_profile: str,
) -> PolicySnapshot:
    """Validate one exact artifact and return an immutable policy snapshot.

    Unknown additive fields are accepted.  Unknown schema versions, malformed
    decisions, duplicate keys, and scope mismatches are rejected.
    """
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyBundleError(f"bundle is not valid UTF-8 JSON: {exc}") from exc
    doc = _object(raw, "bundle")
    version = doc.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise PolicyBundleError(
            f"unsupported schema_version {version!r}; expected {SCHEMA_VERSION}"
        )
    if doc.get("kind") != KIND:
        raise PolicyBundleError(f"kind must be {KIND!r}")
    if doc.get("agent_found") is not True:
        raise PolicyBundleError("agent_found must be true")
    agent = _non_empty_string(doc.get("agent"), "agent")
    profile = _non_empty_string(doc.get("profile"), "profile")
    if agent != expected_agent:
        raise PolicyBundleError(f"bundle agent {agent!r} does not match {expected_agent!r}")
    if profile != expected_profile:
        raise PolicyBundleError(
            f"bundle profile {profile!r} does not match active profile {expected_profile!r}"
        )

    state = _object(doc.get("graph_state"), "graph_state")
    instance_id = _non_empty_string(state.get("instance_id"), "graph_state.instance_id")
    generation = state.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise PolicyBundleError("graph_state.generation must be a non-negative integer")

    tools = doc.get("tools")
    if not isinstance(tools, list):
        raise PolicyBundleError("tools must be an array")
    decisions: dict[str, PolicyDecision] = {}
    for index, value in enumerate(tools):
        item = _object(value, f"tools[{index}]")
        key = _non_empty_string(item.get("tool_key"), f"tools[{index}].tool_key")
        if _TOOL_KEY_RE.fullmatch(key) is None:
            raise PolicyBundleError(f"tools[{index}].tool_key must be server-qualified")
        if key in decisions:
            raise PolicyBundleError(f"duplicate tool_key {key!r}")
        decision = item.get("decision")
        if decision not in {"eligible", "rejected"}:
            raise PolicyBundleError(f"tools[{index}].decision is invalid")
        reason = item.get("reason")
        if decision == "rejected" and not isinstance(reason, str):
            raise PolicyBundleError(f"tools[{index}].reason is required for a rejection")
        risk = item.get("risk_score")
        if risk is not None and (
            isinstance(risk, bool) or not isinstance(risk, (int, float)) or not 0 <= risk <= 1
        ):
            raise PolicyBundleError(f"tools[{index}].risk_score must be null or within [0,1]")
        decisions[key] = PolicyDecision(
            tool_key=key,
            contract_digest=_digest(
                item.get("tool_contract_digest"), f"tools[{index}].tool_contract_digest"
            ),
            decision=decision,
            reason=reason if isinstance(reason, str) else None,
            risk_score=float(risk) if risk is not None else None,
        )

    return PolicySnapshot(
        bundle_digest=_sha256(payload),
        instance_id=instance_id,
        generation=generation,
        governance_digest=_digest(doc.get("governance_digest"), "governance_digest"),
        catalog_digest=_digest(doc.get("catalog_digest"), "catalog_digest"),
        agent=agent,
        profile=profile,
        decisions=decisions,
    )


def load_policy_bundle(path: Path, *, expected_agent: str, expected_profile: str) -> PolicySnapshot:
    try:
        payload = path.expanduser().read_bytes()
    except OSError as exc:
        raise PolicyBundleError(f"cannot read policy bundle {path.expanduser()}: {exc}") from exc
    return parse_policy_bundle(
        payload, expected_agent=expected_agent, expected_profile=expected_profile
    )
