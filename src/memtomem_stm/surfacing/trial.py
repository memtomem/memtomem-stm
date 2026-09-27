"""Outcome resolution for the surfacing holdout trial.

The holdout (``surfacing.holdout_rate``) randomly withholds a share of hook
injections and records the assignment on the event row. The offline extractor
(``scripts/stm_trial.py``) copies those assignments and the host transcripts'
tool calls, outputs and injections into a pseudonymous database, every id, path
and text item a keyed hash. :func:`resolve` turns those rows into one outcome
per eligible surfaced memory: did the agent open the memory's file soon after
(Y1), reuse its text (Y2), see it again in a later block (re-exposure), and was
the block delivered at all.

It is a pure function over already-decoded rows — no I/O, no database, no
clock — so every rule below is testable from literals and the result cannot
depend on the order rows were read in. Hashes only ever meet other hashes made
under the same key; nothing here can reverse one.

The rules, all confined to the one transcript file (the *stream*) the event's
host ids name — a copy of the call in another file is never read:

- **Anchor** *s*: the ledger entry whose call key is the event's.
- **Entries**: successful calls (``ok = 1``) of the tools the hook runs on,
  in transcript order.
- **Y1 window**: after the last entry of the assistant message that issued *s*
  (its parallel siblings were chosen before the injection was shown), the next
  ``n`` entries. Entries dated before *s* are copies and are skipped; the window
  ends at the first entry that would take the running maximum timestamp past
  ``ts(s) + t``.
- **Y1**: a window entry names the memory's file — its lexical path hash, or an
  ancestor directory together with a pattern token equal to the basename.
- **Y2**: some snippet 4-gram (minus the frozen stoplist) appears in the
  agent's own output within ``t`` after *s* and did not appear within ``t``
  before it.
- **Re-exposure**: a later STM block on one of the window's calls, or dated
  inside its time span, carries one of those grams again. Reported only; the
  assigned arm never changes.
- **Delivery**: by the event's own id when it was advertised, otherwise by
  exactly one id-less block whose first line hashes to the event's header.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Arm = Literal["shown", "withheld"]
Status = Literal["ok", "no_ledger", "not_extracted", "no_timestamp"]
Delivery = Literal["delivered", "undelivered", "ambiguous_delivery"]
DeliveryBasis = Literal["id", "header"]

WINDOW_ENTRIES = 12
"""Default ``n``: the Y1 window length in entries."""

WINDOW_SECONDS = 600
"""Default ``t``: the Y1 / Y2 time bound in seconds."""


@dataclass(frozen=True)
class Assignment:
    """One event with a drawn arm, its ids already keyed."""

    event_key: str
    created_at: float
    stream_key: str | None
    call_key: str | None
    id_advertised: bool
    header_digest: str | None
    arm: Arm
    holdout_rate: float | None


@dataclass(frozen=True)
class MemoryRow:
    """One delivered memory of an event, as collection hashed it."""

    event_key: str
    memory_key: str
    eligible: bool
    path_hash_lexical: str | None
    dir_hashes: frozenset[str]
    basename_hash: str | None
    snippet_grams: frozenset[str]


@dataclass(frozen=True)
class LedgerEntry:
    """One ``tool_use`` in a stream (every call is indexed, not only entries)."""

    stream_key: str
    ordinal: int
    call_key: str
    message_key: str | None
    eligible: bool
    """The tool is one the hook surfaces on."""
    ok: bool | None
    """True for a successful result, False for an error, None when none was seen."""
    ts: float | None
    paths: frozenset[str] = frozenset()
    patterns: frozenset[str] = frozenset()


@dataclass(frozen=True)
class OutputRecord:
    """The 4-gram hashes of one assistant record's text and tool inputs."""

    stream_key: str
    ordinal: int
    ts: float | None
    grams: frozenset[str]


@dataclass(frozen=True)
class InjectionRecord:
    """One ``hook_additional_context`` record."""

    stream_key: str
    ordinal: int
    call_key: str | None
    ts: float | None
    event_key: str | None
    """The keyed ``_surfacing_id`` the block carries, if any."""
    header_sha256: str
    stm_wrapped: bool
    grams: frozenset[str]


@dataclass(frozen=True)
class Ledger:
    """Everything taken from the transcripts."""

    streams: frozenset[str]
    """Stream keys that were extracted at least once."""
    entries: tuple[LedgerEntry, ...] = ()
    outputs: tuple[OutputRecord, ...] = ()
    injections: tuple[InjectionRecord, ...] = ()


@dataclass(frozen=True)
class Outcome:
    """One outcome row: an eligible memory of an assigned event.

    ``y1``, ``y2`` and ``reexposed`` are None when ``status`` is not ``ok``
    (a missing outcome, reported by arm, never dropped). Delivery and the
    integrity flags are event facts, repeated on each of the event's rows.
    """

    event_key: str
    memory_key: str
    arm: Arm
    status: Status
    y1: bool | None
    y2: bool | None
    reexposed: bool | None
    delivery: Delivery
    delivery_basis: DeliveryBasis
    window_len: int
    arm_violation: bool
    duplicate_run: bool
    stream_mismatch: bool
    stale_entry: bool
    clock_anomaly: bool
    null_ts: bool
    """An entry, output or block of the stream had no timestamp to place it by."""
    shared_ordinal: bool
    """Two window entries share one transcript record, so their order is by call key."""


@dataclass(frozen=True)
class _Window:
    entries: tuple[LedgerEntry, ...]
    span_end: float
    stale_entry: bool
    clock_anomaly: bool
    null_ts: bool
    shared_ordinal: bool


def resolve(
    assignments: Iterable[Assignment],
    memory_rows: Iterable[MemoryRow],
    ledger: Ledger,
    stoplist: Iterable[str],
    n: int = WINDOW_ENTRIES,
    t: float = WINDOW_SECONDS,
) -> tuple[Outcome, ...]:
    """Resolve every eligible memory row of every assigned event.

    Rows come back sorted by ``(event_key, memory_key)``, so the result does
    not depend on the order of any input.
    """
    stop = frozenset(stoplist)
    by_stream: dict[str, list[LedgerEntry]] = defaultdict(list)
    streams_of_call: dict[str, set[str]] = defaultdict(set)
    for entry in ledger.entries:
        by_stream[entry.stream_key].append(entry)
        streams_of_call[entry.call_key].add(entry.stream_key)
    for stream_entries in by_stream.values():
        stream_entries.sort(key=lambda e: (e.ordinal, e.call_key))
    outputs: dict[str, list[OutputRecord]] = defaultdict(list)
    for output in ledger.outputs:
        outputs[output.stream_key].append(output)
    injections: dict[str, list[InjectionRecord]] = defaultdict(list)
    for injection in ledger.injections:
        injections[injection.stream_key].append(injection)
    # outputs and blocks without a timestamp cannot be placed against ts(s)
    untimed = {o.stream_key for o in ledger.outputs if o.ts is None} | {
        i.stream_key for i in ledger.injections if i.ts is None
    }

    events = sorted(assignments, key=lambda a: a.event_key)
    runs: dict[tuple[str | None, str | None], int] = defaultdict(int)
    for event in events:
        if event.stream_key is not None and event.call_key is not None:
            runs[(event.stream_key, event.call_key)] += 1
    memories: dict[str, list[MemoryRow]] = defaultdict(list)
    for memory in memory_rows:
        if memory.eligible:
            memories[memory.event_key].append(memory)

    out: list[Outcome] = []
    for event in events:
        rows = sorted(memories.get(event.event_key, ()), key=lambda r: r.memory_key)
        if not rows:
            continue
        stream = by_stream.get(event.stream_key or "", [])
        on_call = [
            i
            for i in injections.get(event.stream_key or "", ())
            if event.call_key is not None and i.call_key == event.call_key
        ]
        delivery, basis = _delivery(event, on_call)
        anchor = next((e for e in stream if e.call_key == event.call_key), None)
        mismatch = (
            anchor is None
            and event.call_key is not None
            and bool(streams_of_call.get(event.call_key, set()) - {event.stream_key})
        )
        if event.stream_key is None or event.stream_key not in ledger.streams:
            status: Status = "not_extracted"
        elif anchor is None:
            status = "no_ledger"
        elif anchor.ts is None:
            status = "no_timestamp"
        else:
            status = "ok"
        window = _window(stream, anchor, n, t) if status == "ok" and anchor is not None else None
        # a withheld call with any STM block is a violation: one in STM's wrapper (of any
        # event), or one identified as this event's by its id or its header
        violation = event.arm == "withheld" and (
            delivery != "undelivered"
            or any(i.stm_wrapped or i.event_key == event.event_key for i in on_call)
        )
        # only two events known to share one call are a duplicate run
        duplicate = runs.get((event.stream_key, event.call_key), 0) > 1
        for row in rows:
            if window is None or anchor is None or anchor.ts is None:
                y1 = y2 = reexposed = None
            else:
                grams = row.snippet_grams - stop
                y1 = any(_names_file(entry, row) for entry in window.entries)
                y2 = _after_only(outputs.get(anchor.stream_key, ()), grams, anchor.ts, t)
                reexposed = _reexposed(injections.get(anchor.stream_key, ()), anchor, grams, window)
            out.append(
                Outcome(
                    event_key=event.event_key,
                    memory_key=row.memory_key,
                    arm=event.arm,
                    status=status,
                    y1=y1,
                    y2=y2,
                    reexposed=reexposed,
                    delivery=delivery,
                    delivery_basis=basis,
                    window_len=len(window.entries) if window else 0,
                    arm_violation=violation,
                    duplicate_run=duplicate,
                    stream_mismatch=mismatch,
                    stale_entry=bool(window and window.stale_entry),
                    clock_anomaly=bool(window and window.clock_anomaly),
                    null_ts=bool(window and (window.null_ts or event.stream_key in untimed)),
                    shared_ordinal=bool(window and window.shared_ordinal),
                )
            )
    return tuple(out)


def _delivery(event: Assignment, on_call: list[InjectionRecord]) -> tuple[Delivery, DeliveryBasis]:
    """Delivered by the event's own id, else by exactly one id-less header match.

    Without its own id record — never advertised, or withdrawn after a late
    write and re-rendered without it — the header rule decides, so every label
    but an id match carries basis ``header``. A block carrying another event's
    id is never this event's delivery, even when its header matches: two events
    can share one call.
    """
    if event.id_advertised and any(i.event_key == event.event_key for i in on_call):
        return "delivered", "id"
    matches = sum(
        1
        for i in on_call
        if i.event_key is None
        and event.header_digest is not None
        and i.header_sha256 == event.header_digest
    )
    if matches == 1:
        return "delivered", "header"
    if matches == 0:
        return "undelivered", "header"
    return "ambiguous_delivery", "header"


def _window(stream: list[LedgerEntry], anchor: LedgerEntry, n: int, t: float) -> _Window:
    assert anchor.ts is not None
    start = stream.index(anchor) + 1
    if anchor.message_key is not None:
        for i, entry in enumerate(stream):
            if entry.message_key == anchor.message_key:
                start = max(start, i + 1)
    limit = anchor.ts + t
    running = anchor.ts
    cut_by_time = False
    kept: list[LedgerEntry] = []
    stale = anomaly = null_ts = boundary_tie = False
    for entry in stream[start:]:
        if not entry.eligible or entry.ok is not True:
            continue
        if len(kept) >= n:
            # the first entry left out shares the last kept entry's record: which of
            # the two made the window was decided by call key, not by the transcript
            boundary_tie = bool(kept) and entry.ordinal == kept[-1].ordinal
            break
        if entry.ts is None:
            null_ts = True
            continue
        if entry.ts < anchor.ts:
            stale = True
            continue
        if entry.ts > limit:
            cut_by_time = True
            break
        if entry.ts < running:
            anomaly = True
        running = max(running, entry.ts)
        kept.append(entry)
    ordinals = [e.ordinal for e in kept]
    return _Window(
        entries=tuple(kept),
        span_end=limit if cut_by_time else running,
        stale_entry=stale,
        clock_anomaly=anomaly,
        null_ts=null_ts,
        shared_ordinal=boundary_tie or len(set(ordinals)) != len(ordinals),
    )


def _names_file(entry: LedgerEntry, row: MemoryRow) -> bool:
    if row.path_hash_lexical is not None and row.path_hash_lexical in entry.paths:
        return True
    return (
        row.basename_hash is not None
        and row.basename_hash in entry.patterns
        and not entry.paths.isdisjoint(row.dir_hashes)
    )


def _after_only(
    outputs: Iterable[OutputRecord], grams: frozenset[str], anchor_ts: float, t: float
) -> bool:
    """A gram in the output within ``t`` after *s* that was not there within ``t`` before.

    The anchor's own record sits at ``ts(s)`` and counts as before: it was
    written before the injection could be read.
    """
    if not grams:
        return False
    before: set[str] = set()
    after: set[str] = set()
    for output in outputs:
        if output.ts is None:
            continue
        if anchor_ts - t <= output.ts <= anchor_ts:
            before |= output.grams & grams
        elif anchor_ts < output.ts <= anchor_ts + t:
            after |= output.grams & grams
    return bool(after - before)


def _reexposed(
    injections: Iterable[InjectionRecord],
    anchor: LedgerEntry,
    grams: frozenset[str],
    window: _Window,
) -> bool:
    """A later STM block within the window carries one of the grams.

    Later means after the anchor in transcript order, whatever its timestamp.
    Within means on one of the window's calls — a hook's block is written after
    its call, so it can postdate the window's last timestamp — or dated inside
    the window's time span. The anchor's own block is the exposure itself.
    """
    assert anchor.ts is not None
    anchor_ts, span_end = anchor.ts, window.span_end
    window_calls = {e.call_key for e in window.entries}
    return bool(grams) and any(
        i.stm_wrapped
        and i.ordinal > anchor.ordinal
        and i.call_key != anchor.call_key
        and (i.call_key in window_calls or (i.ts is not None and anchor_ts < i.ts <= span_end))
        and not i.grams.isdisjoint(grams)
        for i in injections
    )
