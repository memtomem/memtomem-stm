"""Regenerate the committed bench_qa cross-version drift baseline.

Run from the repo root:

    uv run python tests/bench/bench_qa/regen_drift_baseline.py

Replays the full scenario roster in-process (the exact code path the drift
gate uses), canonicalizes the merged report, prints a directional drift
summary against the existing baseline — so you SEE which fields moved which
way before committing — and writes ``tests/bench/fixtures/drift_baseline.json``.

When a PR legitimately changes ``report.json`` the ``bench_qa_drift`` advisory
gate goes red; run this, read the printed REGRESSION / IMPROVEMENT / NEUTRAL
table, confirm the change is intended, and commit the regenerated baseline IN
THE SAME PR with a "## Baseline change" callout. Never hand-edit the baseline —
byte-stability is the gate's contract and this script is its only writer.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

# Self-bootstrap: this module lives at tests/bench/bench_qa/regen_drift_baseline.py.
# Put tests/ on sys.path so ``import bench...`` resolves when the file is run as a
# script by path, mirroring how pytest makes the bench package importable (a raw
# ``python`` invocation does not add tests/ the way pytest's rootdir insertion does).
_TESTS_DIR = Path(__file__).resolve().parents[2]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from bench.bench_qa import (  # noqa: E402  (import follows the sys.path bootstrap above)
    canonicalize_report,
    classify_drift,
    fixtures_dir,
    format_drift_md,
    run_full_suite,
)

BASELINE_PATH = fixtures_dir() / "drift_baseline.json"


def serialize_baseline(report: dict) -> str:
    """Exact on-disk form of the baseline.

    Mirrors ``report.py``'s ``json.dumps(report, indent=2, sort_keys=True)``
    (ensure_ascii default, no trailing newline) so the written file is
    byte-identical to what a re-run produces — that is what makes
    ``test_regen_baseline_is_idempotent`` meaningful.
    """
    return json.dumps(report, indent=2, sort_keys=True)


async def build_canonical_report() -> dict:
    """Replay the full roster under a throwaway temp dir, return canonical form."""
    with tempfile.TemporaryDirectory(prefix="bench_qa_drift_regen_") as td:
        report = await run_full_suite(Path(td))
    return canonicalize_report(report)


def main() -> int:
    new_report = asyncio.run(build_canonical_report())
    if BASELINE_PATH.exists():
        old_report = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        print(format_drift_md(classify_drift(old_report, new_report)))
    else:
        print("No existing baseline — writing a fresh one.")
    BASELINE_PATH.write_text(serialize_baseline(new_report), encoding="utf-8")
    print(f"\nWrote {BASELINE_PATH} ({len(new_report['scenarios'])} scenarios).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
