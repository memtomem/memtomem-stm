"""Portable Toolgraph policy-bundle consumer.

This module deliberately imports no Toolgraph Python package.  The JSON
artifact is the compatibility boundary between Toolgraph's control plane and
STM's enforcement plane.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memtomem_stm.proxy.tool_eligibility import (
    toolgraph_reject_code,
)

SCHEMA_VERSION = 1
KIND = "toolgraph.policy-bundle"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_TOOL_KEY_RE = re.compile(r"^[^:]+::[^:]+$")


class PolicyBundleError(ValueError):
    """The artifact cannot be trusted as an enforcement snapshot."""


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
    """Match Toolgraph's canonical JSON encoding for contract fingerprints."""
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
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
        "read_only_hint": getattr(annotations, "readOnlyHint", None),
        "destructive_hint": getattr(annotations, "destructiveHint", None),
        "idempotent_hint": getattr(annotations, "idempotentHint", None),
        "open_world_hint": getattr(annotations, "openWorldHint", None),
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
