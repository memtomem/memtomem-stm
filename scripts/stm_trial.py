#!/usr/bin/env python3
"""Offline extractor for the surfacing holdout trial.

The holdout (``surfacing.holdout_rate``) randomly withholds a share of hook
injections and records the assignment on the event row. Whether showing the
block changed what the agent did next can only be read from the host's own
transcripts, which Claude Code prunes after ``cleanupPeriodDays`` (default 30).
This script copies what the trial's analysis needs, while it still exists, into
a separate pseudonymous database::

    uv run python scripts/stm_trial.py                 # daily, from launchd/cron
    uv run python scripts/stm_trial.py --freeze --holdout-rate 0.2 --target 6225
    uv run python scripts/stm_trial.py --purge --yes   # after the report

Every id, path and text item is stored as a keyed hash under the HMAC key in
``stm_feedback.db`` (read-only here; the key is never copied), with paths and
4-grams hashed by :mod:`memtomem_stm.surfacing.grams` exactly as collection
hashes them. Extraction is incremental per transcript file (transcripts are
append-only; a file that shrank or whose first line changed is halted, never
re-extracted) and idempotent. ``--freeze`` ends the burn-in by writing the trial
record with the frozen snippet stoplist; ``--purge`` deletes the database.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shlex
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from memtomem_stm.surfacing.feedback_store import load_hmac_key
from memtomem_stm.surfacing.grams import (
    _PLATFORM_CASEFOLD,
    gram_hashes,
    keyed_hash,
    path_key,
    unsanitize,
)
from memtomem_stm.surfacing.trial import (
    Assignment,
    InjectionRecord,
    Ledger,
    LedgerEntry,
    MemoryRow,
    OutputRecord,
)

WINDOW_ENTRIES = 12
"""``n``: the Y1 window length in ledger entries, frozen into the trial record."""

WINDOW_SECONDS = 600
"""``t``: the Y1 / Y2 time bound in seconds, frozen into the trial record."""

DEFAULT_TRIAL_DB = Path("~/.memtomem/stm_trial.db")
DEFAULT_FEEDBACK_DB = Path("~/.memtomem/stm_feedback.db")
DEFAULT_PROJECTS_DIR = Path("~/.claude/projects")
DEFAULT_CLAUDE_SETTINGS = Path("~/.claude/settings.json")
DEFAULT_CLEANUP_PERIOD_DAYS = 30
DEFAULT_STATS_RETENTION_DAYS = 90
STALE_RUN_SECONDS = 7 * 86400
BURN_IN_MIN_DAYS = 7.0
BURN_IN_MIN_EVENTS = 500

CANONICAL_TOOLS = {"Read": "read", "Grep": "grep", "Glob": "glob", "Bash": "shell"}
"""Claude's native names for the tools the STM hook surfaces on (the
``READLIKE_SURFACE_TOOLS`` of the hook adapter). Only these are entries."""

SURFACED_OPEN = "<surfaced-memories>"
SURFACED_CLOSE = "</surfaced-memories>"
_SURFACING_ID_RE = re.compile(r"_surfacing_id: ([0-9a-f]{16})_")
_TIMESTAMP_RE = re.compile(rb'"timestamp"\s*:\s*"([^"]+)"')
_REDIRECT_CHARS = "<>|&;()"
_LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")
_PATTERN_SPLIT_RE = re.compile(r"[/\s*?\[\]{}()|^$+,!]+")
_LABEL_RE = re.compile(r"^[A-Za-z0-9_:]{1,64}$")
_SUBAGENT_RE = re.compile(r"^agent-(.+)\.jsonl$")
BATCH_BYTES = 16 << 20
"""Bytes of complete lines parsed and committed at a time per transcript."""
_HEAD_LINE_LIMIT = 1 << 20
"""The first line is the rewrite tripwire; a longer one is only hashed up to this."""
_SIDECARS = ("-journal", "-wal", "-shm")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
    stream_key TEXT NOT NULL, ordinal INTEGER NOT NULL, record_key TEXT,
    call_key TEXT NOT NULL, message_key TEXT, tool TEXT NOT NULL,
    eligible INTEGER NOT NULL, ok INTEGER, ts REAL,
    PRIMARY KEY (stream_key, call_key, ordinal));
CREATE TABLE IF NOT EXISTS entry_paths (
    stream_key TEXT NOT NULL, ordinal INTEGER NOT NULL, call_key TEXT NOT NULL,
    paths BLOB NOT NULL, patterns BLOB NOT NULL,
    PRIMARY KEY (stream_key, call_key, ordinal));
CREATE TABLE IF NOT EXISTS output_grams (
    stream_key TEXT NOT NULL, ordinal INTEGER NOT NULL, record_key TEXT, ts REAL,
    grams BLOB NOT NULL, PRIMARY KEY (stream_key, ordinal));
CREATE TABLE IF NOT EXISTS injection_grams (
    stream_key TEXT NOT NULL, ordinal INTEGER NOT NULL, record_key TEXT, call_key TEXT,
    ts REAL, event_key TEXT, header_sha256 TEXT NOT NULL, stm_wrapped INTEGER NOT NULL,
    grams BLOB NOT NULL, PRIMARY KEY (stream_key, ordinal));
CREATE TABLE IF NOT EXISTS assignments (
    event_key TEXT PRIMARY KEY, created_at REAL NOT NULL, stream_key TEXT, call_key TEXT,
    id_advertised INTEGER, header_digest TEXT, arm TEXT NOT NULL, holdout_rate REAL,
    injected_chars INTEGER, memory_keys TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assignment_memories (
    event_key TEXT NOT NULL, memory_key TEXT NOT NULL, eligible INTEGER NOT NULL,
    path_hash_lexical TEXT, dir_hashes TEXT, basename_hash TEXT, snippet_grams TEXT NOT NULL,
    PRIMARY KEY (event_key, memory_key));
CREATE TABLE IF NOT EXISTS assignment_opportunities (
    opportunity_key TEXT PRIMARY KEY, event_key TEXT, arm TEXT NOT NULL, holdout_rate REAL,
    gate_decision TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS streams (
    stream_key TEXT PRIMARY KEY, is_subagent INTEGER NOT NULL, first_ts REAL, newest_ts REAL,
    bytes_read INTEGER NOT NULL DEFAULT 0, lines_read INTEGER NOT NULL DEFAULT 0,
    head_mac TEXT, halted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS coverage (
    run_id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL,
    files_read INTEGER, files_skipped INTEGER, files_halted INTEGER,
    cleanup_period_days INTEGER NOT NULL, stats_retention_days INTEGER NOT NULL,
    key_fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coverage_streams (
    run_id INTEGER NOT NULL, stream_key TEXT NOT NULL, newest_ts REAL,
    PRIMARY KEY (run_id, stream_key));
CREATE TABLE IF NOT EXISTS trial_record (
    id INTEGER PRIMARY KEY CHECK (id = 1), stoplist BLOB NOT NULL, stoplist_sha256 TEXT NOT NULL,
    stoplist_size INTEGER NOT NULL, burnin_start REAL NOT NULL, burnin_end REAL NOT NULL,
    burnin_events INTEGER NOT NULL, n INTEGER NOT NULL, t INTEGER NOT NULL,
    holdout_rate REAL NOT NULL, target_count INTEGER NOT NULL, key_fingerprint TEXT NOT NULL,
    frozen_at REAL NOT NULL);
"""

_DATA_TABLES = (
    "ledger",
    "entry_paths",
    "output_grams",
    "injection_grams",
    "assignments",
    "assignment_memories",
    "assignment_opportunities",
    "streams",
    "coverage",
    "coverage_streams",
    "trial_record",
)


class TrialError(RuntimeError):
    """A refusal: the run stops without writing anything further."""


def _warn(message: str) -> None:
    print(f"stm_trial: warning: {message}", file=sys.stderr)


# ── hashing helpers ───────────────────────────────────────────────────


def key_fingerprint(key: bytes) -> str:
    """First eight bytes of the key's SHA-256, as 16 hex chars."""
    return hashlib.sha256(key).digest()[:8].hex()


def pack_grams(grams: Iterable[str]) -> bytes:
    """A set of keyed hashes (grams or paths) as sorted, concatenated 16-byte digests."""
    return b"".join(bytes.fromhex(g) for g in sorted(set(grams)))


def unpack_grams(blob: bytes) -> list[str]:
    return [blob[i : i + 16].hex() for i in range(0, len(blob), 16)]


def stream_key(session_id: str, agent_id: str | None, key: bytes) -> str:
    """``HMAC(session_id + "/" + agent_id)``, the agent id empty for a main file."""
    return keyed_hash(f"{session_id}/{agent_id or ''}", key)


def _bytes_mac(data: bytes, key: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).digest()[:16].hex()


# ── entry paths ───────────────────────────────────────────────────────


def _expand_home(token: str) -> str:
    """Expand a bare ``~`` or ``~/…`` from ``$HOME`` only.

    ``~user`` is left alone: expanding it consults the account database, which
    collection avoids for the same reason (``grams.path_key``).
    """
    if token == "~" or token.startswith("~/"):
        home = os.environ.get("HOME")
        if home:
            return home + token[1:]
    return token


def _lexical(token: str, base: str | None, casefold: bool) -> str | None:
    token = _expand_home(token)
    if not token:
        return None
    if not os.path.isabs(token) and base is None:
        return None
    return path_key(token, cwd=base, casefold=casefold)


def _bash_paths(command: str, cwd: str | None, casefold: bool) -> set[str]:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    # Known limit: `cd` options (`cd -P dir`) are not parsed; 0 of 51,687
    # cd-bearing Bash commands in the transcripts measured here used one.
    # A token resolves against cwd and every cd target *earlier* in the command
    # (cumulative, as the shell applies them); a cd after it cannot have moved it,
    # and a cd's own target resolves against the directories before that cd.
    bases: list[str | None] = [cwd]
    current = cwd
    out: set[str] = set()
    for index, token in enumerate(tokens):
        pieces = {token}
        for piece in token.split("="):
            piece = piece.strip(_REDIRECT_CHARS)
            pieces |= {piece, _LINE_SUFFIX_RE.sub("", piece)}
        for piece in pieces:
            for base in bases:
                lexical = _lexical(piece, base, casefold)
                if lexical is not None:
                    out.add(lexical)
        if index == 0 or tokens[index - 1] != "cd":
            continue
        target = _expand_home(token.strip(_REDIRECT_CHARS))
        if not target or target == "-":
            continue
        if os.path.isabs(target):
            current = os.path.normpath(target)
        elif current is not None:
            current = os.path.normpath(os.path.join(current, target))
        if current is not None and current not in bases:
            bases.append(current)
    return out


def _pattern_tokens(tool_input: dict[str, Any], casefold: bool) -> set[str]:
    text = f"{tool_input.get('pattern') or ''} {tool_input.get('glob') or ''}"
    if casefold:
        text = text.lower()
    return {t.replace("\\.", ".") for t in _PATTERN_SPLIT_RE.split(text) if t}


def entry_path_keys(
    tool: str, tool_input: dict[str, Any], cwd: str | None, *, casefold: bool
) -> tuple[set[str], set[str]]:
    """The lexical path keys and Grep/Glob pattern tokens of one entry's input.

    Returned unhashed so they can be tested; the extractor hashes both.
    """
    paths: set[str] = set()
    patterns: set[str] = set()
    if tool == "read":
        value = tool_input.get("file_path")
        if isinstance(value, str):
            lexical = _lexical(value, cwd, casefold)
            if lexical is not None:
                paths.add(lexical)
    elif tool in ("grep", "glob"):
        value = tool_input.get("path")
        target = value if isinstance(value, str) and value else cwd
        if target is not None:
            lexical = _lexical(target, cwd, casefold)
            if lexical is not None:
                paths.add(lexical)
        patterns = _pattern_tokens(tool_input, casefold)
    elif tool == "shell":
        value = tool_input.get("command")
        if isinstance(value, str):
            paths = _bash_paths(value, cwd, casefold)
    return paths, patterns


# ── injections ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Injection:
    event_id: str | None
    header_sha256: str
    stm_wrapped: bool
    text: str
    """The text the grams are taken from (unsanitized when STM wrote it)."""


def parse_injection(content: Any) -> Injection:
    """Classify one ``hook_additional_context`` record's content."""
    if isinstance(content, list):
        text = "\n".join(item for item in content if isinstance(item, str))
    elif isinstance(content, str):
        text = content
    else:
        text = ""
    wrapped = text.startswith(SURFACED_OPEN)
    inner = text
    if wrapped:
        inner = text[len(SURFACED_OPEN) :]
        # exactly the wrapper's own separator: an empty section_header is a
        # real (empty) first line, and collection hashed it as one
        inner = inner[1:] if inner.startswith("\n") else inner
        close = inner.rfind(SURFACED_CLOSE)
        if close != -1:
            inner = inner[:close].rstrip("\n")
    first_line = inner.split("\n", 1)[0]
    header = hashlib.sha256(first_line.encode("utf-8", errors="surrogatepass")).hexdigest()
    match = _SURFACING_ID_RE.search(inner)
    return Injection(
        event_id=match.group(1) if match else None,
        header_sha256=header,
        stm_wrapped=wrapped,
        text=unsanitize(inner) if wrapped else inner,
    )


# ── transcripts ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Transcript:
    path: Path
    session_id: str
    agent_id: str | None


def discover(projects_dir: Path) -> tuple[list[Transcript], int]:
    """Main files ``<proj>/<sid>.jsonl`` and ``<proj>/<sid>/subagents/agent-<aid>.jsonl``.

    Returns the transcripts and the number of other ``.jsonl`` files skipped.
    """
    found: list[Transcript] = []
    skipped = 0
    if not projects_dir.is_dir():
        return found, skipped
    for path in sorted(projects_dir.rglob("*.jsonl")):
        rel = path.relative_to(projects_dir).parts
        if len(rel) == 2:
            found.append(Transcript(path, path.stem, None))
            continue
        match = _SUBAGENT_RE.match(rel[-1]) if len(rel) == 4 else None
        if match and rel[2] == "subagents":
            found.append(Transcript(path, rel[1], match.group(1)))
            continue
        skipped += 1
    return found, skipped


def _parse_ts(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


@dataclass
class _FileBatch:
    ledger: list[tuple[object, ...]] = field(default_factory=list)
    entry_paths: list[tuple[object, ...]] = field(default_factory=list)
    output_grams: list[tuple[object, ...]] = field(default_factory=list)
    injections: list[tuple[object, ...]] = field(default_factory=list)
    results: list[tuple[int, str]] = field(default_factory=list)
    first_ts: float | None = None
    newest_ts: float | None = None

    def saw(self, ts: float | None) -> None:
        if ts is None:
            return
        self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
        self.newest_ts = ts if self.newest_ts is None else max(self.newest_ts, ts)


def _parse_line(
    line: bytes, ordinal: int, skey: str, key: bytes, casefold: bool, batch: _FileBatch
) -> None:
    stamp = _TIMESTAMP_RE.search(line)
    if stamp is not None:
        batch.saw(_parse_ts(stamp.group(1).decode("utf-8", "replace")))
    if not (
        b'"assistant"' in line or b'"tool_result"' in line or b"hook_additional_context" in line
    ):
        return
    try:
        record = json.loads(line)
    except ValueError:
        return
    if not isinstance(record, dict):
        return
    ts = _parse_ts(record.get("timestamp"))
    uuid = record.get("uuid")
    record_key = keyed_hash(uuid, key) if isinstance(uuid, str) else None
    attachment = record.get("attachment")
    if isinstance(attachment, dict) and attachment.get("type") == "hook_additional_context":
        injection = parse_injection(attachment.get("content"))
        tool_use_id = attachment.get("toolUseID")
        batch.injections.append(
            (
                skey,
                ordinal,
                record_key,
                keyed_hash(tool_use_id, key) if isinstance(tool_use_id, str) else None,
                ts,
                keyed_hash(injection.event_id, key) if injection.event_id else None,
                injection.header_sha256,
                int(injection.stm_wrapped),
                pack_grams(gram_hashes(injection.text, key)),
            )
        )
        return
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    if record.get("type") == "user":
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                call = block.get("tool_use_id")
                if isinstance(call, str):
                    ok = 0 if block.get("is_error") is True else 1
                    batch.results.append((ok, keyed_hash(call, key)))
        return
    if record.get("type") != "assistant":
        return
    message_id = message.get("id")
    message_key = keyed_hash(message_id, key) if isinstance(message_id, str) else None
    cwd = record.get("cwd") if isinstance(record.get("cwd"), str) else None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            raw_input = block.get("input")
            tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
            parts.append(json.dumps(tool_input, ensure_ascii=False))
            call = block.get("id")
            if not isinstance(call, str):
                continue
            call_key = keyed_hash(call, key)
            name = block.get("name")
            tool = CANONICAL_TOOLS.get(name, "other") if isinstance(name, str) else "other"
            eligible = tool != "other"
            batch.ledger.append(
                (skey, ordinal, record_key, call_key, message_key, tool, int(eligible), ts)
            )
            if eligible:
                paths, patterns = entry_path_keys(tool, tool_input, cwd, casefold=casefold)
                batch.entry_paths.append(
                    (
                        skey,
                        ordinal,
                        call_key,
                        pack_grams(keyed_hash(lexical, key) for lexical in paths),
                        pack_grams(keyed_hash(token, key) for token in patterns),
                    )
                )
    if parts:
        batch.output_grams.append(
            # per block: a 4-gram never spans two blocks (or a text and a tool input)
            (
                skey,
                ordinal,
                record_key,
                ts,
                pack_grams(g for p in parts for g in gram_hashes(p, key)),
            )
        )


# ── database ──────────────────────────────────────────────────────────


def _restrict_mode(path: Path) -> None:
    for candidate in (path, *(Path(f"{path}{suffix}") for suffix in _SIDECARS)):
        if candidate.exists():
            os.chmod(candidate, 0o600)


def open_trial_db(path: Path) -> sqlite3.Connection:
    """Open (creating, mode 0600) the trial DB; existing files are narrowed to 0600."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    _restrict_mode(path)
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA busy_timeout = 5000")
    db.executescript(_SCHEMA)
    return db


def _has_data(db: sqlite3.Connection) -> bool:
    return any(
        db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        for table in _DATA_TABLES
    )


def pin_key(db: sqlite3.Connection, key: bytes) -> str:
    """Pin the key fingerprint before anything keyed is stored; refuse a different key."""
    fingerprint = key_fingerprint(key)
    row = db.execute("SELECT value FROM meta WHERE name = 'key_fingerprint'").fetchone()
    if row is not None:
        if row[0] != fingerprint:
            raise TrialError(
                f"HMAC key fingerprint changed ({row[0]} pinned, {fingerprint} now); "
                "the trial DB cannot mix hashes from two keys"
            )
        return fingerprint
    if _has_data(db):
        raise TrialError("trial DB holds extracted rows but no key fingerprint pin")
    db.execute("INSERT INTO meta (name, value) VALUES ('key_fingerprint', ?)", (fingerprint,))
    return fingerprint


def _cleanup_period_days(settings_path: Path) -> int:
    try:
        data = json.loads(settings_path.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_CLEANUP_PERIOD_DAYS
    value = data.get("cleanupPeriodDays") if isinstance(data, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_CLEANUP_PERIOD_DAYS


def _stats_retention_days() -> int:
    try:
        from memtomem_stm.config import STMConfig

        return int(STMConfig().surfacing.stats_retention_days)
    except Exception as exc:  # a broken local config must not stop the daily run
        _warn(f"could not read stats_retention_days ({type(exc).__name__}); assuming 90")
        return DEFAULT_STATS_RETENTION_DAYS


def _next_lines(handle: Any, limit: int) -> tuple[list[bytes], int]:
    """The complete lines in the next *limit* bytes (or the one longer line there).

    Returns the lines and the bytes they span; ``([], 0)`` when only a partial
    line (one still being written) remains.
    """
    chunk = handle.read(limit)
    end = chunk.rfind(b"\n")
    if end == -1 and len(chunk) == limit:
        chunk += handle.readline()  # one line longer than the batch: take it whole
        end = chunk.rfind(b"\n")
    if end == -1:
        return [], 0
    return chunk[:end].split(b"\n"), end + 1


def _head_mac(handle: Any, key: bytes) -> str | None:
    """MAC of the first line, or of its first ``_HEAD_LINE_LIMIT`` bytes when longer.

    ``None`` only while that first line is shorter and still unterminated, since
    its bytes can still grow; once written, an append-only file never changes them.
    """
    head = handle.readline(_HEAD_LINE_LIMIT)
    if head.endswith(b"\n") or len(head) == _HEAD_LINE_LIMIT:
        return _bytes_mac(head, key)
    return None


def _commit_batch(
    db: sqlite3.Connection,
    transcript: Transcript,
    skey: str,
    run_id: int,
    batch: _FileBatch,
    position: tuple[int, int],
    head_mac: str | None,
) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        db.executemany(
            "INSERT OR IGNORE INTO ledger (stream_key, ordinal, record_key, call_key, "
            "message_key, tool, eligible, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            batch.ledger,
        )
        db.executemany(
            "INSERT OR IGNORE INTO entry_paths VALUES (?, ?, ?, ?, ?)", batch.entry_paths
        )
        db.executemany(
            "INSERT OR IGNORE INTO output_grams VALUES (?, ?, ?, ?, ?)", batch.output_grams
        )
        db.executemany(
            "INSERT OR IGNORE INTO injection_grams VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            batch.injections,
        )
        db.executemany(
            "UPDATE ledger SET ok = ? WHERE stream_key = ? AND call_key = ?",
            [(ok, skey, call_key) for ok, call_key in batch.results],
        )
        db.execute(
            "INSERT INTO streams (stream_key, is_subagent, first_ts, newest_ts, bytes_read, "
            "lines_read, head_mac) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (stream_key) DO UPDATE SET "
            "first_ts = CASE WHEN first_ts IS NULL OR excluded.first_ts < first_ts "
            "THEN COALESCE(excluded.first_ts, first_ts) ELSE first_ts END, "
            "newest_ts = CASE WHEN newest_ts IS NULL OR excluded.newest_ts > newest_ts "
            "THEN COALESCE(excluded.newest_ts, newest_ts) ELSE newest_ts END, "
            "bytes_read = excluded.bytes_read, lines_read = excluded.lines_read, "
            "head_mac = COALESCE(head_mac, excluded.head_mac)",
            (
                skey,
                int(transcript.agent_id is not None),
                batch.first_ts,
                batch.newest_ts,
                position[0],
                position[1],
                head_mac,
            ),
        )
        db.execute(
            "INSERT OR REPLACE INTO coverage_streams (run_id, stream_key, newest_ts) "
            "VALUES (?, ?, (SELECT newest_ts FROM streams WHERE stream_key = ?))",
            (run_id, skey, skey),
        )
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def _extract_file(
    db: sqlite3.Connection,
    transcript: Transcript,
    run_id: int,
    key: bytes,
    casefold: bool,
    batch_bytes: int | None = None,
) -> str:
    """Extract one file's new complete lines. Returns ``read``, ``halted`` or ``unchanged``.

    Lines are parsed and committed in batches of about *batch_bytes*, each with
    its resume offset, so memory stays bounded however much the file grew.
    """
    skey = stream_key(transcript.session_id, transcript.agent_id, key)
    row = db.execute(
        "SELECT bytes_read, lines_read, head_mac, halted FROM streams WHERE stream_key = ?",
        (skey,),
    ).fetchone()
    bytes_read, lines_read, head_mac, halted = row if row is not None else (0, 0, None, 0)
    if halted:
        return "halted"
    read_any = False
    try:
        with open(transcript.path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            current_head = _head_mac(handle, key)
            if size < bytes_read or (head_mac is not None and current_head != head_mac):
                _warn(f"transcript rewritten, halting its stream: {transcript.path.name}")
                db.execute("UPDATE streams SET halted = 1 WHERE stream_key = ?", (skey,))
                return "halted"
            handle.seek(bytes_read)
            while bytes_read < size:
                lines, consumed = _next_lines(handle, batch_bytes or BATCH_BYTES)
                if not lines:
                    break
                batch = _FileBatch()
                for offset, line in enumerate(lines):
                    _parse_line(line, lines_read + offset, skey, key, casefold, batch)
                bytes_read += consumed
                lines_read += len(lines)
                handle.seek(bytes_read)
                _commit_batch(
                    db, transcript, skey, run_id, batch, (bytes_read, lines_read), current_head
                )
                read_any = True
    except OSError as exc:
        _warn(f"cannot read {transcript.path.name}: {exc}")
    return "read" if read_any else "unchanged"


def _opt_key(value: object, key: bytes) -> str | None:
    return keyed_hash(value, key) if isinstance(value, str) and value else None


def copy_assignments(db: sqlite3.Connection, feedback_db: Path, key: bytes) -> int:
    """Copy every drawn event, its memory rows and drawn opportunities, keyed.

    Read in one read transaction on a ``mode=ro`` connection, so an event and
    its memory rows (written in one transaction by the store) are seen together.
    """
    resolved = feedback_db.expanduser().resolve()
    with closing(sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)) as src:
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {str(r[1]) for r in src.execute("PRAGMA table_info('surfacing_events')")}
        if "arm" not in columns:
            return 0
        src.execute("BEGIN")
        events = src.execute(
            "SELECT id, created_at, tool_use_id, host_session_id, host_agent_id, id_advertised, "
            "header_digest, arm, holdout_rate, injected_chars, memory_ids "
            "FROM surfacing_events WHERE arm IS NOT NULL"
        ).fetchall()
        memories = (
            src.execute(
                "SELECT p.surfacing_id, p.memory_id, p.eligible, p.path_hash_lexical, "
                "p.dir_hashes, p.basename_hash, p.snippet_grams FROM surfacing_memory_paths p "
                "JOIN surfacing_events e ON e.id = p.surfacing_id WHERE e.arm IS NOT NULL"
            ).fetchall()
            if "surfacing_memory_paths" in tables
            else []
        )
        opp_columns = {
            str(r[1]) for r in src.execute("PRAGMA table_info('surfacing_opportunities')")
        }
        opportunities = (
            src.execute(
                "SELECT id, surfacing_id, arm, holdout_rate, gate_decision, created_at "
                "FROM surfacing_opportunities WHERE arm IS NOT NULL"
            ).fetchall()
            if "arm" in opp_columns
            else []
        )
        src.execute("COMMIT")
    event_rows = []
    for (
        event_id,
        created_at,
        tool_use_id,
        session_id,
        agent_id,
        id_advertised,
        header_digest,
        arm,
        holdout_rate,
        injected_chars,
        memory_ids,
    ) in events:
        try:
            ids = json.loads(memory_ids)
        except (TypeError, ValueError):
            ids = []
        memory_keys = [keyed_hash(m, key) for m in ids if isinstance(m, str)]
        event_rows.append(
            (
                keyed_hash(event_id, key),
                created_at,
                stream_key(session_id, agent_id, key) if isinstance(session_id, str) else None,
                _opt_key(tool_use_id, key),
                id_advertised,
                header_digest,
                arm,
                holdout_rate,
                injected_chars,
                json.dumps(memory_keys),
            )
        )
    memory_rows = [
        (keyed_hash(sid, key), keyed_hash(mid, key), eligible, lexical, dirs, base, grams)
        for sid, mid, eligible, lexical, dirs, base, grams in memories
    ]
    opportunity_rows = [
        (
            keyed_hash(oid, key),
            _opt_key(sid, key),
            arm,
            rate,
            decision if isinstance(decision, str) and _LABEL_RE.match(decision) else "other",
            created_at,
        )
        for oid, sid, arm, rate, decision, created_at in opportunities
    ]
    db.execute("BEGIN IMMEDIATE")
    try:
        before = db.total_changes
        db.executemany(
            "INSERT OR IGNORE INTO assignments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", event_rows
        )
        added = db.total_changes - before
        db.executemany(
            "INSERT OR IGNORE INTO assignment_memories VALUES (?, ?, ?, ?, ?, ?, ?)", memory_rows
        )
        db.executemany(
            "INSERT OR IGNORE INTO assignment_opportunities VALUES (?, ?, ?, ?, ?, ?)",
            opportunity_rows,
        )
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return added


# ── modes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExtractSummary:
    run_id: int
    files_read: int
    files_unchanged: int
    files_halted: int
    files_skipped: int
    assignments_added: int
    key_fingerprint: str


def extract(
    trial_db: Path,
    feedback_db: Path,
    projects_dir: Path,
    *,
    claude_settings: Path = DEFAULT_CLAUDE_SETTINGS,
    stats_retention_days: int | None = None,
    casefold: bool = _PLATFORM_CASEFOLD,
    now: Callable[[], float] = time.time,
) -> ExtractSummary:
    """One daily run: pin/check the key, extract new transcript lines, copy assignments."""
    key = load_hmac_key(feedback_db)
    if not projects_dir.expanduser().is_dir():
        raise TrialError(f"transcript directory not found: {projects_dir}")
    with closing(open_trial_db(trial_db)) as db:
        fingerprint = pin_key(db, key)
        started = now()
        last = db.execute("SELECT MAX(ended_at) FROM coverage").fetchone()[0]
        if last is not None and started - last > STALE_RUN_SECONDS:
            _warn(
                f"last extractor run was {(started - last) / 86400:.1f} days ago; "
                "transcripts older than cleanupPeriodDays may be gone"
            )
        retention = (
            stats_retention_days if stats_retention_days is not None else _stats_retention_days()
        )
        cursor = db.execute(
            "INSERT INTO coverage (started_at, cleanup_period_days, stats_retention_days, "
            "key_fingerprint) VALUES (?, ?, ?, ?)",
            (started, _cleanup_period_days(claude_settings), retention, fingerprint),
        )
        run_id = int(cursor.lastrowid or 0)
        transcripts, skipped = discover(projects_dir.expanduser())
        counts = {"read": 0, "unchanged": 0, "halted": 0}
        for transcript in transcripts:
            counts[_extract_file(db, transcript, run_id, key, casefold)] += 1
        added = copy_assignments(db, feedback_db, key)
        db.execute(
            "UPDATE coverage SET ended_at = ?, files_read = ?, files_skipped = ?, "
            "files_halted = ? WHERE run_id = ?",
            (now(), counts["read"], skipped, counts["halted"], run_id),
        )
    _restrict_mode(trial_db.expanduser())
    return ExtractSummary(
        run_id,
        counts["read"],
        counts["unchanged"],
        counts["halted"],
        skipped,
        added,
        fingerprint,
    )


def burn_in_stoplist(rows: Iterable[tuple[str, str]]) -> set[str]:
    """Grams in the snippets of more than two distinct memories.

    *rows* are ``(memory_id, snippet_grams JSON)``. A memory's grams are the
    union over all its rows (its preview can differ between renders), so a
    memory surfaced many times still counts once.
    """
    per_memory: dict[str, set[str]] = {}
    for memory_id, grams_json in rows:
        try:
            grams = json.loads(grams_json)
        except (TypeError, ValueError):
            continue
        if isinstance(grams, list):
            per_memory.setdefault(memory_id, set()).update(g for g in grams if isinstance(g, str))
    counts: dict[str, int] = {}
    for grams in per_memory.values():
        for gram in grams:
            counts[gram] = counts.get(gram, 0) + 1
    return {gram for gram, count in counts.items() if count > 2}


def freeze(
    trial_db: Path,
    feedback_db: Path,
    *,
    holdout_rate: float,
    target: int,
    min_days: float = BURN_IN_MIN_DAYS,
    min_events: int = BURN_IN_MIN_EVENTS,
    now: Callable[[], float] = time.time,
) -> dict[str, object]:
    """End the burn-in: write the one-row trial record with the frozen stoplist."""
    if not 0 < holdout_rate <= 0.5:
        raise TrialError("--holdout-rate must be in (0, 0.5], the range collection clamps to")
    if target <= 0:
        raise TrialError("--target must be positive")
    key = load_hmac_key(feedback_db)
    if not trial_db.expanduser().exists():
        raise TrialError("no extractor run yet; the burn-in starts at the first run")
    with closing(open_trial_db(trial_db)) as db:
        start = db.execute("SELECT MIN(started_at) FROM coverage").fetchone()[0]
        if start is None:
            raise TrialError("no extractor run yet; the burn-in starts at the first run")
        fingerprint = pin_key(db, key)
        if db.execute("SELECT 1 FROM trial_record").fetchone() is not None:
            raise TrialError("the trial record is already frozen")
        if db.execute("SELECT 1 FROM streams LIMIT 1").fetchone() is None:
            raise TrialError("no transcript has been extracted yet; check --projects-dir")
        # the shortest retention any run recorded: a setting raised later cannot
        # bring back rows an earlier, shorter one already deleted
        shortest = db.execute(
            "SELECT MIN(stats_retention_days) FROM coverage WHERE stats_retention_days > 0"
        ).fetchone()[0]
        frozen_at = now()
        elapsed_days = (frozen_at - start) / 86400
        if elapsed_days < min_days:
            raise TrialError(f"burn-in has run {elapsed_days:.1f} days; needs {min_days}")
        if shortest is not None and elapsed_days > shortest:
            raise TrialError(
                f"burn-in ({elapsed_days:.1f} days) is longer than stats_retention_days "
                f"({shortest}); its earliest snippets may be gone"
            )
        resolved = feedback_db.expanduser().resolve()
        with closing(sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)) as src:
            # one read snapshot for the arm check and the burn-in reads, so a draw
            # committed between them cannot slip into the frozen stoplist unseen
            src.execute("BEGIN")
            src.execute("SELECT 1 FROM sqlite_master LIMIT 1")  # starts the snapshot
            drawn = False
            for table in ("surfacing_events", "surfacing_opportunities"):
                columns = {str(r[1]) for r in src.execute(f"PRAGMA table_info('{table}')")}
                if (
                    "arm" in columns
                    and src.execute(
                        f"SELECT 1 FROM {table} WHERE arm IS NOT NULL LIMIT 1"
                    ).fetchone()
                ):
                    drawn = True
            if drawn:
                src.execute("COMMIT")
                raise TrialError(
                    "drawn events already exist; holdout_rate must stay 0 until after the freeze"
                )
            # the population a draw can reach: hook calls carrying both host ids
            # (proxy events and events from before the ids existed never are)
            events_columns = {
                str(r[1]) for r in src.execute("PRAGMA table_info('surfacing_events')")
            }
            if not {"tool_use_id", "host_session_id"} <= events_columns:
                src.execute("COMMIT")
                raise TrialError("stm_feedback.db predates hook provenance; upgrade STM first")
            window = (
                "e.created_at >= ? AND e.created_at < ? "
                "AND e.tool_use_id IS NOT NULL AND e.host_session_id IS NOT NULL"
            )
            events = src.execute(
                f"SELECT COUNT(*) FROM surfacing_events e WHERE {window}", (start, frozen_at)
            ).fetchone()[0]
            rows = src.execute(
                "SELECT p.memory_id, p.snippet_grams FROM surfacing_memory_paths p "
                f"JOIN surfacing_events e ON e.id = p.surfacing_id WHERE {window}",
                (start, frozen_at),
            ).fetchall()
            src.execute("COMMIT")
        if events < min_events:
            raise TrialError(f"burn-in has {events} events; needs {min_events}")
        stoplist = pack_grams(burn_in_stoplist(rows))
        digest = hashlib.sha256(stoplist).hexdigest()
        db.execute(
            "INSERT INTO trial_record VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stoplist,
                digest,
                len(stoplist) // 16,
                start,
                frozen_at,
                events,
                WINDOW_ENTRIES,
                WINDOW_SECONDS,
                holdout_rate,
                target,
                fingerprint,
                frozen_at,
            ),
        )
    _restrict_mode(trial_db.expanduser())
    return {
        "stoplist_size": len(stoplist) // 16,
        "stoplist_sha256": digest,
        "burnin_events": events,
        "burnin_days": round(elapsed_days, 2),
        "key_fingerprint": fingerprint,
    }


def _hex_set(blob: bytes | None) -> frozenset[str]:
    return frozenset(unpack_grams(blob)) if blob else frozenset()


def _json_set(text: str | None) -> frozenset[str]:
    return frozenset(json.loads(text)) if text else frozenset()


def load_resolve_inputs(
    db: sqlite3.Connection,
) -> tuple[list[Assignment], list[MemoryRow], Ledger, frozenset[str]]:
    """Decode the trial DB into :func:`memtomem_stm.surfacing.trial.resolve`'s inputs.

    Hash sets are stored two ways — BLOBs of packed digests for what the
    extractor took from transcripts, JSON lists for what it copied from
    ``stm_feedback.db`` — and both come back as sets of hex keys. The stoplist is
    empty until ``--freeze`` wrote the trial record.
    """
    assignments = [
        Assignment(
            event_key=row[0],
            created_at=row[1],
            stream_key=row[2],
            call_key=row[3],
            id_advertised=bool(row[4]),
            header_digest=row[5],
            arm=row[6],
            holdout_rate=row[7],
        )
        for row in db.execute(
            "SELECT event_key, created_at, stream_key, call_key, id_advertised,"
            " header_digest, arm, holdout_rate FROM assignments"
        )
    ]
    memory_rows = [
        MemoryRow(
            event_key=row[0],
            memory_key=row[1],
            eligible=bool(row[2]),
            path_hash_lexical=row[3],
            dir_hashes=_json_set(row[4]),
            basename_hash=row[5],
            snippet_grams=_json_set(row[6]),
        )
        for row in db.execute(
            "SELECT event_key, memory_key, eligible, path_hash_lexical, dir_hashes,"
            " basename_hash, snippet_grams FROM assignment_memories"
        )
    ]
    entries = tuple(
        LedgerEntry(
            stream_key=row[0],
            ordinal=row[1],
            call_key=row[2],
            message_key=row[3],
            eligible=bool(row[4]),
            ok=None if row[5] is None else bool(row[5]),
            ts=row[6],
            paths=_hex_set(row[7]),
            patterns=_hex_set(row[8]),
        )
        for row in db.execute(
            "SELECT l.stream_key, l.ordinal, l.call_key, l.message_key, l.eligible, l.ok, l.ts,"
            " p.paths, p.patterns FROM ledger l LEFT JOIN entry_paths p"
            " ON p.stream_key = l.stream_key AND p.call_key = l.call_key"
            " AND p.ordinal = l.ordinal"
        )
    )
    outputs = tuple(
        OutputRecord(stream_key=row[0], ordinal=row[1], ts=row[2], grams=_hex_set(row[3]))
        for row in db.execute("SELECT stream_key, ordinal, ts, grams FROM output_grams")
    )
    injections = tuple(
        InjectionRecord(
            stream_key=row[0],
            ordinal=row[1],
            call_key=row[2],
            ts=row[3],
            event_key=row[4],
            header_sha256=row[5],
            stm_wrapped=bool(row[6]),
            grams=_hex_set(row[7]),
        )
        for row in db.execute(
            "SELECT stream_key, ordinal, call_key, ts, event_key, header_sha256, stm_wrapped,"
            " grams FROM injection_grams"
        )
    )
    streams = frozenset(row[0] for row in db.execute("SELECT stream_key FROM streams"))
    record = db.execute("SELECT stoplist FROM trial_record WHERE id = 1").fetchone()
    stoplist = _hex_set(record[0]) if record else frozenset()
    return assignments, memory_rows, Ledger(streams, entries, outputs, injections), stoplist


def purge_targets(trial_db: Path) -> list[Path]:
    path = trial_db.expanduser()
    return [p for p in (path, *(Path(f"{path}{s}") for s in _SIDECARS)) if p.exists()]


def purge(trial_db: Path) -> list[Path]:
    targets = purge_targets(trial_db)
    for target in targets:
        target.unlink()
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--freeze", action="store_true", help="end the burn-in; write the trial record"
    )
    mode.add_argument("--purge", action="store_true", help="delete the trial DB (needs --yes)")
    parser.add_argument("--yes", action="store_true", help="confirm --purge")
    parser.add_argument("--holdout-rate", type=float, help="planned holdout_rate (--freeze)")
    parser.add_argument("--target", type=int, help="target assigned eligible memories (--freeze)")
    parser.add_argument("--trial-db", type=Path, default=DEFAULT_TRIAL_DB)
    parser.add_argument("--feedback-db", type=Path, default=DEFAULT_FEEDBACK_DB)
    parser.add_argument("--projects-dir", type=Path, default=DEFAULT_PROJECTS_DIR)
    parser.add_argument("--claude-settings", type=Path, default=DEFAULT_CLAUDE_SETTINGS)
    parser.add_argument("--stats-retention-days", type=int)
    args = parser.parse_args(argv)
    try:
        if args.purge:
            targets = purge(args.trial_db) if args.yes else purge_targets(args.trial_db)
            verb = "deleted" if args.yes else "would delete (pass --yes)"
            for target in targets:
                print(f"{verb}: {target}")
            if not targets:
                print("nothing to delete")
            return 0
        if args.freeze:
            if args.holdout_rate is None or args.target is None:
                parser.error("--freeze needs --holdout-rate and --target")
            result = freeze(
                args.trial_db, args.feedback_db, holdout_rate=args.holdout_rate, target=args.target
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        summary = extract(
            args.trial_db,
            args.feedback_db,
            args.projects_dir,
            claude_settings=args.claude_settings,
            stats_retention_days=args.stats_retention_days,
        )
        print(json.dumps(summary.__dict__, sort_keys=True))
        return 0
    except (TrialError, RuntimeError) as exc:
        print(f"stm_trial: refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
