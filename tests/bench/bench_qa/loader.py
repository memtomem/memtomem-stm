"""Fixture loader with strict schema_version enforcement."""

from __future__ import annotations

import json
from pathlib import Path

from .schema import FIXTURE_SCHEMA_VERSION, BenchFixture


def fixtures_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "fixtures"


def load_fixture(scenario_id: str) -> BenchFixture:
    path = fixtures_dir() / f"{scenario_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"bench_qa fixture missing: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    version = raw.get("schema_version")
    if version != FIXTURE_SCHEMA_VERSION:
        raise AssertionError(
            f"fixture {scenario_id} has schema_version={version}; "
            f"expected {FIXTURE_SCHEMA_VERSION}. Migrate the fixture before running bench_qa."
        )
    if raw.get("scenario_id") != scenario_id:
        raise AssertionError(
            f"fixture {path} declares scenario_id={raw.get('scenario_id')!r}; "
            f"file name implies {scenario_id!r}"
        )
    multiplier = int(raw.get("payload_multiplier", 1))
    if multiplier < 1:
        raise AssertionError(f"fixture {scenario_id}: payload_multiplier={multiplier} must be >= 1")
    if multiplier > 1:
        raw["payload"] = raw["payload"] * multiplier
    return raw  # type: ignore[return-value]
