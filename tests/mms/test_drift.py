"""Tests for ``mms.drift`` — canonical form + stable hash.

The hash is the foundation for W2's drift detection. These tests pin its
shape so that future canonical-form changes are loud (the golden hex assert)
and that the documented invariants hold (env-order indifference, args-order
sensitivity, prefix exclusion, empty-vs-absent equivalence).

Cross-host fixture-driven tests are added in a follow-up commit — they
validate that the parse-stage funnel (``_to_registry_server``) makes the
hash host-format-agnostic in practice, not just in theory.
"""

from __future__ import annotations

import json
import re

from memtomem_stm.mms.drift import (
    HASH_HEX_LEN,
    HASH_PREFIX,
    canonical_form,
    compute_drift_hash,
)
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
