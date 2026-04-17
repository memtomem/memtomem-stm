"""Drift gate: fixtures produced by a committed generator stay in sync.

Some bench_qa fixtures are too large to hand-author (e.g., s05's 40-turn
chat transcript, ~12 KB). The generator lives at
``tests/bench/fixtures/_generators/<scenario_id>.py`` and emits canonical
JSON into ``tests/bench/fixtures/<scenario_id>.json``. This test re-runs
the generator in-memory and asserts the output matches the committed
fixture byte-for-byte.

If this test fails, the fixture and the generator have drifted. The
fix is almost always to regenerate — see the assertion message.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bench.fixtures._generators import s05 as s05_generator

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.mark.bench_qa
def test_s05_fixture_matches_generator_output():
    committed_path = _FIXTURES_DIR / "s05.json"
    committed = committed_path.read_text(encoding="utf-8")
    generated = s05_generator.canonical_dump(s05_generator.build_fixture())
    assert generated == committed, (
        "s05 fixture drifted from its generator output. "
        "If the drift is intentional, regenerate the fixture with:\n\n"
        "    uv run python -m tests.bench.fixtures._generators.s05\n\n"
        "...and commit tests/bench/fixtures/s05.json alongside the generator edit. "
        "If you hand-edited the fixture, either revert the edit or mirror the "
        "change into the generator so both stay in sync. "
        "(Byte-level compare uses canonical JSON: sort_keys=True, indent=2, "
        "ensure_ascii=False, trailing newline — see "
        "tests/bench/fixtures/_generators/s05.py::canonical_dump.)"
    )
