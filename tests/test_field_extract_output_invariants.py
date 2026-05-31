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
    """A partially-shown nested dict keeps retained keys + an omitted-count marker
    (never a silent prefix). The budget must leave room to retain >= 1 key: when
    NONE fit, the monotone fill instead collapses the whole nested dict to the
    compact ``{N keys}`` stub (a smaller, still-explicit omission indicator that —
    unlike a marker-only dict — never regresses against the stub as the budget
    grows; see test_dict_fully_omitted_nested_collapses_to_stub)."""
    payload = json.dumps({"outer": {f"k{i}": {"a": 1} for i in range(1000)}, "id": "abc"})
    out = FieldExtractCompressor().compress(payload, max_chars=120)
    parsed = json.loads(out)
    outer = parsed["outer"]
    marker = outer.get("_truncated", "")
    retained = len([k for k in outer if k != "_truncated"])
    omitted = int(marker.split(" of ")[0])
    assert len(out) <= 120
    assert parsed["id"] == "abc"
    assert retained >= 1  # at this budget at least the leading key survives
    assert omitted + retained == 1000


def test_dict_fully_omitted_nested_collapses_to_stub() -> None:
    """When a nested dict is too big to retain even ONE key, it collapses to the
    compact ``{N keys}`` stub rather than a marker-only ``{"_truncated": ...}``
    dict. The stub is smaller AND carries a content leaf, so the fill never
    regresses from the stub to a larger-but-emptier marker as the budget grows
    (the monotonicity contract). The sibling ``id`` still survives."""
    payload = json.dumps({"outer": {f"k{i}": {"a": 1} for i in range(1000)}, "id": "abc"})
    out = FieldExtractCompressor().compress(payload, max_chars=80)
    parsed = json.loads(out)
    assert len(out) <= 80
    assert parsed["id"] == "abc"
    assert parsed["outer"] == "{1000 keys}"  # compact omission indicator, not a marker dict


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


# ── E3. Monotonicity: a larger budget never shows less content (the 4B fix) ───


def _preserved_leaves(obj: object, original: object) -> int:
    """Count preserved ORIGINAL scalar leaves — the true monotonicity metric, scored
    purely by PROVENANCE against ``original`` (NO string-pattern filters, which can't
    tell a real ``"[2 items]"`` / ``"... omitted"`` value or a real ``_truncated_x``
    key from a generated marker or stub).

    An output scalar counts 1 iff it derives from an original scalar — an exact
    original value, or a ``"…"``-truncated prefix of one. Generated placeholders
    therefore count 0 automatically (they are absent from ``original``): omitted-count
    markers, the empty-string / shape stubs from ``_stub_value``, and empty
    containers. No key/value is skipped by name, so a real key like ``_truncated_x``
    or a real value containing ``omitted`` is still scored on its own provenance."""
    strings: set[str] = set()

    def collect(o: object) -> None:
        if isinstance(o, dict):
            for v in o.values():
                collect(v)
        elif isinstance(o, list):
            for e in o:
                collect(e)
        elif isinstance(o, str):
            strings.add(o)

    collect(original)

    def derives(s: str) -> bool:
        if s in strings:
            return True
        if not s.endswith("..."):
            return False
        prefix = s[:-3]  # a bare "..." has an empty prefix → preserves no source char
        return bool(prefix) and any(o.startswith(prefix) for o in strings)

    def count(o: object) -> int:
        if isinstance(o, dict):
            return sum(count(v) for v in o.values())
        if isinstance(o, list):
            return sum(count(e) for e in o)
        if isinstance(o, str):
            return 1 if derives(o) else 0
        return 1  # numbers / bools / None are emitted verbatim, never fabricated

    return count(obj)


def test_bare_ellipsis_does_not_count_as_preserved_content() -> None:
    """A content-free ``"..."`` boundary fill preserves ZERO original characters, so
    neither the provenance metric nor the runtime guard may treat it as preserved.

    Regression for the empty-prefix bug: ``"..."[:-3] == ""`` and every string
    ``.startswith("")``, so a bare ``"..."`` was falsely read as a truncated prefix of
    any source string. An ORIGINAL value that is literally ``"..."`` is still matched
    by equality and scores 1.
    """
    # Metric: a generated bare "..." against a real source scalar -> 0.
    assert _preserved_leaves({"payload": "..."}, {"payload": "secret"}) == 0
    # Metric: a genuine "..."-truncated prefix of the source -> 1.
    assert _preserved_leaves({"payload": "sec..."}, {"payload": "secret"}) == 1
    # Metric: an original value that is literally "..." -> matched by equality -> 1.
    assert _preserved_leaves({"payload": "..."}, {"payload": "..."}) == 1

    comp = FieldExtractCompressor()
    # Runtime guard mirrors the metric on the same cases.
    assert comp._fill_preserves_source({"payload": "..."}, {"payload": "secret"}) is False
    assert comp._fill_preserves_source({"payload": "sec..."}, {"payload": "secret"}) is True
    assert comp._fill_preserves_source({"payload": "..."}, {"payload": "..."}) is True

    # End-to-end (codex's repro): at a tight budget the bulky payload would truncate to
    # a content-free "..."; the fixed guard rejects that shell in favor of the cheaper
    # empty stub, so the surviving "id" is the ONLY preserved leaf — never the "...".
    source = {"id": "abc", "payload": "x" * 100}
    out = json.loads(comp._fit_extracted(source, 31))
    assert out["id"] == "abc"
    assert out["payload"] == ""  # content-free fill dropped to the empty stub
    assert _preserved_leaves(out, source) == 1  # only "id":"abc", NOT the "..."


_LIST_MONO_FIXTURES: dict[str, object] = {
    # The canonical regression: a bulky nested field on the FIRST element. The
    # old `_take`-based enrichment flipped element 0 into a longer-but-emptier
    # marker as the budget grew (content 4 @ 62 -> 2 @ 63).
    "bulky_first": [{"a": 1, "b": {"c": "deep", "d": [10, 20]}}, {"a": 2}, {"a": 3}],
    "wide_records": [{"id": i, "v": "x" * (i % 7)} for i in range(40)],
    "nested_lists": [[1, 2, 3], [4, 5], [6]],
    "strings": ["x" * 30, "y" * 20, "z" * 10, "short"],
    "mixed": [{"x": "hello world", "y": [1, 2, 3]}, {"x": "hi"}, {"z": 9}],
    "big_dicts": _big_list_of_dicts(8, 6),
}


@pytest.mark.parametrize("name", sorted(_LIST_MONO_FIXTURES))
def test_list_root_content_is_monotone_in_budget(name: str) -> None:
    """For a list root, a LARGER budget must never preserve FEWER original leaves
    in the final tier.

    ``_take``'s content is non-monotone in its budget (a larger child budget can
    flip a value into a longer-but-emptier ``_truncated`` marker), so any search
    that maximizes the per-child budget regresses. ``_fit_monotone`` keeps the
    prefix shape but makes every degree of freedom monotone. Budgets are swept
    CONTIGUOUSLY so a one-char cliff cannot hide between sampled points; output
    is asserted valid + within budget at every step too.

    Exercises ``_fit_extracted`` (the final tier the 4B fix rewrites) directly,
    the same surface the design probe swept. The ``_compress_json`` *router* in
    front of it had a SEPARATE non-monotonicity — it preferred a fixed 5-item
    ``indent=2`` head preview over the budget-filling compact final tier — now
    fixed and covered by the E4 router tests below.
    """
    fixture = _LIST_MONO_FIXTURES[name]
    data = json.loads(fixture) if isinstance(fixture, str) else fixture
    compressor = FieldExtractCompressor()
    prev: int | None = None
    for budget in range(2, len(json.dumps(data)) + 5):
        out = compressor._fit_extracted(data, budget)
        parsed = json.loads(out)  # always valid JSON
        assert len(out) <= max(2, budget), f"{name}@{budget}: {len(out)} over budget"
        leaves = _preserved_leaves(parsed, data)
        if prev is not None:
            assert leaves >= prev, f"{name}@{budget}: content regressed {prev} -> {leaves}"
        prev = leaves


def test_small_list_keeps_markerless_prefix_below_marker_width() -> None:
    """Below the omission-marker's width, a SMALL list keeps a markerless leading
    prefix instead of collapsing to ``[]``. The marker tier is unreachable for such
    a value (its full form is cheaper than the marker), so dropping the marker
    stays monotone. ``_fit_extracted([1, 2, 3], 8)`` regressed to ``[]`` mid-review.
    """
    c = FieldExtractCompressor()
    assert c._fit_extracted([1, 2, 3], 8) == "[1, 2]"
    assert c._fit_extracted([1, 2, 3], 5) == "[1]"
    assert json.loads(c._fit_extracted([1, 2, 3], 2)) == []  # the true floor


def test_huge_boundary_item_does_not_scan_quadratically() -> None:
    """A huge leading item under a tight budget must bound the boundary cap by the
    budget, not the item's full size — otherwise the scan is quadratic and hangs.
    The fixed path is sub-millisecond; the generous bound only catches a regression
    back to O(item_size**2)."""
    import time

    c = FieldExtractCompressor()
    start = time.perf_counter()
    out = c._fit_extracted(["x" * 100_000, "y"], 100)
    elapsed = time.perf_counter() - start
    assert len(out) <= 100
    json.loads(out)  # valid JSON
    assert elapsed < 1.0, f"boundary scan took {elapsed:.2f}s (quadratic regression?)"


def test_list_string_element_budgeted_by_serialized_length() -> None:
    """A list string element that needs JSON escaping must be budgeted by its
    SERIALIZED length, not raw char count. Otherwise _fit_monotone returns an
    over-budget candidate, the frame rejects it, and fitting content (a truncated
    string, then the cheap tail item) is wrongly dropped to a marker. (`['"'*20,
    'ok']` at max_chars=45 emitted only the omission marker mid-review.)"""
    c = FieldExtractCompressor()
    out45 = c._fit_extracted(['"' * 20, "ok"], 45)
    parsed45 = json.loads(out45)
    assert len(out45) <= 45
    # A truncated escaped-string element survives instead of marker-only output.
    assert isinstance(parsed45[0], str) and parsed45[0].startswith('"')
    out50 = c._fit_extracted(['"' * 20, "ok"], 50)
    assert len(out50) <= 50
    assert "ok" in json.loads(out50)  # the cheap tail survives once there is room


# ── E4. Router monotonicity: the _compress_json head-preview cliff is gone ─────
#
# These exercise ``_compress_json`` DIRECTLY (like E3 hits ``_fit_extracted``):
# the public ``compress`` short-circuits and returns the input verbatim once it
# fits the budget, which masks the router at the very budgets where the cliff
# lived. The router now emits the WHOLE value as ``indent=2`` pretty JSON when it
# fits (lossless, max content) and otherwise hands every overflow to the
# budget-filling final tier — instead of preferring a fixed 5-item pretty head
# preview that carried fewer leaves than the compact tier did at a smaller budget.


_ROUTER_LIST_FIXTURES: dict[str, object] = {
    # Each hit the old router cliff: at the budget where the fixed 5-item
    # ``indent=2`` head preview just fit, the router returned it (few leaves) even
    # though the compact final tier carried MORE leaves one char below.
    "ints_50": list(range(50)),  # leaves 9 @ 60 -> 5 @ 61 pre-fix
    "small_dicts_100": [{"id": i, "name": f"item{i}"} for i in range(100)],  # 15 @ 246 -> 10 @ 247
    "short_strings": [f"val-{i}" for i in range(40)],
    "nested_lists": [[i, i + 1, i + 2] for i in range(20)],
}


@pytest.mark.parametrize("name", sorted(_ROUTER_LIST_FIXTURES))
def test_list_root_monotone_through_router(name: str) -> None:
    """For a LIST root, a larger budget never preserves fewer leaves through the
    full router. Pre-fix the fixed ``indent=2`` head preview undercut the compact
    final tier; routing every overflow through ``_fit_extracted`` (itself monotone
    for lists, the 4B fix) removes the cliff. Budgets swept CONTIGUOUSLY."""
    data = _ROUTER_LIST_FIXTURES[name]
    c = FieldExtractCompressor()
    prev: int | None = None
    for budget in range(2, len(json.dumps(data, indent=2)) + 5):
        out = c._compress_json(data, budget)
        parsed = json.loads(out)  # always valid JSON
        assert len(out) <= max(2, budget), f"{name}@{budget}: {len(out)} over budget"
        leaves = _preserved_leaves(parsed, data)
        if prev is not None:
            assert leaves >= prev, f"{name}@{budget}: content regressed {prev} -> {leaves}"
        prev = leaves


_FLAT_DICT_FIXTURES: dict[str, object] = {
    # Scalar-only dicts: the dict final tier (skeleton + scalar enrich) is itself
    # monotone, so end-to-end monotonicity holds once the router preview cliff is
    # gone. Dicts with NESTED collections also lose the router cliff, but their
    # final tier still inherits ``_take``'s non-monotonicity (the dict analog of
    # the list 4B fix, tracked as a follow-up) — so they are asserted only via the
    # weaker pretty-is-lossless invariant below, not full monotonicity.
    "string_values_30": {f"k{i}": f"value-{i}" for i in range(30)},
    "mixed_scalars": {f"f{i}": (i if i % 2 else f"s{i}") for i in range(25)},
}


@pytest.mark.parametrize("name", sorted(_FLAT_DICT_FIXTURES))
def test_flat_dict_monotone_through_router(name: str) -> None:
    """A scalar-only DICT root is monotone end-to-end once the router no longer
    prefers the lossy ``indent=2`` preview (its final tier carries no nested
    collection that ``_take`` could flip into an emptier marker)."""
    data = _FLAT_DICT_FIXTURES[name]
    c = FieldExtractCompressor()
    prev: int | None = None
    for budget in range(2, len(json.dumps(data, indent=2)) + 5):
        out = c._compress_json(data, budget)
        parsed = json.loads(out)
        assert len(out) <= max(2, budget), f"{name}@{budget}: {len(out)} over budget"
        leaves = _preserved_leaves(parsed, data)
        if prev is not None:
            assert leaves >= prev, f"{name}@{budget}: content regressed {prev} -> {leaves}"
        prev = leaves


@pytest.mark.parametrize(
    "data",
    [
        list(range(50)),
        [{"id": i, "name": f"item{i}"} for i in range(30)],
        {"a": 1, "b": 2, "items": [{"x": i, "y": f"v{i}"} for i in range(20)], "tail": "end"},
        {f"k{i}": "x" * 50 for i in range(10)},
    ],
)
def test_router_pretty_output_is_always_lossless(data: object) -> None:
    """The router emits ``indent=2`` pretty JSON ONLY for the WHOLE value (the
    lossless tier); every overflow is the compact final tier. So whenever the
    output is pretty (contains a newline) it must round-trip to the original data
    EXACTLY — never a lossy head preview. Holds for list AND dict roots,
    independent of the dict final tier's separate residual non-monotonicity."""
    c = FieldExtractCompressor()
    for budget in range(2, len(json.dumps(data, indent=2)) + 5):
        out = c._compress_json(data, budget)
        if "\n" in out:  # pretty (indent=2) => must be the full, lossless value
            assert json.loads(out) == data, f"@{budget}: pretty output is lossy"


def test_router_no_longer_prefers_lossy_preview() -> None:
    """Pin the canonical pre-fix regressions: a larger budget returned the bulky
    5-item ``indent=2`` preview (fewer leaves) than the compact tier carried one
    char below. After the fix the leaf count is non-decreasing across the
    boundary, for both a list-of-ints and a list-of-dicts root."""
    c = FieldExtractCompressor()
    ints: list[object] = list(range(50))
    assert _preserved_leaves(json.loads(c._compress_json(ints, 61)), ints) >= _preserved_leaves(
        json.loads(c._compress_json(ints, 60)), ints
    )
    dicts: list[object] = [{"id": i, "name": f"item{i}"} for i in range(100)]
    assert _preserved_leaves(json.loads(c._compress_json(dicts, 247)), dicts) >= _preserved_leaves(
        json.loads(c._compress_json(dicts, 246)), dicts
    )


def test_router_emits_whole_value_pretty_when_it_fits() -> None:
    """A value that fits in full is emitted losslessly as ``indent=2`` pretty JSON
    for containers — the readable tier the router still keeps."""
    c = FieldExtractCompressor()
    d = {"name": "svc", "port": 8080, "tags": ["a", "b"]}
    out = c._compress_json(d, 500)
    assert "\n" in out and json.loads(out) == d  # pretty + lossless
    lst = [1, 2, 3]
    out2 = c._compress_json(lst, 100)
    assert "\n" in out2 and json.loads(out2) == lst


# ── E5. Monotonicity: the DICT final tier (the dict analog of the 4B list fix) ─
#
# The old dict enrich filled the budget with a per-key greedy ``_take`` search.
# That was non-monotone two ways: (1) ``_take``'s own content flips to a
# longer-but-emptier marker as its budget grows (the 4B root cause), and (2) the
# per-key greedy let a growing budget hand an EARLIER key more budget, displacing
# a LATER key. ``_enrich_dict_monotone`` replaces it with ``_fit_monotone``'s
# discipline — a growing full-value prefix plus ONE partial boundary, every other
# key kept as its stub — so only one key is ever partial and a larger budget never
# shows fewer leaves on a dict root.


_DICT_MONO_FIXTURES: dict[str, object] = {
    # cfg (a nested dict) used to enrich to a marker-only form that displaced the
    # trailing ``tags`` content as the budget grew.
    "displacing_coll": {
        "name": "app",
        "cfg": {f"o{i}": i for i in range(15)},
        "tags": list(range(20)),
    },
    # An early bulky collection used to steal budget from a later collection.
    "early_big_late": {
        "A": [{"p": i, "q": f"v{i}"} for i in range(15)],
        "B": {"x": 1, "y": 2, "z": 3},
    },
    # A huge leading scalar must not crowd out short later identifiers.
    "oversized_first": {"blob": "x" * 300, "id": "abc", "name": "Alice", "cfg": {"a": 1, "b": 2}},
    # Wide config: every value a nested dict; fill must stay tight AND monotone.
    "wide_config": _wide_config_dict(20),
    "nested_lists": {
        "a": 1,
        "b": 2,
        "items": [{"x": i, "y": f"v{i}"} for i in range(20)],
        "tail": "end",
    },
    "flat_scalars": {f"k{i}": f"value-{i}" for i in range(25)},
    "deep": {"l1": {"l2": {"l3": {"l4": [1, 2, 3], "id": "x"}, "k": "v"}, "m": 5}, "n": 9},
    # Empty-nested values (0 original scalars): a full ``{"x": []}`` must not score
    # below its ``{1 keys}`` stub — the metric counts both as 0 preserved content.
    "empty_nested": {"a": {"x": []}, "b": "tail", "c": [[], {}], "d": 5},
}


@pytest.mark.parametrize("name", sorted(_DICT_MONO_FIXTURES))
def test_dict_root_content_is_monotone_in_budget(name: str) -> None:
    """For a dict root, a LARGER budget must never preserve FEWER original leaves
    in the final tier. Budgets are swept CONTIGUOUSLY so a one-char cliff cannot
    hide between sampled points; output is asserted valid + within budget too."""
    fixture = _DICT_MONO_FIXTURES[name]
    data = json.loads(fixture) if isinstance(fixture, str) else fixture
    c = FieldExtractCompressor()
    prev: int | None = None
    for budget in range(2, len(json.dumps(data)) + 5):
        out = c._fit_extracted(data, budget)
        parsed = json.loads(out)  # always valid JSON
        assert len(out) <= max(2, budget), f"{name}@{budget}: {len(out)} over budget"
        leaves = _preserved_leaves(parsed, data)
        if prev is not None:
            assert leaves >= prev, f"{name}@{budget}: content regressed {prev} -> {leaves}"
        prev = leaves


def test_dict_single_partial_boundary_no_cross_key_displacement() -> None:
    """The canonical pre-fix cross-key regression: as the budget grows one char,
    an early collection used to grab more budget and an emptier-marker form
    displaced a later key (content dropped). With a single partial boundary the
    leaf count is non-decreasing across that exact boundary."""
    c = FieldExtractCompressor()
    data = {
        "A": [{"p": i, "q": f"v{i}", "r": [i, i + 1]} for i in range(15)],
        "B": {"x": 1, "y": 2, "z": 3, "w": 4},
    }
    prev = -1
    for budget in range(2, 200):
        leaves = _preserved_leaves(json.loads(c._fit_extracted(data, budget)), data)
        assert leaves >= prev, f"@{budget}: regressed {prev} -> {leaves}"
        prev = leaves


def test_dict_oversized_scalar_ranked_last_preserves_short_siblings() -> None:
    """A huge scalar value is ranked LAST, so short, high-value siblings land in
    the full prefix instead of being crowded into the boundary/marker. Mirrors the
    contract behind test_later_short_scalars_* but through the new enrich path."""
    c = FieldExtractCompressor()
    data = {"blob": "x" * 500, "id": "abc", "name": "Alice", "flag": True}
    out = c._fit_extracted(data, 120)
    parsed = json.loads(out)
    assert len(out) <= 120
    assert parsed["id"] == "abc"
    assert parsed["name"] == "Alice"
    assert parsed["flag"] is True
    assert parsed["blob"] != "x" * 500  # the huge value is the one that degrades


def test_dict_small_container_full_fits_after_larger_prefix_overflows() -> None:
    """The full-prefix search must take the MAX fitting prefix, not stop at the
    first overflow: a small container's FULL form is SHORTER than its ``{N items}``
    stub, so a longer prefix can fit after a shorter one overflows. Here both ``a``
    and ``b`` fit fully at a budget where the (b-stub) prefix-of-1 overflows — the
    early-break search wrongly stubbed ``b``."""
    c = FieldExtractCompressor()
    data = {"a": [0, 1, 2, 3], "b": [0], "blob": "x" * 100}
    out = c._fit_extracted(data, 48)
    parsed = json.loads(out)
    assert len(out) <= 48
    assert parsed["a"] == [0, 1, 2, 3]
    assert parsed["b"] == [0]  # the small container survives FULL, not "[1 items]"


def test_dict_empty_nested_value_is_monotone_against_its_stub() -> None:
    """A key whose whole value is empty-nested (``{"x": []}`` — zero original
    scalars) must not score below its ``{1 keys}`` stub as the budget grows. The
    stub preserves no original scalar (counts 0); so does the full empty-nested
    value, so promoting it to full is monotone. Pre-metric-fix this regressed from
    2 -> 1 'leaves' at the budget where the full value first fit, because the stub
    string was wrongly counted as a preserved leaf."""
    c = FieldExtractCompressor()
    data = {"a": {"x": []}, "b": "tail"}
    prev = -1
    for budget in range(2, len(json.dumps(data)) + 5):
        leaves = _preserved_leaves(json.loads(c._fit_extracted(data, budget)), data)
        assert leaves >= prev, f"@{budget}: regressed {prev} -> {leaves}"
        prev = leaves


@pytest.mark.parametrize(
    "data",
    [
        # A real list value containing the word "omitted" must not be mistaken for a
        # generated omitted-count marker by the boundary guard.
        {"a": ["a real omitted note", "tail"], "b": "x" * 40, "c": [1, 2, 3]},
        # A real string value that reads like the in-array marker.
        {"a": {"label": "... (2 of 2 items omitted)", "note": "hello"}, "b": "x" * 40},
        # A real key whose name starts with the collision-safe marker prefix.
        {"outer": {"_truncated_real": "keep me", "z": "tail"}, "id": "abc", "pad": "y" * 40},
        # A real top-level _truncated* key.
        {"_truncated_x": "keep", "data": [{"v": i} for i in range(15)], "id": "z"},
    ],
)
def test_dict_provenance_not_pattern_for_markers(data: object) -> None:
    """Marker/placeholder detection is by PROVENANCE, not string pattern: a real
    value containing ``omitted`` or a real ``_truncated*`` key is genuine content,
    so the boundary guard must not drop it and the budget must stay monotone."""
    c = FieldExtractCompressor()
    prev = -1
    for budget in range(2, len(json.dumps(data)) + 5):
        out = c._fit_extracted(data, budget)
        assert len(out) <= max(2, budget), f"@{budget}: over budget"
        leaves = _preserved_leaves(json.loads(out), data)
        assert leaves >= prev, f"@{budget}: regressed {prev} -> {leaves}"
        prev = leaves
