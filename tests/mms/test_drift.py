"""Tests for ``mms.drift`` — canonical form + stable hash.

The hash is the foundation for W2's drift detection. These tests pin its
shape so that future canonical-form changes are loud (the golden hex assert)
and that the documented invariants hold (env-order indifference, args-order
sensitivity, prefix exclusion, empty-vs-absent equivalence).

Cross-host fixture-driven tests below validate that the parse-stage funnel
(``_to_registry_server``) makes the hash host-format-agnostic in practice,
not just in theory — a "github" server defined in claude-code JSON, cursor
JSON, codex TOML, and claude-desktop JSON with identical command/args/env
must hash identically.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from memtomem_stm.mms.drift import (
    HASH_HEX_LEN,
    HASH_PREFIX,
    canonical_form,
    compute_drift_hash,
)
from memtomem_stm.mms.import_hosts import _to_registry_server
from memtomem_stm.mms.state import RegistryServer

# ---------------------------------------------------------------------------
# Hash invariants
# ---------------------------------------------------------------------------


def test_hash_is_stable_across_runs():
    server = RegistryServer(command="npx", args=["-y", "@mcp/fs"], prefix="fs")
    h1 = compute_drift_hash(server)
    h2 = compute_drift_hash(server)
    assert h1 == h2
    # Golden — any change to the canonical form must update this string,
    # which forces a deliberate review of every existing sidecar's hash.
    assert h1 == "sha256:40c96f4ab4762ceb"


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
    assert compute_drift_hash(a) == compute_drift_hash(b)


def test_args_order_does_affect_hash():
    a = RegistryServer(command="x", args=["-y", "@mcp/fs"], prefix="x")
    b = RegistryServer(command="x", args=["@mcp/fs", "-y"], prefix="x")
    assert compute_drift_hash(a) != compute_drift_hash(b)


def test_empty_and_absent_args_env_equivalent():
    """Pydantic defaults make absent fields equivalent to empty collections.

    This is load-bearing: if a host config omits ``args`` and another spells
    it as ``"args": []``, both must produce the same hash post-import.
    """
    a = RegistryServer(command="x", prefix="x")
    b = RegistryServer(command="x", args=[], env={}, prefix="x")
    assert compute_drift_hash(a) == compute_drift_hash(b)


def test_prefix_excluded_from_hash():
    """Prefix is mms-derived metadata; ``_derive_prefix`` algorithm changes
    or user hand-edits to ``prefix`` must not look like host-side drift."""
    a = RegistryServer(command="x", prefix="short")
    b = RegistryServer(command="x", prefix="much_longer_prefix")
    assert compute_drift_hash(a) == compute_drift_hash(b)


def test_command_change_flips_hash():
    a = RegistryServer(command="npx", prefix="x")
    b = RegistryServer(command="uvx", prefix="x")
    assert compute_drift_hash(a) != compute_drift_hash(b)


def test_env_value_change_flips_hash():
    a = RegistryServer(command="x", env={"TOKEN": "v1"}, prefix="x")
    b = RegistryServer(command="x", env={"TOKEN": "v2"}, prefix="x")
    assert compute_drift_hash(a) != compute_drift_hash(b)


def test_env_key_addition_flips_hash():
    a = RegistryServer(command="x", env={"A": "1"}, prefix="x")
    b = RegistryServer(command="x", env={"A": "1", "B": "2"}, prefix="x")
    assert compute_drift_hash(a) != compute_drift_hash(b)


def test_unicode_env_value_stable():
    """Non-ASCII env values must hash identically across runs.

    ``ensure_ascii=False`` guards against silent canonical-form drift if a
    future Python version changes JSON escape defaults.
    """
    server = RegistryServer(command="x", env={"FOO": "命令"}, prefix="x")
    h = compute_drift_hash(server)
    # Re-compute fresh — same input must yield same hash within the run.
    assert compute_drift_hash(server) == h
    # Canonical form contains the literal CJK chars, not \\u escapes.
    assert "命令" in canonical_form(server)
    assert "\\u" not in canonical_form(server)


def test_hash_format_matches_advertised_shape():
    server = RegistryServer(command="x", prefix="x")
    h = compute_drift_hash(server)
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
    return compute_drift_hash(server)


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
