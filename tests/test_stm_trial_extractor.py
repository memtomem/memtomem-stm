"""The holdout-trial extractor (``scripts/stm_trial.py``).

The extractor copies what the trial's offline analysis needs out of Claude Code
transcripts (pruned after ``cleanupPeriodDays``) and ``stm_feedback.db`` into a
pseudonymous ``stm_trial.db``. These tests pin that its hashes join with what
collection stored, that it is incremental and idempotent, that nothing raw
reaches the trial DB, and the refusals of the key pin and ``--freeze``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from memtomem_stm.cli.hook_adapter import READLIKE_SURFACE_TOOLS
from memtomem_stm.cli.hook_cmd import _SURFACED_CLOSE, _SURFACED_OPEN
from memtomem_stm.surfacing.feedback_store import (
    FeedbackStore,
    OpportunityRow,
    load_hmac_key,
)
from memtomem_stm.surfacing.grams import _PLATFORM_CASEFOLD, gram_hashes, keyed_hash, path_key

from stm_trial_fixtures import (
    AGENT,
    DAY,
    EVENT_ID,
    MEMORY_ID,
    NOTE,
    SESSION,
    _block,
    _extract,
    _key,
    _main_records,
    _record_event,
    _rows,
    _sub_records,
    _text,
    _ts,
    _write,
    make_env,
    st,
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    return make_env(tmp_path, monkeypatch)


_CONTENT_TABLES = (
    "ledger",
    "entry_paths",
    "output_grams",
    "injection_grams",
    "assignments",
    "assignment_memories",
    "assignment_opportunities",
    "streams",
)


def _dump(db_path: Path) -> dict[str, list[tuple[Any, ...]]]:
    return {t: sorted(_rows(db_path, f"SELECT * FROM {t}"), key=repr) for t in _CONTENT_TABLES}


# ── contract constants ─────────────────────────────────────────────────


def test_wrapper_and_tool_names_match_the_hook() -> None:
    assert (st.SURFACED_OPEN, st.SURFACED_CLOSE) == (_SURFACED_OPEN, _SURFACED_CLOSE)
    assert set(st.CANONICAL_TOOLS.values()) == set(READLIKE_SURFACE_TOOLS)


# ── entry paths ────────────────────────────────────────────────────────


def _n(path: str) -> str:
    """A POSIX-literal expectation in this platform's lexical form (``\\`` on Windows)."""
    return os.path.normpath(path)


def _paths(tool: str, tool_input: dict[str, Any], cwd: str | None = "/r") -> set[str]:
    return st.entry_path_keys(tool, tool_input, cwd, casefold=False)[0]


def test_read_path_absolute_and_relative() -> None:
    assert _paths("read", {"file_path": _n("/a/B.md")}) == {_n("/a/B.md")}
    assert _paths("read", {"file_path": "sub/../B.md"}) == {_n("/r/B.md")}
    assert _paths("read", {"file_path": "B.md"}, cwd=None) == set()


def test_bash_cumulative_cd_and_token_shapes() -> None:
    keys = _paths("shell", {"command": "cd sub && cd child && cat ../x.md"})
    assert _n("/r/sub/x.md") in keys  # the cumulative directory, as the shell resolves it
    assert _n("/r/x.md") in keys  # against cwd too (a cd can fail)
    keys = _paths("shell", {"command": "grep -n foo --file=/a/y.md /a/z.py:12:3 >/a/out.md"})
    assert {_n("/a/y.md"), _n("/a/z.py"), _n("/a/out.md")} <= keys


def test_bash_cd_applies_only_to_later_tokens() -> None:
    keys = _paths("shell", {"command": "cat x.md && cd sub"})
    assert _n("/r/x.md") in keys
    assert _n("/r/sub/x.md") not in keys  # the read happened before the cd
    assert _n("/r/sub/sub") not in keys  # a cd's target resolves against the dirs before it


def test_bash_expands_only_a_bare_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/me")
    keys = _paths("shell", {"command": "cat ~/n.md ~other/n.md"})
    assert _n("/home/me/n.md") in keys
    assert not any(k.startswith(_n("/home/other")) for k in keys)
    assert _n("/r/~other/n.md") in keys


def test_grep_path_defaults_to_cwd_and_pattern_tokens() -> None:
    paths, patterns = st.entry_path_keys(
        "grep", {"pattern": r"notfoo\.py|Bar", "glob": "**/*.md"}, "/R", casefold=True
    )
    assert paths == {_n("/r")}
    assert "notfoo.py" in patterns and "bar" in patterns
    assert "foo.py" not in patterns  # a substring never matches


def test_casefold_is_passed_through() -> None:
    assert _paths("read", {"file_path": _n("/A/B.md")}) == {_n("/A/B.md")}
    assert st.entry_path_keys("read", {"file_path": _n("/A/B.md")}, None, casefold=True)[0] == {
        _n("/a/b.md")
    }


def test_entry_hashes_join_with_collection_hashes(env: dict[str, Path]) -> None:
    _record_event(env, EVENT_ID, arm="withheld")
    key = _key(env)
    ((_, _, _, lexical, dirs, basename, _),) = _rows(
        env["feedback"], "SELECT * FROM surfacing_memory_paths"
    )
    read = st.entry_path_keys("read", {"file_path": NOTE}, None, casefold=_PLATFORM_CASEFOLD)[0]
    bash = st.entry_path_keys(
        "shell",
        {"command": "cd notes && cat Alpha.md"},
        "/Users/tester",
        casefold=_PLATFORM_CASEFOLD,
    )[0]
    grep_paths, grep_tokens = st.entry_path_keys(
        "grep", {"pattern": "Alpha.md", "path": "/Users/tester"}, None, casefold=_PLATFORM_CASEFOLD
    )
    assert {keyed_hash(k, key) for k in read} == {lexical}
    assert lexical in {keyed_hash(k, key) for k in bash}
    assert {keyed_hash(k, key) for k in grep_paths} <= set(json.loads(dirs))
    assert basename in {keyed_hash(t, key) for t in grep_tokens}
    assert path_key(NOTE) in read


# ── injections ─────────────────────────────────────────────────────────


def test_parse_injection_stm_block() -> None:
    parsed = st.parse_injection(
        [_block(EVENT_ID, r"- **n/a.md**: snake\_case word\_two three four")]
    )
    assert parsed.event_id == EVENT_ID
    assert parsed.stm_wrapped is True
    assert parsed.header_sha256 == hashlib.sha256(b"## Relevant Memories").hexdigest()
    assert "snake_case word_two" in parsed.text  # formatter escapes undone
    assert _SURFACED_CLOSE not in parsed.text


@pytest.mark.parametrize("header", ["", "\n## Two-line header"])
def test_parse_injection_keeps_an_empty_first_header_line(header: str) -> None:
    block = f"{_SURFACED_OPEN}\n{header}\n> preamble\n- bullet\n{_SURFACED_CLOSE}"
    parsed = st.parse_injection([block])
    assert parsed.header_sha256 == hashlib.sha256(b"").hexdigest()


def test_parse_injection_foreign_record() -> None:
    parsed = st.parse_injection(["Other hook\nliteral \\_x"])
    assert (parsed.stm_wrapped, parsed.event_id) == (False, None)
    assert parsed.header_sha256 == hashlib.sha256(b"Other hook").hexdigest()
    assert "\\_x" in parsed.text  # never unsanitized


# ── extraction ─────────────────────────────────────────────────────────


def test_extraction_rows_and_keys(env: dict[str, Path]) -> None:
    summary = _extract(env)
    key = _key(env)
    main_key = st.stream_key(SESSION, None, key)
    sub_key = st.stream_key(SESSION, AGENT, key)
    assert (summary.files_read, summary.files_skipped) == (2, 0)
    assert sorted(_rows(env["trial"], "SELECT stream_key, is_subagent FROM streams")) == sorted(
        [(main_key, 0), (sub_key, 1)]
    )
    ledger = {
        row[0]: row[1:]
        for row in _rows(
            env["trial"],
            "SELECT call_key, stream_key, ordinal, tool, eligible, ok, message_key FROM ledger",
        )
    }
    assert ledger[keyed_hash("toolu_A", key)] == (
        main_key,
        1,
        "shell",
        1,
        1,
        keyed_hash("msg_A", key),
    )
    assert ledger[keyed_hash("toolu_B", key)][2:4] == ("read", 1)
    assert ledger[keyed_hash("toolu_C", key)][2:5] == ("other", 0, 0)  # indexed, not an entry
    assert ledger[keyed_hash("toolu_S", key)][:2] == (sub_key, 0)
    ((event_key, call_key, stream, wrapped, grams),) = _rows(
        env["trial"],
        "SELECT event_key, call_key, stream_key, stm_wrapped, grams FROM injection_grams",
    )
    assert (event_key, call_key, stream, wrapped) == (
        keyed_hash(EVENT_ID, key),
        keyed_hash("toolu_A", key),
        main_key,
        1,
    )
    assert gram_hashes("alpha beta gamma delta", key) <= set(st.unpack_grams(grams))
    outputs = _rows(
        env["trial"], "SELECT ordinal, grams FROM output_grams WHERE stream_key = ?", (main_key,)
    )
    text_grams = dict(outputs)[8]
    assert gram_hashes("reuse alpha beta gamma", key) <= set(st.unpack_grams(text_grams))


def test_stream_times_are_recorded(env: dict[str, Path]) -> None:
    _extract(env)
    key = _key(env)
    main_key = st.stream_key(SESSION, None, key)
    first, newest = _rows(
        env["trial"], "SELECT first_ts, newest_ts FROM streams WHERE stream_key = ?", (main_key,)
    )[0]
    assert (first, newest) == (st._parse_ts(_ts(0)), st._parse_ts(_ts(9)))
    assert _rows(
        env["trial"], "SELECT newest_ts FROM coverage_streams WHERE stream_key = ?", (main_key,)
    ) == [(newest,)]


def test_output_grams_never_span_two_blocks(env: dict[str, Path]) -> None:
    record = _text("u9", "msg_E", "one two alpha beta", 10, "/r")
    record["message"]["content"].append({"type": "text", "text": "gamma delta three four"})
    _write(env["main"], [record], mode="a")
    _extract(env)
    key = _key(env)
    (grams,) = _rows(env["trial"], "SELECT grams FROM output_grams WHERE ordinal = 9")[0]
    stored = set(st.unpack_grams(grams))
    assert gram_hashes("one two alpha beta", key) <= stored
    assert not gram_hashes("alpha beta gamma delta", key) & stored


def test_second_run_changes_nothing(env: dict[str, Path]) -> None:
    _extract(env)
    first = _dump(env["trial"])
    summary = _extract(env)
    assert summary.files_read == 0
    assert _dump(env["trial"]) == first


def test_appending_in_steps_equals_one_pass(env: dict[str, Path], tmp_path: Path) -> None:
    records = _main_records()
    env["main"].unlink()
    _write(env["main"], records[:3])
    _extract(env)
    with env["main"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(records[3]) + "\n" + json.dumps(records[4])[:20])  # partial line
    _extract(env)
    with env["main"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(records[4])[20:] + "\n")
        for record in records[5:]:
            handle.write(json.dumps(record) + "\n")
    _extract(env)
    stepped = _dump(env["trial"])
    oneshot = dict(env, trial=tmp_path / "oneshot.db")
    _extract(oneshot)
    assert stepped == _dump(oneshot["trial"])


def test_small_batches_equal_one_pass(
    env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oneshot = dict(env, trial=tmp_path / "oneshot.db")
    _extract(oneshot)
    monkeypatch.setattr(st, "BATCH_BYTES", 64)  # smaller than every line
    _extract(env)
    assert _dump(env["trial"]) == _dump(oneshot["trial"])
    runs = _rows(env["trial"], "SELECT lines_read FROM streams ORDER BY is_subagent")
    assert runs == [(len(_main_records()),), (len(_sub_records()),)]


def test_long_first_line_still_arms_the_tripwire(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(st, "_HEAD_LINE_LIMIT", 16)  # the mode record is longer
    _extract(env)
    assert _rows(env["trial"], "SELECT COUNT(*) FROM streams WHERE head_mac IS NULL") == [(0,)]
    records = _main_records()
    records[0] = {"mode": "rewritten", "type": "mode"}  # differs inside the hashed prefix
    _write(env["main"], records + records)
    assert _extract(env).files_halted == 1


def test_result_arriving_in_a_later_run_sets_ok(env: dict[str, Path]) -> None:
    _write(env["sub"], [_sub_records()[0]])
    _extract(env)
    call = keyed_hash("toolu_S", _key(env))
    assert _rows(env["trial"], "SELECT ok FROM ledger WHERE call_key = ?", (call,)) == [(None,)]
    _write(env["sub"], [_sub_records()[1]], mode="a")
    _extract(env)
    assert _rows(env["trial"], "SELECT ok FROM ledger WHERE call_key = ?", (call,)) == [(1,)]


@pytest.mark.parametrize("first", ["fork", "main"])
def test_shared_records_extracted_in_any_order(
    env: dict[str, Path], tmp_path: Path, first: str
) -> None:
    fork = env["main"].with_name("99999999-2222-3333-4444-555555555555.jsonl")
    held = {"fork": env["main"], "main": fork}[first]
    records = {env["main"]: _main_records(), fork: _main_records()[:4]}
    _write(fork, records[fork])  # a fork copies the same uuids and call ids
    held.unlink()
    _extract(env)  # one file on the first day
    _write(held, records[held])
    _extract(env)  # the other on the next
    together = dict(env, trial=tmp_path / "together.db")
    _extract(together)
    assert _dump(env["trial"]) == _dump(together["trial"])
    key = _key(env)
    streams = _rows(
        env["trial"],
        "SELECT stream_key FROM ledger WHERE call_key = ?",
        (keyed_hash("toolu_A", key),),
    )
    assert len(streams) == 2  # each file keeps its own copy; nothing is deduplicated


@pytest.mark.parametrize("change", ["shrink", "rewrite_head"])
def test_rewritten_transcript_is_halted_and_rows_kept(env: dict[str, Path], change: str) -> None:
    _extract(env)
    before = _dump(env["trial"])
    records = _main_records()
    if change == "shrink":
        _write(env["main"], records[:2])
    else:
        records[0] = {"type": "mode", "mode": "plan"}
        _write(env["main"], records + records)
    summary = _extract(env)
    assert summary.files_halted == 1
    after = _dump(env["trial"])
    assert {t: v for t, v in after.items() if t != "streams"} == {
        t: v for t, v in before.items() if t != "streams"
    }
    assert _rows(env["trial"], "SELECT SUM(halted) FROM streams") == [(1,)]
    _write(env["main"], records, mode="a")
    assert _extract(env).files_halted == 1  # stays halted


def test_no_raw_identifier_or_text_reaches_the_trial_db(env: dict[str, Path]) -> None:
    _record_event(env, EVENT_ID, arm="shown")
    store = FeedbackStore(env["feedback"])
    store.initialize()
    store.record_opportunity(
        OpportunityRow(
            id="opp-raw-id-1",
            server="builtin",
            tool="Bash",
            arg_shape_json="{}",
            response_len=10,
            gate_decision="surfaced",
            host_session_id=SESSION,
            surfacing_id=EVENT_ID,
            arm="shown",
            holdout_rate=0.2,
        )
    )
    store.close()
    _extract(env)
    raw = [
        SESSION,
        AGENT,
        EVENT_ID,
        MEMORY_ID,
        "opp-raw-id-1",
        "toolu_A",
        "msg_A",
        "u1",
        "/Users/tester",
        "notes",
        "Alpha",
        "reuse",
        "Relevant Memories",
        "cat",
        "epsilon",  # snippet text
        "sub-cwd-sentinel",
    ]
    cells: list[str] = []
    with sqlite3.connect(env["trial"]) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            for row in db.execute(f"SELECT * FROM {table}"):
                for cell in row:
                    if isinstance(cell, bytes):
                        cells.append(cell.decode("latin-1"))
                    elif cell is not None:
                        cells.append(str(cell))
    assert len(cells) > 50
    for value in raw:
        assert not any(value in cell for cell in cells), value
    blob = env["trial"].read_bytes()
    for value in raw:
        assert value.encode() not in blob, value


def test_trial_db_mode_is_0600(env: dict[str, Path]) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits")
    env["trial"].parent.mkdir(parents=True)
    env["trial"].touch(mode=0o644)
    os.chmod(env["trial"], 0o644)
    _extract(env)
    assert stat.S_IMODE(env["trial"].stat().st_mode) == 0o600


def test_rows_survive_the_transcript(env: dict[str, Path]) -> None:
    _extract(env)
    env["main"].unlink()
    _extract(env)
    assert _rows(env["trial"], "SELECT COUNT(*) FROM injection_grams") == [(1,)]
    assert _rows(env["trial"], "SELECT COUNT(*) FROM output_grams WHERE ordinal = 8") == [(1,)]


# ── coverage and key pin ───────────────────────────────────────────────


def test_coverage_records_retention_and_per_run_streams(env: dict[str, Path]) -> None:
    env["settings"].write_text(json.dumps({"cleanupPeriodDays": 14}))
    _extract(env, stats_retention_days=45)
    _write(env["sub"], _sub_records(), mode="a")
    _extract(env, stats_retention_days=45)
    runs = _rows(
        env["trial"],
        "SELECT run_id, cleanup_period_days, stats_retention_days, files_read FROM coverage",
    )
    assert [r[1:] for r in runs] == [(14, 45, 2), (14, 45, 1)]
    per_run = _rows(env["trial"], "SELECT run_id, COUNT(*) FROM coverage_streams GROUP BY run_id")
    assert per_run == [(runs[0][0], 2), (runs[1][0], 1)]


def test_cleanup_period_defaults_to_30(env: dict[str, Path]) -> None:
    _extract(env)
    assert _rows(env["trial"], "SELECT cleanup_period_days FROM coverage") == [(30,)]


def test_stale_last_run_warns(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    _extract(env, now=lambda: 1_000_000.0)
    capsys.readouterr()
    _extract(env, now=lambda: 1_000_000.0 + 8 * DAY)
    assert "days ago" in capsys.readouterr().err


def test_a_different_key_is_refused(env: dict[str, Path], tmp_path: Path) -> None:
    _extract(env)
    other = tmp_path / "other.db"
    store = FeedbackStore(other)
    store.initialize()
    store.close()
    with pytest.raises(st.TrialError, match="fingerprint changed"):
        _extract(dict(env, feedback=other))
    with pytest.raises(st.TrialError, match="fingerprint changed"):
        st.freeze(env["trial"], other, holdout_rate=0.2, target=10)


def test_rows_without_a_pin_are_refused(env: dict[str, Path]) -> None:
    _extract(env)
    with sqlite3.connect(env["trial"]) as db:
        db.execute("DELETE FROM meta")
    with pytest.raises(st.TrialError, match="no key fingerprint pin"):
        _extract(env)


def test_pin_is_committed_before_extraction(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(*args: Any) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(st, "_extract_file", crash)
    with pytest.raises(KeyboardInterrupt):
        _extract(env)
    assert _rows(env["trial"], "SELECT value FROM meta") == [(st.key_fingerprint(_key(env)),)]
    assert _rows(env["trial"], "SELECT COUNT(*) FROM ledger") == [(0,)]


def test_load_hmac_key_is_read_only(env: dict[str, Path], tmp_path: Path) -> None:
    before = env["feedback"].read_bytes()
    assert len(load_hmac_key(env["feedback"])) == 32
    assert env["feedback"].read_bytes() == before
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    with pytest.raises(RuntimeError, match="HMAC key"):
        load_hmac_key(bare)
    with pytest.raises(RuntimeError, match="not found"):
        load_hmac_key(tmp_path / "missing.db")


# ── assignments ────────────────────────────────────────────────────────


def test_only_drawn_rows_are_copied_and_keys_join(env: dict[str, Path]) -> None:
    _record_event(env, EVENT_ID, arm="withheld")
    _record_event(env, "fedcba9876543210", arm=None, tool_use_id="toolu_B")
    store = FeedbackStore(env["feedback"])
    store.initialize()
    for oid, arm in (("o1", "withheld"), ("o2", None)):
        store.record_opportunity(
            OpportunityRow(
                id=oid,
                server="builtin",
                tool="Bash",
                arg_shape_json="{}",
                response_len=1,
                gate_decision="held_out" if arm else "surfaced",
                surfacing_id=EVENT_ID,
                arm=arm,
                holdout_rate=0.2 if arm else None,
            )
        )
    store.close()
    summary = _extract(env)
    key = _key(env)
    assert summary.assignments_added == 1
    ((event_key, stream, call, arm, rate, memory_keys),) = _rows(
        env["trial"],
        "SELECT event_key, stream_key, call_key, arm, holdout_rate, memory_keys FROM assignments",
    )
    assert (arm, rate) == ("withheld", 0.2)
    assert json.loads(memory_keys) == [keyed_hash(MEMORY_ID, key)]
    assert _rows(
        env["trial"],
        "SELECT COUNT(*) FROM ledger WHERE stream_key = ? AND call_key = ?",
        (stream, call),
    ) == [(1,)]
    assert _rows(env["trial"], "SELECT event_key FROM injection_grams") == [(event_key,)]
    memories = _rows(
        env["trial"], "SELECT event_key, memory_key, eligible FROM assignment_memories"
    )
    assert memories == [(event_key, keyed_hash(MEMORY_ID, key), 1)]
    opps = _rows(
        env["trial"],
        "SELECT opportunity_key, event_key, gate_decision FROM assignment_opportunities",
    )
    assert opps == [(keyed_hash("o1", key), event_key, "held_out")]
    assert _extract(env).assignments_added == 0


# ── freeze ─────────────────────────────────────────────────────────────


def _burn_in(env: dict[str, Path], start: float, events: int = 3) -> None:
    _extract(env, now=lambda: start)
    for i in range(events):
        _record_event(
            env,
            f"{i:016x}",
            arm=None,
            memory_id=f"m{i}",
            preview=f"shared boiler plate words here unique{i} tail{i} end{i}",
            created_at=start + 60 + i,
        )


def _freeze(env: dict[str, Path], now: float, **kwargs: Any) -> dict[str, object]:
    kwargs.setdefault("min_events", 3)
    return st.freeze(
        env["trial"], env["feedback"], holdout_rate=0.2, target=100, now=lambda: now, **kwargs
    )


def test_freeze_writes_the_trial_record(env: dict[str, Path]) -> None:
    start = 2_000_000.0
    _burn_in(env, start)
    # one memory surfaced three times must not stoplist its own grams
    for n in range(3):
        _record_event(
            env,
            f"ff{n:014x}",
            arm=None,
            memory_id="solo",
            preview="solo only words appear again",
            created_at=start + 100 + n,
        )
    result = _freeze(env, start + 8 * DAY)
    key = _key(env)
    ((stoplist, digest, size, b_start, b_end, events, n, t, rate, target, fp, frozen),) = _rows(
        env["trial"],
        "SELECT stoplist, stoplist_sha256, stoplist_size, burnin_start, burnin_end, burnin_events, "
        "n, t, holdout_rate, target_count, key_fingerprint, frozen_at FROM trial_record",
    )
    expected = gram_hashes("shared boiler plate words here", key)
    assert set(st.unpack_grams(stoplist)) == expected
    assert not gram_hashes("solo only words appear", key) & set(st.unpack_grams(stoplist))
    assert digest == hashlib.sha256(stoplist).hexdigest() == result["stoplist_sha256"]
    assert (size, b_start, events, n, t, rate, target) == (
        len(expected),
        start,
        6,
        12,
        600,
        0.2,
        100,
    )
    assert b_end == frozen == start + 8 * DAY
    assert fp == st.key_fingerprint(key)
    with pytest.raises(st.TrialError, match="already frozen"):
        _freeze(env, start + 9 * DAY)


def test_freeze_ignores_events_after_frozen_at(env: dict[str, Path]) -> None:
    start = 5_000_000.0
    _burn_in(env, start)
    frozen = start + 8 * DAY
    for n in range(3):  # written "during" the freeze, dated at or after its end
        _record_event(
            env,
            f"ee{n:014x}",
            arm=None,
            memory_id=f"late{n}",
            preview="late words only here now",
            created_at=frozen + n,
        )
    result = _freeze(env, frozen)
    (stoplist,) = _rows(env["trial"], "SELECT stoplist FROM trial_record")[0]
    assert result["burnin_events"] == 3
    assert not gram_hashes("late words only here", _key(env)) & set(st.unpack_grams(stoplist))


def test_freeze_checks_the_shortest_retention_any_run_recorded(env: dict[str, Path]) -> None:
    start = 6_000_000.0
    _extract(env, now=lambda: start, stats_retention_days=5)
    _burn_in(env, start + 1)  # a later run with the default 90
    with pytest.raises(st.TrialError, match="longer than stats_retention_days"):
        _freeze(env, start + 8 * DAY)


def test_freeze_without_a_run_leaves_no_state(env: dict[str, Path]) -> None:
    with pytest.raises(st.TrialError, match="no extractor run"):
        _freeze(env, 1.0)
    assert not env["trial"].exists()


def test_freeze_counts_only_drawable_hook_events(env: dict[str, Path]) -> None:
    start = 7_000_000.0
    _burn_in(env, start)
    for n in range(3):  # proxy-path events: no host ids, never drawable
        _record_event(
            env,
            f"cc{n:014x}",
            arm=None,
            memory_id=f"proxy{n}",
            preview="proxy only words appear here",
            created_at=start + 200 + n,
            hook=False,
        )
    with pytest.raises(st.TrialError, match="burn-in has 3 events; needs 4"):
        _freeze(env, start + 8 * DAY, min_events=4)  # 3 hook + 3 proxy would pass


def test_freeze_stoplist_ignores_proxy_events(env: dict[str, Path]) -> None:
    start = 7_500_000.0
    _burn_in(env, start)
    for n in range(3):
        _record_event(
            env,
            f"cd{n:014x}",
            arm=None,
            memory_id=f"proxy{n}",
            preview="proxy only words appear here",
            created_at=start + 200 + n,
            hook=False,
        )
    result = _freeze(env, start + 8 * DAY)
    (stoplist,) = _rows(env["trial"], "SELECT stoplist FROM trial_record")[0]
    assert result["burnin_events"] == 3
    assert not gram_hashes("proxy only words appear", _key(env)) & set(st.unpack_grams(stoplist))


def test_missing_projects_dir_is_refused(env: dict[str, Path], tmp_path: Path) -> None:
    with pytest.raises(st.TrialError, match="transcript directory not found"):
        _extract(dict(env, projects=tmp_path / "nowhere"))
    assert not env["trial"].exists()


def test_freeze_needs_extracted_transcripts(env: dict[str, Path], tmp_path: Path) -> None:
    empty = tmp_path / "empty-projects"
    empty.mkdir()
    start = 8_000_000.0
    _burn_in(dict(env, projects=empty), start)
    with pytest.raises(st.TrialError, match="no transcript has been extracted"):
        _freeze(env, start + 8 * DAY)


def test_burn_in_stoplist_counts_distinct_memories() -> None:
    # g1: memories a and b (a has two differing rows) -> 2, kept; g3: a, c, d -> 3, stoplisted
    rows = [
        ("a", '["g1","g2"]'),
        ("a", '["g1","g3"]'),
        ("b", '["g1"]'),
        ("c", '["g3"]'),
        ("d", '["g3"]'),
    ]
    assert st.burn_in_stoplist(rows) == {"g3"}


@pytest.mark.parametrize(
    ("setup", "now_days", "kwargs", "message"),
    [
        ("none", 8, {}, "no extractor run"),
        ("burn", 3, {}, "needs 7"),
        ("burn", 8, {"min_events": 500}, "needs 500"),
        ("drawn", 8, {}, "drawn events already exist"),
        ("drawn_opportunity", 8, {}, "drawn events already exist"),
        ("short_retention", 8, {}, "longer than stats_retention_days"),
    ],
)
def test_freeze_refusals(
    env: dict[str, Path], setup: str, now_days: float, kwargs: dict[str, Any], message: str
) -> None:
    start = 3_000_000.0
    if setup != "none":
        if setup == "short_retention":
            _extract(env, now=lambda: start, stats_retention_days=5)
            _burn_in(env, start + 1)
            _extract(env, now=lambda: start + 2, stats_retention_days=5)
        else:
            _burn_in(env, start)
        if setup == "drawn":
            _record_event(env, "d" * 16, arm="shown", created_at=start + 10)
        if setup == "drawn_opportunity":  # its event write was lost; the opportunity row remains
            store = FeedbackStore(env["feedback"])
            store.initialize()
            store.record_opportunity(
                OpportunityRow(
                    id="o-lost",
                    server="builtin",
                    tool="Bash",
                    arg_shape_json="{}",
                    response_len=1,
                    gate_decision="surfaced",
                    surfacing_id="e" * 16,
                    arm="shown",
                    holdout_rate=0.2,
                )
            )
            store.close()
    with pytest.raises(st.TrialError, match=message):
        _freeze(env, start + now_days * DAY, **kwargs)
    if setup != "none":  # "none" leaves no DB at all (test_freeze_without_a_run_leaves_no_state)
        assert _rows(env["trial"], "SELECT COUNT(*) FROM trial_record") == [(0,)]


@pytest.mark.parametrize("rate", [0.0, 0.6])
def test_freeze_rejects_a_rate_collection_cannot_use(env: dict[str, Path], rate: float) -> None:
    with pytest.raises(st.TrialError, match="holdout-rate"):
        st.freeze(env["trial"], env["feedback"], holdout_rate=rate, target=10)


# ── purge and CLI ──────────────────────────────────────────────────────


def test_purge_needs_yes(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    _extract(env)
    assert st.main(["--purge", "--trial-db", str(env["trial"])]) == 0
    assert "would delete" in capsys.readouterr().out
    assert env["trial"].exists()
    assert st.main(["--purge", "--yes", "--trial-db", str(env["trial"])]) == 0
    assert not env["trial"].exists()
    assert st.purge_targets(env["trial"]) == []


def test_cli_refusal_exits_nonzero(env: dict[str, Path], tmp_path: Path) -> None:
    args = ["--trial-db", str(env["trial"]), "--feedback-db", str(tmp_path / "missing.db")]
    assert st.main(args) == 1
    assert not env["trial"].exists()  # refused before the trial DB was created


def test_feedback_db_from_before_the_holdout(env: dict[str, Path]) -> None:
    """A 0.6.0 store has no ``arm`` columns: nothing is copied and ``--freeze`` still works."""
    start = 4_000_000.0
    _burn_in(env, start)
    with sqlite3.connect(env["feedback"]) as db:
        db.execute("ALTER TABLE surfacing_events DROP COLUMN arm")
        db.execute("ALTER TABLE surfacing_opportunities DROP COLUMN arm")
    assert _extract(env).assignments_added == 0
    assert _freeze(env, start + 8 * DAY)["burnin_events"] == 3
