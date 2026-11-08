"""``MetricsStore.get_tool_profiles`` as one statement (#1348).

It used to run ``1 + 2G`` statements for ``G`` ``(server, tool)`` groups, each
of the extra two re-scanning the window. The single statement must return what
those per-group statements returned; ``_per_group_profiles`` below is that
implementation as the oracle — the same predicates and expressions, with the
aggregation's column aliases dropped and whitespace reflowed. Its strategy
``ORDER BY`` is unchanged and names no
tie-break; on a count tie it returned the label that sorts last (every one of 765
tied groups measured on SQLite 3.47), and the new statement makes that the rule,
so the comparison also checks that claim on each SQLite build CI runs.
"""

from __future__ import annotations

import random
import sqlite3
import time

import pytest

from memtomem_stm.proxy.metrics_store import (
    METADATA_BYPASS_STRATEGY,
    STRUCTURED_BYPASS_STRATEGY,
    MetricsStore,
)

_EXCLUDED = (METADATA_BYPASS_STRATEGY, STRUCTURED_BYPASS_STRATEGY)


def _per_group_profiles(db: sqlite3.Connection, cutoff: float) -> list[dict]:
    rows = db.execute(
        """
        SELECT server, tool, COUNT(*), SUM(ratio_violation),
            AVG(CASE WHEN cleaned_chars > 0 AND is_error = 0
                      AND compression_accounting = 'initial_response_v1'
                     THEN CAST(compressed_chars AS REAL) / cleaned_chars END),
            SUM(CASE WHEN cleaned_chars > 0 AND is_error = 0
                      AND compression_accounting = 'initial_response_v1'
                     THEN 1 ELSE 0 END),
            SUM(is_error)
        FROM proxy_metrics
        WHERE created_at >= ? AND source = 'mcp'
            AND compression_strategy IS NOT ? AND compression_strategy IS NOT ?
        GROUP BY server, tool
        """,
        (cutoff, *_EXCLUDED),
    ).fetchall()
    profiles = []
    for server, tool, calls, violations, avg_ratio, ratio_count, errors in rows:
        p95_row = db.execute(
            """
            SELECT original_chars FROM proxy_metrics
            WHERE server = ? AND tool = ? AND created_at >= ? AND source = 'mcp'
                AND compression_strategy IS NOT ? AND compression_strategy IS NOT ?
            ORDER BY original_chars ASC
            LIMIT 1 OFFSET MAX(0, CAST(
                (SELECT COUNT(*) FROM proxy_metrics
                 WHERE server = ? AND tool = ? AND created_at >= ? AND source = 'mcp'
                     AND compression_strategy IS NOT ? AND compression_strategy IS NOT ?)
                * 0.95 AS INTEGER) - 1)
            """,
            (server, tool, cutoff, *_EXCLUDED, server, tool, cutoff, *_EXCLUDED),
        ).fetchone()
        strat_row = db.execute(
            """
            SELECT
                CASE WHEN instr(compression_strategy, '→') > 0
                     THEN substr(compression_strategy, 1,
                                 instr(compression_strategy, '→') - 1)
                     ELSE compression_strategy END AS selected_strategy,
                COUNT(*),
                (SELECT COUNT(*) FROM proxy_metrics
                 WHERE server = ? AND tool = ? AND created_at >= ?
                     AND compression_strategy IS NOT NULL
                     AND compression_strategy IS NOT ? AND compression_strategy IS NOT ?
                     AND strategy_auto_selected = 1 AND source = 'mcp')
            FROM proxy_metrics
            WHERE server = ? AND tool = ? AND created_at >= ?
                AND compression_strategy IS NOT NULL
                AND compression_strategy IS NOT ? AND compression_strategy IS NOT ?
                AND strategy_auto_selected = 1 AND source = 'mcp'
            GROUP BY selected_strategy
            ORDER BY COUNT(*) DESC
            LIMIT 1
            """,
            (server, tool, cutoff, *_EXCLUDED, server, tool, cutoff, *_EXCLUDED),
        ).fetchone()
        profiles.append(
            {
                "server": server,
                "tool": tool,
                "call_count": calls,
                "violation_count": violations or 0,
                "avg_ratio": round(avg_ratio, 4) if avg_ratio is not None else None,
                "ratio_count": ratio_count or 0,
                "p95_original_chars": p95_row[0] if p95_row else 0,
                "auto_dominant_strategy": strat_row[0] if strat_row else None,
                "auto_dominant_strategy_count": strat_row[1] if strat_row else 0,
                "auto_strategy_count": strat_row[2] if strat_row else 0,
                "error_count": errors or 0,
            }
        )
    return profiles


_STRATEGIES = [
    None,
    "hybrid",
    "hybrid→progressive_fallback",
    "truncate",
    "truncate→hybrid→progressive_fallback",
    "selective",
    METADATA_BYPASS_STRATEGY,
    STRUCTURED_BYPASS_STRATEGY,
]


def _seed(store: MetricsStore, rng: random.Random, n: int, now: float) -> None:
    assert store._db is not None
    rows = []
    for _ in range(n):
        rows.append(
            (
                f"s{rng.randrange(4)}",
                f"t{rng.randrange(3)}",
                rng.choice([0, 10, 10, 500, rng.randrange(1, 100_000)]),
                rng.randrange(0, 5_000),
                rng.choice([0, 0, rng.randrange(1, 10_000)]),
                now - rng.uniform(0, 7_200),  # half of them outside a 1h window
                rng.choice(["mcp", "mcp", "mcp", "hook"]),
                rng.choice([0, 0, 0, 1]),
                rng.choice(["initial_response_v1", "initial_structured_v1", None]),
                rng.choice(_STRATEGIES),
                rng.choice([None, 0, 1, 1]),
                rng.choice([0, 0, 1]),
            )
        )
    store._db.executemany(
        "INSERT INTO proxy_metrics (server, tool, original_chars, compressed_chars, "
        "cleaned_chars, created_at, source, is_error, compression_accounting, "
        "compression_strategy, strategy_auto_selected, ratio_violation) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    store._db.commit()


@pytest.fixture
def store(tmp_path):
    s = MetricsStore(tmp_path / "metrics.db", max_history=1_000_000)
    s.initialize()
    yield s
    s.close()


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("n", [1, 7, 60, 400])
def test_matches_the_per_group_statements(store, monkeypatch, seed, n):
    now = time.time()
    _seed(store, random.Random(seed * 1000 + n), n, now)
    assert store._db is not None
    # Freeze the clock so both sides read the same window.
    monkeypatch.setattr("memtomem_stm.proxy.metrics_store.time.time", lambda: now)

    got = store.get_tool_profiles(since_seconds=3_600.0)

    expected = _per_group_profiles(store._db, now - 3_600.0)
    # Ordered lists of ordered items: list order and key order are output too.
    assert [list(p.items()) for p in got] == [list(p.items()) for p in expected]


def test_the_comparison_covers_every_field_shape(tmp_path):
    """Positive control for the differential test: across its seeds and sizes
    the seeded data reaches every branch whose equivalence it claims."""
    seen: list[dict] = []
    for seed in range(12):
        for n in (1, 7, 60, 400):
            store = MetricsStore(tmp_path / f"m-{seed}-{n}.db", max_history=1_000_000)
            store.initialize()
            try:
                now = time.time()
                _seed(store, random.Random(seed * 1000 + n), n, now)
                assert store._db is not None
                seen.extend(_per_group_profiles(store._db, now - 3_600.0))
            finally:
                store.close()

    assert any(p["auto_dominant_strategy"] is None for p in seen)
    assert any(p["auto_dominant_strategy"] == "hybrid" for p in seen), "arrow folding"
    assert any(p["auto_strategy_count"] > p["auto_dominant_strategy_count"] > 0 for p in seen)
    assert any(p["avg_ratio"] is None for p in seen)
    assert any(p["avg_ratio"] is not None for p in seen)
    assert any(p["call_count"] == 1 for p in seen)
    assert any(p["call_count"] >= 20 for p in seen)
    assert any(p["violation_count"] for p in seen)
    assert any(p["error_count"] for p in seen)


def test_a_strategy_tie_goes_to_the_label_that_sorts_last(store):
    assert store._db is not None
    now = time.time()
    for strategy in ("truncate", "hybrid→progressive_fallback", "truncate", "hybrid"):
        store._db.execute(
            "INSERT INTO proxy_metrics (server, tool, original_chars, compressed_chars, "
            "cleaned_chars, created_at, source, is_error, compression_strategy, "
            "strategy_auto_selected) VALUES ('s', 't', 10, 5, 10, ?, 'mcp', 0, ?, 1)",
            (now, strategy),
        )
    store._db.commit()

    [profile] = store.get_tool_profiles(since_seconds=3_600.0)

    assert profile["auto_dominant_strategy"] == "truncate"
    assert profile["auto_dominant_strategy_count"] == 2
    assert profile["auto_strategy_count"] == 4


def test_uses_one_statement_regardless_of_group_count(store):
    rng = random.Random(3)
    _seed(store, rng, 400, time.time())
    statements: list[str] = []
    assert store._db is not None
    store._db.set_trace_callback(statements.append)
    try:
        profiles = store.get_tool_profiles(since_seconds=3_600.0)
    finally:
        store._db.set_trace_callback(None)

    assert len(profiles) >= 10, "positive control: many groups"
    assert len(statements) == 1, statements
