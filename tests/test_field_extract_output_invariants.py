"""Output-contract invariants for FieldExtractCompressor's final tier.

FieldExtract's contract is "JSON: preserve key structure + truncate values;
Text: head + tail lines." Its pre-fix overflow handling was
``result[:max_chars] + "\\n... (truncated)"`` at three sites
(``_compress_json`` for dict + scalar, ``_compress_text`` for short and
many-line text). That BOTH overshot the budget by the 16-char suffix AND, on the
JSON path, cut the JSON mid-token -> INVALID JSON. Separately, a top-level array
appended an ``"\\n... (N items total ...)"`` text marker *after* ``json.dumps``,
so any array of >5 items was unparseable JSON even before any budget pressure.

This file is the regression net. Contract now:

* JSON input (dict / list / scalar root) -> output is ALWAYS valid JSON.
* every path -> ``len(output) <= max(2, max_chars)`` (the ``{}`` / ``[]`` / ``""``
  floor is the one edge where validity wins over a sub-token cap).
* dict: keep as many top-level keys as fit, dropping trailing ones into a valid
  collision-safe ``_truncated`` marker;
* list: preview head items + an in-array marker that counts against the ORIGINAL
  length;
* text: within budget, suffix room reserved.
"""

from __future__ import annotations

import json

import pytest

from memtomem_stm.proxy.compression import (
    CompressionStrategy,
    FieldExtractCompressor,
    auto_select_strategy,
)


# ── Fixtures: each routes through a distinct _compress_json / _compress_text path


def _wide_config_dict(services: int = 40) -> str:
    """Many top-level keys, each a nested dict (nested >= 3 -> EXTRACT_FIELDS via
    auto_select). Even with values truncated, the keys overflow a tight budget,
    so trailing keys must be dropped into a valid marker."""
    return json.dumps(
        {
            f"service_{i}": {
                "host": f"10.0.{i}.{i}",
                "port": 8000 + i,
                "description": "A microservice that handles " + ("x" * 200),
                "tags": [f"tag{j}" for j in range(4)],
            }
            for i in range(services)
        }
    )


def _big_list_of_dicts(rows: int = 30, fields: int = 12) -> str:
    """Top-level array of wide dicts. Pre-fix the trailing text marker made this
    invalid JSON for any rows > 5; under budget pressure items are dropped."""
    return json.dumps([{f"f{j}": f"value_{i}_{j}" * 3 for j in range(fields)} for i in range(rows)])


def _long_scalar_string() -> str:
    """A top-level JSON string scalar far larger than any final-tier budget."""
    return json.dumps("a very long scalar value " * 200)


_JSON_FIXTURES = {
    "wide_config_dict": _wide_config_dict(),
    "big_list_of_dicts": _big_list_of_dicts(),
    "long_scalar_string": _long_scalar_string(),
}

_TEXT_FIXTURES = {
    # <= 10 lines but each line huge -> the short-text branch.
    "few_long_lines": "\n".join("x" * 4000 for _ in range(5)),
    # > 10 lines -> the head/tail summary branch.
    "many_lines": "\n".join(f"log line {i}: " + "y" * 120 for i in range(200)),
}


# ── A. Universal JSON invariants: valid JSON, within budget ───────────────────


@pytest.mark.parametrize("name", sorted(_JSON_FIXTURES))
@pytest.mark.parametrize("budget", [2000, 800, 500, 200, 80, 30, 10, 5, 2])
def test_json_final_tier_is_valid_json(name: str, budget: int) -> None:
    """The core regression: every overflowing JSON payload parses as JSON.
    Pre-fix the mid-token slice (dict/scalar) and the trailing text marker
    (list) both produced ``JSONDecodeError``."""
    out = FieldExtractCompressor().compress(_JSON_FIXTURES[name], max_chars=budget)
    json.loads(out)  # raises if invalid — the bug that shipped


@pytest.mark.parametrize("name", sorted(_JSON_FIXTURES))
@pytest.mark.parametrize("budget", [2000, 800, 500, 200, 80, 30, 10])
def test_json_never_exceeds_budget(name: str, budget: int) -> None:
    """len(output) <= max(2, budget). The pre-fix slice appended a 16-char
    suffix after a budget-filling slice, overshooting every time."""
    out = FieldExtractCompressor().compress(_JSON_FIXTURES[name], max_chars=budget)
    assert len(out) <= max(2, budget), (
        f"{name}@{budget}: produced {len(out)} chars (+{len(out) - budget})"
    )


# ── B. Text path stays within budget ─────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(_TEXT_FIXTURES))
@pytest.mark.parametrize("budget", [3000, 1000, 300, 100, 20, 10])
def test_text_never_exceeds_budget(name: str, budget: int) -> None:
    """Both text branches reserved no room for their ``\\n... (truncated)``
    suffix and overshot by 16 chars. Text has no JSON contract, but the budget
    must hold."""
    out = FieldExtractCompressor().compress(_TEXT_FIXTURES[name], max_chars=budget)
    assert len(out) <= budget, f"{name}@{budget}: +{len(out) - budget}"


# ── C. Dict: drop whole trailing keys into a valid collision-safe marker ──────


def test_wide_dict_keeps_prefix_and_records_omissions() -> None:
    """When the keys overflow, a valid ``_truncated`` member reports an
    internally-consistent omitted count and the kept keys survive intact."""
    out = FieldExtractCompressor().compress(_wide_config_dict(40), max_chars=500)
    parsed = json.loads(out)
    assert "service_0" in parsed
    marker = parsed.get("_truncated", "")
    assert "keys omitted" in marker
    omitted = int(marker.split(" of ")[0])
    # kept real keys + the marker key; omitted counts the rest of the 40.
    kept_real = [k for k in parsed if k != "_truncated"]
    assert omitted + len(kept_real) == 40


def test_marker_key_is_collision_safe() -> None:
    """A real top-level ``_truncated`` key must not be clobbered or repurposed
    as the omitted-count marker."""
    data = {"_truncated": "REAL VALUE THAT MUST SURVIVE OR BE COUNTED"}
    data.update({f"k{i}": "v" * 100 for i in range(60)})
    out = FieldExtractCompressor().compress(json.dumps(data), max_chars=300)
    parsed = json.loads(out)
    json.loads(out)  # valid
    # The synthetic marker key is distinct from the real one.
    synth = [k for k in parsed if k.startswith("_truncated") and k != "_truncated"]
    if "_truncated" in parsed and synth:
        assert "keys omitted" in parsed[synth[0]]


# ── D. List: valid JSON, in-array marker, counts original length ──────────────


def test_list_output_is_valid_json_with_marker() -> None:
    """When a >5-item array is compressed, the head preview + omitted marker is
    valid JSON. Pre-fix the trailing ``"\\n... (N items total ...)"`` text marker
    was appended after ``json.dumps``, so the output was unparseable JSON for any
    array over the 5-item preview."""
    payload = _big_list_of_dicts(30)
    budget = 3000  # < len(payload) so compression runs; > preview so items are kept
    assert len(payload) > budget
    out = FieldExtractCompressor().compress(payload, max_chars=budget)
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert any(isinstance(e, str) and "items omitted" in e for e in parsed)


def test_list_marker_counts_original_length() -> None:
    """The omitted count is against the ORIGINAL length, never the sampled
    preview — the bug class #395's review caught on SchemaPruning. Uses small
    items at a budget that fits several head items plus the marker."""
    payload = json.dumps([{"id": i} for i in range(30)])
    out = FieldExtractCompressor().compress(payload, max_chars=200)
    parsed = json.loads(out)
    marker = next(e for e in parsed if isinstance(e, str) and "items omitted" in e)
    # "... (N of 30 items omitted)" — total is the real length.
    assert "of 30 items omitted" in marker


# ── E. Scalar root ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("budget", [200, 50, 10, 5, 2])
def test_scalar_string_is_valid_and_within_budget(budget: int) -> None:
    out = FieldExtractCompressor().compress(_long_scalar_string(), max_chars=budget)
    json.loads(out)
    assert len(out) <= max(2, budget)


@pytest.mark.parametrize("literal", ["true", "false", "null", "12345", "-98765", "3.14159"])
def test_non_string_scalar_root_verbatim_when_it_fits(literal: str) -> None:
    """A bare scalar root that fits the budget is emitted VERBATIM, never
    rewritten to a different value. (The first cut floored every overflowing
    scalar to "null", turning ``true``/``42`` into ``null`` — value corruption.)"""
    out = FieldExtractCompressor().compress(literal, max_chars=len(literal))
    assert json.loads(out) == json.loads(literal), f"{literal} -> {out!r} (corrupted)"


def test_huge_scalar_root_stays_within_budget() -> None:
    """A scalar literal longer than the budget (e.g. a 1000-digit number) is
    bounded — max_chars is a hard token budget — by degrading to a truncated
    string, never emitted verbatim over budget."""
    out = FieldExtractCompressor().compress("1" * 1000, max_chars=100)
    json.loads(out)
    assert len(out) <= 100


# ── G. Adversarial-review regressions ─────────────────────────────────────────


def test_list_root_does_not_collapse_to_empty() -> None:
    """A list of small objects must keep leading items, not floor to '[]'. (A
    fixed-size marker once inflated a small truncation above the untruncated
    size, breaking the search's monotonicity so it stranded on '[]' though a
    head-bearing array fit the budget.)"""
    payload = json.dumps([{"a": 1, "b": 2} for _ in range(10)])
    out = FieldExtractCompressor().compress(payload, max_chars=60)
    parsed = json.loads(out)
    assert len(out) <= 60
    assert isinstance(parsed, list) and len(parsed) >= 2
    assert parsed[0] == {"a": 1, "b": 2}  # leading item kept in full, not stubbed


def test_marker_does_not_clobber_real_truncated_key_in_dropped_tail() -> None:
    """A real top-level '_truncated' key sitting in the DROPPED tail must not be
    impersonated by the omitted-count marker (the collision guard must consider
    every original key, not just the kept prefix)."""
    data = {"a": "", "b": ""}
    data.update({f"pad{i}": "" for i in range(20)})
    data["_truncated"] = "REAL VALUE"
    out = FieldExtractCompressor().compress(json.dumps(data), max_chars=60)
    parsed = json.loads(out)
    # The synthetic marker is disambiguated (e.g. '_truncated_'); if the literal
    # '_truncated' key is present it still carries the REAL value, never a count.
    if "_truncated" in parsed:
        assert "keys omitted" not in str(parsed["_truncated"])


def test_list_fit_reserves_space_for_omission_marker() -> None:
    """A tight list budget must prefer fewer leading items plus an omitted-count
    marker over silently returning a longer prefix with no marker."""
    payload = json.dumps([{"a": 1} for _ in range(1000)])
    out = FieldExtractCompressor().compress(payload, max_chars=50)
    parsed = json.loads(out)
    marker = next(e for e in parsed if isinstance(e, str) and "items omitted" in e)
    assert len(out) <= 50
    assert parsed[0] == {"a": 1}
    assert "999 of 1000 items omitted" in marker


def test_list_fit_allows_exact_marker_fit() -> None:
    """The marker budget check must use exact JSON length: marker-only output
    can fit exactly when item-plus-marker cannot."""
    payload = json.dumps([{"a": 1} for _ in range(1000)])
    out = FieldExtractCompressor().compress(payload, max_chars=36)
    parsed = json.loads(out)
    assert len(out) == 36
    assert parsed == ["... (1000 of 1000 items omitted)"]


def test_list_fit_preserves_truncated_item_before_marker() -> None:
    """If a smaller first item plus marker fits, keep that leading content
    instead of dropping straight to marker-only output."""
    payload = json.dumps(["x" * 1000 for _ in range(100)])
    out = FieldExtractCompressor().compress(payload, max_chars=100)
    parsed = json.loads(out)
    assert len(out) <= 100
    assert len(parsed) == 2
    assert parsed[0].startswith("x")
    assert parsed[0].endswith("...")
    assert parsed[1] == "... (99 of 100 items omitted)"


def test_list_fit_keeps_small_items_when_marker_is_too_large() -> None:
    """A marker that cannot fit must not prevent later small items from being
    considered when the list itself can still fit."""
    assert FieldExtractCompressor()._take([1, 2], 6) == [1, 2]


def test_take_keeps_full_container_when_it_fits() -> None:
    """A fitting container must never be replaced by a longer omission marker."""
    comp = FieldExtractCompressor()
    cases = [
        ([1, 2], 30),
        ({"a": 1, "b": 2}, 40),
        ([[1, 2]], 32),
        ({"x": {"a": 1, "b": 2}}, 44),
    ]
    for value, budget in cases:
        assert len(json.dumps(value, ensure_ascii=False)) <= budget
        assert comp._take(value, budget) == value


def test_dict_fit_reserves_space_for_omission_marker() -> None:
    """Nested dict fitting must prefer an omitted-count marker over silently
    returning a longer key prefix with no marker."""
    payload = json.dumps(
        {"outer": {f"k{i}": {"a": 1} for i in range(1000)}, "id": "abc"}
    )
    out = FieldExtractCompressor().compress(payload, max_chars=80)
    parsed = json.loads(out)
    outer = parsed["outer"]
    marker = outer.get("_truncated", "")
    retained = len([k for k in outer if k != "_truncated"])
    omitted = int(marker.split(" of ")[0])
    assert len(out) <= 80
    assert parsed["id"] == "abc"
    assert omitted + retained == 1000


def test_dict_fit_allows_exact_marker_fit() -> None:
    """Dict marker checks use exact JSON length, so marker-only output can fit
    exactly when key-plus-marker cannot."""
    data = {f"k{i}": {"a": 1} for i in range(1000)}
    out = FieldExtractCompressor()._take(data, 43)
    assert len(json.dumps(out, ensure_ascii=False)) == 43
    assert out == {"_truncated": "1000 of 1000 keys omitted"}


def test_later_short_scalars_survive_after_long_scalar() -> None:
    """Long early scalar values must not consume the whole enrichment budget
    before later short identifiers get restored from their stubs."""
    payload = json.dumps({"description": "x" * 10_000, "id": "abc", "name": "Alice"})
    out = FieldExtractCompressor().compress(payload, max_chars=100)
    parsed = json.loads(out)
    assert len(out) <= 100
    assert parsed["id"] == "abc"
    assert parsed["name"] == "Alice"
    assert parsed["description"]


def test_later_short_scalars_survive_after_medium_scalar() -> None:
    """Even when an early scalar fits whole by itself, shorter later scalars get
    first claim and the early value is trimmed if necessary."""
    payload = json.dumps({"description": "x" * 55, "id": "abc", "name": "Alice"})
    out = FieldExtractCompressor().compress(payload, max_chars=100)
    parsed = json.loads(out)
    assert len(out) <= 100
    assert parsed["id"] == "abc"
    assert parsed["name"] == "Alice"
    assert parsed["description"].endswith("...")


def test_oversized_non_string_scalar_stub_does_not_hide_later_keys() -> None:
    """Huge numeric literals are bounded at the skeleton layer so other keys
    can survive instead of collapsing the whole dict to a marker."""
    payload = '{"huge": ' + ("1" * 1000) + ', "id": "abc"}'
    out = FieldExtractCompressor().compress(payload, max_chars=80)
    parsed = json.loads(out)
    assert len(out) <= 80
    assert "huge" in parsed
    assert parsed["id"] == "abc"


def test_nested_oversized_non_string_scalar_stub_preserves_later_keys() -> None:
    """The same scalar bound applies inside recursive _take() paths, not just
    top-level skeletons."""
    payload = '{"outer": {"huge": ' + ("1" * 1000) + ', "id": "abc"}, "name": "foo"}'
    out = FieldExtractCompressor().compress(payload, max_chars=80)
    parsed = json.loads(out)
    assert len(out) <= 80
    assert parsed["outer"]["huge"] == ""
    assert parsed["outer"]["id"] == "abc"
    assert parsed["name"] == "foo"


def test_nested_array_oversized_non_string_scalar_stub_preserves_later_keys() -> None:
    """Objects inside arrays also keep short fields when a huge scalar is
    bounded to a stub."""
    payload = '{"items": [{"huge": ' + ("1" * 1000) + ', "id": "abc"}], "name": "foo"}'
    out = FieldExtractCompressor().compress(payload, max_chars=80)
    parsed = json.loads(out)
    assert len(out) <= 80
    assert parsed["items"][0]["huge"] == ""
    assert parsed["items"][0]["id"] == "abc"
    assert parsed["name"] == "foo"


# ── E2. Budget-fill: no collapse cliff, keep leading full-length values ───────


@pytest.mark.parametrize("name", sorted(_JSON_FIXTURES))
@pytest.mark.parametrize("budget", [2000, 1200, 800])
def test_final_tier_fills_the_budget(name: str, budget: int) -> None:
    """The final tier fills the budget rather than collapsing far under it: more
    than half the budget is always used (a naive depth-collapse left 15-50% —
    the cliff documented for the SchemaPruning final tier). Lists fill at item
    granularity (coarser than dicts), hence the > 50% — not > 90% — bar here."""
    payload = _JSON_FIXTURES[name]
    out = FieldExtractCompressor().compress(payload, max_chars=budget)
    assert len(out) <= budget
    assert len(out) > 0.5 * budget, f"{name}@{budget}: only {len(out) / budget:.0%} of budget used"


def test_dict_final_tier_fills_budget_tightly() -> None:
    """A dict's skeleton+enrich fills the budget tightly (the per-key greedy has
    fine granularity), unlike the coarse list-item case."""
    cfg = _wide_config_dict(40)
    for budget in (800, 1200, 2000):
        out = FieldExtractCompressor().compress(cfg, max_chars=budget)
        assert len(out) <= budget
        assert len(out) >= 0.9 * budget, f"@{budget}: only {len(out) / budget:.0%} used"


def test_skeleton_keeps_all_top_level_keys_when_it_fits() -> None:
    """When the all-stub skeleton fits the budget, EVERY top-level key survives
    (FieldExtract's 'preserve key structure' contract); only when the stubs
    themselves overflow are trailing keys dropped."""
    cfg = _wide_config_dict(40)
    out = FieldExtractCompressor().compress(cfg, max_chars=1500)
    parsed = json.loads(out)
    assert "_truncated" not in parsed
    for i in range(40):
        assert f"service_{i}" in parsed, f"top-level key service_{i} dropped at a fitting budget"


def test_leading_short_values_survive_under_pressure() -> None:
    """Short scalar values in the leading content stay full-length under budget
    pressure (uniform string-capping would shred them; the greedy fill keeps
    leading values whole). Mirrors the bench s02 QA contract."""
    payload = json.dumps(
        {
            "page": {"next": "?page=2"},
            "users": [
                {"name": "Alice Park", "role": "admin", "dept": "Engineering"},
                {"name": "Bob Chen", "role": "developer", "dept": "Platform"},
                {"name": "Carla Ruiz", "role": "designer", "dept": "Product"},
            ]
            + [{"name": f"Filler {i}", "role": "viewer", "dept": "x" * 120} for i in range(20)],
        }
    )
    out = FieldExtractCompressor().compress(payload, max_chars=600)
    assert len(out) <= 600
    json.loads(out)
    # The leading user's full name + role survive intact (not truncated to a stub).
    assert "Alice Park" in out
    assert "admin" in out


# ── F. Passthrough / reachability ────────────────────────────────────────────


def test_short_input_passthrough() -> None:
    assert FieldExtractCompressor().compress("short", max_chars=100) == "short"


def test_fits_budget_passthrough_is_unchanged() -> None:
    """When the extracted form already fits, output equals the plain dump (no
    final-tier reshaping)."""
    text = json.dumps({"a": {"b": 1}, "c": {"d": 2}, "e": {"f": 3}})
    out = FieldExtractCompressor().compress(text, max_chars=10_000)
    assert json.loads(out) == json.loads(text)


def test_wide_config_dict_routes_to_extract_fields() -> None:
    """Confirms the fixture is genuinely reachable via auto_select (nested >= 3,
    no array >= 20) — the final tier is not dead code."""
    assert (
        auto_select_strategy(_wide_config_dict(40), max_chars=500)
        == CompressionStrategy.EXTRACT_FIELDS
    )
