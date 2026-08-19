"""Append-only JSONL sink for tool-selection telemetry (#467).

The proxy is the one component in the call path, so it can record what an
advisory analyzer never sees: which tool the client model actually called,
out of which advertised candidate set, and how the call went. This log is
the substrate for offline replay/eval (#468) and every later learning stage
(#469/#470) — and the landing zone for the STM-native hard filter's (#465)
``reject_reasons``, so replay sees the tools that were withheld, not just
the ones advertised.

Schema v1 — three event types, every record self-describing via
``schema_version`` + ``ranker_version``; one JSON object per line, keys
sorted, so a replay harness can stream-parse and diff runs:

``selection``
    ``selection_id`` (joins the paired ``execution`` row), ``trace_id``
    (joins ``proxy_metrics.db`` for full per-stage diagnostics),
    ``server``, ``selected_tool`` (prefixed name, same vocabulary as
    ``candidate_tools``), ``candidate_tools`` (what the proxy last
    advertised — today the client model does the selecting),
    ``candidate_count``, ``reject_reasons`` (prefixed tool → reason code
    for every tool the #465 hard filter withheld from that advertisement;
    ``{}`` when nothing was rejected or the filter never ran — reason
    vocabulary in ``proxy/tool_eligibility.py``), ``candidate_features``
    (#466 ranking output) / ``graph_generation`` (reserved ``null`` until
    toolgraph#13/#15), ``args_sha256`` + ``args_chars`` (canonical-JSON
    hash — see redaction below), ``ts``.

``execution``
    ``selection_id`` / ``trace_id`` / ``server`` / ``selected_tool``
    mirroring the paired selection, ``ok``, ``latency_ms`` (proxy-side
    wall time for the full pipeline, what the agent experienced),
    ``error_type`` (exception class name; the *typed* error category
    stays in ``proxy_metrics.db``, joinable via ``trace_id`` — not
    duplicated here), ``cache_hit`` (``true`` when the result was served
    from the proxy response cache, ``false`` on a live upstream call,
    ``null`` when telemetry can't attribute it — e.g. an in-pipeline
    raise), ``retry_count`` / ``cost`` (reserved ``null``), ``ts``.

``feedback``
    ``selection_id`` (+ optional ``trace_id``), ``user_corrected``,
    ``operator_override``, ``ts``. Both label fields are three-valued:
    ``false`` records that the selection was RIGHT (a positive example),
    ``null`` records nothing for that field. ``ranker_version`` mirrors the
    labelled selection's stamp, not the emitter's. Written by
    ``mms selection feedback`` (#469), the operator-facing labelling command;
    nothing on the proxy's call path emits it, because the client model never
    sees a ``selection_id`` to reference. Labels join their selection
    left-outer, like executions, and several may accumulate for one
    selection — a later non-null value supersedes an earlier one per field.

Redaction policy is structural, not filter-based: no field ever carries
raw arguments, results, prompts, resource URIs, or error message strings —
argument payloads appear only as a sha256 over their canonical JSON plus a
char count. As a belt-and-suspenders backstop (the storage-gating rule from
``privacy.py``), every serialized line is screened with
``contains_sensitive_content`` before persisting and dropped (counted) on a
match, mirroring the never-persist-secrets rule from #460/#462. Files are
created ``0o600`` (#458); the parent directory is created ``0o700``, but a
pre-existing parent's mode is left untouched — the same posture as every
other STM store (``MetricsStore``, ``memory_ops`` #456/#464): ``mkdir`` mode
applies at creation time only, and the file mode alone guards the content.

Sampling and write failures apply to the whole selection+execution pair at
selection time — ``log_selection`` returning ``None`` means the call was
sampled out or its record never reached disk, and the caller skips the
execution event, so neither produces orphan halves. Redaction drops, by
contrast, are per-record (never-persist beats pairing): an execution whose
paired selection was dropped can appear alone, so replay tooling must treat
``selection_id`` joins as left-outer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import sys
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

from memtomem_stm.proxy.metrics import _percentile
from memtomem_stm.proxy.privacy import contains_sensitive_content
from memtomem_stm.utils import json_out
from memtomem_stm.utils.fileio import fsync_dir
from memtomem_stm.utils.locking import open_lock_fd, release_lock, try_lock

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Longest line the log's readers will parse. Defined here rather than in the
# replay harness that first needed it, because the labelling command has to
# apply the SAME cut: a line one reader parses and the other drops is a
# selection the two disagree exists.
MAX_LINE_BYTES = 1_048_576

# ``_append`` outcomes. Separate values because "consumed" and "on disk" are
# not the same fact, and which one a caller needs depends on whether a human
# is waiting to hear that their record exists.
APPEND_WRITTEN = "written"
APPEND_REDACTED = "redacted"
APPEND_FAILED = "failed"
# The record's bytes are complete in the file, but something about them could
# not be established. Three causes, deliberately sharing one status because the
# caller's move is identical for all of them: a durable append whose ``fsync``
# or ``close`` failed (bytes may not survive a crash), a reachability probe
# that could not run (unknown whether a rotation orphaned them), and a short
# write repaired by a second ``os.write`` (another writer may have appended
# between the two, fusing the lines). Distinct from ``failed`` because the two
# need opposite handling: a failure means "write it again", while an
# unconfirmed append may already be on disk, so the caller must be told what is
# and is not known rather than being handed a verdict the process cannot
# support.
APPEND_UNCONFIRMED = "unconfirmed"
APPEND_STATUSES = (APPEND_WRITTEN, APPEND_REDACTED, APPEND_FAILED, APPEND_UNCONFIRMED)


def rotation_lock_path(log_path: Path | str) -> Path:
    """Advisory lock guarding the log's file *identities*, not its contents.

    Appends need no lock — ``O_APPEND`` is atomic for small writes, which is
    why the writer takes none per record, and why an append that a rotation
    orphans is *detected* afterwards rather than excluded beforehand (see
    ``_still_reachable``). Rotation is different: it renames every segment at
    once, so a READER holding a filename can end up scanning a file that is no
    longer what it thought, and with ``max_backups == 0`` the oldest content is
    unlinked outright. This lock exists only for that window, and only readers
    and the rotation itself take it.
    """
    log_path = Path(log_path).expanduser()
    return log_path.with_name(log_path.name + ".rotate.lock")


@contextmanager
def _hold_lock(lock_path: Path, *, attempts: int, delay: float) -> Iterator[bool]:
    """Hold *lock_path* for the block; yield whether it was acquired."""
    fd = open_lock_fd(lock_path)
    acquired = False
    try:
        for attempt in range(max(1, attempts)):
            if try_lock(fd):
                acquired = True
                break
            if attempt + 1 < max(1, attempts):
                time.sleep(delay)
        yield acquired
    finally:
        release_lock(fd)


@contextmanager
def rotation_lock(
    log_path: Path | str, *, attempts: int = 1, delay: float = 0.05
) -> Iterator[bool]:
    """Hold the rotation lock for the block; yields whether it was acquired.

    Never blocks. The writer takes it *only when it has already decided to
    rotate* and simply defers rotation when it cannot — a size-triggered
    rotation is not urgent, and the next append retries — so a reader holding
    the lock can never stall a proxied call. Readers may retry briefly, since
    the writer's hold is a handful of renames.
    """
    with _hold_lock(rotation_lock_path(log_path), attempts=attempts, delay=delay) as acquired:
        yield acquired


# Per-record default when no ranking informed the call — the client model
# picked from the full advertised set unaided — so replay tooling can treat
# this version as the unranked baseline. Calls where a ranker ran stamp
# their own version (e.g. ``tool_relevance.RANKER_VERSION_BM25``) on BOTH
# halves of the pair via the ``ranker_version`` parameter, letting replay
# split cohorts on this field alone.
RANKER_VERSION = "v0-passthrough"


def _still_reachable(fd: int) -> bool | None:
    """Whether anything still names the file this descriptor wrote to.

    ``True`` reachable, ``False`` orphaned, ``None`` could not be established.

    Rotation can destroy the directory entry while an append already holds the
    inode open, and the append then succeeds into storage no reader can open.
    Two ways in: ``max_backups == 0`` unlinks outright, and every other setting
    evicts the same inode once ``max_backups + 1`` rotations have shifted it
    past the last backup slot — a rename orphans an append just as thoroughly
    as an unlink, it only takes more of them.

    What this is NOT is a guarantee, and the difference matters enough to name.
    It describes the instant it ran: a rotation landing immediately afterwards
    can still evict the record, and ``st_nlink`` counts *any* link, so an
    operator's hard link to the log would report reachable while no reader
    scans that name. It is a detector that turns the common silent loss into a
    counted, reported one. A caller that needs the stronger statement — the
    label emitter — takes the rotation lock across its append and its report,
    which is what actually excludes a concurrent rotation.

    ``None`` rather than an optimistic ``True`` when the probe itself fails:
    "I could not tell" is what ``unconfirmed`` exists to say, and answering
    ``written`` there would be the same overstatement in miniature.

    Windows is exempt: an open descriptor blocks the rename, so rotation fails
    rather than orphaning the append. That is the documented behaviour of
    ``os.open`` without ``FILE_SHARE_DELETE`` on local volumes — the same
    property ``utils.fileio._replace_with_windows_retry`` exists to ride out —
    and is not claimed for network shares.
    """
    if sys.platform == "win32":  # pragma: no cover - POSIX-only rotation hazard
        return True
    try:
        return os.fstat(fd).st_nlink > 0
    except OSError:
        logger.debug("Could not probe whether the selection log is still linked", exc_info=True)
        return None


def _needs_leading_newline(path: Path) -> bool:
    """Whether an append to *path* must open a line of its own first.

    True when the file's last byte is not a newline — a crash mid-append, or a
    hand edit — because an append landing there is swallowed into that line and
    the writer would be told its record survived while readers reject the fused
    line.

    Probed through its own read handle rather than the append descriptor: a
    read-modify sequence on an ``O_APPEND`` descriptor would need a separate
    argument about what each platform guarantees, and this needs none — the
    append itself is unchanged. A missing or empty file needs nothing (there is
    no line to fuse with); any other failure answers **True**, because the
    unknown case is the one where a wrong guess costs a record. A stray blank
    line, which is what that guess costs when the file was in fact terminated,
    is skipped by every reader of this log.
    """
    try:
        with path.open("rb") as fh:
            if fh.seek(0, os.SEEK_END) == 0:
                return False
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except FileNotFoundError:
        return False
    except OSError:
        logger.debug("Could not probe the selection log's tail", exc_info=True)
        return True


def _canonical_args(arguments: dict[str, Any] | None) -> str:
    """Serialize call arguments deterministically for hashing.

    ``default=str`` keeps non-JSON-native values (e.g. Path) from raising —
    the output is only ever hashed, never persisted, so a lossy fallback
    rendering is fine as long as it is stable. Surrogate-safe because that
    hash encodes it (#757): "only hashed" is not the same as "never
    encoded", and an upstream tool argument is exactly the kind of value
    that carries one.
    """
    return json_out.dumps(
        arguments or {},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


class SelectionTelemetryLog:
    """Append-only JSONL writer with rotation, sampling, and redaction.

    Write failures never propagate: a telemetry problem must not break the
    proxied call it accounts for. Failures are counted (``write_errors``)
    and logged at WARNING once per process, DEBUG thereafter.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = 50_000_000,
        max_backups: int = 3,
        sample_rate: float = 1.0,
        rng: random.Random | None = None,
    ) -> None:
        self._path = Path(path).expanduser()
        self._max_bytes = max_bytes
        self._max_backups = max_backups
        self._sample_rate = sample_rate
        # Injectable for deterministic sampling tests; rates 0.0 / 1.0
        # short-circuit and never consult it.
        self._rng = rng or random.Random()
        # Writers share the lock defensively, same posture as MetricsStore:
        # callers are asyncio single-thread today, but the guard makes the
        # store ready for an off-loop move without a rewrite.
        self._lock = threading.Lock()
        self._warned_write_failure = False
        # Counters are part of the observable contract (wire-in tests
        # snapshot them); mutate only under ``self._lock``.
        self.events_written = 0
        self.events_sampled_out = 0
        self.redaction_drops = 0
        self.write_errors = 0

    @property
    def path(self) -> Path:
        return self._path

    def initialize(self) -> None:
        """Create the log file privately (0600 file; parent created 0700).

        Also tightens a pre-existing *file* left at a permissive mode by an
        older run or umask — same rationale as
        ``sqlite_private.ensure_private_db_files``. A pre-existing *parent
        directory* keeps its mode (see module docstring).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        try:
            self._path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        """No-op — the file is opened per append so rotation never holds a
        stale descriptor. Exists so lifespan teardown can treat every store
        uniformly."""

    def snapshot(self) -> dict[str, int]:
        """Return this process's live write-path counters.

        These reflect the running instance only (reset at restart), unlike
        ``aggregate_selection_log`` which reads the persisted history off
        disk — the same in-memory-vs-store split as ``stm_surfacing_stats``.
        Read under ``self._lock`` so a concurrent
        ``_append`` can't tear a counter.
        """
        with self._lock:
            return {
                "events_written": self.events_written,
                "events_sampled_out": self.events_sampled_out,
                "redaction_drops": self.redaction_drops,
                "write_errors": self.write_errors,
            }

    # ── event emitters ───────────────────────────────────────────────────

    def log_selection(
        self,
        *,
        server: str,
        selected_tool: str,
        candidate_tools: list[str],
        arguments: dict[str, Any] | None,
        trace_id: str | None,
        candidate_features: dict[str, Any] | None = None,
        ranker_version: str | None = None,
        reject_reasons: dict[str, str] | None = None,
        graph_generation: int | None = None,
    ) -> str | None:
        """Record a selection event; returns its ``selection_id``.

        Returns ``None`` when the call is sampled out — the caller must then
        skip ``log_execution`` so pairs stay atomic. ``candidate_features``
        is the ranker's output object (#466) — the caller guarantees it
        carries no raw text, only scores/hashes; ``ranker_version`` stamps
        which ranker produced it (``None`` = the unranked default).
        ``reject_reasons`` is the #465 hard filter's verdict for the
        advertisement this call selected from — reason codes only, no tool
        metadata (``None`` records the empty map). ``graph_generation`` pins
        the external tool-graph generation the advertisement was filtered
        under (#465/#468 replay) — ``None`` when the provider is disabled,
        degraded, or unconsulted.
        """
        if not self._sampled_in():
            with self._lock:
                self.events_sampled_out += 1
            return None
        selection_id = uuid.uuid4().hex
        canonical = _canonical_args(arguments)
        appended = self._append(
            {
                "schema_version": SCHEMA_VERSION,
                "ranker_version": ranker_version or RANKER_VERSION,
                "event": "selection",
                "ts": time.time(),
                "selection_id": selection_id,
                "trace_id": trace_id,
                "server": server,
                "selected_tool": selected_tool,
                "candidate_tools": list(candidate_tools),
                "candidate_count": len(candidate_tools),
                "reject_reasons": dict(reject_reasons) if reject_reasons else {},
                "candidate_features": candidate_features,
                "graph_generation": graph_generation,
                "args_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "args_chars": len(canonical),
            }
        )
        # A write failure means the selection never reached disk — report
        # ``None`` so the caller skips the paired execution event rather
        # than persisting an orphan half (a transient failure could let the
        # execution write succeed). An intentional redaction drop still
        # returns the id: that is the documented left-outer case.
        return selection_id if appended != APPEND_FAILED else None

    def log_execution(
        self,
        *,
        selection_id: str,
        trace_id: str | None,
        server: str,
        selected_tool: str,
        ok: bool,
        latency_ms: float,
        error_type: str | None = None,
        ranker_version: str | None = None,
        cache_hit: bool | None = None,
    ) -> None:
        self._append(
            {
                "schema_version": SCHEMA_VERSION,
                "ranker_version": ranker_version or RANKER_VERSION,
                "event": "execution",
                "ts": time.time(),
                "selection_id": selection_id,
                "trace_id": trace_id,
                "server": server,
                "selected_tool": selected_tool,
                "ok": ok,
                "latency_ms": round(latency_ms, 3),
                "error_type": error_type,
                "retry_count": None,
                "cost": None,
                "cache_hit": cache_hit,
            }
        )

    def log_feedback(
        self,
        *,
        selection_id: str,
        trace_id: str | None = None,
        user_corrected: bool | None = None,
        operator_override: bool | None = None,
        ranker_version: str | None = None,
    ) -> str:
        """Record a label for one selection; returns the append status.

        Unlike the call-path emitters this one reports its outcome, because its
        caller is a person waiting to hear whether the label exists. A silent
        failure here would tell an operator their judgement was recorded when
        nothing was written.

        The append is *durable*: unlike the call-path emitters, whose records
        are one sample among many, this one is a human's judgement that exists
        nowhere else, so the write is flushed to the storage device — and the
        directory entry with it when this append created the file — before the
        status says it exists. ``"unconfirmed"`` is the honest answer when that
        flush could not be completed.

        ``ranker_version`` must be the stamp of the selection being labelled,
        not this process's: every record is self-describing, and a label that
        claims the unranked baseline while pointing at a ranked selection would
        put itself in the wrong cohort the moment replay splits feedback by
        this field. The emitter reads it off the record it resolved.
        """
        return self._append(
            {
                "schema_version": SCHEMA_VERSION,
                "ranker_version": ranker_version or RANKER_VERSION,
                "event": "feedback",
                "ts": time.time(),
                "selection_id": selection_id,
                "trace_id": trace_id,
                "user_corrected": user_corrected,
                "operator_override": operator_override,
            },
            durable=True,
        )

    # ── internals ────────────────────────────────────────────────────────

    def _sampled_in(self) -> bool:
        if self._sample_rate >= 1.0:
            return True
        if self._sample_rate <= 0.0:
            return False
        return self._rng.random() < self._sample_rate

    def _append(self, record: dict[str, Any], *, durable: bool = False) -> str:
        """Persist one record; returns one of :data:`APPEND_STATUSES`.

        Four outcomes, not two, because they do not all look alike to *some*
        callers: ``"redacted"`` means the record was consumed but wrote
        nothing, which ``log_selection`` treats like a success (keeping the
        documented left-outer pairing semantics) and an operator-facing caller
        must not — a label that never reached disk is not a recorded label.
        ``"failed"`` means no complete record was written. ``"unconfirmed"``
        means the record's bytes are complete but the flush that would prove
        they survive a crash did not complete. ``"written"`` is the only
        outcome where the append completed — and, when *durable* was asked for,
        where the flush proving it survives a crash completed too. A call-path
        append returns it without any flush, which is the point of asking.

        With *durable*, the descriptor (and, when this append created the file,
        its parent directory) is ``fsync``-ed before reporting success. Paid
        only by the operator labelling path: on the proxied call path it would
        put a device flush in front of every upstream call to insure a record
        that sampling is entitled to drop outright.

        **What "failed" does and does not promise.** No *record* is written —
        nothing a reader will parse, join, or count. It does not promise the
        file is byte-identical to before: a write that lands short leaves that
        fragment behind, newline-terminated so it cannot swallow the next
        record (see :meth:`_terminate_fragment`). Rolling it back would mean
        truncating a file other processes append to concurrently, which would
        destroy *their* records to tidy up this one. Readers count the fragment
        as one malformed line; it carries no ``selection_id``, so it joins
        nothing and inflates no cohort.
        """
        # The ``.encode`` below sits outside this method's write-failure
        # handling, so an unencodable record would raise past the caller's
        # "nothing reached disk" contract rather than returning APPEND_FAILED (#757).
        line = json_out.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        # Structural redaction means no payload text should ever be here;
        # this screen is the storage-gating backstop (full DEFAULT_PATTERNS
        # per the privacy.py contract) against a secret smuggled through a
        # field nobody expected to carry one — e.g. an upstream tool *name*.
        # A false positive costs one dropped telemetry record, never data.
        if contains_sensitive_content(line):
            with self._lock:
                self.redaction_drops += 1
            logger.warning(
                "Dropped a selection-telemetry record that matched a sensitive-content pattern"
            )
            return APPEND_REDACTED
        data = (line + "\n").encode("utf-8")
        if len(data) - 1 > MAX_LINE_BYTES:
            # Every reader of this log drops a line longer than this
            # (``TelemetryReader._admit``), so writing one would put bytes on
            # disk that each consumer counts as oversized and skips — a record
            # that exists for nobody. Refused here so the writer and the
            # readers cannot disagree about what a record is.
            with self._lock:
                self.write_errors += 1
            logger.warning(
                "Selection telemetry record refused: %d bytes exceeds the %d-byte line limit",
                len(data) - 1,
                MAX_LINE_BYTES,
            )
            return APPEND_FAILED
        # Synchronous file append on the asyncio event loop: while it runs,
        # every runnable coroutine stalls — other in-flight proxied calls
        # included. Accepted for the current local single-MCP-client
        # deployment (same contract as MetricsStore.record): a small
        # O_APPEND write is far cheaper than the upstream call it accounts
        # for. Multi-client serving is the reopen trigger to move this
        # off-loop; writers already serialize on ``self._lock``.
        with self._lock:
            try:
                self._rotate_if_needed_locked()
                # Whether THIS append creates the file decides if the
                # directory entry also needs syncing: an fsync on the
                # descriptor alone does not promise a newly created name
                # survives a crash. Checked under the lock, immediately before
                # the open, so at most a concurrent external creator makes it
                # conservative (one extra directory sync), never optimistic.
                created = durable and not self._path.exists()
                # Probed before the open, through a read handle of its own, so
                # the append descriptor keeps the plain write-only append it
                # has always had.
                payload = b"\n" + data if _needs_leading_newline(self._path) else data
                fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                closed = False
                try:
                    written = os.write(fd, payload)
                    if written != len(payload):
                        # Close the fragment's LINE while the descriptor is
                        # still open. Without a newline the next append is
                        # concatenated onto it, and THAT caller — whose own
                        # write succeeded — would be told its record survived
                        # while readers reject the fused line. One byte turns a
                        # cascading corruption into a single skipped line.
                        #
                        # When the ONLY missing byte was that newline, the
                        # repair restores the exact intended bytes: the record
                        # is complete and readable, so reporting a failure
                        # would be false — and would invite a retry that
                        # duplicates the label.
                        repaired = self._terminate_fragment(fd, payload, written)
                        if repaired and written == len(payload) - 1:
                            # Every byte of the record is now on disk — but it
                            # took two writes to get there, with no
                            # cross-process lock between them. Another writer's
                            # append can land in that gap, and this newline
                            # then closes THEIR line while ours is fused onto
                            # it. Complete or corrupt, and this process cannot
                            # tell which apart without reading the file back:
                            # exactly the "may already be on disk, do not
                            # simply retry" case ``unconfirmed`` names.
                            self.write_errors += 1
                            logger.warning(
                                "Selection telemetry record needed a newline repair; another "
                                "writer may have appended between the two writes"
                            )
                            return APPEND_UNCONFIRMED
                    if written != len(payload):
                        # No record is on disk, and a caller waiting to hear
                        # that its label exists must not be told otherwise.
                        # Never retried at this level: appending the remainder
                        # would duplicate the prefix into a second fragment.
                        # The terminated fragment stays (see the method
                        # docstring) — truncating it back would mean rewinding
                        # a file other processes append to concurrently.
                        self.write_errors += 1
                        logger.warning(
                            "Selection telemetry write was short (%d of %d bytes)",
                            written,
                            len(payload),
                        )
                        return APPEND_FAILED
                    reachable = _still_reachable(fd)
                    if reachable is False:
                        # The bytes are complete, and in an inode a rotation
                        # unlinked while this append held it open — durable,
                        # perhaps, and named by nothing. No reader will ever
                        # see this record, so the one verdict that must not be
                        # returned is the one that says it exists.
                        self.write_errors += 1
                        logger.warning(
                            "Selection telemetry write landed in a log segment that was "
                            "rotated away before the write completed"
                        )
                        return APPEND_FAILED
                    if reachable is None:
                        # The probe itself failed, so whether a reader can
                        # reach this record is unknown — not established, and
                        # therefore not something to report as written.
                        self.write_errors += 1
                        logger.warning(
                            "Selection telemetry write could not be confirmed reachable",
                        )
                        return APPEND_UNCONFIRMED
                    # From here the record's bytes are complete in a file that
                    # still had a name a moment ago, so nothing below may
                    # report ``failed``: that would send an operator to write
                    # the same label a second time on the strength of a claim
                    # this process cannot make. What can still be unknown is
                    # whether the bytes SURVIVE, which is what ``unconfirmed``
                    # says.
                    try:
                        if durable:
                            os.fsync(fd)
                    except OSError:
                        self.write_errors += 1
                        logger.warning(
                            "Selection telemetry write could not be flushed to disk",
                            exc_info=True,
                        )
                        return APPEND_UNCONFIRMED
                    # Marked closed before the call: a failed ``close`` still
                    # consumes the descriptor, so retrying it in ``finally``
                    # could close an unrelated file this process later opens.
                    closed = True
                    try:
                        os.close(fd)
                    except OSError:
                        self.write_errors += 1
                        logger.warning(
                            "Selection telemetry descriptor failed to close after a complete write",
                            exc_info=True,
                        )
                        return APPEND_UNCONFIRMED
                    if created and not fsync_dir(self._path.parent):
                        self.write_errors += 1
                        return APPEND_UNCONFIRMED
                finally:
                    if not closed:
                        try:
                            os.close(fd)
                        except OSError:
                            # Swallowed deliberately. This close runs on paths
                            # that have already decided what to report — a
                            # short write, or a flush that failed with the
                            # record complete in the file. Letting it raise
                            # would hand those returns to the OSError handler
                            # below and turn an ``unconfirmed`` into a
                            # ``failed``, which is the one answer that sends an
                            # operator to write the label a second time.
                            logger.debug(
                                "Could not close the selection telemetry descriptor",
                                exc_info=True,
                            )
                self.events_written += 1
            except OSError:
                self.write_errors += 1
                if not self._warned_write_failure:
                    self._warned_write_failure = True
                    logger.warning(
                        "Selection telemetry write failed — proxied calls unaffected",
                        exc_info=True,
                    )
                else:
                    logger.debug("Selection telemetry write failed", exc_info=True)
                return APPEND_FAILED
        return APPEND_WRITTEN

    @staticmethod
    def _terminate_fragment(fd: int, data: bytes, written: int) -> bool:
        """Best-effort newline after a short write; ``True`` if one was added.

        Failing here costs what the missing newline would have cost anyway, so
        it must never mask the short write that caused it.

        This is a second write, and it is NOT serialized against other
        processes appending to the same file. A concurrent writer landing
        between the fragment and this newline fuses the two into one malformed
        line — but that needs a short write and a second writer at the same
        instant, and the alternative is a cross-process lock on every append,
        paid by every proxied call to insure against an intersection of two
        rare faults. Readers count the fused line as malformed rather than
        misparsing it.
        """
        if written <= 0 or data[:written].endswith(b"\n"):
            return False
        try:
            return os.write(fd, b"\n") == 1
        except OSError:
            logger.debug("Could not terminate a short-written telemetry record", exc_info=True)
            return False

    def _rotate_if_needed_locked(self) -> None:
        """Size-based rotation. The caller holds no lock on entry.

        ``log → log.1 → … → log.N``, oldest dropped. Renames preserve the 0600
        mode; the fresh file is recreated 0600 by the ``os.open`` in
        ``_append``. With ``max_backups == 0`` the file is simply truncated by
        deletion (append recreates it).

        Does not decide whether the append may proceed. An append that this or
        another process's rotation orphans is detected afterwards by
        ``_still_reachable`` rather than excluded beforehand, because the
        settings deciding whether rotation unlinks belong to whichever process
        rotates — which may not be this one. Rotation itself is never urgent: a
        deferred one fires again on the next append, since the size trigger is
        still true.
        """
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size < self._max_bytes:
            return
        # Taken here and nowhere else on the write path: the size check above
        # has already passed, so this costs one open+flock per actual rotation,
        # not per record. A reader (``mms selection feedback``) holding it
        # means a resolve/append is in flight against these exact filenames;
        # deferring is harmless, while renaming underneath that reader is not.
        with rotation_lock(self._path) as acquired:
            if not acquired:
                logger.debug("Deferring selection-log rotation: the log is locked")
                return
            self._rotate_locked()

    def _rotate_locked(self) -> None:
        """Perform the renames. The caller holds the rotation lock.

        Note what this does NOT coordinate with: an append already holding a
        descriptor. Renaming the inode it writes to is harmless once, but the
        same inode is evicted after ``max_backups + 1`` rotations, and
        ``max_backups == 0`` unlinks it immediately. The appender detects that
        for itself (``_still_reachable``) rather than being excluded here,
        because the rotator whose settings decide the hazard may be another
        process entirely.
        """
        if self._max_backups <= 0:
            self._path.unlink(missing_ok=True)
            return
        for i in range(self._max_backups - 1, 0, -1):
            src = self._path.with_name(f"{self._path.name}.{i}")
            if src.exists():
                src.replace(self._path.with_name(f"{self._path.name}.{i + 1}"))
        self._path.replace(self._path.with_name(f"{self._path.name}.1"))


def discover_log_files(path: Path | str, *, include_rotated: bool = True) -> list[Path]:
    """The files that make up one selection log, oldest first.

    Rotation names backups ``<log>.1`` … ``<log>.N`` with ``.1`` the *newest*
    backup, so ordering by descending numeric suffix and appending the active
    file yields append order across the whole history — which is what "the most
    recent selection" means to a reader. Non-numeric siblings are ignored: the
    suffix space belongs to rotation, and an operator's ``.bak`` copy is not
    part of the log.

    Shared by the replay harness and ``mms selection feedback`` so the two
    cannot disagree about which files are the log.
    """
    path = Path(path).expanduser()
    if not include_rotated:
        return [path] if path.exists() else []
    backups: list[tuple[int, Path]] = []
    if path.parent.exists():
        prefix = path.name + "."
        for candidate in path.parent.iterdir():
            if candidate.name.startswith(prefix):
                suffix = candidate.name[len(prefix) :]
                if suffix.isdigit() and candidate.is_file():
                    backups.append((int(suffix), candidate))
    ordered = [candidate for _, candidate in sorted(backups, reverse=True)]
    if path.exists():
        ordered.append(path)
    return ordered


def selection_defect(record: dict[str, Any]) -> str | None:
    """Why *record* cannot carry a label, or ``None`` if it can.

    A label is only ever as good as the join it feeds, and the replay harness
    is the reader on the other end: it drops records whose ``schema_version``
    it does not support (``selection_eval._read_telemetry``) and marks a run
    invalid when a ``selection_id`` is missing. Resolving against a laxer bar
    than that would let ``mms selection feedback`` write a label that joins a
    selection replay refuses to load — the same dead record the command
    already refuses to create for a mistyped id.

    ``ranker_version`` is checked because the label *inherits* it: a selection
    with no usable stamp would have its label filed under the emitter's guess
    at a cohort, which is the defect the stamp was made self-describing to
    avoid.

    Reason codes, not prose, so the CLI can render one and a test can pin it.
    """
    if record.get("schema_version") != SCHEMA_VERSION:
        return f"unsupported schema_version {record.get('schema_version')!r}"
    selection_id = record.get("selection_id")
    if not isinstance(selection_id, str) or not selection_id:
        return "no selection_id"
    ranker_version = record.get("ranker_version")
    if not isinstance(ranker_version, str) or not ranker_version:
        return "no ranker_version"
    return None


class SelectionLogUnreadable(OSError):
    """A segment of the log could not be read.

    Raised rather than skipped: "I could not look there" is not "no such
    selection", and the difference decides whether an operator's label lands on
    the row they meant. Read-side reporting (``aggregate_selection_log``)
    stays tolerant — it summarizes what it could read — but a resolve that
    feeds a WRITE cannot, because a missed segment silently promotes an older
    row to "most recent".

    Carries the segment name and the underlying error so each caller can phrase
    it in its own vocabulary without parsing the message back apart.
    """

    def __init__(self, segment: str, error: OSError) -> None:
        super().__init__(f"cannot read log segment {segment}: {error}")
        self.segment = segment
        self.error = error


# Replay stamps this on every record it loads to order them; it is not part of
# the record and never participates in comparing two copies.
_ORDER_KEY = "_order"


def _json_equal(left: Any, right: Any) -> bool:
    """Whether two parsed JSON values are the same VALUE.

    Not Python's ``==``, which folds ``True`` into ``1`` and ``False`` into
    ``0`` — JSON has no such identity, and two records that disagree about a
    boolean field are two different records however Python compares them. Not
    a comparison of serialized forms either, which would split ``1`` from
    ``1.0``: JSON has one number type, and the writer's own round-trip can
    change which side of that a value lands on.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if type(left) is not type(right):
        return False
    return bool(left == right)


def records_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Whether two copies of one ``selection_id`` DISAGREE.

    ``False`` means they are one record — replay keeps the first and counts the
    repeat — and ``True`` means the id is poisoned: replay discards the
    selection and marks the run invalid, and ``mms selection feedback`` refuses
    to label it. The one definition, shared with
    ``selection_eval._observed_telemetry``, so the labelling command and the
    reader it writes for cannot disagree about what a duplicate is.
    """
    return not _json_equal(
        {k: v for k, v in left.items() if k != _ORDER_KEY},
        {k: v for k, v in right.items() if k != _ORDER_KEY},
    )


# How many distinct recent selections ``--last`` will consider. Fallthrough
# exists for the rare hand-edited or double-ingested id; a log whose 64 most
# recent matching selections are ALL unlabellable is a broken log, and is
# reported as such rather than as "nothing matches".
_MAX_FALLTHROUGH = 64

_CONFLICT_REASON = (
    "copies of this selection_id disagree; offline replay drops the selection "
    "and marks the run invalid"
)


def _exhausted_reason(count: int) -> str:
    """Why a search that had candidates returned none.

    Counts what was actually examined: claiming sixty-four when one defective
    row was all there was sends an operator looking for sixty-three selections
    that do not exist.
    """
    subject = (
        "matching selection is" if count == 1 else f"{count} most recent matching selections are"
    )
    return f"the {subject} unlabellable; name one with --selection-id"


class TelemetryReader:
    """The one definition of what this log *contains*.

    Four rounds of review found the labelling command and the replay harness
    disagreeing about which records exist — over encodings, over the active
    file's unterminated tail, over the line-length cut, over duplicate copies —
    each time in a place where one of them had re-derived a rule the other
    owned. So neither owns it now: segment discovery, framing, the size
    admission, strict decoding, schema and event admission, and append-order
    stamping happen here, and both callers read the same stream.

    Reads a line at a time — a segment is 50 MB by default — while collecting
    what the replay harness reports about the read: per-segment name, sha256
    and line count in :attr:`files`, the skipped/rejected line counters in
    :attr:`quality`, and :attr:`warnings`. Those are complete only once
    :meth:`records` has been consumed.

    ``keep_unsupported`` yields records of an unsupported ``schema_version``
    (still counted) instead of dropping them. Replay must not see them at all;
    the labelling command needs them to say *this row is of a schema I cannot
    label* rather than "no such selection" about a row that is right there.

    Raises :class:`SelectionLogUnreadable` for a segment it cannot read: "I
    could not look there" is not "there is nothing there", and skipping a
    segment silently promotes an older row to "most recent".
    """

    EVENTS = ("selection", "execution", "feedback")

    def __init__(
        self, path: Path | str, *, include_rotated: bool = True, keep_unsupported: bool = False
    ) -> None:
        self.path = Path(path).expanduser()
        self.keep_unsupported = keep_unsupported
        self.segments = discover_log_files(self.path, include_rotated=include_rotated)
        self.quality: Counter[str] = Counter()
        self.files: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def records(self) -> Iterator[dict[str, Any]]:
        for index, log_path in enumerate(self.segments):
            yield from self._segment_records(index, log_path)

    def _segment_records(self, index: int, log_path: Path) -> Iterator[dict[str, Any]]:
        is_active = log_path == self.path
        digest = hashlib.sha256()
        line_no = 0
        pending: bytes | None = None
        try:
            with log_path.open("rb") as handle:
                # The scan reads the segment as it was when it started, not as
                # it keeps becoming: the proxy appends to the active file while
                # this runs, and a reader that followed the writer would report
                # a digest, a line count and an ordering for a file no one ever
                # saw whole — and could be walked forward indefinitely by a
                # busy one.
                budget = self._segment_size(handle)
                for chunk in handle:
                    if budget <= 0:
                        break
                    if len(chunk) > budget:
                        chunk = chunk[:budget]
                    budget -= len(chunk)
                    digest.update(chunk)
                    if pending is not None:
                        admitted, line_no = self._consume(pending, index, line_no)
                        yield from admitted
                    pending = chunk
                if pending is not None:
                    # The active file's last line, when unterminated, is a
                    # record still being written. A rotated backup's is closed
                    # history and stays.
                    tail = is_active and not pending.endswith(b"\n")
                    admitted, line_no = self._consume(pending, index, line_no, drop_last=tail)
                    yield from admitted
                    if tail:
                        self.quality["truncated_tail_lines"] += 1
                        self.warnings.append(
                            "active log ended with an incomplete line; tail skipped"
                        )
        except OSError as exc:
            raise SelectionLogUnreadable(log_path.name, exc) from exc
        self.files.append({"name": log_path.name, "sha256": digest.hexdigest(), "lines": line_no})

    @staticmethod
    def _segment_size(handle: IO[bytes]) -> int:
        """Bytes to read from this segment: its size when the scan reached it.

        ``os.fstat`` on the open descriptor rather than a path ``stat``, so the
        number describes the file this handle holds even if the name is renamed
        by rotation a moment later.
        """
        fileno = getattr(handle, "fileno", None)
        if fileno is None:
            return sys.maxsize
        try:
            return os.fstat(fileno()).st_size
        except (OSError, ValueError):
            # Not seekable, or not a real file (a pipe, a test double): read
            # what there is rather than refusing to read at all.
            return sys.maxsize

    def _consume(
        self, chunk: bytes, index: int, line_no: int, *, drop_last: bool = False
    ) -> tuple[list[dict[str, Any]], int]:
        """Split one read chunk the way a whole-file read would, and admit it.

        ``splitlines`` rather than a newline strip, because that is what a
        reader of the whole file does — a bare carriage return inside a record
        ends a line for it, and a reader that disagreed would parse bytes the
        other never sees. The line counter is returned rather than kept as
        state so a piece nobody admitted still advances it.
        """
        pieces = chunk.splitlines()
        if drop_last and pieces:
            pieces = pieces[:-1]
        admitted: list[dict[str, Any]] = []
        for piece in pieces:
            line_no += 1
            record = self._admit(piece, index, line_no)
            if record is not None:
                admitted.append(record)
        return admitted, line_no

    def _admit(self, line: bytes, index: int, line_no: int) -> dict[str, Any] | None:
        if not line.strip():
            return None
        if len(line) > MAX_LINE_BYTES:
            self.quality["oversized_lines"] += 1
            return None
        try:
            record = json.loads(line)
        except (ValueError, TypeError, UnicodeDecodeError):
            self.quality["malformed_lines"] += 1
            return None
        if not isinstance(record, dict):
            self.quality["malformed_lines"] += 1
            return None
        if record.get("schema_version") != SCHEMA_VERSION:
            self.quality["unsupported_schema_records"] += 1
            if not self.keep_unsupported:
                return None
        elif record.get("event") not in self.EVENTS:
            self.quality["unknown_event_records"] += 1
            return None
        record[_ORDER_KEY] = (index, line_no)
        return record


def _iter_selection_records(path: Path, *, include_rotated: bool) -> Iterator[dict[str, Any]]:
    """Every ``selection`` record of the log, in append order.

    Unsupported-schema rows are kept: :func:`resolve_selection` has to name
    what is wrong with one an operator asked for by id.
    """
    reader = TelemetryReader(path, include_rotated=include_rotated, keep_unsupported=True)
    for record in reader.records():
        if record.get("event") == "selection":
            yield record


def _supported(record: dict[str, Any]) -> bool:
    """Whether replay would load this record at all.

    ``_read_telemetry`` drops an unsupported ``schema_version`` before anything
    else looks at the record, so a row of some future schema is not a copy that
    can conflict with anything — the duplicate fold must not see it either.
    """
    return record.get("schema_version") == SCHEMA_VERSION


def _fold_ids(
    path: Path, ids: set[str], *, include_rotated: bool
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Replay's duplicate fold, for *ids*, in ONE pass.

    Returns the record replay keeps for each id — the first supported copy —
    and the ids whose copies disagree. Scans to the end rather than stopping at
    the first conflict, so a segment that cannot be read is still reported.
    """
    kept: dict[str, dict[str, Any]] = {}
    conflicting: set[str] = set()
    for record in _iter_selection_records(path, include_rotated=include_rotated):
        record_id = record.get("selection_id")
        if not isinstance(record_id, str) or record_id not in ids or not _supported(record):
            continue
        previous = kept.get(record_id)
        if previous is None:
            kept[record_id] = record
        elif records_conflict(previous, record):
            conflicting.add(record_id)
    return kept, conflicting


def resolve_selection(
    path: Path | str,
    *,
    selection_id: str | None = None,
    server: str | None = None,
    tool: str | None = None,
    include_rotated: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    """Locate one labellable ``selection``; returns ``(record, defect)``.

    Exactly one of the two is ever set. A *defect* is the reason a record that
    IS in the log cannot carry a label, so a caller can say what is wrong with
    the row rather than "not found" about one an operator can plainly see.

    With *selection_id*, resolves that exact row. Otherwise the most recent
    **labellable** selection matching the optional *server* / *tool* filters,
    where "most recent" is **append order** (see :func:`discover_log_files`)
    rather than the ``ts`` field: ``ts`` is wall clock and a clock step
    backwards would reorder history, while append order is what actually
    happened. A row that cannot carry a label is not the answer to "the most
    recent one I can label", so the search falls through it — across up to
    :data:`_MAX_FALLTHROUGH` distinct selections, beyond which it says so
    rather than reporting no match — and ``--last`` prints what it resolved so
    the operator sees which row that was.

    Duplicates follow replay's policy, via the shared :func:`records_conflict`:
    equal copies of one ``selection_id`` are one record and the first is kept,
    while copies that DISAGREE poison the id — replay discards that selection
    and marks the run invalid, so a label on it would join nothing. Copies
    replay never loads (an unsupported ``schema_version``) take no part in
    that comparison, though an id with only such copies still reports the
    unsupported schema as its defect rather than going missing.

    Malformed lines are skipped, not raised on, because a hand-edited or
    half-written log must not make labelling impossible. A segment that cannot
    be READ is different and raises :class:`SelectionLogUnreadable`: skipping
    it would silently promote an older row to "most recent".

    *tool* matches ``selected_tool``, the prefixed name the client called, so
    it uses the same vocabulary an operator reads out of a report.
    """
    path = Path(path).expanduser()
    if selection_id is not None:
        kept: dict[str, Any] | None = None
        unsupported: dict[str, Any] | None = None
        conflicts = False
        for record in _iter_selection_records(path, include_rotated=include_rotated):
            if record.get("selection_id") != selection_id:
                continue
            if not _supported(record):
                unsupported = unsupported or record
                continue
            if kept is None:
                kept = record
            elif records_conflict(kept, record):
                conflicts = True
        if kept is None:
            # Only rows replay would not load: report what is wrong with them
            # rather than "no such selection" about a row that is right there.
            return (
                (None, selection_defect(unsupported)) if unsupported is not None else (None, None)
            )
        if conflicts:
            return None, _CONFLICT_REASON
        defect = selection_defect(kept)
        return (None, defect) if defect is not None else (kept, None)

    # Pass 1: the matching selections, one entry per id, at the position of
    # the id's FIRST copy — which is the copy replay keeps, so a later
    # duplicate must not make the selection look newer than the reader thinks
    # it is. Sixty-four COPIES of one id must not crowd out the labellable row
    # behind them either, so the budget counts ids. Defects are screened in
    # pass 2, not here: a run of sixty-four unlabellable matches is the
    # exhaustion this reports, and dropping them here would report it as
    # "nothing matched".
    candidates: dict[str, dict[str, Any]] = {}
    # Every matching id ever seen, not only the ones still in the window: an id
    # that fell out of it is not a NEW selection when a duplicate of it turns
    # up later, and re-admitting it would date the selection by its copy. Ids
    # only — the records themselves stay bounded by the window, and the reader
    # this feeds materializes every record anyway.
    seen: set[str] = set()
    for record in _iter_selection_records(path, include_rotated=include_rotated):
        if not _supported(record):
            continue
        if server is not None and record.get("server") != server:
            continue
        if tool is not None and record.get("selected_tool") != tool:
            continue
        record_id = record.get("selection_id")
        if not isinstance(record_id, str) or not record_id or record_id in seen:
            continue
        seen.add(record_id)
        candidates[record_id] = record
        if len(candidates) > _MAX_FALLTHROUGH:
            del candidates[next(iter(candidates))]
    if not candidates:
        return None, None
    # Pass 2: one more scan, resolving every candidate at once rather than one
    # scan per candidate, and yielding the copy replay would keep.
    kept_by_id, conflicting = _fold_ids(path, set(candidates), include_rotated=include_rotated)
    for record_id in reversed(list(candidates)):
        if record_id in conflicting:
            continue
        kept_copy = kept_by_id.get(record_id)
        if kept_copy is None:
            continue
        record = kept_copy
        # The kept copy is re-screened, not assumed: between the two passes the
        # id can acquire an earlier copy — or the whole row can be rewritten —
        # and returning the pass-1 record would label something the filters
        # never matched.
        if server is not None and record.get("server") != server:
            continue
        if tool is not None and record.get("selected_tool") != tool:
            continue
        if selection_defect(record) is not None:
            continue
        return record, None
    return None, _exhausted_reason(len(candidates))


def find_selection(
    path: Path | str,
    *,
    selection_id: str | None = None,
    server: str | None = None,
    tool: str | None = None,
    include_rotated: bool = True,
) -> dict[str, Any] | None:
    """The labellable record :func:`resolve_selection` found, or ``None``.

    Thin wrapper for readers that only need the row; a caller about to WRITE
    wants the defect string too, since "no such selection" and "that selection
    cannot carry a label" send an operator to different places — and wants the
    :class:`SelectionLogUnreadable` this one lets through, since a segment it
    could not read is not a segment without a match.
    """
    return resolve_selection(
        path,
        selection_id=selection_id,
        server=server,
        tool=tool,
        include_rotated=include_rotated,
    )[0]


def _top(counter: Counter[str], n: int) -> list[list[Any]]:
    """Return the ``n`` highest-count entries as ``[[key, count], …]``.

    Sorted by count descending then key ascending so the order is stable
    across runs with the same data (ties never reshuffle a rendered table).
    """
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [[k, v] for k, v in ordered[:n]]


def aggregate_selection_log(path: Path | str, *, top_n: int = 10) -> dict[str, Any]:
    """Stream-aggregate a selection-telemetry JSONL log for read-side stats.

    Pure and side-effect-free: opens the *active* log file (rotated backups
    ``.1``/``.2``/… are counted via ``rotated_backups`` but not parsed — a
    50 MB default means rotation is rare, and reading the tail of history is
    a wait-for-signal extension), parses one JSON object per line, and never
    raises on a malformed line (counted in ``malformed`` and skipped) so a
    half-written tail or a hand-edited file can't break the stats tool.

    The high-cardinality maps (server / tool / error-type / reject-reason)
    are returned as ``top_n`` ``[key, count]`` lists plus a ``*_distinct``
    cardinality so truncation never reads as "that's all there was";
    ``by_ranker_version`` is returned in full (low cardinality, and the
    cohort split is the #468 replay signal).
    """
    path = Path(path).expanduser()
    # Numeric suffixes only — the same rule ``discover_log_files`` applies. A
    # bare ``<log>.*`` glob also matches the rotation lock file and any operator
    # copy, reporting backups that rotation never made.
    #
    # Listing the directory is the one step here that can fail on its own —
    # ``discover_log_files`` iterates it, and an unreadable directory raises
    # where the ``glob`` this replaced swallowed the error. Counting backups is
    # a decoration on the summary, so a failure degrades rather than taking
    # down a stats call this function's contract says cannot break. Reported as
    # UNKNOWN rather than as zero: a directory can deny enumeration while still
    # permitting the active file to be opened, and "no history beyond this
    # file" is a different claim from "I could not look".
    rotated_unknown = False
    try:
        rotated = len([segment for segment in discover_log_files(path) if segment != path])
    except OSError:
        logger.debug("Could not list the selection log's directory to count backups", exc_info=True)
        rotated = 0
        rotated_unknown = True
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "rotated_backups": rotated,
        "rotated_backups_unknown": rotated_unknown,
        "total_lines": 0,
        "malformed": 0,
        "events": {"selection": 0, "execution": 0, "feedback": 0},
        "by_ranker_version": [],
        "by_server": [],
        "by_server_distinct": 0,
        "by_selected_tool": [],
        "by_selected_tool_distinct": 0,
        "outcomes": {"ok": 0, "error": 0, "error_rate": 0.0},
        "by_error_type": [],
        "by_error_type_distinct": 0,
        "reject_reasons": [],
        "reject_reasons_distinct": 0,
        "latency_ms": {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0},
        "cache": {"hit": 0, "miss": 0, "unknown": 0, "hit_rate": 0.0},
    }
    if not result["exists"]:
        return result

    rankers: Counter[str] = Counter()
    servers: Counter[str] = Counter()
    tools: Counter[str] = Counter()
    error_types: Counter[str] = Counter()
    reject_reasons: Counter[str] = Counter()
    ok = err = 0
    cache_hit = cache_miss = cache_unknown = 0
    latencies: list[float] = []

    try:
        # Binary, decoded per line: a record truncated mid-character (a short
        # write, or a crash) makes the STREAM undecodable, and a text-mode read
        # would raise out of the iterator — past the per-line handler below and
        # out of an observability path this function promises never to raise
        # from. ``_read_telemetry`` already treats those bytes as malformed.
        with path.open("rb") as fh:
            # ``splitlines`` per chunk, not one line per ``\n``: a bare carriage
            # return ends a line for every other reader of this log
            # (``TelemetryReader``, which frames the same way), and a stats
            # view that fused two records into one malformed line would report
            # a corruption the readers do not see.
            for chunk in fh:
                for raw_line in chunk.splitlines():
                    if not raw_line.strip():
                        continue
                    result["total_lines"] += 1
                    try:
                        rec = json.loads(raw_line.decode("utf-8"))
                    except (ValueError, TypeError, UnicodeDecodeError):
                        result["malformed"] += 1
                        continue
                    if not isinstance(rec, dict):
                        result["malformed"] += 1
                        continue
                    event = rec.get("event")
                    if event == "selection":
                        result["events"]["selection"] += 1
                        rankers[str(rec.get("ranker_version"))] += 1
                        if rec.get("server") is not None:
                            servers[str(rec["server"])] += 1
                        if rec.get("selected_tool") is not None:
                            tools[str(rec["selected_tool"])] += 1
                        rr = rec.get("reject_reasons")
                        if isinstance(rr, dict):
                            for reason in rr.values():
                                reject_reasons[str(reason)] += 1
                    elif event == "execution":
                        result["events"]["execution"] += 1
                        if rec.get("ok"):
                            ok += 1
                        else:
                            err += 1
                        etype = rec.get("error_type")
                        if etype is not None:
                            error_types[str(etype)] += 1
                        ch = rec.get("cache_hit")
                        if ch is True:
                            cache_hit += 1
                        elif ch is False:
                            cache_miss += 1
                        else:
                            cache_unknown += 1
                        lat = rec.get("latency_ms")
                        if isinstance(lat, (int, float)) and not isinstance(lat, bool):
                            latencies.append(float(lat))
                    elif event == "feedback":
                        result["events"]["feedback"] += 1
                    else:
                        result["malformed"] += 1
    except OSError:
        # Treat an unreadable file like an absent one rather than raising
        # out of an observability path — the tool reports what it could read.
        return result

    result["by_ranker_version"] = _top(rankers, len(rankers))
    result["by_server"] = _top(servers, top_n)
    result["by_server_distinct"] = len(servers)
    result["by_selected_tool"] = _top(tools, top_n)
    result["by_selected_tool_distinct"] = len(tools)
    result["by_error_type"] = _top(error_types, top_n)
    result["by_error_type_distinct"] = len(error_types)
    result["reject_reasons"] = _top(reject_reasons, top_n)
    result["reject_reasons_distinct"] = len(reject_reasons)
    result["outcomes"] = {
        "ok": ok,
        "error": err,
        "error_rate": round(err / (ok + err), 4) if (ok + err) else 0.0,
    }
    if latencies:
        latencies.sort()
        result["latency_ms"] = {
            "count": len(latencies),
            "p50": round(_percentile(latencies, 50), 2),
            "p95": round(_percentile(latencies, 95), 2),
            "p99": round(_percentile(latencies, 99), 2),
        }
    # hit_rate is over attributable executions only (hit + miss); unknowns
    # (in-pipeline raises before the field is set) are excluded from the
    # denominator so a burst of errors can't deflate the cache hit rate.
    attributable = cache_hit + cache_miss
    result["cache"] = {
        "hit": cache_hit,
        "miss": cache_miss,
        "unknown": cache_unknown,
        "hit_rate": round(cache_hit / attributable, 4) if attributable else 0.0,
    }
    return result
