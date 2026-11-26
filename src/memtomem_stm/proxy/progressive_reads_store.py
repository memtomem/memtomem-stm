"""SQLite persistence for progressive-delivery read events.

Every progressive response generates one row at creation (``offset=0``,
``chars=<initial chunk>``) plus one additional row per follow-up
``stm_proxy_read_more`` call. The table is append-only and purely
observational — writes feed ``stm_progressive_stats`` and future
analysis of nudge-strength vs. follow-up rate; they never affect the
response served to the caller.

The store shares its SQLite file with surfacing feedback and compression
feedback (``~/.memtomem/stm_feedback.db`` by default). SQLite WAL
journaling makes concurrent connections to the same file safe, and the
table namespaces are disjoint so the three subsystems don't conflict.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

import math
from typing import Any

from memtomem_stm.proxy.metrics_store import _count, _finite, _finite_total
from memtomem_stm.utils.json_out import require_utf8_identifier
from memtomem_stm.utils.sqlite_private import ensure_private_db_files
from memtomem_stm.utils.sqlite_tuning import tune_connection

logger = logging.getLogger(__name__)


# ── Per-response rollup (#1328, #1316 part B) ──────────────────────────────
#
# ``progressive_reads`` is purged by age at startup, by this version and by
# older ones. ``progressive_reads_rollup`` holds the rows that have LEFT the
# table: an AFTER DELETE trigger folds each deleted row into its ``(key, tool)``
# bucket, and an all-time figure is the buckets plus the rows still retained,
# grouped by key. This is ``metrics_store``'s daily rollup (#1316 part A)
# keyed by response instead of by day, because every figure
# ``read_progressive_summary`` reports is per response (distinct keys, keys
# followed up, the largest ``total_chars`` of each key): those do not add up
# across days when one response's reads straddle a purge, but they do merge
# exactly per key. The cost is one rollup row per response ever purged.
#
# The trigger lives in the file, so a purge by an older STM build that knows
# nothing about the rollup still feeds it. It must therefore be unable to
# fail (a failure fails the purge, and with it every binary's startup): the
# table is not STRICT, its counters are nullable, the key columns fall back to
# ``''``, and every summed value is guarded so a NULL, text, ``inf`` or NaN
# contributes 0 — or NULL for the maximum and minimum, which ignore it. The
# all-time reader passes retained rows through the same guards, so a
# malformed row counts the same before and after it is purged. Figures are
# exact for what real writers store (``len()`` sizes, epoch timestamps): a
# single value beyond ±9.2e18 counts as 0, and a bucket summed past int64
# turns REAL, exact only to 2^53.
#
# Contract: ANY delete from ``progressive_reads`` is rolled up, so to reset the
# history delete from both tables in one transaction, ``progressive_reads``
# first: ``BEGIN IMMEDIATE; DELETE FROM progressive_reads; DELETE FROM
# progressive_reads_rollup; COMMIT;``. Never ``DROP`` the rollup table while the
# trigger exists: every purge on every binary would fail with "no such table".
#
# ``progressive_reads_meta`` holds three markers: when the rollup was
# installed, how many rows had already been purged then (``'unknown'`` when
# that cannot be established), and from when the history is known complete.
# The rollup is whole only when the table and the trigger — exactly as this
# version creates them, compared by their stored SQL — and all three markers
# are present, valid and consistent with each other. Edits that leave all of
# that intact (to the rollup's rows, or to markers rewritten consistently)
# are not detected. When some of them are and others are not, something
# outside STM removed part of it, and rows may have been purged unrolled at
# any time before now: the installer then records the loss as unknown and
# moves the "complete from" boundary to now, rather than trying to tell the
# damage cases apart.
#
# Changing the trigger body later needs a new trigger name (the ``_v1`` suffix)
# plus a DROP of the old one: ``CREATE TRIGGER IF NOT EXISTS`` keeps an existing
# trigger as it is.

_META_TABLE = "progressive_reads_meta"
_ROLLUP_TABLE = "progressive_reads_rollup"
_ROLLUP_TRIGGER = "progressive_reads_rollup_on_delete_v1"
_INSTALLED_KEY = "rollup_installed_at"
_LOST_KEY = "rollup_rows_lost_before_install"
_COMPLETE_FROM_KEY = "rollup_complete_from"
_LOST_UNKNOWN = "unknown"
_MARKER_KEYS = (_INSTALLED_KEY, _LOST_KEY, _COMPLETE_FROM_KEY)

_META_CREATE = (
    f"CREATE TABLE IF NOT EXISTS {_META_TABLE} (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID"
)
_ROLLUP_CREATE = (
    f"CREATE TABLE IF NOT EXISTS {_ROLLUP_TABLE} (\n"
    "    key TEXT NOT NULL,\n"
    "    tool TEXT NOT NULL,\n"
    # Nullable on purpose — see the notes above.
    "    reads INTEGER DEFAULT 0,\n"
    "    follow_up_reads INTEGER DEFAULT 0,\n"
    "    unclassified_reads INTEGER DEFAULT 0,\n"
    "    max_total_chars INTEGER,\n"
    "    delivered_chars INTEGER DEFAULT 0,\n"
    "    min_created_at REAL,\n"
    "    PRIMARY KEY (key, tool)\n"
    ") WITHOUT ROWID"
)


def _finite_or_null(expr: str) -> str:
    """``expr`` when it is a number in ``_finite``'s range, else NULL.

    For the maximum and the minimum: NULL is what ``MAX``/``MIN`` ignore, so a
    rejected value never beats or replaces a real one.
    """
    return f"CASE WHEN {expr} BETWEEN -9.2e18 AND 9.2e18 THEN {expr} ELSE NULL END"


def _finite_total_or_null(expr: str) -> str:
    """``expr`` when it is any finite number, else NULL — for a stored extreme."""
    return (
        f"CASE WHEN {expr} BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308 "
        f"THEN {expr} ELSE NULL END"
    )


def _row_terms(prefix: str, has_is_initial: bool = True) -> tuple[str, ...]:
    """What one ``progressive_reads`` row contributes, in rollup column order.

    ``(reads, follow_up_reads, unclassified_reads, max_total_chars,
    delivered_chars, min_created_at)``. ``prefix`` is ``"OLD."`` in the trigger
    and ``""`` in the reader's retained half, so both apply one rule.
    """
    p = prefix
    follow_up = f"CASE WHEN {p}is_initial = 0 THEN 1 ELSE 0 END" if has_is_initial else "0"
    unclassified = f"CASE WHEN {p}is_initial IS NULL THEN 1 ELSE 0 END" if has_is_initial else "1"
    return (
        "1",
        follow_up,
        unclassified,
        _finite_or_null(f"{p}total_chars"),
        _finite(f"{p}chars"),
        _finite_total_or_null(f"{p}created_at"),
    )


def _null_aware(name: str, fn: str) -> str:
    # Scalar ``max(x, NULL)`` is NULL in SQLite, so a NULL on either side keeps
    # the other rather than erasing a known value.
    return (
        f"{name} = CASE WHEN excluded.{name} IS NULL THEN {name} "
        f"WHEN {name} IS NULL THEN excluded.{name} ELSE {fn}({name}, excluded.{name}) END"
    )


def _rollup_trigger_ddl() -> str:
    reads, follow_up, unclassified, total, chars, created = _row_terms("OLD.")
    return (
        f"CREATE TRIGGER IF NOT EXISTS {_ROLLUP_TRIGGER} "
        "AFTER DELETE ON progressive_reads FOR EACH ROW BEGIN "
        f"INSERT INTO {_ROLLUP_TABLE} (key, tool, reads, follow_up_reads, "
        "unclassified_reads, max_total_chars, delivered_chars, min_created_at) "
        f"VALUES (COALESCE(OLD.key, ''), COALESCE(OLD.tool, ''), {reads}, {follow_up}, "
        f"{unclassified}, {total}, {chars}, {created}) "
        "ON CONFLICT (key, tool) DO UPDATE SET "
        "reads = COALESCE(reads, 0) + excluded.reads, "
        "follow_up_reads = COALESCE(follow_up_reads, 0) + excluded.follow_up_reads, "
        "unclassified_reads = COALESCE(unclassified_reads, 0) + excluded.unclassified_reads, "
        "delivered_chars = COALESCE(delivered_chars, 0) + excluded.delivered_chars, "
        f"{_null_aware('max_total_chars', 'max')}, {_null_aware('min_created_at', 'min')}; END"
    )


_ROLLUP_TRIGGER_DDL = _rollup_trigger_ddl()
# What SQLite keeps in ``sqlite_master.sql`` for it: the statement as written,
# minus ``IF NOT EXISTS``.
_ROLLUP_TRIGGER_STORED_SQL = _ROLLUP_TRIGGER_DDL.replace(
    "CREATE TRIGGER IF NOT EXISTS ", "CREATE TRIGGER ", 1
)
_ROLLUP_STORED_SQL = _ROLLUP_CREATE.replace("CREATE TABLE IF NOT EXISTS ", "CREATE TABLE ", 1)


def _finite_float(value: object) -> float | None:
    """A marker timestamp, or None unless it is a finite number."""
    if not isinstance(value, str | int | float):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _lost_count(value: object) -> int | None:
    """The recorded pre-install loss, or None when unknown or unreadable."""
    # ``isdigit`` alone accepts digits ``int()`` rejects, such as ``'²'``, and
    # the length cap keeps ``int()`` below Python's digit limit: a row count
    # never needs more than an int64's 19 digits.
    if not isinstance(value, str) or not (len(value) <= 19 and value.isascii() and value.isdigit()):
        return None
    return int(value)


def _marker_valid(key: str, value: object) -> bool:
    if key == _LOST_KEY:
        return value == _LOST_UNKNOWN or _lost_count(value) is not None
    return _finite_float(value) is not None


def _decoded(value: object) -> object:
    """A marker read as bytes, as text — or None when it is not valid UTF-8."""
    if not isinstance(value, bytes):
        return value
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _rollup_state(db: sqlite3.Connection) -> tuple[set[str], dict[str, object], set[str]]:
    """``(objects, markers, foreign)`` for the rollup's table, trigger and meta rows.

    The rollup table and the trigger count only when their stored SQL is
    exactly what ``_install_rollup`` creates: an altered table can reject the
    trigger's writes (a CHECK fails every purge), and renaming either table
    rewrites the trigger (onto the renamed ``progressive_reads``, or into the
    renamed rollup table), where it feeds nothing the reader counts. ``foreign``
    names the objects that carry the name but are not this version's. Markers
    are read with ``text_factory = bytes``, which SQLite hands over as UTF-8
    whatever the DB's encoding, so a value that is not valid UTF-8 reads as
    invalid rather than failing the read.
    """
    expected = {_ROLLUP_TABLE: _ROLLUP_STORED_SQL, _ROLLUP_TRIGGER: _ROLLUP_TRIGGER_STORED_SQL}
    objects: set[str] = set()
    foreign: set[str] = set()
    for name, sql in db.execute(
        "SELECT name, sql FROM sqlite_master WHERE "
        "(type = 'table' AND name IN (?, ?)) OR (type = 'trigger' AND name = ?)",
        (_META_TABLE, _ROLLUP_TABLE, _ROLLUP_TRIGGER),
    ):
        name = str(name)
        if name in expected and sql != expected[name]:
            foreign.add(name)
        else:
            objects.add(name)
    markers: dict[str, object] = {}
    if _META_TABLE in objects:
        previous = db.text_factory
        db.text_factory = bytes
        try:
            rows = db.execute(
                f"SELECT key, value FROM {_META_TABLE} WHERE key IN (?, ?, ?)", _MARKER_KEYS
            ).fetchall()
        finally:
            db.text_factory = previous
        markers = {str(_decoded(key)): _decoded(value) for key, value in rows}
    return objects, markers, foreign


def _rollup_whole(objects: set[str], markers: dict[str, object]) -> bool:
    """Whether the table, the trigger and every marker are there, valid and consistent.

    A recorded loss count is written only by a first installation, which
    writes one timestamp as both the installation time and the start of the
    complete history; any other pairing was not written by STM.
    """
    if not {_META_TABLE, _ROLLUP_TABLE, _ROLLUP_TRIGGER} <= objects:
        return False
    if not all(_marker_valid(key, markers.get(key)) for key in _MARKER_KEYS):
        return False
    if _lost_count(markers.get(_LOST_KEY)) is not None:
        return markers.get(_INSTALLED_KEY) == markers.get(_COMPLETE_FROM_KEY)
    return True


def _install_rollup(db: sqlite3.Connection) -> None:
    """Create the rollup inside the caller's ``BEGIN IMMEDIATE``, before its purge.

    Three cases, by what is already there (see the notes above):

    - nothing at all — no table, trigger, meta table or marker, valid or not:
      a first installation. The loss is ``sqlite_sequence.seq - COUNT(*)`` —
      AUTOINCREMENT ids are never reused once committed, so this is how many
      rows already left the table — and the history is complete from now.
    - everything, valid: nothing to do.
    - anything in between: the loss becomes unknown and the history is
      complete only from now; a valid installation time is kept. The trigger
      is re-created, and a rollup table that is not this version's is renamed
      aside (``progressive_reads_rollup_replaced_<ms>``), its rows kept but no
      longer counted. The trigger goes first: renaming its target table would
      rewrite it.
    """
    objects, markers, foreign = _rollup_state(db)
    if _rollup_whole(objects, markers):
        return
    now = str(time.time())
    if objects or markers or foreign:
        db.execute(f"DROP TRIGGER IF EXISTS {_ROLLUP_TRIGGER}")
    if _ROLLUP_TABLE in foreign:
        db.execute(
            f"ALTER TABLE {_ROLLUP_TABLE} RENAME TO "
            f"{_ROLLUP_TABLE}_replaced_{int(time.time() * 1000)}"
        )
    if not (objects or markers or foreign):
        lost = db.execute(
            "SELECT max(0, COALESCE((SELECT seq FROM sqlite_sequence "
            "WHERE name = 'progressive_reads'), 0) - (SELECT COUNT(*) FROM progressive_reads))"
        ).fetchone()[0]
        lost_value = str(int(lost))
    else:
        lost_value = _LOST_UNKNOWN
    db.execute(_META_CREATE)
    db.execute(_ROLLUP_CREATE)
    db.execute(_ROLLUP_TRIGGER_DDL)
    installed = markers.get(_INSTALLED_KEY)
    values = {
        _INSTALLED_KEY: str(installed) if _finite_float(installed) is not None else now,
        _LOST_KEY: lost_value,
        _COMPLETE_FROM_KEY: now,
    }
    db.executemany(
        f"INSERT INTO {_META_TABLE} (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        list(values.items()),
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS progressive_reads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT    NOT NULL,
    trace_id    TEXT,
    server      TEXT    NOT NULL,
    tool        TEXT    NOT NULL,
    offset      INTEGER NOT NULL,
    chars       INTEGER NOT NULL,
    served_to   INTEGER NOT NULL,
    total_chars INTEGER NOT NULL,
    created_at  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pr_key     ON progressive_reads(key);
CREATE INDEX IF NOT EXISTS idx_pr_trace   ON progressive_reads(trace_id);
CREATE INDEX IF NOT EXISTS idx_pr_tool    ON progressive_reads(tool);
CREATE INDEX IF NOT EXISTS idx_pr_created ON progressive_reads(created_at);
"""


class ProgressiveReadsStore:
    """SQLite store for per-event progressive-delivery reads.

    Opens its own connection to ``db_path`` — safe to share the file
    with other stores (e.g. surfacing ``FeedbackStore`` and
    ``CompressionFeedbackStore``) because SQLite WAL journaling permits
    multiple concurrent connections and the tables are disjoint.
    """

    def __init__(self, db_path: Path, *, retention_days: int) -> None:
        """*retention_days* is required so every call site states its intent.

        ``0`` disables the startup purge; the configured default is 90
        (``ProgressiveReadsConfig.retention_days``). Production callers pass
        that config value through rather than a literal — a constructor
        default here would silently diverge from the config (#876).
        """
        self._db_path = db_path
        self._retention_days = retention_days
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(str(self._db_path), check_same_thread=False)
        try:
            ensure_private_db_files(self._db_path)
            tune_connection(db)
            db.executescript(_SCHEMA)
            # Serialize inspection + migration across concurrently starting MCPs.
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(progressive_reads)")}
            if "is_initial" not in columns:
                db.execute(
                    "ALTER TABLE progressive_reads ADD COLUMN is_initial INTEGER DEFAULT NULL"
                )
            if "wire_bytes" not in columns:
                db.execute(
                    "ALTER TABLE progressive_reads ADD COLUMN wire_bytes INTEGER DEFAULT NULL"
                )
            # After the migrations (the trigger reads ``is_initial``) and before
            # the purge, so the purge below is already rolled up (#1328).
            _install_rollup(db)
            # Startup purge of aged-out rows (#584) — the table is append-only
            # and otherwise unbounded. Runs once per process start, mirroring
            # ProxyCache's startup expiry purge. ``retention_days=0`` disables.
            if self._retention_days > 0:
                cutoff = time.time() - self._retention_days * 86400.0
                deleted = db.execute(
                    "DELETE FROM progressive_reads WHERE created_at < ?", (cutoff,)
                ).rowcount
                if deleted:
                    logger.info(
                        "Purged %d progressive_reads rows older than %d days (#584)",
                        deleted,
                        self._retention_days,
                    )
            db.commit()
        except Exception:
            db.close()
            raise
        self._db = db

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    def record(
        self,
        key: str,
        trace_id: str | None,
        server: str,
        tool: str,
        offset: int,
        chars: int,
        served_to: int,
        total_chars: int,
        *,
        is_initial: bool | None = None,
    ) -> None:
        """Persist one read event.

        No-op when the store is closed; callers are expected to
        construct the tracker via ``app_lifespan`` and rely on the
        finally-block close, so hitting a closed store from the hot
        path would be a shutdown-race and losing a row is preferable
        to raising into the response path.

        Synchronous sqlite write on the asyncio event loop: while it
        runs, every runnable coroutine stalls — other in-flight proxied
        calls included, not just the request being served. Accepted for
        the current local single-MCP-client deployment (low call
        volume; far cheaper than the upstream call). Multi-client
        serving (or materially higher concurrency) is the reopen
        trigger: move the write off-loop (e.g. ``asyncio.to_thread``) —
        and note ``self._lock`` serializes the write paths only; reads
        share the connection unlocked, so a thread move also needs
        every connection access locked (the ``MetricsStore.__init__``
        reader/writer convention) or per-thread connections.
        """
        if self._db is None:
            return
        require_utf8_identifier(key, "key")
        require_utf8_identifier(trace_id, "trace_id")
        require_utf8_identifier(server, "server")
        require_utf8_identifier(tool, "tool")
        with self._lock:
            self._db.execute(
                "INSERT INTO progressive_reads "
                "(key, trace_id, server, tool, offset, chars, served_to, "
                "total_chars, created_at, is_initial) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    trace_id,
                    server,
                    tool,
                    offset,
                    chars,
                    served_to,
                    total_chars,
                    time.time(),
                    None if is_initial is None else int(is_initial),
                ),
            )
            self._db.commit()

    def record_wire(self, key: str, wire_bytes: int, *, initial: bool, offset: int = 0) -> None:
        if self._db is None:
            return
        with self._lock:
            self._db.execute(
                "UPDATE progressive_reads SET wire_bytes = ? WHERE id = "
                "(SELECT MAX(id) FROM progressive_reads "
                "WHERE key = ? AND is_initial = ? AND offset = ?)",
                (wire_bytes, key, int(initial), offset),
            )
            self._db.commit()

    def get_stats(self, tool: str | None = None) -> dict:
        """Return aggregate stats for ``stm_progressive_stats``.

        Shape::

            {
                "initial_payload_chars": int,    # content chars, creation events
                "follow_up_payload_chars": int,  # content chars, follow-up reads
                "unclassified_reads": int,   # legacy rows without is_initial
                "total_reads": int,          # row count
                "total_responses": int,      # DISTINCT keys
                "follow_up_rate": float,     # keys w/ >1 row / total_responses
                "avg_chars_served": float,   # avg MAX(served_to) per key
                "avg_total_chars": float,    # avg total_chars per key
                "avg_coverage": float,       # avg MAX(served_to)/total_chars per key
                "by_tool": {tool: {"responses": int, "follow_up_rate": float}},
            }

        When ``tool`` is provided, ``by_tool`` is returned as an empty
        dict so callers can rely on the key always being present
        (parity with ``CompressionFeedbackStore.get_stats``). Averages
        are computed per-key so a 5-follow-up response is weighted the
        same as a 0-follow-up response.
        """
        empty = {
            "initial_payload_chars": 0,
            "follow_up_payload_chars": 0,
            "unclassified_reads": 0,
            "total_reads": 0,
            "total_responses": 0,
            "follow_up_rate": 0.0,
            "avg_chars_served": 0.0,
            "avg_total_chars": 0.0,
            "avg_coverage": 0.0,
            "by_tool": {},
            "initial_wire_bytes": 0,
            "follow_up_wire_bytes": 0,
            "wire_measured_reads": 0,
        }
        if self._db is None:
            return empty

        where = ""
        params: tuple = ()
        if tool is not None:
            try:
                require_utf8_identifier(tool, "tool")
            except ValueError:
                return empty
            where = "WHERE tool = ?"
            params = (tool,)

        # One statement, so every figure below describes the same snapshot even
        # while another process appends rows. Payload volumes sum deliveries,
        # not max offsets: repeated/overlapping reads consume context again.
        # Rows written before ``is_initial`` existed cannot tell creation from
        # an offset-0 reread, so they are counted apart as unclassified.
        per_key_rows = self._db.execute(
            "SELECT key, MAX(served_to), MAX(total_chars), tool, COUNT(*), "
            "COALESCE(SUM(CASE WHEN is_initial = 1 THEN chars ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN is_initial = 0 THEN chars ELSE 0 END), 0), "
            "SUM(CASE WHEN is_initial IS NULL THEN 1 ELSE 0 END), "
            "COALESCE(SUM(CASE WHEN is_initial = 1 THEN wire_bytes END),0), "
            "COALESCE(SUM(CASE WHEN is_initial = 0 THEN wire_bytes END),0), COUNT(wire_bytes) "
            f"FROM progressive_reads {where} GROUP BY key",
            params,
        ).fetchall()
        total_responses = len(per_key_rows)
        if total_responses == 0:
            return empty
        total_reads = sum(r[4] for r in per_key_rows)

        followed_up = sum(1 for r in per_key_rows if r[4] > 1)
        follow_up_rate = followed_up / total_responses
        avg_chars_served = sum(r[1] for r in per_key_rows) / total_responses
        avg_total_chars = sum(r[2] for r in per_key_rows) / total_responses
        # Coverage is bounded to 1.0 per key: rare edge case where a
        # misbehaving upstream revises total_chars downward between
        # rows would otherwise push the ratio above 1.0 and skew the
        # average.
        avg_coverage = (
            sum(min(1.0, r[1] / r[2]) if r[2] > 0 else 0.0 for r in per_key_rows) / total_responses
        )

        by_tool: dict[str, dict] = {}
        if tool is None:
            # Re-group per-key rows by tool for the breakdown.
            tool_buckets: dict[str, list[int]] = {}
            for _, _, _, tool_name, n, *_ in per_key_rows:
                tool_buckets.setdefault(tool_name, []).append(n)
            for tool_name, counts in tool_buckets.items():
                responses = len(counts)
                followed = sum(1 for n in counts if n > 1)
                by_tool[tool_name] = {
                    "responses": responses,
                    "follow_up_rate": followed / responses if responses else 0.0,
                }

        wires = tuple(sum(r[i] for r in per_key_rows) for i in (8, 9, 10))
        return {
            "initial_wire_bytes": wires[0],
            "follow_up_wire_bytes": wires[1],
            "wire_measured_reads": wires[2],
            "initial_payload_chars": sum(r[5] for r in per_key_rows),
            "follow_up_payload_chars": sum(r[6] for r in per_key_rows),
            "unclassified_reads": sum(r[7] for r in per_key_rows),
            "total_reads": total_reads,
            "total_responses": total_responses,
            "follow_up_rate": follow_up_rate,
            "avg_chars_served": avg_chars_served,
            "avg_total_chars": avg_total_chars,
            "avg_coverage": avg_coverage,
            "by_tool": by_tool,
        }


def read_progressive_summary(
    db_path: Path, tool: str | None = None, *, all_time: bool = False
) -> dict[str, object]:
    """Aggregate progressive-delivery volume read-only from disk, for ``mms stats``.

    Unlike :meth:`ProgressiveReadsStore.initialize` this never creates,
    migrates or purges the DB: it opens the file via ``?mode=ro`` and skips
    ``tune_connection`` (``PRAGMA journal_mode=WAL`` would write), like
    ``read_compression_summary`` and ``read_surfacing_summary``.

    ``produced_chars`` sums each response's ``total_chars`` once (per ``key``);
    ``delivered_chars`` sums ``chars`` over every recorded row, so rereads and
    overlapping reads count again and the ratio can exceed 1.0. Rows written
    before ``is_initial`` existed are included in both and counted in
    ``unclassified_reads``. ``oldest_at`` is the earliest retained row: older
    rows may already have been purged by ``retention_days``.
    ``follow_up_responses`` counts a response with a row marked
    ``is_initial = 0`` even when its initial row is gone, so it can exceed what
    :meth:`ProgressiveReadsStore.get_stats` derives from row counts alone
    (``follow_up_rate``). ``available`` is
    ``False`` when the file is missing or has no ``progressive_reads`` table.
    The optional ``tool`` filter matches the raw upstream tool name.

    ``scope`` is ``"retained"``, or ``"all_time"`` when *all_time* is set: then
    the figures are the retained rows plus ``progressive_reads_rollup`` (#1328),
    merged per response, and ``oldest_at`` is the earliest counted row. A
    ``rollup`` key describes coverage: ``installed``, ``installed_at``,
    ``lost_before_install`` (rows purged before installation; ``None`` when
    unknown), ``earliest_created_at`` and ``incomplete_before`` — the time
    before which rows may be missing, set whenever the loss is not known to be
    zero, whatever the counted rows or the ``tool`` filter. ``available`` stays
    ``False`` while the rollup is not installed: there is no fallback to the
    retained rows.
    """
    from memtomem_stm.surfacing.feedback_store import _locate_db
    from memtomem_stm.utils.json_out import has_lone_surrogate
    from memtomem_stm.utils.redact import exception_summary

    summary: dict[str, object] = {
        "path": str(db_path),
        "available": False,
        "classified_supported": False,
        "total_responses": 0,
        "total_reads": 0,
        "follow_up_responses": 0,
        "unclassified_reads": 0,
        "produced_chars": 0,
        "delivered_chars": 0,
        "delivered_ratio": 0.0,
        "oldest_at": None,
        "error": None,
        "scope": "all_time" if all_time else "retained",
    }
    rollup: dict[str, object] = {
        "installed": False,
        "installed_at": None,
        "lost_before_install": None,
        "earliest_created_at": None,
        "incomplete_before": None,
    }
    if all_time:
        summary["rollup"] = rollup
    try:
        resolved, exists = _locate_db(db_path)
    except (OSError, RuntimeError) as exc:
        summary["error"] = exception_summary(exc)
        return summary
    summary["path"] = str(resolved)
    if not exists:
        return summary

    try:
        db = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        summary["error"] = exception_summary(exc)
        return summary

    try:
        if all_time:
            # One read transaction from the schema checks to the aggregate, so a
            # migration or installation committed meanwhile is seen whole or
            # not at all.
            db.execute("BEGIN")
        tables = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "progressive_reads" not in tables:
            return summary
        columns = {str(row[1]) for row in db.execute("PRAGMA table_info('progressive_reads')")}
        # Capability flag describes the FILE, so it is set before the filter exit.
        has_is_initial = "is_initial" in columns
        summary["classified_supported"] = has_is_initial

        if all_time:
            # The installer's own test, so a state it would repair never
            # reads as installed.
            objects, markers, _ = _rollup_state(db)
            installed_at = _finite_float(markers.get(_INSTALLED_KEY))
            complete_from = _finite_float(markers.get(_COMPLETE_FROM_KEY))
            if not _rollup_whole(objects, markers) or installed_at is None or complete_from is None:
                return summary
            lost = _lost_count(markers.get(_LOST_KEY))
            rollup.update(
                {
                    "installed": True,
                    "installed_at": installed_at,
                    "lost_before_install": lost,
                    # Rows can be missing only from before the boundary, and
                    # only if the loss is not known to be zero. The lost rows'
                    # dates and tools are unknown, so neither the counted rows
                    # nor the filter can rule them out.
                    "incomplete_before": complete_from if lost != 0 else None,
                }
            )
            if tool is not None and has_lone_surrogate(tool):
                summary["available"] = True
                return summary
            _read_all_time(db, summary, tool, has_is_initial)
            rollup["earliest_created_at"] = summary["oldest_at"]
            return summary

        if tool is not None and has_lone_surrogate(tool):
            # Cannot be bound as a parameter and can never match a stored row.
            summary["available"] = True
            return summary

        where = " WHERE tool = ?" if tool is not None else ""
        params: tuple[object, ...] = (tool,) if tool is not None else ()
        unclassified = (
            "SUM(CASE WHEN is_initial IS NULL THEN 1 ELSE 0 END)" if has_is_initial else "COUNT(*)"
        )
        # A response was followed up when a row is marked as a follow-up — even
        # if its initial row was lost or purged — or, for legacy rows that
        # carry no marker, when it has more than one row.
        followed = (
            "(COUNT(*) > 1 OR COALESCE(SUM(is_initial = 0), 0) > 0)"
            if has_is_initial
            else "(COUNT(*) > 1)"
        )
        # One statement, so every figure describes the same snapshot while the
        # server keeps appending rows.
        row = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(n), 0), COALESCE(SUM(followed), 0), "
            "COALESCE(SUM(unclassified), 0), COALESCE(SUM(total), 0), "
            "COALESCE(SUM(delivered), 0), MIN(first_at) FROM ("
            f"SELECT COUNT(*) AS n, {followed} AS followed, "
            f"{unclassified} AS unclassified, "
            "MAX(total_chars) AS total, SUM(chars) AS delivered, MIN(created_at) AS first_at "
            f"FROM progressive_reads{where} GROUP BY key)",
            params,
        ).fetchone()
        responses, reads, follow_ups, unclassified_n, produced, delivered, oldest = row
        summary.update(
            {
                "available": True,
                "total_responses": int(responses),
                "total_reads": int(reads),
                "follow_up_responses": int(follow_ups),
                "unclassified_reads": int(unclassified_n),
                "produced_chars": int(produced),
                "delivered_chars": int(delivered),
                "delivered_ratio": (delivered / produced) if produced > 0 else 0.0,
                "oldest_at": float(oldest) if oldest is not None else None,
            }
        )
    except sqlite3.Error as exc:
        summary["error"] = exception_summary(exc)
    finally:
        db.close()
    return summary


def _read_all_time(
    db: sqlite3.Connection, summary: dict[str, object], tool: str | None, has_is_initial: bool
) -> None:
    """Fill *summary* from the retained rows plus the rollup, merged per response.

    One statement over both halves, so a purge that moves rows into the rollup
    while this runs cannot count them twice or not at all. The retained half
    goes through the trigger's own guards (``_row_terms``) and the rollup half
    through ``_finite_total``, so a malformed row counts the same before and
    after it is purged. ``SUM`` raises past int64; the retry with ``TOTAL``
    returns floats that ``_count`` turns back into ints where integral.
    """
    where = " WHERE tool = ?" if tool is not None else ""
    params: tuple[object, ...] = (tool, tool) if tool is not None else ()
    # The first arm of the UNION names the columns.
    names = ("n", "follow_ups", "unclassified", "total", "delivered", "first_at")
    retained = ", ".join(
        f"{term} AS {name}"
        for term, name in zip(_row_terms("", has_is_initial), names, strict=True)
    )
    rolled = ", ".join(
        (
            _finite_total("reads"),
            _finite_total("follow_up_reads"),
            _finite_total("unclassified_reads"),
            _finite_total_or_null("max_total_chars"),
            _finite_total("delivered_chars"),
            _finite_total_or_null("min_created_at"),
        )
    )
    statement = (
        "SELECT COUNT(*), {agg}(n), {agg}(followed), {agg}(unclassified), {agg}(total), "
        "{agg}(delivered), MIN(first_at) FROM ("
        "SELECT key, {agg}(n) AS n, ({agg}(n) > 1 OR {agg}(follow_ups) > 0) AS followed, "
        "{agg}(unclassified) AS unclassified, MAX(total) AS total, "
        "{agg}(delivered) AS delivered, MIN(first_at) AS first_at FROM ("
        f"SELECT key, {retained} FROM progressive_reads{where} "
        f"UNION ALL SELECT key, {rolled} FROM {_ROLLUP_TABLE}{where}"
        ") GROUP BY key)"
    )
    try:
        row = db.execute(statement.replace("{agg}", "SUM"), params).fetchone()
    except sqlite3.OperationalError as exc:
        if "integer overflow" not in str(exc):
            raise
        row = db.execute(statement.replace("{agg}", "TOTAL"), params).fetchone()
    responses, reads, follow_ups, unclassified, produced, delivered, oldest = row
    produced_n: Any = _count(produced)
    delivered_n: Any = _count(delivered)
    summary.update(
        {
            "available": True,
            "total_responses": int(responses),
            "total_reads": _count(reads),
            "follow_up_responses": _count(follow_ups),
            "unclassified_reads": _count(unclassified),
            "produced_chars": produced_n,
            "delivered_chars": delivered_n,
            "delivered_ratio": (delivered_n / produced_n) if produced_n > 0 else 0.0,
            "oldest_at": float(oldest) if oldest is not None else None,
        }
    )
