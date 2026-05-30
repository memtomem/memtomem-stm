"""Output-contract invariants for SchemaPruningCompressor's final tier.

SchemaPruning's whole contract is "JSON schema-preserving": keep every key and
emit valid JSON. But ``_prune`` keeps all keys and never drops whole array
items, so a payload can still overflow the budget at the minimal-detail tier —
a wide object (many top-level keys), a deeply nested object, or a heavy array.
The pre-fix final tier handled that overflow with
``result[:max_chars] + "\\n... (pruned)"``, which BOTH overshot the budget by
the 13-char suffix AND cut the JSON mid-token, producing INVALID JSON. A 200-key
dict at a routine 500-char budget already triggered it.

This file is the regression net. At the final tier the output must always
(a) parse as JSON and (b) stay within ``max(2, max_chars)`` (the ``{}`` / ``[]``
/ ``""`` floor, the one edge where validity wins over the cap — a budget the
manager retention ladder never produces). The graceful degradation is:

1. keep EVERY top-level key, collapsing the deepest nested levels to compact
   shape stubs (``{N keys}`` / ``[N items]``) so the schema shape survives;
2. only when the top-level keys themselves cannot fit, drop whole trailing
   keys/items into a valid marker.
"""

from __future__ import annotations

import json

import pytest

from memtomem_stm.proxy.compression import SchemaPruningCompressor


def _wide_dict(n: int = 200) -> str:
    """Many short top-level keys: the key *names* alone overflow a tight budget,
    so even stubbed values cannot fit them all -> trailing keys are dropped."""
    return json.dumps({f"key_{i:03d}": f"value_number_{i}" for i in range(n)})


def _deep_single_key(subkeys: int = 300) -> str:
    """One top-level key with a huge value: dropping the single key is the only
    way to fit -> degrades to the marker / ``{}`` floor."""
    return json.dumps({"big": {f"sub_{i}": f"data_{i}" * 3 for i in range(subkeys)}})


def _heavy_list(rows: int = 20, fields: int = 40) -> str:
    """Top-level array of wide dicts: even after first-2 + last-1 sampling the
    elements overflow, so they collapse to stubs / items are dropped."""
    return json.dumps([{f"f{j}": f"val_{i}_{j}" * 2 for j in range(fields)} for i in range(rows)])


def _nested_config(services: int = 4) -> str:
    """Few top-level keys, deep nesting, serialized with indent (like a real
    config dump). Hits the depth-collapse path: top-level keys are kept while
    the deepest levels become stubs in proportion to the budget."""
    return json.dumps(
        {
            f"service_{s}": {
                "host": f"svc{s}.internal.example.com",
                "port": 8000 + s,
                "pool": {
                    "min": 2,
                    "max": 20,
                    "timeout_seconds": 30,
                    "retry": {"count": 3, "backoff": "exp"},
                },
                "endpoints": [f"/api/v1/res{i}" for i in range(6)],
                "flags": {"tls": True, "compression": False, "debug": False},
            }
            for s in range(services)
        },
        indent=2,
    )


_FINAL_TIER_FIXTURES = {
    "wide_dict": _wide_dict(),
    "deep_single_key": _deep_single_key(),
    "heavy_list": _heavy_list(),
    "nested_config": _nested_config(),
}


# ── A. Universal output invariants ───────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(_FINAL_TIER_FIXTURES))
@pytest.mark.parametrize("budget", [500, 200, 80, 30, 10, 5, 2])
def test_final_tier_emits_valid_json(name: str, budget: int) -> None:
    """Invariant B (the core regression): the final tier always emits parseable
    JSON. Pre-fix the mid-token slice raised ``JSONDecodeError`` for every
    overflowing payload."""
    out = SchemaPruningCompressor().compress(_FINAL_TIER_FIXTURES[name], max_chars=budget)
    json.loads(out)  # raises if invalid — the bug that shipped


@pytest.mark.parametrize("name", sorted(_FINAL_TIER_FIXTURES))
@pytest.mark.parametrize("budget", [500, 200, 80, 30, 10])
def test_final_tier_never_exceeds_budget(name: str, budget: int) -> None:
    """Invariant A: len(output) <= max(2, budget). The pre-fix hard slice
    appended a 13-char suffix *after* a budget-filling slice, overshooting."""
    out = SchemaPruningCompressor().compress(_FINAL_TIER_FIXTURES[name], max_chars=budget)
    assert len(out) <= max(2, budget), (
        f"{name}@{budget}: produced {len(out)} chars (+{len(out) - budget})"
    )


# ── B. Depth-collapse keeps every top-level key ──────────────────────────────


@pytest.mark.parametrize("budget", [1500, 900, 600, 400, 250, 150])
def test_nested_config_keeps_all_top_level_keys(budget: int) -> None:
    """A few-key, deeply-nested object keeps ALL top-level keys across the
    final-tier budget range — the deepest levels collapse first, the schema
    shape is never lost to a key drop until the keys themselves overflow."""
    out = SchemaPruningCompressor().compress(_nested_config(4), max_chars=budget)
    parsed = json.loads(out)
    assert len(out) <= budget
    assert "_pruned" not in parsed
    for s in range(4):
        assert f"service_{s}" in parsed, f"top-level key service_{s} dropped at budget {budget}"


def test_nested_config_collapses_deep_levels_to_stubs() -> None:
    """Under budget pressure the deepest nested containers become compact
    ``{N keys}`` / ``[N items]`` stubs, while every top-level key survives and
    the output stays valid JSON."""
    out = SchemaPruningCompressor().compress(_nested_config(4), max_chars=600)
    json.loads(out)
    assert "keys}" in out or "items]" in out, "expected a collapsed nested stub"


# ── C. Wide object: drop whole trailing keys into a marker ───────────────────


def test_wide_dict_records_omitted_keys() -> None:
    """When the top-level key names alone overflow, whole trailing keys are
    dropped into a valid ``_pruned`` member with an internally-consistent
    count, instead of vanishing behind an invalid mid-string cut."""
    out = SchemaPruningCompressor().compress(_wide_dict(200), max_chars=400)
    parsed = json.loads(out)
    assert "_pruned" in parsed
    assert parsed["_pruned"].endswith("of 200 keys omitted")
    omitted = int(parsed["_pruned"].split(" of ")[0])
    retained = len(parsed) - 1  # minus the _pruned marker itself
    assert omitted + retained == 200
    assert omitted > 0 and retained > 0


def test_wide_dict_retained_keys_are_an_order_preserving_prefix() -> None:
    """Kept keys are a prefix of the original order (trailing keys are dropped),
    and the marker is the only synthetic member."""
    out = SchemaPruningCompressor().compress(_wide_dict(200), max_chars=400)
    parsed = json.loads(out)
    kept = [k for k in parsed if k != "_pruned"]
    assert kept == [f"key_{i:03d}" for i in range(len(kept))]


# ── D. Array root ────────────────────────────────────────────────────────────


def test_heavy_list_stays_valid_array_within_budget() -> None:
    """A top-level array degrades to a valid JSON array within budget (elements
    collapse to stubs; whole items drop only when even those overflow)."""
    out = SchemaPruningCompressor().compress(_heavy_list(20, 40), max_chars=300)
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert len(out) <= 300


# ── E. Pathological-budget floor ─────────────────────────────────────────────


@pytest.mark.parametrize("budget", [1, 2, 5])
def test_pathological_budget_floor(budget: int) -> None:
    """Contract floor: valid JSON cannot be shorter than ``{}`` (2 chars), so a
    sub-2-char budget keeps validity over the length cap. This pins the one edge
    where len > max_chars is allowed — a budget config and the manager retention
    ladder never produce. Above the floor the cap holds."""
    out = SchemaPruningCompressor().compress(_wide_dict(200), max_chars=budget)
    json.loads(out)  # always parseable
    assert len(out) <= max(2, budget)


# ── F. Scalar root (defensive) ───────────────────────────────────────────────


@pytest.mark.parametrize("budget", [50, 13, 10, 4, 2])
def test_scalar_root_stays_valid_json(budget: int) -> None:
    """A top-level JSON string overflowing a sub-scalar budget shrinks to a
    shorter valid JSON string rather than an unterminated slice."""
    out = SchemaPruningCompressor().compress(json.dumps("x" * 5000), max_chars=budget)
    assert isinstance(json.loads(out), str)
    assert len(out) <= max(2, budget)


# ── G. No change when the payload fits ───────────────────────────────────────


def test_unchanged_when_input_fits() -> None:
    """When the raw payload already fits, compress returns it verbatim — no
    spurious marker and no pruning."""
    text = json.dumps({"a": "short", "b": "also short", "c": 42})
    out = SchemaPruningCompressor().compress(text, max_chars=10_000)
    assert out == text
    assert "_pruned" not in out


# ── H. Non-string scalar & empty-container floor (sub-token budgets) ──────────


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps(3.141592653589793),
        json.dumps(1e300),
        json.dumps(-12345678901234567890),
        "true",
        "false",
        "null",
        "{}",
        "[]",
    ],
)
@pytest.mark.parametrize("budget", [0, 1, 2, 3])
def test_non_string_scalar_and_empty_roots_stay_valid(payload: str, budget: int) -> None:
    """A non-string scalar or empty container root must stay VALID JSON even at a
    sub-token budget — never a mid-token slice like ``'3.'`` / ``'tru'`` / ``'{'``.
    The shortest valid token wins over the cap (``{}``/``[]`` -> 2 chars keep their
    type; any other scalar -> ``null``, 4 chars), so the bound is ``max(4, budget)``."""
    out = SchemaPruningCompressor().compress(payload, max_chars=budget)
    json.loads(out)  # the core contract: always parseable
    assert len(out) <= max(4, budget)
    # Empty containers keep their JSON type rather than degrading to null.
    if payload in ("{}", "[]"):
        assert out == payload


# ── I. Marker fidelity: collision-safe key, accurate counts ──────────────────


def test_dict_marker_does_not_clobber_existing_pruned_key() -> None:
    """A payload that legitimately has a top-level ``_pruned`` key keeps its real
    value (the marker moves to a collision-free key) and the omitted count stays
    internally consistent."""
    data = {"_pruned": "REAL_VALUE", **{f"field_{i}": "x" * 40 for i in range(60)}}
    out = SchemaPruningCompressor().compress(json.dumps(data), max_chars=400)
    parsed = json.loads(out)
    assert parsed["_pruned"] == "REAL_VALUE"  # user value preserved, not clobbered
    marker_key = next(k for k in parsed if k != "_pruned" and "omitted" in str(parsed[k]))
    omitted = int(parsed[marker_key].split(" of ")[0])
    total = int(parsed[marker_key].split(" of ")[1].split()[0])
    retained = len([k for k in parsed if k != marker_key])
    assert total == 61  # 60 fields + the real _pruned key
    assert omitted + retained == total  # count reconciles


@pytest.mark.parametrize("n", [10, 100, 1000])
def test_list_omitted_count_matches_original_length(n: int) -> None:
    """The dropped-items marker counts against the ORIGINAL list length, not the
    already-sampled (~4-element) intermediate form — pre-fix a 1000-item list
    claimed only '3 items omitted'."""
    out = SchemaPruningCompressor().compress(
        json.dumps([{"a": i, "b": "x" * 20} for i in range(n)]), max_chars=50
    )
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    marker = next(el for el in parsed if isinstance(el, str) and "omitted" in el)
    assert f"of {n} items omitted" in marker  # true total, not the sampled count


# ── J. Depth ladder: every rung reachable ────────────────────────────────────


def test_depth_ladder_full_and_intermediate_tiers() -> None:
    """Pin the two depth rungs the budget-range tests never land on: the
    full-depth tier (deepest key survives) and the first intermediate collapse
    (deepest key gone, the level above it retained)."""
    comp = SchemaPruningCompressor()
    # Full-depth tier wins in [1746, 1774): the deepest key 'backoff' survives.
    full = comp.compress(_nested_config(4), max_chars=1750)
    json.loads(full)
    assert len(full) <= 1750
    assert "backoff" in full and all(f"service_{s}" in full for s in range(4))
    # First intermediate collapse wins in [1570, 1746): 'backoff' (deepest) is
    # gone but 'retry' (the level above) and all top-level keys remain.
    mid = comp.compress(_nested_config(4), max_chars=1600)
    json.loads(mid)
    assert len(mid) <= 1600
    assert "retry" in mid and "backoff" not in mid
    assert all(f"service_{s}" in mid for s in range(4))


# ── K. Direct unit coverage of the fitter ────────────────────────────────────


class TestFitMinimal:
    def _comp(self) -> SchemaPruningCompressor:
        return SchemaPruningCompressor()

    def test_passthrough_when_minimal_fits(self) -> None:
        out = self._comp()._fit_minimal({"a": 1, "b": 2}, 1000)
        assert json.loads(out) == {"a": 1, "b": 2}

    def test_keeps_all_top_level_keys_via_depth_collapse(self) -> None:
        data = {"alpha": {"deep": {"x": "y" * 50}}, "beta": {"deep": {"x": "z" * 50}}}
        out = self._comp()._fit_minimal(data, 80)
        parsed = json.loads(out)
        assert len(out) <= 80
        assert set(parsed) == {"alpha", "beta"}  # both top-level keys survive
        assert "keys}" in out  # the deep subtree collapsed to a stub

    def test_wide_dict_drops_trailing_keys_into_marker(self) -> None:
        pruned = {f"k{i}": "v" for i in range(50)}
        out = self._comp()._fit_minimal(pruned, 120)
        parsed = json.loads(out)
        assert len(out) <= 120
        assert "of 50 keys omitted" in parsed["_pruned"]

    def test_dict_floor_is_empty_object(self) -> None:
        out = self._comp()._fit_minimal({f"k{i}": "v" for i in range(50)}, 5)
        assert out == "{}"

    def test_list_drops_trailing_items(self) -> None:
        out = self._comp()._fit_minimal([{"x": "y" * 30} for _ in range(30)], 100)
        parsed = json.loads(out)
        assert isinstance(parsed, list)
        assert len(out) <= 100

    def test_list_floor_is_empty_array(self) -> None:
        out = self._comp()._fit_minimal([{"x": "y" * 50} for _ in range(30)], 5)
        assert out == "[]"

    def test_string_scalar_shrinks_to_fit(self) -> None:
        # Budget below the max_str=10 capped form ("zzzzzzzzzz..." -> 15 JSON
        # chars) forces the scalar shrink branch past the depth tiers.
        out = self._comp()._fit_minimal("z" * 500, 8)
        parsed = json.loads(out)
        assert isinstance(parsed, str) and parsed.startswith("z")
        assert len(out) <= 8

    def test_string_floor_below_two_chars(self) -> None:
        # Sub-2 budget: the shrink loop cannot fit even '""' (2 chars), so the
        # explicit string floor returns a valid empty JSON string.
        assert self._comp()._fit_minimal("hello", 1) == '""'
        assert self._comp()._fit_minimal("hello", 0) == '""'

    def test_non_string_scalar_floor_is_null(self) -> None:
        # No shorter same-type valid form exists -> a valid `null`, never a slice.
        assert self._comp()._fit_minimal(3.14159, 2) == "null"
        assert self._comp()._fit_minimal(True, 1) == "null"
        assert self._comp()._fit_minimal(123456789012345, 3) == "null"

    def test_empty_container_passthrough(self) -> None:
        assert self._comp()._fit_minimal({}, 100) == "{}"
        assert self._comp()._fit_minimal([], 100) == "[]"

    def test_empty_container_floor_below_two(self) -> None:
        # Even below the 2-char floor an empty container keeps its type.
        assert self._comp()._fit_minimal({}, 1) == "{}"
        assert self._comp()._fit_minimal([], 0) == "[]"

    def test_collision_safe_marker_preserves_real_pruned_key(self) -> None:
        data = {"_pruned": "keep me", **{f"k{i}": "v" * 20 for i in range(40)}}
        out = self._comp()._fit_minimal(data, 150)
        parsed = json.loads(out)
        assert parsed["_pruned"] == "keep me"
        assert any(k.startswith("_pruned_") and "omitted" in parsed[k] for k in parsed)
