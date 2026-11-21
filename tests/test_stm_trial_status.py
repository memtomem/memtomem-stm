"""``scripts/stm_trial.py --status``: progress toward the frozen target, outcome-free."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from stm_trial_fixtures import DAY, _extract, make_env, st

START = 2_000_000.0
FROZEN = START + 8 * DAY


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    env = make_env(tmp_path, monkeypatch)
    _extract(env, now=lambda: START)
    st.freeze(
        env["trial"],
        env["feedback"],
        holdout_rate=0.2,
        target=10,
        min_events=0,
        now=lambda: FROZEN,
    )
    return env


def _insert(env: dict[str, Path], sql: str, rows: list[tuple[Any, ...]]) -> None:
    with sqlite3.connect(env["trial"]) as db:
        db.executemany(sql, rows)


def _assign(
    env: dict[str, Path],
    event: str,
    created_at: float,
    rate: float | None,
    eligible: list[int],
) -> None:
    _insert(
        env,
        "INSERT INTO assignments (event_key, created_at, arm, holdout_rate, memory_keys)"
        " VALUES (?, ?, 'shown', ?, '[]')",
        [(event, created_at, rate)],
    )
    _insert(
        env,
        "INSERT INTO assignment_memories (event_key, memory_key, eligible, snippet_grams)"
        " VALUES (?, ?, ?, '[]')",
        [(event, f"{event}-m{i}", flag) for i, flag in enumerate(eligible)],
    )


def _run(
    env: dict[str, Path],
    started: float,
    ended: float | None,
    retention: int = 90,
    cleanup: int = 30,
) -> None:
    _insert(
        env,
        "INSERT INTO coverage (started_at, ended_at, cleanup_period_days, stats_retention_days,"
        " key_fingerprint) VALUES (?, ?, ?, ?, 'x')",
        [(started, ended, cleanup, retention)],
    )


def _status(env: dict[str, Path], at: float = FROZEN + DAY) -> dict[str, Any]:
    return st.status(env["trial"], env["feedback"], now=lambda: at)


def test_counts_follow_the_stopping_unit(env: dict[str, Path]) -> None:
    _assign(env, "a", FROZEN + 10, 0.2, [1, 1, 0])  # two eligible rows, one not
    _assign(env, "at", FROZEN, 0.2, [1])  # exactly at frozen_at counts
    _assign(env, "rate", FROZEN + 20, 0.5, [1])
    _assign(env, "null", FROZEN + 30, None, [1])
    _assign(env, "early", FROZEN - 1, 0.2, [1])
    _assign(env, "early0", FROZEN - 5, 0.2, [0])  # no eligible row, still pre-freeze
    out = _status(env)
    assert out["assigned_events"] == 6
    assert out["eligible_rows"] == 6
    assert out["rate_excluded_rows"] == 2  # a different rate and a NULL rate
    assert out["analysable_candidates"] == 3  # "a" x2 + "at"; "early" is before the freeze
    assert out["pre_freeze_events"] == 2
    assert out["progress"] == 0.3
    assert out["target_count"] == 10 and out["holdout_rate"] == 0.2
    assert any("predate frozen_at" in n for n in out["notes"])


def test_assignment_times_are_days_only(env: dict[str, Path]) -> None:
    _assign(env, "a", FROZEN + 10, 0.2, [1])
    out = _status(env)
    day = st.datetime.fromtimestamp(FROZEN + 10, st.UTC).date().isoformat()
    assert out["first_assignment_day"] == out["last_assignment_day"] == day


def test_coverage_facts_are_raw_and_post_freeze(env: dict[str, Path]) -> None:
    # START (the env's own run) → r1 is the largest gap but lies wholly before the
    # freeze, and r1's retention (45) is recorded before it: neither may show up
    r1 = START + 7.5 * DAY
    _run(env, r1, r1 + 60, retention=45)
    # r1 → r2 straddles the freeze and holds both maxima: observed 3.5 d, worst 3.75 d
    _run(env, FROZEN + 3 * DAY, FROZEN + 3.25 * DAY)
    _run(env, FROZEN + 4 * DAY, FROZEN + 4.1 * DAY, retention=30)  # 0.75 d / 1.1 d
    _run(env, FROZEN + 6 * DAY, None)  # unfinished
    out = _status(env, at=FROZEN + 9 * DAY)
    cov = out["coverage"]
    assert cov["completed_runs"] == 4 and cov["unfinished_runs"] == 1
    assert cov["newest_unfinished_start"] == st._iso(FROZEN + 6 * DAY)
    assert cov["last_completed_run"] == st._iso(FROZEN + 4.1 * DAY)
    assert cov["max_observed_gap_days_since_freeze"] == 3.5
    assert cov["max_worst_case_gap_days_since_freeze"] == 3.75
    assert cov["worst_case_days_since_last_copy"] == 5.0
    assert cov["recorded_stats_retention_days"] == [30, 90]
    assert cov["recorded_cleanup_period_days"] == [30]
    assert any("never finished" in n for n in out["notes"])
    assert not any("cleanupPeriodDays" in n for n in out["notes"])
    # no verdict is drawn from these facts
    assert not {"target_reached", "count_confirmed"} & (set(out) | set(cov))


def test_stale_completed_run_is_noted(env: dict[str, Path]) -> None:
    out = _status(env, at=START + 30 * DAY)
    assert out["coverage"]["days_since_last_completed_run"] == 30.0
    assert any("days ago" in n for n in out["notes"])
    assert not any("days ago" in n for n in _status(env, at=START + DAY)["notes"])


def test_overlapping_runs_report_the_uncovered_gap_and_no_worst_case(
    env: dict[str, Path],
) -> None:
    _run(env, FROZEN - DAY / 10, FROZEN - DAY / 10 + 60)  # keeps the straddling gap short
    _run(env, FROZEN + 1 * DAY, FROZEN + 11 * DAY)  # a long run ...
    _run(env, FROZEN + 3 * DAY, FROZEN + 4 * DAY)  # ... with a short one nested inside
    _run(env, FROZEN + 12 * DAY, FROZEN + 13 * DAY)
    out = _status(env, at=FROZEN + 13.5 * DAY)
    cov = out["coverage"]
    # uncovered: 1.1 d before the long run and 1 d after it — not the 8 d from
    # the nested run's end, and never negative
    assert cov["max_observed_gap_days_since_freeze"] == st._days(1.1 * DAY - 60)
    assert cov["max_worst_case_gap_days_since_freeze"] is None
    assert any("overlap" in n for n in out["notes"])
    assert cov["worst_case_days_since_last_copy"] == 1.5


def test_an_overlapping_only_pair_is_a_zero_gap_never_negative(env: dict[str, Path]) -> None:
    _run(env, START + DAY, FROZEN - DAY)  # ends before the freeze: its pair is skipped
    _run(env, FROZEN - 2 * DAY, FROZEN + DAY)  # starts inside it
    assert _status(env)["coverage"]["max_observed_gap_days_since_freeze"] == 0.0


def test_with_overlap_the_observed_gap_still_feeds_the_transcript_note(
    env: dict[str, Path],
) -> None:
    _run(env, FROZEN - DAY / 10, FROZEN - DAY / 10 + 60)
    _run(env, FROZEN + 3 * DAY, FROZEN + 5 * DAY, cleanup=2)  # ~3.1 d after the last run
    _run(env, FROZEN + 4 * DAY, FROZEN + 4.5 * DAY)  # nested: no worst case is given
    out = _status(env, at=FROZEN + 5.5 * DAY)  # 1.5 d since the last copy: below 2
    assert out["coverage"]["max_worst_case_gap_days_since_freeze"] is None
    assert any("cleanupPeriodDays (2)" in n for n in out["notes"])


def test_disjoint_runs_raise_no_overlap_note(env: dict[str, Path]) -> None:
    _run(env, FROZEN + DAY, FROZEN + DAY + 60)
    assert not any("overlap" in n for n in _status(env, at=FROZEN + 2 * DAY)["notes"])


def test_a_run_ending_exactly_at_the_freeze_closes_a_counted_gap(env: dict[str, Path]) -> None:
    _run(env, FROZEN - DAY, FROZEN)
    cov = _status(env)["coverage"]
    assert cov["max_worst_case_gap_days_since_freeze"] == 8.0  # START → FROZEN
    assert cov["max_observed_gap_days_since_freeze"] == 7.0


@pytest.mark.parametrize(("since_run", "noted"), [(2.5 * DAY, True), (2 * DAY, False)])
def test_an_open_interval_longer_than_the_transcript_clock_is_noted(
    env: dict[str, Path], since_run: float, noted: bool
) -> None:
    _run(env, FROZEN - DAY / 2, FROZEN - DAY / 2 + 60)  # keeps the straddling pair short
    _run(env, FROZEN + DAY, FROZEN + DAY + 60, cleanup=2)  # worst-case pair: 1.5 d + 60 s
    out = _status(env, at=FROZEN + DAY + since_run)
    assert any("cleanupPeriodDays (2)" in n for n in out["notes"]) is noted


@pytest.mark.parametrize(("gap", "noted"), [(2.5 * DAY, True), (1.5 * DAY, False)])
def test_a_historical_gap_longer_than_the_shortest_transcript_clock_is_noted(
    env: dict[str, Path], gap: float, noted: bool
) -> None:
    _assign(env, "a", FROZEN + 10, 0.2, [1])
    _run(env, FROZEN - DAY / 2, FROZEN - DAY / 2 + 60)  # keeps the straddling pair short
    first = FROZEN + DAY / 10
    _run(env, first, first + 60, cleanup=5)  # a 2.5 d gap is inside this setting ...
    later = first + gap - 60  # worst case from that run's start = gap
    _run(env, later, later + 60, cleanup=2)  # ... but not the shortest one
    out = _status(env, at=later + DAY)  # the open interval stays below 2 d
    assert out["coverage"]["recorded_cleanup_period_days"] == [2, 5]
    assert any("cleanupPeriodDays (2)" in n for n in out["notes"]) is noted
    assert out["analysable_candidates"] == 1  # a note, never an exclusion


TOP_KEYS = {
    "frozen_at",
    "holdout_rate",
    "target_count",
    "assigned_events",
    "pre_freeze_events",
    "eligible_rows",
    "rate_excluded_rows",
    "analysable_candidates",
    "progress",
    "first_assignment_day",
    "last_assignment_day",
    "coverage",
    "notes",
}
COVERAGE_KEYS = {
    "completed_runs",
    "unfinished_runs",
    "newest_unfinished_start",
    "last_completed_run",
    "days_since_last_completed_run",
    "worst_case_days_since_last_copy",
    "max_observed_gap_days_since_freeze",
    "max_worst_case_gap_days_since_freeze",
    "recorded_stats_retention_days",
    "recorded_cleanup_period_days",
}
# every (table, column) the mode may read; "" is a bare COUNT(*)
READABLE = (
    {("trial_record", c) for c in ("frozen_at", "holdout_rate", "target_count", "key_fingerprint")}
    | {("assignments", c) for c in ("event_key", "created_at", "holdout_rate", "")}
    | {("assignment_memories", c) for c in ("event_key", "eligible")}
    | {
        ("coverage", c)
        for c in ("run_id", "started_at", "ended_at", "stats_retention_days", "cleanup_period_days")
    }
)


def test_status_reads_no_outcome_and_nothing_per_arm(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _assign(env, "a", FROZEN + 10, 0.2, [1])

    def boom(*_: object, **__: object) -> None:
        raise AssertionError("status must not load outcome inputs")

    monkeypatch.setattr(st, "load_resolve_inputs", boom)
    reads: set[tuple[str, str]] = set()
    real = sqlite3.connect

    def watch(action: int, arg1: str | None, arg2: str | None, *_: object) -> int:
        if action == sqlite3.SQLITE_READ:
            reads.add((arg1 or "", arg2 or ""))
        return sqlite3.SQLITE_OK

    key_reads: set[tuple[str, str]] = set()

    def watch_key(action: int, arg1: str | None, arg2: str | None, *_: object) -> int:
        if action == sqlite3.SQLITE_READ:
            key_reads.add((arg1 or "", arg2 or ""))
        return sqlite3.SQLITE_OK

    def connect(target: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = real(target, *args, **kwargs)
        conn.set_authorizer(watch if env["trial"].name in str(target) else watch_key)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    out = _status(env)
    assert set(out) == TOP_KEYS
    assert set(out["coverage"]) == COVERAGE_KEYS
    assert reads and reads <= READABLE, sorted(reads - READABLE)
    # the feedback store is opened for the key only
    assert key_reads and key_reads <= {("stm_meta", "name"), ("stm_meta", "value")}, key_reads
    assert "shown" not in json.dumps(out) and "withheld" not in json.dumps(out)


def test_every_connection_is_read_only(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    targets: list[str] = []
    flags: list[object] = []
    real = sqlite3.connect

    def spy(target: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        targets.append(str(target))
        flags.append(kwargs.get("uri"))
        return real(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    before = {
        p: (hashlib.sha256(p.read_bytes()).hexdigest(), stat.S_IMODE(p.stat().st_mode))
        for p in (env["trial"], env["feedback"])
    }
    _status(env)
    assert len(targets) == 2  # the key read and the trial snapshot
    assert all(t.startswith("file:") and t.endswith("?mode=ro") for t in targets), targets
    assert flags == [True, True]
    after = {
        p: (hashlib.sha256(p.read_bytes()).hexdigest(), stat.S_IMODE(p.stat().st_mode))
        for p in (env["trial"], env["feedback"])
    }
    assert before == after


class _Interleaving:
    """A connection that lets a second connection commit right after the record read."""

    def __init__(self, inner: sqlite3.Connection, trial: Path) -> None:
        self.inner, self.trial, self.committed = inner, trial, False

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        cursor = self.inner.execute(sql, *args)
        if "FROM trial_record" in sql and not self.committed:
            with closing(sqlite3.connect(self.trial)) as writer, writer:
                writer.execute(
                    "INSERT INTO assignments (event_key, created_at, arm, holdout_rate,"
                    " memory_keys) VALUES ('late', ?, 'shown', 0.2, '[]')",
                    (FROZEN + 50,),
                )
                writer.execute(
                    "INSERT INTO assignment_memories"
                    " (event_key, memory_key, eligible, snippet_grams)"
                    " VALUES ('late', 'late-m0', 1, '[]')"
                )
                writer.execute(
                    "INSERT INTO coverage (started_at, ended_at, cleanup_period_days,"
                    " stats_retention_days, key_fingerprint) VALUES (?, ?, 30, 90, 'x')",
                    (FROZEN + 60, FROZEN + 70),
                )
            self.committed = True
        return cursor

    def close(self) -> None:
        self.inner.close()


def test_counts_come_from_one_snapshot(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with sqlite3.connect(env["trial"]) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    _assign(env, "a", FROZEN + 10, 0.2, [1])
    real = sqlite3.connect
    made: list[_Interleaving] = []

    def connect(target: Any, *args: Any, **kwargs: Any) -> Any:
        conn = real(target, *args, **kwargs)
        # the mode's own URI connection, not the test writer's plain-path one
        if str(target).startswith("file:") and env["trial"].name in str(target):
            made.append(_Interleaving(conn, env["trial"]))
            return made[-1]
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    out = _status(env)
    assert made and made[0].committed  # the writer did land between the reads
    assert out["assigned_events"] == 1  # read before the commit ...
    assert out["eligible_rows"] == 1  # ... and so is every later count
    assert out["analysable_candidates"] == 1
    assert out["coverage"]["completed_runs"] == 1  # the coverage read too
    monkeypatch.setattr(sqlite3, "connect", real)
    later = _status(env)  # the rows are there for the next reading
    assert (later["eligible_rows"], later["coverage"]["completed_runs"]) == (2, 2)


def _main(env: dict[str, Path], *extra: str) -> int:
    return int(
        st.main(
            ["--status", "--trial-db", str(env["trial"]), "--feedback-db", str(env["feedback"])]
            + list(extra)
        )
    )


def _refused(env: dict[str, Path], capsys: pytest.CaptureFixture[str], text: str) -> None:
    assert _main(env) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("stm_trial: refused: ") and text in captured.err


def test_main_prints_one_json_object(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _main(env) == 0
    assert json.loads(capsys.readouterr().out)["target_count"] == 10


def test_refuses_before_the_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = make_env(tmp_path, monkeypatch)
    _extract(env, now=lambda: START)
    _refused(env, capsys, "not frozen")


def test_refuses_a_missing_trial_db_without_creating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = make_env(tmp_path, monkeypatch)
    _refused(env, capsys, "no trial database")
    assert not env["trial"].exists() and not env["trial"].parent.exists()


def test_refuses_another_key(
    env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = make_env(tmp_path / "other", monkeypatch)
    env = {**env, "feedback": other["feedback"]}
    _refused(env, capsys, "HMAC key differs")


def test_an_uninspectable_path_is_a_refusal(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = Path.exists
    trial = env["trial"]

    def exists(self: Path, *args: Any, **kwargs: Any) -> bool:
        if self == trial:
            raise PermissionError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists)
    _refused(env, capsys, "cannot inspect the database paths (PermissionError)")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need a privilege on Windows")
def test_a_symlink_loop_is_a_refusal_that_names_no_path(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    env["trial"].unlink()
    env["trial"].symlink_to(env["trial"])
    assert _main(env) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("stm_trial: refused: ")
    assert str(env["trial"].parent) not in captured.err


def test_unreadable_trial_db_is_a_refusal(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    env["trial"].write_bytes(b"not a database" * 100)
    _refused(env, capsys, "cannot read the trial database (DatabaseError)")


def test_status_is_its_own_mode(env: dict[str, Path]) -> None:
    with pytest.raises(SystemExit) as raised:
        _main(env, "--freeze")
    assert raised.value.code == 2
