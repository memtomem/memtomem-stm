"""Outcome resolution for the holdout trial (``memtomem_stm.surfacing.trial``).

``resolve`` is a pure function over keyed rows, so most tests build rows from
literals: keys are opaque strings to it. The end-to-end tests at the bottom run
real transcripts through the extractor and its loader so the hashes are the
ones collection and extraction actually produce.
"""

from __future__ import annotations

import random
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from stm_trial_fixtures import (
    AGENT,
    EVENT_ID,
    PREVIEW,
    SESSION,
    _block,
    _extract,
    _injection,
    _main_records,
    _record_event,
    _tool_use,
    _write,
    make_env,
    st,
)

from memtomem_stm.surfacing.trial import (
    Assignment,
    InjectionRecord,
    Ledger,
    LedgerEntry,
    MemoryRow,
    Outcome,
    OutputRecord,
    resolve,
)

S = "stream-main"
T0 = 1_000.0
FILE = "hash-of-surfaced-file"


# ── literal builders ────────────────────────────────────────────────────


def _asg(
    event: str = "E",
    call: str | None = "c0",
    stream: str | None = S,
    *,
    arm: str = "shown",
    advertised: bool = True,
    header: str = "H",
) -> Assignment:
    return Assignment(event, T0, stream, call, advertised, header, arm, 0.2)  # type: ignore[arg-type]


def _mem(
    event: str = "E",
    memory: str = "M",
    *,
    path: str | None = FILE,
    dirs: tuple[str, ...] = (),
    base: str | None = None,
    grams: tuple[str, ...] = (),
    eligible: bool = True,
) -> MemoryRow:
    return MemoryRow(event, memory, eligible, path, frozenset(dirs), base, frozenset(grams))


def _entry(
    ordinal: int,
    call: str,
    ts: float | None,
    *,
    msg: str | None = None,
    paths: tuple[str, ...] = (),
    patterns: tuple[str, ...] = (),
    stream: str = S,
    eligible: bool = True,
    ok: bool | None = True,
) -> LedgerEntry:
    return LedgerEntry(
        stream,
        ordinal,
        call,
        msg if msg is not None else f"msg-{call}",
        eligible,
        ok,
        ts,
        frozenset(paths),
        frozenset(patterns),
    )


def _anchor(stream: str = S, call: str = "c0") -> LedgerEntry:
    return _entry(0, call, T0, stream=stream)


def _inj(
    ordinal: int,
    call: str | None,
    ts: float,
    *,
    event: str | None = None,
    header: str = "H",
    wrapped: bool = True,
    grams: tuple[str, ...] = (),
    stream: str = S,
) -> InjectionRecord:
    return InjectionRecord(stream, ordinal, call, ts, event, header, wrapped, frozenset(grams))


def _out(ordinal: int, ts: float, grams: tuple[str, ...], stream: str = S) -> OutputRecord:
    return OutputRecord(stream, ordinal, ts, frozenset(grams))


def _run(
    assignments: list[Assignment],
    memories: list[MemoryRow],
    entries: list[LedgerEntry],
    *,
    outputs: tuple[OutputRecord, ...] = (),
    injections: tuple[InjectionRecord, ...] = (),
    streams: tuple[str, ...] = (S,),
    stoplist: tuple[str, ...] = (),
    n: int = 12,
) -> tuple[Outcome, ...]:
    ledger = Ledger(frozenset(streams), tuple(entries), outputs, injections)
    return resolve(assignments, memories, ledger, stoplist, n=n)


def _one(*args: Any, **kwargs: Any) -> Outcome:
    (outcome,) = _run(*args, **kwargs)
    return outcome


def _reads(i: int, ts: float, *, stream: str = S, path: str = FILE) -> LedgerEntry:
    return _entry(i, f"c{i}", ts, paths=(path,), stream=stream)


def _other(i: int, ts: float, *, stream: str = S) -> LedgerEntry:
    return _entry(i, f"c{i}", ts, paths=(f"elsewhere-{i}",), stream=stream)


# ── 1. streams ──────────────────────────────────────────────────────────


def test_main_and_subagent_streams_resolve_as_they_do_separately() -> None:
    sub = "stream-sub"
    main_entries = [_anchor(), _other(2, T0 + 2), _reads(4, T0 + 4)]
    sub_entries = [_anchor(sub, "d0"), _reads(1, T0 + 1, stream=sub), _other(3, T0 + 3, stream=sub)]
    main_event = _asg("E1")
    sub_event = _asg("E2", "d0", sub)
    memories = [_mem("E1"), _mem("E2")]
    together = _run([main_event, sub_event], memories, main_entries + sub_entries, streams=(S, sub))
    alone = _run([main_event], [memories[0]], main_entries) + _run(
        [sub_event], [memories[1]], sub_entries, streams=(sub,)
    )
    assert together == alone
    assert [o.y1 for o in together] == [True, True]


def test_a_subagent_read_of_the_parents_file_does_not_set_the_parents_y1() -> None:
    sub = "stream-sub"
    outcome = _one(
        [_asg()],
        [_mem()],
        [_anchor(), _other(2, T0 + 2), _reads(1, T0 + 1, stream=sub)],
        streams=(S, sub),
    )
    assert outcome.status == "ok" and outcome.y1 is False


def test_a_copy_of_the_call_in_a_forked_file_changes_nothing() -> None:
    fork = "stream-fork"
    base = [_anchor(), _other(1, T0 + 1)]
    copied = [_anchor(fork), _reads(1, T0 + 1, stream=fork)]
    without = _one([_asg()], [_mem()], base, streams=(S, fork))
    with_copy = _one([_asg()], [_mem()], base + copied, streams=(S, fork))
    assert with_copy == without
    assert with_copy.y1 is False and with_copy.stream_mismatch is False


def test_a_named_file_without_the_call_is_no_ledger_and_a_mismatch_if_found_elsewhere() -> None:
    other = "stream-other"
    alone = _one([_asg()], [_mem()], [_other(1, T0)])
    assert (alone.status, alone.y1, alone.stream_mismatch) == ("no_ledger", None, False)
    elsewhere = _one([_asg()], [_mem()], [_other(1, T0), _anchor(other)], streams=(S, other))
    assert (elsewhere.status, elsewhere.stream_mismatch) == ("no_ledger", True)


def test_an_unextracted_stream_is_missing_and_still_reports_a_mismatch() -> None:
    other = "stream-other"
    outcome = _one([_asg()], [_mem()], [_anchor(other)], streams=(other,))
    assert (outcome.status, outcome.y1, outcome.stream_mismatch) == ("not_extracted", None, True)


def test_a_copied_entry_dated_before_s_neither_counts_nor_matches() -> None:
    stale = _reads(1, T0 - 50)
    outcome = _one([_asg()], [_mem()], [_anchor(), stale, _other(2, T0 + 1)], n=1)
    assert (outcome.y1, outcome.stale_entry, outcome.window_len) == (False, True, 1)
    # it did not use up the single slot: a match right after it still counts
    outcome = _one([_asg()], [_mem()], [_anchor(), stale, _reads(2, T0 + 1)], n=1)
    assert outcome.y1 is True


def test_a_parallel_sibling_of_s_neither_matches_nor_counts() -> None:
    sibling = _entry(1, "c1", T0, msg="msg-c0", paths=(FILE,))
    outcome = _one([_asg()], [_mem()], [_anchor(), sibling, _other(2, T0 + 1)], n=1)
    assert (outcome.y1, outcome.window_len) == (False, 1)
    outcome = _one([_asg()], [_mem()], [_anchor(), sibling, _reads(2, T0 + 1)], n=1)
    assert outcome.y1 is True


# ── 2. delivery ─────────────────────────────────────────────────────────


def _delivery(injections: tuple[InjectionRecord, ...], **asg: Any) -> Outcome:
    return _one([_asg(**asg)], [_mem()], [_anchor()], injections=injections)


def test_delivered_by_its_own_id_and_never_by_another_events_id() -> None:
    own = _delivery((_inj(1, "c0", T0, event="E"),))
    assert (own.delivery, own.delivery_basis) == ("delivered", "id")
    foreign = _delivery((_inj(1, "c0", T0, event="OTHER"),))
    assert (foreign.delivery, foreign.delivery_basis) == ("undelivered", "header")


def test_unadvertised_delivery_needs_exactly_one_id_less_header_record() -> None:
    one = _delivery((_inj(1, "c0", T0),), advertised=False)
    assert (one.delivery, one.delivery_basis) == ("delivered", "header")
    two = _delivery((_inj(1, "c0", T0), _inj(2, "c0", T0)), advertised=False)
    assert two.delivery == "ambiguous_delivery"
    none = _delivery((), advertised=False)
    assert none.delivery == "undelivered"


def test_an_id_bearing_record_with_the_same_header_is_not_an_unadvertised_delivery() -> None:
    outcome = _delivery((_inj(1, "c0", T0, event="OTHER"),), advertised=False)
    assert outcome.delivery == "undelivered"


def test_a_foreign_record_with_stms_header_is_the_documented_residual() -> None:
    # STM's own injection was lost; another hook wrote STM's exact header
    outcome = _delivery((_inj(1, "c0", T0, wrapped=False),), advertised=False)
    assert (outcome.delivery, outcome.delivery_basis) == ("delivered", "header")


def test_a_header_changed_after_collection_does_not_change_the_result() -> None:
    # the digest is the header the event was rendered with, fixed at collection. A record
    # under a newer section_header on the same call is another header, not a second match.
    injections = (_inj(1, "c0", T0, header="H-old"), _inj(2, "c0", T0, header="H-new"))
    old = _delivery(injections, advertised=False, header="H-old")
    assert (old.delivery, old.delivery_basis) == ("delivered", "header")
    new = _delivery(injections, advertised=False, header="H-new")
    assert new.delivery == "delivered"
    neither = _delivery(injections, advertised=False, header="H-other")
    assert neither.delivery == "undelivered"


def test_an_advertised_id_withdrawn_after_a_late_write_falls_back_to_the_header() -> None:
    outcome = _delivery((_inj(1, "c0", T0),), advertised=True)
    assert (outcome.delivery, outcome.delivery_basis) == ("delivered", "header")


def test_two_events_on_one_call_only_the_event_whose_id_is_present_is_delivered() -> None:
    injections = (_inj(1, "c0", T0, event="B"),)
    outcomes = _run(
        [_asg("A"), _asg("B")], [_mem("A"), _mem("B")], [_anchor()], injections=injections
    )
    by_event = {o.event_key: o for o in outcomes}
    assert by_event["A"].delivery == "undelivered"
    assert by_event["B"].delivery == "delivered"
    assert by_event["A"].duplicate_run and by_event["B"].duplicate_run


def test_a_withheld_event_with_any_stm_record_on_its_call_is_an_arm_violation() -> None:
    clean = _delivery((), arm="withheld")
    assert clean.arm_violation is False
    own = _delivery((_inj(1, "c0", T0, event="E"),), arm="withheld")
    assert own.arm_violation is True
    # another event's block on the same call: this event is undelivered, still a violation
    foreign = _delivery((_inj(1, "c0", T0, event="OTHER"),), arm="withheld")
    assert (foreign.delivery, foreign.arm_violation) == ("undelivered", True)
    # a non-STM record is not an STM injection
    other_hook = _delivery((_inj(1, "c0", T0, header="X", wrapped=False),), arm="withheld")
    assert other_hook.arm_violation is False
    # ...unless it is identified as this event's block by its header or its id
    by_header = _delivery((_inj(1, "c0", T0, wrapped=False),), arm="withheld", advertised=False)
    assert (by_header.delivery, by_header.arm_violation) == ("delivered", True)
    # its own id on an unadvertised event: undelivered by the id-less rule, still a violation
    by_id = _delivery(
        (_inj(1, "c0", T0, event="E", header="X", wrapped=False),),
        arm="withheld",
        advertised=False,
    )
    assert (by_id.delivery, by_id.arm_violation) == ("undelivered", True)


# ── 3. Y1 window ────────────────────────────────────────────────────────


def test_a_match_at_the_thirteenth_later_entry_does_not_count() -> None:
    fillers = [_other(i, T0 + i) for i in range(1, 13)]
    twelfth = _one([_asg()], [_mem()], [_anchor(), *fillers[:11], _reads(12, T0 + 12)])
    assert (twelfth.y1, twelfth.window_len) == (True, 12)
    thirteenth = _one([_asg()], [_mem()], [_anchor(), *fillers, _reads(13, T0 + 13)])
    assert (thirteenth.y1, thirteenth.window_len) == (False, 12)


def test_the_window_is_cut_where_the_running_maximum_passes_t() -> None:
    at_limit = _one([_asg()], [_mem()], [_anchor(), _reads(1, T0 + 600)])
    assert at_limit.y1 is True
    past = _one([_asg()], [_mem()], [_anchor(), _reads(1, T0 + 601)])
    assert (past.y1, past.window_len) == (False, 0)
    # a later in-bound entry does not reopen a window the running maximum already closed
    reopened = _one([_asg()], [_mem()], [_anchor(), _other(1, T0 + 601), _reads(2, T0 + 5)])
    assert reopened.y1 is False


def test_a_backwards_timestamp_not_before_s_keeps_its_entry_and_is_an_anomaly() -> None:
    outcome = _one([_asg()], [_mem()], [_anchor(), _other(1, T0 + 50), _reads(2, T0 + 10)])
    assert (outcome.y1, outcome.clock_anomaly, outcome.window_len) == (True, True, 2)


def test_insertion_order_never_changes_the_result() -> None:
    assignments = [_asg(f"E{i}", f"c{i * 10}") for i in range(5)]
    memories = [_mem(f"E{i}", f"M{j}") for i in range(5) for j in range(2)]
    entries = [_entry(i * 10, f"c{i * 10}", T0 + i * 10) for i in range(5)] + [
        _reads(i * 10 + 1, T0 + i * 10 + 1) for i in range(5)
    ]
    expected = _run(assignments, memories, entries)
    rng = random.Random(7)
    for _ in range(5):
        rng.shuffle(assignments)
        rng.shuffle(memories)
        rng.shuffle(entries)
        assert _run(assignments, memories, entries) == expected


def test_a_short_window_is_scored_on_the_entries_it_has() -> None:
    outcome = _one(
        [_asg()], [_mem()], [_anchor(), _other(1, T0 + 1), _other(2, T0 + 2), _reads(3, T0 + 3)]
    )
    assert (outcome.y1, outcome.window_len) == (True, 3)


def test_only_successful_calls_are_entries() -> None:
    failed = _entry(1, "c1", T0 + 1, paths=(FILE,), ok=False)
    pending = _entry(2, "c2", T0 + 2, paths=(FILE,), ok=None)
    other_tool = _entry(3, "c3", T0 + 3, paths=(FILE,), eligible=False)
    outcome = _one([_asg()], [_mem()], [_anchor(), failed, pending, other_tool])
    assert (outcome.y1, outcome.window_len) == (False, 0)


def test_missing_timestamps_are_reported_not_guessed() -> None:
    no_anchor_ts = _one([_asg()], [_mem()], [_entry(0, "c0", None), _reads(1, T0)])
    assert (no_anchor_ts.status, no_anchor_ts.y1) == ("no_timestamp", None)
    null_entry = _entry(1, "c1", None, paths=(FILE,))
    outcome = _one([_asg()], [_mem()], [_anchor(), null_entry])
    assert (outcome.y1, outcome.null_ts, outcome.window_len) == (False, True, 0)
    # an undated output cannot be placed before or after s: skipped, and reported
    undated = OutputRecord(S, 1, None, frozenset({"g1"}))
    outcome = _one([_asg()], [_mem(grams=("g1",))], [_anchor()], outputs=(undated,))
    assert (outcome.y2, outcome.null_ts) == (False, True)
    dated = _one([_asg()], [_mem(grams=("g1",))], [_anchor()], outputs=(_out(1, T0 + 1, ("g1",)),))
    assert (dated.y2, dated.null_ts) == (True, False)


def test_events_without_a_call_key_are_not_a_duplicate_run() -> None:
    outcomes = _run([_asg("A", None), _asg("B", None)], [_mem("A"), _mem("B")], [_anchor()])
    assert [o.duplicate_run for o in outcomes] == [False, False]


def test_a_window_holding_two_calls_of_one_record_is_flagged() -> None:
    shared = [_entry(1, "c1", T0 + 1, msg="m"), _entry(1, "c2", T0 + 1, msg="m")]
    outcome = _one([_asg()], [_mem()], [_anchor(), *shared])
    assert outcome.shared_ordinal is True
    single = _one([_asg()], [_mem()], [_anchor(), _other(1, T0 + 1)])
    assert single.shared_ordinal is False
    # a tie straddling the n cut decides which call made the window: also flagged
    straddle = _one([_asg()], [_mem()], [_anchor(), *shared], n=1)
    assert (straddle.window_len, straddle.shared_ordinal) == (1, True)
    clean_cut = _one([_asg()], [_mem()], [_anchor(), _other(1, T0 + 1), _other(2, T0 + 2)], n=1)
    assert clean_cut.shared_ordinal is False


# ── 4. Y1 matching ──────────────────────────────────────────────────────


def test_a_directory_search_matches_only_with_the_basename_as_a_pattern_token() -> None:
    memory = _mem(dirs=("dir-a", "dir-root"), base="base-foo")
    grep = _entry(1, "c1", T0 + 1, paths=("dir-a",), patterns=("base-foo",))
    assert _one([_asg()], [memory], [_anchor(), grep]).y1 is True
    wrong_pattern = _entry(1, "c1", T0 + 1, paths=("dir-a",), patterns=("base-notfoo",))
    assert _one([_asg()], [memory], [_anchor(), wrong_pattern]).y1 is False
    wrong_dir = _entry(1, "c1", T0 + 1, paths=("dir-other",), patterns=("base-foo",))
    assert _one([_asg()], [memory], [_anchor(), wrong_dir]).y1 is False


def test_a_symlink_alias_does_not_match() -> None:
    # documented limit: transcript paths are never resolved, and only the lexical
    # hash of the surfaced path is collected, so an alias is simply another path
    outcome = _one([_asg()], [_mem()], [_anchor(), _reads(1, T0 + 1, path="hash-of-alias")])
    assert outcome.y1 is False


def test_ineligible_memories_have_no_outcome_row() -> None:
    outcomes = _run([_asg()], [_mem(), _mem(memory="M2", eligible=False)], [_anchor()])
    assert [o.memory_key for o in outcomes] == ["M"]


# ── 5. Y2 ───────────────────────────────────────────────────────────────


def _y2(outputs: tuple[OutputRecord, ...], **kwargs: Any) -> bool | None:
    return _one([_asg()], [_mem(grams=("g1", "g2"))], [_anchor()], outputs=outputs, **kwargs).y2


def test_a_gram_only_before_does_not_count_and_one_only_after_does() -> None:
    assert _y2((_out(1, T0 - 10, ("g1",)),)) is False
    assert _y2((_out(1, T0 + 10, ("g1",)),)) is True
    assert _y2((_out(1, T0 - 10, ("g1",)), _out(2, T0 + 10, ("g1",)))) is False
    # after-only is per gram: g2 is new after s even though g1 was seen before
    assert _y2((_out(1, T0 - 10, ("g1",)), _out(2, T0 + 10, ("g2",)))) is True


def test_y2_bounds_and_the_anchor_record_counts_as_before() -> None:
    assert _y2((_out(1, T0 + 600, ("g1",)),)) is True
    assert _y2((_out(1, T0 + 601, ("g1",)),)) is False
    assert _y2((_out(0, T0, ("g1",)), _out(1, T0 + 10, ("g1",)))) is False
    assert _y2((_out(1, T0 - 601, ("g1",)), _out(2, T0 + 10, ("g1",)))) is True


def test_a_stoplisted_gram_never_counts() -> None:
    assert _y2((_out(1, T0 + 10, ("g1",)),), stoplist=("g1",)) is False


def test_snippets_added_after_the_freeze_change_no_y2_value() -> None:
    outputs = (_out(1, T0 + 10, ("g1",)),)
    base = _run([_asg()], [_mem(grams=("g1",))], [_anchor()], outputs=outputs, stoplist=())
    later = [_mem("LATER", f"M{i}", grams=("g1",)) for i in range(5)]
    grown = _run(
        [_asg(), _asg("LATER", "c9")],
        [_mem(grams=("g1",)), *later],
        [_anchor(), _entry(9, "c9", T0 + 900)],
        outputs=outputs,
        stoplist=(),
    )
    assert [o for o in grown if o.event_key == "E"] == list(base)


def test_output_in_another_stream_is_not_this_streams_reuse() -> None:
    assert _y2((_out(1, T0 + 10, ("g1",), stream="stream-sub"),)) is False


# ── 6. crossover ────────────────────────────────────────────────────────


def test_withheld_content_in_a_later_pinned_block_is_crossover_and_stays_withheld() -> None:
    later_block = _inj(2, "c1", T0 + 5, header="PINNED", grams=("g1",))
    outcome = _one(
        [_asg(arm="withheld")],
        [_mem(grams=("g1",))],
        [_anchor(), _other(1, T0 + 5)],
        injections=(later_block,),
    )
    assert (outcome.arm, outcome.reexposed, outcome.arm_violation) == ("withheld", True, False)


def test_the_anchors_own_injection_is_not_reexposure() -> None:
    # written after the anchor's result, inside the window's time span
    own = _inj(1, "c0", T0 + 1, event="E", grams=("g1",))
    outcome = _one(
        [_asg()], [_mem(grams=("g1",))], [_anchor(), _other(2, T0 + 5)], injections=(own,)
    )
    assert (outcome.window_len, outcome.reexposed) == (1, False)


def test_reexposure_is_bounded_by_the_windows_time_span() -> None:
    block = (_inj(3, "c3", T0 + 20, grams=("g1",)),)
    inside = _one(
        [_asg()],
        [_mem(grams=("g1",))],
        [_anchor(), _other(1, T0 + 5), _other(3, T0 + 20)],
        injections=block,
    )
    assert inside.reexposed is True
    # window closed at n = 1 with its running maximum at T0 + 5
    outside = _one(
        [_asg()],
        [_mem(grams=("g1",))],
        [_anchor(), _other(1, T0 + 5), _other(3, T0 + 20)],
        injections=block,
        n=1,
    )
    assert outside.reexposed is False
    # a block on a window call lands after that call's own timestamp and still counts
    late = (_inj(3, "c1", T0 + 30, grams=("g1",)),)
    on_window_call = _one(
        [_asg()], [_mem(grams=("g1",))], [_anchor(), _other(1, T0 + 5)], injections=late, n=1
    )
    assert on_window_call.reexposed is True
    stoplisted = _one(
        [_asg()],
        [_mem(grams=("g1",))],
        [_anchor(), _other(1, T0 + 5), _other(3, T0 + 20)],
        injections=block,
        stoplist=("g1",),
    )
    assert stoplisted.reexposed is False


def test_a_block_before_the_anchor_in_the_stream_is_never_reexposure() -> None:
    # dated inside the window's span (a copied record keeps its own clock) but written
    # earlier in the file than the call it would follow
    anchor = _entry(5, "c0", T0)
    entries = [anchor, _other(6, T0 + 5), _other(7, T0 + 20)]
    earlier = (_inj(2, "cX", T0 + 10, grams=("g1",)),)
    outcome = _one([_asg()], [_mem(grams=("g1",))], entries, injections=earlier)
    assert (outcome.window_len, outcome.reexposed) == (2, False)
    later = (_inj(8, "cX", T0 + 10, grams=("g1",)),)
    assert _one([_asg()], [_mem(grams=("g1",))], entries, injections=later).reexposed is True


# ── end to end: extractor rows → loader → resolve ───────────────────────


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    return make_env(tmp_path, monkeypatch)


def _resolve_env(env: dict[str, Path]) -> tuple[Outcome, ...]:
    _extract(env)
    with closing(sqlite3.connect(env["trial"])) as db:
        assignments, memories, ledger, stoplist = st.load_resolve_inputs(db)
    return resolve(assignments, memories, ledger, stoplist)


def _main_with(read: dict[str, Any], cwd: str = "/Users/tester/repo") -> list[dict[str, Any]]:
    records = _main_records(cwd)
    records[4] = _tool_use("u4", "msg_B", "toolu_B", read.pop("name", "Read"), read, 5, cwd)
    return records


def test_e2e_a_delivered_shown_event_resolves_from_real_hashes(env: dict[str, Path]) -> None:
    _record_event(env, EVENT_ID, arm="shown")
    (outcome,) = _resolve_env(env)
    assert (outcome.status, outcome.delivery, outcome.delivery_basis) == ("ok", "delivered", "id")
    assert (outcome.y1, outcome.y2, outcome.arm_violation) == (True, True, False)


def test_e2e_a_read_of_a_file_deleted_before_resolution_still_matches(
    env: dict[str, Path], tmp_path: Path
) -> None:
    note = tmp_path / "notes" / "Moved.md"
    note.parent.mkdir()
    note.write_text("x")
    records = _main_records()
    records[4] = _tool_use("u4", "msg_B", "toolu_B", "Read", {"file_path": str(note)}, 5, "/r")
    _write(env["main"], records)
    _record_event(env, EVENT_ID, arm="shown", source=str(note))
    note.unlink()  # moved or deleted after the call: nothing on disk is consulted
    (outcome,) = _resolve_env(env)
    assert outcome.y1 is True


def test_e2e_an_unadvertised_event_is_delivered_by_its_header(env: dict[str, Path]) -> None:
    # the hook default: no id rendered, so the block carries none and the header decides
    records = _main_records()
    records[3] = _injection("u3", "toolu_A", _block(None, f"- **notes/Alpha.md**: {PREVIEW}"), 1)
    _write(env["main"], records)
    _record_event(env, EVENT_ID, arm="shown", id_advertised=False)
    (outcome,) = _resolve_env(env)
    assert (outcome.delivery, outcome.delivery_basis) == ("delivered", "header")
    # positive control: the same transcript with the id rendered but not advertised by the
    # event would still be the id-bearing block — it is not an id-less header match
    _write(env["main"], _main_records())
    env["trial"].unlink()
    (outcome,) = _resolve_env(env)
    assert outcome.delivery == "undelivered"


def test_e2e_a_subagent_read_of_the_file_does_not_set_the_parents_y1(
    env: dict[str, Path],
) -> None:
    _write(env["main"], _main_with({"file_path": "/Users/tester/other.md"}))
    _record_event(env, EVENT_ID, arm="shown")
    (outcome,) = _resolve_env(env)
    assert outcome.y1 is False
    # positive control: the subagent's read of the same file is in the ledger
    with closing(sqlite3.connect(env["trial"])) as db:
        _, memories, ledger, _ = st.load_resolve_inputs(db)
    target = memories[0].path_hash_lexical
    main_key = st.stream_key(SESSION, None, st.load_hmac_key(env["feedback"]))
    sub_key = st.stream_key(SESSION, AGENT, st.load_hmac_key(env["feedback"]))
    assert {e.stream_key for e in ledger.entries if target in e.paths} == {sub_key}
    assert main_key in ledger.streams


def test_e2e_a_bash_cd_then_relative_read_matches(env: dict[str, Path]) -> None:
    _write(
        env["main"],
        _main_with({"name": "Bash", "command": "cd notes/sub && cat ../Alpha.md"}, "/Users/tester"),
    )
    _record_event(env, EVENT_ID, arm="shown")
    (outcome,) = _resolve_env(env)
    assert outcome.y1 is True


def test_e2e_the_two_part_display_path_does_not_match(env: dict[str, Path]) -> None:
    # the injected bullet shows ``notes/Alpha.md``; read literally from another cwd it is
    # a different file
    _write(env["main"], _main_with({"file_path": "notes/Alpha.md"}, "/elsewhere"))
    _record_event(env, EVENT_ID, arm="shown")
    (outcome,) = _resolve_env(env)
    assert outcome.y1 is False


def test_e2e_a_withheld_event_with_an_stm_block_is_an_arm_violation(
    env: dict[str, Path],
) -> None:
    _record_event(env, EVENT_ID, arm="withheld")
    (outcome,) = _resolve_env(env)
    assert (outcome.arm, outcome.arm_violation) == ("withheld", True)


def test_e2e_a_later_block_repeating_the_preview_is_reexposure(env: dict[str, Path]) -> None:
    records = _main_records()
    # after the Read's result, a pinned bullet repeating the withheld preview
    records.insert(6, _injection("u9", "toolu_B", _block(None, f"- **pinned**: {PREVIEW}"), 6))
    _write(env["main"], records)
    _record_event(env, EVENT_ID, arm="withheld")
    (outcome,) = _resolve_env(env)
    assert (outcome.reexposed, outcome.arm) == (True, "withheld")
