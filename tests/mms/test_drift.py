"""Tests for ``mms.drift`` — canonical form + stable, keyed hash.

The hash is the foundation for W2's drift detection. These tests pin its
shape so that future canonical-form changes are loud (the golden hex assert)
and that the documented invariants hold (env-order indifference, args-order
sensitivity, prefix exclusion, empty-vs-absent equivalence). They also pin
the keying: the printed hash cannot be recomputed without the key, and a
stored version 1 row is re-keyed without changing what it says.

Cross-host fixture-driven tests below validate that the parse-stage funnel
(``_to_registry_server``) makes the hash host-format-agnostic in practice,
not just in theory — a "github" server defined in claude-code JSON, cursor
JSON, codex TOML, and claude-desktop JSON with identical command/args/env
must hash identically.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from memtomem_stm.mms import drift
from memtomem_stm.mms.drift import (
    HASH_HEX_LEN,
    HASH_PREFIX,
    HASH_VERSION,
    canonical_form,
    compute_drift_hash,
    drift_key_id,
    keyed_from_legacy,
    legacy_v1_hash,
)
from memtomem_stm.mms.import_hosts import _to_registry_server
from memtomem_stm.mms.state import ImportStateEntry, RegistryServer

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))

# ---------------------------------------------------------------------------
# Hash invariants
# ---------------------------------------------------------------------------


def test_hash_is_stable_across_runs():
    server = RegistryServer(command="npx", args=["-y", "@mcp/fs"], prefix="fs")
    h1 = compute_drift_hash(server, KEY)
    h2 = compute_drift_hash(server, KEY)
    assert h1 == h2


def test_legacy_hash_golden():
    """Golden — any change to the canonical form must update this string,
    which forces a deliberate review of every existing sidecar's hash: the
    version 2 hash is derived from this value, and version 1 rows written by
    earlier releases are compared against it."""
    server = RegistryServer(command="npx", args=["-y", "@mcp/fs"], prefix="fs")
    assert legacy_v1_hash(server) == "sha256:40c96f4ab4762ceb"


def _expected_v2(legacy: str, key: bytes) -> str:
    """The version 2 derivation, written out with ``hmac`` alone."""
    subkey = hmac.new(key, b"mms-drift-hash-v2", hashlib.sha256).digest()
    data = legacy.encode("utf-8")
    mac = hmac.new(subkey, f"{len(data)}:".encode() + data, hashlib.sha256)
    return "hmac-sha256:" + mac.hexdigest()[:16]


def test_keyed_hash_is_the_documented_derivation():
    server = RegistryServer(command="npx", args=["-y", "@mcp/fs"], prefix="fs")
    expected = _expected_v2("sha256:40c96f4ab4762ceb", KEY)
    assert compute_drift_hash(server, KEY) == expected
    assert keyed_from_legacy(legacy_v1_hash(server), KEY) == expected


def test_hash_depends_on_the_key():
    server = RegistryServer(command="x", env={"TOKEN": "v1"}, prefix="x")
    assert compute_drift_hash(server, KEY) != compute_drift_hash(server, OTHER_KEY)


def test_keyed_hash_is_not_the_legacy_digest():
    """The printed value carries none of the unkeyed digest's hex."""
    server = RegistryServer(command="x", env={"TOKEN": "v1"}, prefix="x")
    legacy_hex = legacy_v1_hash(server).split(":", 1)[1]
    assert legacy_hex not in compute_drift_hash(server, KEY)


def test_key_id_identifies_the_key_without_being_a_hash_of_a_server():
    assert drift_key_id(KEY) == drift_key_id(KEY)
    assert drift_key_id(KEY) != drift_key_id(OTHER_KEY)
    assert re.fullmatch(r"[0-9a-f]{16}", drift_key_id(KEY))
    subkey = hmac.new(KEY, b"mms-drift-hash-v2", hashlib.sha256).digest()
    assert drift_key_id(KEY) == hmac.new(subkey, b"6:key-id", hashlib.sha256).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Sidecar rows: stamp / promote / comparable / matches / reportable
# ---------------------------------------------------------------------------

SERVER = RegistryServer(command="npx", env={"TOKEN": "s3cret"}, prefix="x")
EDITED = RegistryServer(command="npx", env={"TOKEN": "rotated"}, prefix="x")


def _v1_row(server: RegistryServer = SERVER) -> ImportStateEntry:
    return ImportStateEntry(
        drift_hash=legacy_v1_hash(server),
        drift_hash_version=1,
        last_imported="2026-05-02T00:00:00Z",
        source_label="Claude Code (user)",
    )


def _stamp(server: RegistryServer = SERVER, key: bytes = KEY) -> ImportStateEntry:
    return drift.stamp(
        server, key, last_imported="2026-11-14T00:00:00Z", source_label="Claude Code (user)"
    )


def test_stamp_writes_a_current_version_row_under_the_key():
    row = _stamp()
    assert row.drift_hash == compute_drift_hash(SERVER, KEY)
    assert row.drift_hash_version == HASH_VERSION == 2
    assert row.drift_key_id == drift_key_id(KEY)


def test_promote_rekeys_a_v1_row_to_what_a_fresh_stamp_would_write():
    promoted = drift.promote(_v1_row(), KEY)
    fresh = _stamp()
    assert promoted.drift_hash == fresh.drift_hash
    assert promoted.drift_hash_version == fresh.drift_hash_version
    assert promoted.drift_key_id == fresh.drift_key_id
    # The row's history is not rewritten.
    assert promoted.last_imported == "2026-05-02T00:00:00Z"
    assert promoted.source_label == "Claude Code (user)"


def test_promote_leaves_other_rows_alone():
    row = _stamp(key=OTHER_KEY)
    assert drift.promote(row, KEY) is row
    future = ImportStateEntry(
        drift_hash="hmac-sha256:" + "0" * 16,
        drift_hash_version=3,
        last_imported="t",
        source_label="s",
    )
    assert drift.promote(future, KEY) is future


@pytest.mark.parametrize("key", [None, KEY], ids=["no-key", "key"])
def test_a_v1_row_compares_the_same_with_or_without_a_key(key):
    row = _v1_row()
    assert drift.comparable(row, key)
    assert drift.matches(row, SERVER, key)
    assert not drift.matches(row, EDITED, key)


def test_promotion_keeps_what_the_row_says():
    """Unchanged stays unchanged and changed stays changed across the re-key."""
    promoted = drift.promote(_v1_row(), KEY)
    assert drift.matches(promoted, SERVER, KEY)
    assert not drift.matches(promoted, EDITED, KEY)


def test_a_v2_row_is_comparable_only_under_its_own_key():
    row = _stamp()
    assert drift.comparable(row, KEY)
    assert drift.matches(row, SERVER, KEY)
    assert not drift.matches(row, EDITED, KEY)
    for other in (None, OTHER_KEY):
        assert not drift.comparable(row, other)
        assert not drift.matches(row, SERVER, other)


def test_a_row_of_an_unknown_version_is_not_comparable():
    row = ImportStateEntry(
        drift_hash=compute_drift_hash(SERVER, KEY),
        drift_hash_version=3,
        drift_key_id=drift_key_id(KEY),
        last_imported="t",
        source_label="s",
    )
    assert not drift.comparable(row, KEY)
    assert not drift.matches(row, SERVER, KEY)


def test_reportable_baseline_never_returns_an_unkeyed_digest():
    v1 = _v1_row()
    assert drift.reportable_baseline(v1, None) is None
    assert drift.reportable_baseline(v1, KEY) == compute_drift_hash(SERVER, KEY)
    # Version 1 is the only row that can hold an unkeyed value: the model
    # refuses ``sha256:`` under any other version, so there is no such row to
    # report.
    with pytest.raises(ValueError):
        ImportStateEntry(
            drift_hash=v1.drift_hash, drift_hash_version=3, last_imported="t", source_label="s"
        )
    # Keyed values pass through, also under a key that cannot verify them.
    v2 = _stamp()
    for key in (None, KEY, OTHER_KEY):
        assert drift.reportable_baseline(v2, key) == v2.drift_hash


def test_canonical_form_shape():
    server = RegistryServer(
        command="npx", args=["-y", "@mcp/fs"], env={"B": "2", "A": "1"}, prefix="fs"
    )
    canon = canonical_form(server)
    # Round-trip through json to assert structure rather than byte-equality
    parsed = json.loads(canon)
    assert parsed == {"args": ["-y", "@mcp/fs"], "command": "npx", "env": {"A": "1", "B": "2"}}
    # Top-level keys are sorted (sort_keys=True): args < command < env
    assert canon.index('"args"') < canon.index('"command"') < canon.index('"env"')


def test_env_order_does_not_affect_hash():
    a = RegistryServer(command="x", env={"A": "1", "B": "2"}, prefix="x")
    b = RegistryServer(command="x", env={"B": "2", "A": "1"}, prefix="x")
    assert compute_drift_hash(a, KEY) == compute_drift_hash(b, KEY)


def test_args_order_does_affect_hash():
    a = RegistryServer(command="x", args=["-y", "@mcp/fs"], prefix="x")
    b = RegistryServer(command="x", args=["@mcp/fs", "-y"], prefix="x")
    assert compute_drift_hash(a, KEY) != compute_drift_hash(b, KEY)


def test_empty_and_absent_args_env_equivalent():
    """Pydantic defaults make absent fields equivalent to empty collections.

    This is load-bearing: if a host config omits ``args`` and another spells
    it as ``"args": []``, both must produce the same hash post-import.
    """
    a = RegistryServer(command="x", prefix="x")
    b = RegistryServer(command="x", args=[], env={}, prefix="x")
    assert compute_drift_hash(a, KEY) == compute_drift_hash(b, KEY)


def test_prefix_excluded_from_hash():
    """Prefix is mms-derived metadata; ``_derive_prefix`` algorithm changes
    or user hand-edits to ``prefix`` must not look like host-side drift."""
    a = RegistryServer(command="x", prefix="short")
    b = RegistryServer(command="x", prefix="much_longer_prefix")
    assert compute_drift_hash(a, KEY) == compute_drift_hash(b, KEY)


def test_command_change_flips_hash():
    a = RegistryServer(command="npx", prefix="x")
    b = RegistryServer(command="uvx", prefix="x")
    assert compute_drift_hash(a, KEY) != compute_drift_hash(b, KEY)


def test_env_value_change_flips_hash():
    a = RegistryServer(command="x", env={"TOKEN": "v1"}, prefix="x")
    b = RegistryServer(command="x", env={"TOKEN": "v2"}, prefix="x")
    assert compute_drift_hash(a, KEY) != compute_drift_hash(b, KEY)


def test_env_key_addition_flips_hash():
    a = RegistryServer(command="x", env={"A": "1"}, prefix="x")
    b = RegistryServer(command="x", env={"A": "1", "B": "2"}, prefix="x")
    assert compute_drift_hash(a, KEY) != compute_drift_hash(b, KEY)


def test_unicode_env_value_stable():
    """Non-ASCII env values must hash identically across runs.

    ``ensure_ascii=False`` guards against silent canonical-form drift if a
    future Python version changes JSON escape defaults.
    """
    server = RegistryServer(command="x", env={"FOO": "命令"}, prefix="x")
    h = compute_drift_hash(server, KEY)
    # Re-compute fresh — same input must yield same hash within the run.
    assert compute_drift_hash(server, KEY) == h
    # Canonical form contains the literal CJK chars, not \\u escapes.
    assert "命令" in canonical_form(server)
    assert "\\u" not in canonical_form(server)


def test_hash_format_matches_advertised_shape():
    server = RegistryServer(command="x", prefix="x")
    h = compute_drift_hash(server, KEY)
    assert re.fullmatch(rf"{HASH_PREFIX}:[0-9a-f]{{{HASH_HEX_LEN}}}", h)


# ---------------------------------------------------------------------------
# Cross-host fixtures — same logical server in different host formats
# must produce the same drift hash after going through ``_to_registry_server``
# ---------------------------------------------------------------------------


FIXTURES = Path(__file__).parent / "fixtures" / "host_configs"


def _load_json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _load_toml(name: str) -> dict[str, Any]:
    with (FIXTURES / name).open("rb") as f:
        return tomllib.load(f)


def _hash_from_host_entry(name: str, raw: dict[str, Any]) -> str:
    server = _to_registry_server(name, raw)
    assert server is not None, f"_to_registry_server rejected {name}: {raw}"
    return compute_drift_hash(server, KEY)


def test_cross_host_same_logical_server_same_hash():
    """The ``github`` MCP defined in claude-code JSON, cursor JSON, codex
    TOML, and claude-desktop JSON — same command/args/env — hashes
    identically after the parse-stage funnel. This is the load-bearing
    assertion that ``drift.py`` is host-format-agnostic."""
    cc = _load_json("claude_code_user.json")["mcpServers"]["github"]
    cu = _load_json("cursor_user.json")["mcpServers"]["github"]
    co = _load_toml("codex_global.toml")["mcp_servers"]["github"]
    cd = _load_json("claude_desktop_macos.json")["mcpServers"]["github"]

    h_cc = _hash_from_host_entry("github", cc)
    h_cu = _hash_from_host_entry("github", cu)
    h_co = _hash_from_host_entry("github", co)
    h_cd = _hash_from_host_entry("github", cd)

    assert h_cc == h_cu == h_co == h_cd


def test_perturbations_hash_identically_to_base():
    """Same logical content with cosmetic JSON differences (4-space indent,
    env-key insertion order, trailing newline) parses to the same
    ``RegistryServer`` and so hashes identically. Cosmetic quirks die at
    parse time — no normalization in ``drift.py`` is needed for them.
    """
    base = _load_json("claude_code_user.json")["mcpServers"]["github"]
    p_indent = _load_json("perturbations/claude_code_user_4space.json")["mcpServers"]["github"]
    p_keys = _load_json("perturbations/claude_code_user_keyreorder.json")["mcpServers"]["github"]
    p_newline = _load_json("perturbations/cursor_user_trailing_newline.json")["mcpServers"][
        "github"
    ]

    h_base = _hash_from_host_entry("github", base)
    assert _hash_from_host_entry("github", p_indent) == h_base
    assert _hash_from_host_entry("github", p_keys) == h_base
    assert _hash_from_host_entry("github", p_newline) == h_base


def test_semantic_edit_changes_hash():
    """Smoke test for 'drift detection actually detects drift' — every
    flavor of host-side semantic edit (added arg, mutated env value,
    removed env key, swapped command) flips the hash."""
    base = _load_json("claude_code_user.json")["mcpServers"]["github"]
    h_base = _hash_from_host_entry("github", base)

    edited_args = {**base, "args": [*base["args"], "--verbose"]}
    assert _hash_from_host_entry("github", edited_args) != h_base

    edited_env = {**base, "env": {**base["env"], "GITHUB_TOKEN": "ghp_rotated"}}
    assert _hash_from_host_entry("github", edited_env) != h_base

    stripped = {**base, "env": {}}
    assert _hash_from_host_entry("github", stripped) != h_base

    swapped = {**base, "command": "uvx"}
    assert _hash_from_host_entry("github", swapped) != h_base


@pytest.mark.parametrize(
    "host_file",
    [
        "claude_code_user.json",
        "claude_code_project.json",
        "cursor_user.json",
        "cursor_project_scoped.json",
        "claude_desktop_macos.json",
    ],
)
def test_every_json_fixture_round_trips(host_file: str):
    """Every committed JSON fixture must parse and produce a well-formed
    hash for each MCP entry. Guards against a fixture being silently
    dropped by ``_to_registry_server`` (e.g. typo eliding ``command``)."""
    raw = _load_json(host_file)["mcpServers"]
    assert isinstance(raw, dict) and raw, f"{host_file} has no MCP entries"
    for name, entry in raw.items():
        h = _hash_from_host_entry(name, entry)
        assert re.fullmatch(rf"{HASH_PREFIX}:[0-9a-f]{{{HASH_HEX_LEN}}}", h)


def test_codex_toml_fixture_round_trips():
    raw = _load_toml("codex_global.toml")["mcp_servers"]
    assert isinstance(raw, dict) and raw, "codex_global.toml has no MCP entries"
    for name, entry in raw.items():
        h = _hash_from_host_entry(name, entry)
        assert re.fullmatch(rf"{HASH_PREFIX}:[0-9a-f]{{{HASH_HEX_LEN}}}", h)
