"""Directional classifier for the bench_qa cross-version drift gate.

The drift gate's pass/fail is a whole-report deep-equal against a committed
baseline (``test_bench_qa_drift.py``) — this module never decides pass/fail.
Its only job is to make a failing diff *readable*: given the old (baseline)
and new (live) canonical reports, it walks every changed field and tags it
``REGRESSION`` / ``IMPROVEMENT`` / ``NEUTRAL`` from a hard-coded direction
table, so the failure message and PR surface say "s03 compression_ratio up
[REGRESSION]" in English instead of a bare ``dict != dict``.

Pure functions, no I/O. The direction table is the single place that encodes
"which way is bad" for each report field; the coverage test in
``test_bench_qa_drift.py`` asserts every schema field is either classified
here or explicitly excluded — a new ScenarioReport field can't silently
become an unclassified drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Label = Literal["REGRESSION", "IMPROVEMENT", "NEUTRAL"]

# Direction policies (see per-field table below):
#   UP_BAD          increase = REGRESSION, decrease = IMPROVEMENT
#   DOWN_BAD        decrease = REGRESSION, increase = IMPROVEMENT
#   BOOL_TRUE_GOOD  True->False = REGRESSION, False->True = IMPROVEMENT
#   VERDICT         pass<->fail ordered; anything involving "advisory" = NEUTRAL
#   ERROR_PRESENCE  None->str = REGRESSION, str->None = IMPROVEMENT, str->str' = NEUTRAL
#   IDENTITY        any change = REGRESSION (deterministic id; a change is a bug)
#   NEUTRAL         any change = NEUTRAL (routing / structural / read-via-another-field)
UP_BAD = "UP_BAD"
DOWN_BAD = "DOWN_BAD"
BOOL_TRUE_GOOD = "BOOL_TRUE_GOOD"
VERDICT = "VERDICT"
ERROR_PRESENCE = "ERROR_PRESENCE"
IDENTITY = "IDENTITY"
NEUTRAL = "NEUTRAL"

# Keyed by ScenarioReport-relative path: "<section>.<field>" for the nested
# metric dicts, bare names for scenario-level scalars. Rationale per field is
# grounded in report.py / schema.py (compression_ratio = compressed/cleaned;
# lower = more savings; qa/recall higher = better; ratio_violation 0 good).
SCENARIO_DIRECTION: dict[str, str] = {
    # identity
    "trace_id": IDENTITY,  # bench-<sha256(...)>; deterministic, a change = injection bug
    # metrics
    "metrics.original_chars": NEUTRAL,  # input size; fixed payload — change = fixture/cleaning, read via ratio
    "metrics.cleaned_chars": NEUTRAL,  # post-clean size; read via ratio
    "metrics.compressed_chars": UP_BAD,  # more bytes kept = less savings
    "metrics.compression_ratio": UP_BAD,  # compressed/cleaned; up = weaker compression
    "metrics.compression_strategy": NEUTRAL,  # routing label; change = different strategy chosen
    "metrics.ratio_violation": UP_BAD,  # 0 good, 1 = fell to fallback ladder
    "metrics.surfacing_on_progressive_ok": DOWN_BAD,  # 1 = surfacing survived progressive path
    "metrics.surface_error": ERROR_PRESENCE,  # None good; appearance = a new surfacing failure
    # rule_judge.score is EXCLUDED below (always-0.0 sentinel under replay drivers)
    "rule_judge.missing_keywords": NEUTRAL,  # list; informational
    # qa
    "qa.answerable": DOWN_BAD,  # probes still answerable post-compression
    "qa.total": NEUTRAL,  # fixture-fixed probe count; a change is structural
    "qa.ratio": DOWN_BAD,  # answerable/total; primary info-loss guard
    # progressive
    "progressive.round_trip_equal": BOOL_TRUE_GOOD,  # reassembled == cleaned (driver pins True)
    "progressive.chunks": NEUTRAL,  # chunk count; structural
    "progressive.total_chars": NEUTRAL,  # reassembled length; structural
    # surfacing
    "surfacing.recall_at_k": DOWN_BAD,  # right memories surfaced in top-k
    "surfacing.returned_ids": NEUTRAL,  # ordered ids; read via recall
    "surfacing.expected_ids": NEUTRAL,  # ground-truth ids; read via recall
    # scenario-level scalars
    "tier": NEUTRAL,  # == compression_strategy or "unknown"; routing
    "verdict": VERDICT,  # pass<->fail ordered
}

# Fields present in the report but deliberately not direction-classified.
SCENARIO_EXCLUDED: dict[str, str] = {
    "rule_judge.score": (
        "always 0.0 under the replay drivers (not-scored sentinel) — a drop-to-0.0 "
        "would be a guaranteed false REGRESSION; revisit if the drivers ever score it"
    ),
    "scenario_id": "the row key, not a metric",
}

# Top-level BenchReport totals direction.
TOTALS_DIRECTION: dict[str, str] = {
    "totals.scenarios": NEUTRAL,  # roster size; a drop is caught by the roster lockstep test
    "totals.passing": DOWN_BAD,
    "totals.failing": UP_BAD,
    "totals.tokens_saved_approx": DOWN_BAD,  # coarse //4 suite-sum; advisory trend
}


@dataclass(frozen=True)
class FieldDrift:
    """One changed field between baseline and live, with a direction label."""

    scope: str
    old: Any
    new: Any
    label: Label
    note: str = ""

    def render(self) -> str:
        arrow = f"{self.old!r} → {self.new!r}"
        suffix = f" — {self.note}" if self.note else ""
        return f"`{self.scope}`: {arrow}{suffix}"


def _direction_label(policy: str, old: Any, new: Any) -> tuple[Label, str]:
    """Map a (policy, old, new) change to a (label, note)."""
    if policy == NEUTRAL:
        return "NEUTRAL", ""
    if policy == IDENTITY:
        return "REGRESSION", "deterministic id changed — likely an injection bug"
    if policy == BOOL_TRUE_GOOD:
        if old and not new:
            return "REGRESSION", "True→False"
        if new and not old:
            return "IMPROVEMENT", "False→True"
        return "NEUTRAL", ""
    if policy == VERDICT:
        if old == "pass" and new == "fail":
            return "REGRESSION", "pass→fail"
        if old == "fail" and new == "pass":
            return "IMPROVEMENT", "fail→pass"
        return "NEUTRAL", "involves advisory"
    if policy == ERROR_PRESENCE:
        if old is None and new is not None:
            return "REGRESSION", "new surface_error appeared"
        if old is not None and new is None:
            return "IMPROVEMENT", "surface_error cleared"
        return "NEUTRAL", "error text changed (presence unchanged)"
    # numeric directions
    if policy in (UP_BAD, DOWN_BAD):
        try:
            delta = float(new) - float(old)
        except (TypeError, ValueError):
            return "NEUTRAL", "non-numeric change"
        if delta == 0:
            return "NEUTRAL", ""
        worse_on_increase = policy == UP_BAD
        increased = delta > 0
        if increased == worse_on_increase:
            return "REGRESSION", f"{'up' if increased else 'down'}"
        return "IMPROVEMENT", f"{'up' if increased else 'down'}"
    return "NEUTRAL", "unclassified field"


def _diff_section(
    prefix: str, old: dict[str, Any], new: dict[str, Any], table: dict[str, str]
) -> list[FieldDrift]:
    drifts: list[FieldDrift] = []
    for key in sorted(set(old) | set(new)):
        ov, nv = old.get(key), new.get(key)
        if ov == nv:
            continue
        path = f"{prefix}.{key}" if prefix else key
        policy = table.get(path)
        if policy is None:
            # Unknown changed field — surfaced as NEUTRAL so it is never lost,
            # but the coverage test prevents this from happening in practice.
            drifts.append(FieldDrift(path, ov, nv, "NEUTRAL", "unclassified field"))
            continue
        label, note = _direction_label(policy, ov, nv)
        drifts.append(FieldDrift(path, ov, nv, label, note))
    return drifts


def _diff_scenario(sid: str, old: dict[str, Any], new: dict[str, Any]) -> list[FieldDrift]:
    drifts: list[FieldDrift] = []
    nested = ("metrics", "rule_judge", "qa", "progressive", "surfacing")
    scalars = ("trace_id", "tier", "verdict")
    for section in nested:
        ov, nv = old.get(section, {}) or {}, new.get(section, {}) or {}
        for d in _diff_section(section, ov, nv, SCENARIO_DIRECTION):
            if d.scope in SCENARIO_EXCLUDED:
                continue
            drifts.append(FieldDrift(f"{sid}.{d.scope}", d.old, d.new, d.label, d.note))
    for field in scalars:
        ov, nv = old.get(field), new.get(field)
        if ov == nv:
            continue
        policy = SCENARIO_DIRECTION.get(field, NEUTRAL)
        label, note = _direction_label(policy, ov, nv)
        drifts.append(FieldDrift(f"{sid}.{field}", ov, nv, label, note))
    return drifts


def classify_drift(old_report: dict[str, Any], new_report: dict[str, Any]) -> list[FieldDrift]:
    """Walk two canonical reports and return every changed field, direction-tagged.

    *old_report* is the committed baseline, *new_report* the freshly replayed
    live report. Both must already be ``canonicalize_report``-ed (timings and
    the llm_judge block stripped). Returns an empty list when they are equal.
    """
    drifts: list[FieldDrift] = []
    old_s = {s["scenario_id"]: s for s in old_report.get("scenarios", [])}
    new_s = {s["scenario_id"]: s for s in new_report.get("scenarios", [])}

    for sid in sorted(set(old_s) - set(new_s)):
        drifts.append(FieldDrift(sid, "present", "removed", "REGRESSION", "scenario dropped"))
    for sid in sorted(set(new_s) - set(old_s)):
        drifts.append(FieldDrift(sid, "absent", "added", "NEUTRAL", "new scenario"))
    for sid in sorted(set(old_s) & set(new_s)):
        drifts.extend(_diff_scenario(sid, old_s[sid], new_s[sid]))

    drifts.extend(
        _diff_section(
            "totals", old_report.get("totals", {}), new_report.get("totals", {}), TOTALS_DIRECTION
        )
    )
    if old_report.get("tier_histogram") != new_report.get("tier_histogram"):
        drifts.append(
            FieldDrift(
                "tier_histogram",
                old_report.get("tier_histogram"),
                new_report.get("tier_histogram"),
                "NEUTRAL",
                "strategy routing distribution changed",
            )
        )
    return drifts


def format_drift_md(drifts: list[FieldDrift]) -> str:
    """Render a direction-grouped markdown summary of a drift list."""
    if not drifts:
        return "No drift — live report matches the committed baseline."
    order: list[Label] = ["REGRESSION", "NEUTRAL", "IMPROVEMENT"]
    by_label: dict[Label, list[FieldDrift]] = {lbl: [] for lbl in order}
    for d in drifts:
        by_label[d.label].append(d)
    lines = ["### bench_qa drift vs committed baseline", ""]
    for lbl in order:
        items = by_label[lbl]
        if not items:
            continue
        lines.append(f"**{lbl} ({len(items)})**")
        lines.extend(f"- {d.render()}" for d in items)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
