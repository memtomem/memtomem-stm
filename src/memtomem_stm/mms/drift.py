"""Drift hash for imported MCP servers (W2 host-sync foundation).

W2 needs a stable per-entry fingerprint so ``mms import`` can detect when a
host's MCP config drifted (got edited externally) since the last import. The
hash must absorb cosmetic JSON/TOML re-formatting (host serializers vary)
but flip on any semantic edit (new arg, swapped command, mutated env).

Canonical form: ``command`` + ordered ``args`` + key-sorted ``env``. The
``prefix`` field is mms-derived metadata and intentionally excluded — a
future tweak to ``_derive_prefix`` must not false-flag drift on every
imported server.

Hash representation: ``"sha256:" + hex[:16]``. 64 bits of entropy is plenty
per-server-name; the algorithm prefix future-proofs a swap (e.g. blake3)
without ambiguity in the sidecar TOML.

W1 import already coerces args/env to canonical Python types
(``list[str]`` / ``dict[str, str]``) and applies pydantic defaults for the
empty-vs-absent equivalence rule, so this module needs no defensive
parsing.
"""

from __future__ import annotations

import hashlib
import json

from memtomem_stm.mms.state import RegistryServer

HASH_VERSION = 1
HASH_PREFIX = "sha256"
HASH_HEX_LEN = 16


def canonical_form(server: RegistryServer) -> str:
    """Return the JSON canonical form whose SHA-256 is the drift hash.

    Exposed for debuggability — callers should not parse this; treat it as
    opaque. The exact serialization is locked by tests so any future change
    is loud.
    """
    payload = {
        "command": server.command,
        "args": list(server.args),
        "env": dict(sorted(server.env.items())),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_drift_hash(server: RegistryServer) -> str:
    """Return ``"sha256:<16hex>"`` for ``server``'s canonical form."""
    digest = hashlib.sha256(canonical_form(server).encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}:{digest[:HASH_HEX_LEN]}"
