"""Output-contract invariants for TruncateCompressor.

These pin three guarantees that several assemble-then-``[:max_chars]`` paths
used to violate (each silently): the output never exceeds ``max_chars``, the
config-dict JSON path always emits parseable JSON, and the
preserve-the-anomaly / preserve-the-footer paths never slice off the very
content they set out to keep.

Pre-fix, the production paths overshot the budget (suffix/footer appended
after a budget-filling break) and the hard ``[:max_chars]`` cut destroyed the
tail anomaly, the markdown footer, and produced invalid JSON. The existing
suite did not catch any of these, so this file is the regression net.
"""

from __future__ import annotations

import json

import pytest

from memtomem_stm.proxy.compression import TruncateCompressor
from memtomem_stm.proxy.relevance import BM25Scorer

_SENTINEL = "... (truncated"  # both "(truncated)" and "(truncated, original:" forms


def _config_json(n: int = 8) -> str:
    # All values are dicts → routes to TruncateCompressor._json_key_truncate.
    data = {
        f"section_{i}": {
            "enabled": True,
            "params": {"a": i, "b": "x" * 60},
            "items": list(range(8)),
        }
        for i in range(n)
    }
    return json.dumps(data, indent=2)


def _markdown(sections: int = 14) -> str:
    body = "\n\n".join(
        f"## Section {i}\n\nLine one for section {i}.\nLine two with more detail " + ("blah " * 12)
        for i in range(sections)
    )
    return f"# Doc\n\nIntro.\n\n{body}\n\n## Summary\n\nThe conclusion that matters."


def _repetitive_log(lines: int = 40) -> str:
    head = "\n".join(
        f"2026-05-30T10:00:{i:02d} INFO request handled in {i}ms" for i in range(lines)
    )
    return head + "\n2026-05-30T10:01:00 ERROR fatal: connection refused after retries"


def _code(funcs: int = 20) -> str:
    return "\n\n".join(
        f"def func_{i}(x):\n    # does thing {i}\n    y = x + {i}\n    return y * {i}"
        for i in range(funcs)
    )


def _ndjson(lines: int = 40) -> str:
    return "\n".join(
        json.dumps({"ts": i, "level": "info", "msg": f"event {i} happened"}) for i in range(lines)
    )


def _csv(rows: int = 50) -> str:
    return "id,name,value,desc\n" + "\n".join(
        f"{i},name{i},{i * 10},description text here {i}" for i in range(rows)
    )


def _plain(paras: int = 30) -> str:
    return "\n\n".join(
        f"Paragraph {i} explains a thing. It has a second sentence with detail."
        for i in range(paras)
    )


_FIXTURES = {
    "config_json": _config_json(),
    "markdown": _markdown(),
    "repetitive_log": _repetitive_log(),
    "code": _code(),
    "ndjson": _ndjson(),
    "csv": _csv(),
    "plain": _plain(),
}


@pytest.mark.parametrize("name", sorted(_FIXTURES))
@pytest.mark.parametrize("budget", [200, 600, 1200])
def test_output_never_exceeds_max_chars(name: str, budget: int) -> None:
    """Invariant A: len(output) <= max_chars for every content type and budget.

    Covers the plain-suffix, JSON-key, tail-anomaly, section-aware and
    code-aware paths (each a former overshoot site).
    """
    text = _FIXTURES[name]
    out = TruncateCompressor(scorer=BM25Scorer()).compress(text, max_chars=budget)
    assert len(out) <= budget, f"{name}@{budget}: produced {len(out)} chars (+{len(out) - budget})"


@pytest.mark.parametrize("budget", [200, 400, 800, 1200])
def test_json_key_truncate_emits_valid_json(budget: int) -> None:
    """Invariant B: the config-dict JSON path always emits parseable JSON."""
    text = _config_json(10)
    assert len(text) > 1200  # ensure all budgets force truncation
    out = TruncateCompressor().compress(text, max_chars=budget)
    parsed = json.loads(out)  # raises if invalid — the core regression
    assert isinstance(parsed, dict)
    assert len(out) <= budget


def test_json_key_truncate_records_omitted_keys() -> None:
    """Dropped trailing keys are reported in a valid ``_truncated`` member,
    rather than silently lost behind an invalid mid-string cut."""
    text = _config_json(12)
    out = TruncateCompressor().compress(text, max_chars=400)
    parsed = json.loads(out)
    assert "_truncated" in parsed
    assert "omitted" in parsed["_truncated"]


# ── JSON path: budget refill after key drops (the freeze regression) ──────────


def _skewed_config() -> str:
    """One huge key between small ones. Pre-fix, every budget from 80 through
    1500 returned the same 66-char output (key ``a`` + marker): the per-key
    parts were sized against the FULL key set, and the assembler only dropped
    whole keys — it never re-spent the freed budget."""
    return json.dumps({"a": {"x": "aaa"}, "b": {"big": "B" * 3000}, "c": {"y": "ccc"}})


def _preserved_chars(truncated: object, original: object) -> int:
    """Chars of ORIGINAL leaf content preserved in the truncated form."""
    if isinstance(original, str):
        if not isinstance(truncated, str):
            return 0
        s = truncated[:-3] if truncated.endswith("...") else truncated
        return len(s) if original.startswith(s) else 0
    if isinstance(original, dict):
        if not isinstance(truncated, dict):
            return 0
        return sum(_preserved_chars(truncated[k], v) for k, v in original.items() if k in truncated)
    if isinstance(original, list):
        if not isinstance(truncated, list):
            return 0
        return sum(_preserved_chars(t, o) for t, o in zip(truncated, original))
    return len(json.dumps(original)) if truncated == original else 0


@pytest.mark.parametrize("lo,hi", [(80, 200), (200, 400), (400, 800), (800, 1500)])
def test_json_key_truncate_output_grows_with_budget(lo: int, hi: int) -> None:
    """A larger budget must produce more output, not freeze at the key-drop
    point (pre-fix: 66 chars at every budget in [80, 1500] for this payload)."""
    text = _skewed_config()
    out_lo = TruncateCompressor().compress(text, max_chars=lo)
    out_hi = TruncateCompressor().compress(text, max_chars=hi)
    json.loads(out_lo)
    json.loads(out_hi)
    assert len(out_lo) <= lo
    assert len(out_hi) <= hi
    assert len(out_hi) > len(out_lo)


def test_json_key_truncate_refills_boundary_key() -> None:
    """The freed budget is re-spent on a truncated form of the first dropped
    key instead of dropping it whole."""
    out = TruncateCompressor().compress(_skewed_config(), max_chars=800)
    parsed = json.loads(out)
    assert parsed["a"] == {"x": "aaa"}
    assert parsed["b"]["big"].startswith("B")
    assert parsed["b"]["big"].endswith("...")
    assert parsed["_truncated"] == "1 of 3 keys omitted"
    assert len(out) > 700  # fills the budget instead of freezing at 66


@pytest.mark.parametrize(
    "name,data",
    [
        ("skewed", {"a": {"x": "aaa"}, "b": {"big": "B" * 3000}, "c": {"y": "ccc"}}),
        (
            "tail_smalls",
            {
                "big": {"v": "B" * 400},
                "m1": {"v": "M" * 50},
                "m2": {"v": "N" * 50},
                "s": {"x": "y"},
            },
        ),
    ],
)
def test_json_key_truncate_content_is_monotone_in_budget(name: str, data: dict) -> None:
    """Preserved ORIGINAL content never shrinks as the budget grows, swept
    contiguously so a one-char cliff cannot hide between sampled points. Raw
    length may wobble by a few framing chars where the allocator crosses its
    everything-fits threshold; preserved content is the contract. Output stays
    valid JSON and within budget at every step."""
    text = json.dumps(data)
    prev = -1
    for budget in range(40, len(text) + 40):
        out = TruncateCompressor().compress(text, max_chars=budget)
        parsed = json.loads(out)
        assert len(out) <= max(2, budget), f"{name}@{budget}: {len(out)} over budget"
        preserved = _preserved_chars(parsed, data)
        assert preserved >= prev, f"{name}@{budget}: content shrank {prev} -> {preserved}"
        prev = preserved


def test_json_key_truncate_marker_key_is_collision_safe() -> None:
    """A real top-level ``_truncated`` key is never clobbered by the synthetic
    omitted-count marker."""
    data: dict = {"_truncated": {"real": "VALUE"}}
    data.update({f"k{i}": {"v": "x" * 80} for i in range(10)})
    out = TruncateCompressor().compress(json.dumps(data), max_chars=300)
    parsed = json.loads(out)
    assert parsed["_truncated"] == {"real": "VALUE"}
    assert "keys omitted" in parsed["_truncated_"]


@pytest.mark.parametrize("budget", [1, 2, 5])
def test_json_path_stays_valid_json_at_pathological_budget(budget: int) -> None:
    """Contract floor: valid JSON cannot be shorter than ``{}`` (2 chars), so a
    sub-2-char budget keeps JSON validity over the length cap (it returns
    ``{}``). This documents/pins the one edge where len > max_chars is allowed —
    a budget never produced by config or the manager retention ladder. Above the
    2-char floor the length cap holds (budget=5 → still ``<= 5``)."""
    text = _config_json(8)
    out = TruncateCompressor().compress(text, max_chars=budget)
    json.loads(out)  # always parseable
    assert len(out) <= max(2, budget)


def test_tail_anomaly_is_preserved_under_tight_budget() -> None:
    """Invariant C: the repetitive-content path keeps the tail anomaly (the
    whole reason the path exists). Pre-fix the START-anchored slice cut it off."""
    text = _repetitive_log(60)
    out = TruncateCompressor().compress(text, max_chars=300)
    assert len(out) <= 300
    assert "ERROR fatal: connection refused" in out, "tail anomaly was sliced off"
    assert "omitted" in out  # the repetition marker survives too


def test_section_aware_keeps_footer_and_marks_truncation() -> None:
    """Invariant C: many sections at a tight budget still carry the
    ``(original: N chars)`` footer and a truncation sentinel — the footer used
    to be sliced off with no marker when the minimums overflowed."""
    text = _markdown(20)
    out = TruncateCompressor().compress(text, max_chars=500)
    assert len(out) <= 500
    assert "(original:" in out, "footer was sliced off"
    assert _SENTINEL in out, "no truncation marker after cut"


def test_plain_truncate_suffix_within_budget() -> None:
    """The plain-text fallback reserves its suffix instead of appending past
    the budget."""
    text = _plain(40)
    out = TruncateCompressor().compress(text, max_chars=300)
    assert len(out) <= 300
    assert _SENTINEL in out


class TestFitWithFooterHelper:
    """Direct unit coverage of the shared budget helper."""

    def test_passthrough_when_fits(self) -> None:
        out = TruncateCompressor._fit_with_footer("body", "\nfooter", 100)
        assert out == "body\nfooter"

    def test_footer_survives_and_within_budget(self) -> None:
        body = "line\n" * 200
        footer = "\n(original: 1000 chars)"
        out = TruncateCompressor._fit_with_footer(body, footer, 120)
        assert len(out) <= 120
        assert out.endswith(footer), "footer was sliced off"
        assert "... (truncated)" in out

    def test_degrades_for_tiny_budget(self) -> None:
        # Budget smaller than footer+sentinel: still never exceeds max_chars.
        out = TruncateCompressor._fit_with_footer("x" * 100, "\n(original: 100 chars)", 10)
        assert len(out) <= 10
