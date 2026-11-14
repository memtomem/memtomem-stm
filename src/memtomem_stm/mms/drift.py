"""Drift hash for imported MCP servers (W2 host-sync foundation).

W2 needs a stable per-entry fingerprint so ``mms import`` can detect when a
host's MCP config drifted (got edited externally) since the last import. The
hash must absorb cosmetic JSON/TOML re-formatting (host serializers vary)
but flip on any semantic edit (new arg, swapped command, mutated env).

Canonical form: ``command`` + ordered ``args`` + key-sorted ``env``. The
``prefix`` field is mms-derived metadata and intentionally excluded — a
future tweak to ``_derive_prefix`` must not false-flag drift on every
imported server.

The canonical form includes env *values*, which are routinely credentials,
and the hash is printed by ``mms host status --json`` and ``mms host sync
--json``. A plain digest of it is a guess oracle: whoever sees a report and
knows the rest of the entry can confirm a guessed value by recomputing it. So
the hash is keyed with an installation-local secret
(``~/.mms/import_state.toml.key``, see :mod:`memtomem_stm.utils.local_key`),
and nothing unkeyed is printed.

Hash representation (:data:`HASH_VERSION` 2): ``"hmac-sha256:" + hex[:16]``,
an HMAC over the version 1 digest string rather than over the canonical form
directly. That is what lets a stored version 1 baseline be re-keyed without
the server shape it was computed from — see :func:`promote`. The inner
fingerprint therefore stays 64 bits wide, which is plenty per server name.

Version 1 was ``"sha256:" + hex[:16]`` of the canonical form, unkeyed. Rows
of that version are still compared (:func:`matches`) so an upgrade keeps each
entry's drift state, and are re-keyed by the next command that writes the
sidecar.

W1 import already coerces args/env to canonical Python types
(``list[str]`` / ``dict[str, str]``) and applies pydantic defaults for the
empty-vs-absent equivalence rule, so this module needs no defensive
parsing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from memtomem_stm.mms import state
from memtomem_stm.mms.state import ImportStateEntry, RegistryServer
from memtomem_stm.utils.digest import keyed_framed_digest
from memtomem_stm.utils.local_key import (
    LocalKeyError,
    key_path_for,
    load_key,
    load_or_create_key,
)

HASH_VERSION = 2
HASH_PREFIX = "hmac-sha256"
LEGACY_HASH_VERSION = 1
LEGACY_HASH_PREFIX = "sha256"
HASH_HEX_LEN = 16

_DOMAIN = "mms-drift-hash-v2"


class DriftKeyUnusable(state.MmsConfigError):
    """The drift-hash key file exists but cannot be used, or cannot be created.

    A state error so the ``mms`` commands render it as a clean exit like any
    other unusable ``~/.mms`` file. The text names the file, never its content.
    """


def canonical_form(server: RegistryServer) -> str:
    """Return the JSON canonical form the drift hash is derived from.

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


def legacy_v1_hash(server: RegistryServer) -> str:
    """The unkeyed version 1 hash, ``"sha256:<16hex>"``.

    An input to comparison and keying only. It confirms a guessed env value to
    anyone who can recompute it, so it must not reach command output or a newly
    written sidecar row.
    """
    digest = hashlib.sha256(canonical_form(server).encode("utf-8")).hexdigest()
    return f"{LEGACY_HASH_PREFIX}:{digest[:HASH_HEX_LEN]}"


def keyed_from_legacy(legacy: str, key: bytes) -> str:
    """The version 2 hash for the entry whose version 1 hash is *legacy*."""
    digest = keyed_framed_digest((legacy,), key=key, domain=_DOMAIN)
    return f"{HASH_PREFIX}:{digest[:HASH_HEX_LEN]}"


def compute_drift_hash(server: RegistryServer, key: bytes) -> str:
    """Return ``"hmac-sha256:<16hex>"`` for ``server`` under ``key``."""
    return keyed_from_legacy(legacy_v1_hash(server), key)


def drift_key_id(key: bytes) -> str:
    """A non-secret 16-hex identifier of *key*, stored with each row it stamped.

    Without it a row stamped under a key that was later lost and replaced is
    indistinguishable from a row whose host config changed.
    """
    return keyed_framed_digest(("key-id",), key=key, domain=_DOMAIN)[:HASH_HEX_LEN]


# ---------------------------------------------------------------------------
# Key file
# ---------------------------------------------------------------------------


def drift_key_path() -> Path:
    """``~/.mms/import_state.toml.key``."""
    try:
        return key_path_for(state.import_state_path())
    except LocalKeyError as exc:
        raise DriftKeyUnusable(str(exc)) from exc


def load_drift_key() -> bytes | None:
    """Read the key without creating it or touching its mode; ``None`` if absent.

    For the commands that promise to write nothing (``mms host status``,
    ``mms host sync --plan``).
    """
    try:
        return load_key(drift_key_path(), tighten_mode=False)
    except LocalKeyError as exc:
        raise DriftKeyUnusable(str(exc)) from exc


def load_or_create_drift_key() -> bytes:
    """Return the key, creating it on first use. Call under the mms write lock."""
    try:
        return load_or_create_key(drift_key_path())
    except LocalKeyError as exc:
        raise DriftKeyUnusable(str(exc)) from exc


# ---------------------------------------------------------------------------
# Sidecar rows
# ---------------------------------------------------------------------------


def stamp(
    server: RegistryServer, key: bytes, *, last_imported: str, source_label: str
) -> ImportStateEntry:
    """A current-version baseline row for ``server``."""
    return ImportStateEntry(
        drift_hash=compute_drift_hash(server, key),
        drift_hash_version=HASH_VERSION,
        drift_key_id=drift_key_id(key),
        last_imported=last_imported,
        source_label=source_label,
    )


def is_legacy(entry: ImportStateEntry) -> bool:
    """Whether ``entry`` is an unkeyed version 1 baseline."""
    return entry.drift_hash_version == LEGACY_HASH_VERSION


def promote(entry: ImportStateEntry, key: bytes) -> ImportStateEntry:
    """Re-key a version 1 row under ``key``; any other row is returned as is.

    The new hash is derived from the stored one, so the row keeps describing
    the shape it was stamped from: an entry that read ``changed`` before still
    does. ``last_imported`` and ``source_label`` are untouched.
    """
    if not is_legacy(entry):
        return entry
    return entry.model_copy(
        update={
            "drift_hash": keyed_from_legacy(entry.drift_hash, key),
            "drift_hash_version": HASH_VERSION,
            "drift_key_id": drift_key_id(key),
        }
    )


def comparable(entry: ImportStateEntry, key: bytes | None) -> bool:
    """Whether ``entry`` can be compared against a host candidate at all.

    A version 1 row always can: its hash needs no key. A version 2 row can
    only under the key that stamped it — with the key file gone, or replaced,
    a comparison would report every such entry as changed. A row of any other
    version was written by an mms this one does not know.
    """
    if is_legacy(entry):
        return True
    if entry.drift_hash_version == HASH_VERSION:
        return key is not None and entry.drift_key_id == drift_key_id(key)
    return False


def matches(entry: ImportStateEntry, server: RegistryServer, key: bytes | None) -> bool:
    """Whether ``server`` is the shape ``entry`` was stamped from.

    Only meaningful for a :func:`comparable` row; anything else is ``False``.
    """
    if is_legacy(entry):
        return legacy_v1_hash(server) == entry.drift_hash
    if key is None or not comparable(entry, key):
        return False
    return compute_drift_hash(server, key) == entry.drift_hash


def reportable_baseline(entry: ImportStateEntry, key: bytes | None) -> str | None:
    """The value command output may show as ``entry``'s ``baseline_hash``.

    A version 1 hash is the only unkeyed value a row can hold (the sidecar
    model refuses ``sha256:`` under any other version). It is shown re-keyed
    when the key is available and not at all otherwise. Any other row's
    stored hash is keyed already and is shown as stored — also when its key
    is gone, since nothing can be recomputed against it then.
    """
    if not is_legacy(entry):
        return entry.drift_hash
    return keyed_from_legacy(entry.drift_hash, key) if key is not None else None
