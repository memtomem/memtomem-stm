"""Daemon discovery — the per-config ``stm-daemon-<fingerprint>.json`` handshake.

The daemon publishes its loopback endpoint + auth token to a file under
``data_dir`` (default ``~/.memtomem``). ``mms hook`` and ``mms daemon
status/stop`` read it to find a running daemon; the daemon writes it
**atomically at ``0o600`` only after a successful port bind**, so the file
never advertises an endpoint that isn't accepting yet.

The filename is **keyed by the config fingerprint** (``stm-daemon-<fp>.json``),
so daemons started under different configs (different LTM command, feedback DB,
…) own *distinct* handshake files and coexist instead of clobbering one shared
file — config-drift coexistence. A reader only ever opens the file for *its
own* config's fingerprint, so it never even sees a mismatched daemon's
handshake; the in-file ``config_fingerprint`` then double-checks the content
matches the path (defense against a corrupted/hand-edited file). Within a
single fingerprint the write is still last-writer-wins, not a mutual-exclusion
primitive — preventing a second *same-config* daemon is the (likewise
fingerprint-keyed) lock's job (see :mod:`~memtomem_stm.daemon.locking`).
Liveness for a *current-config* daemon is proven by a successful ``ping`` over
the socket — not by the recorded ``pid`` — so a recycled PID can't be mistaken
for the daemon. The foreign-orphan path (:func:`iter_foreign_handshakes`) can't
``ping`` a daemon at an incompatible ``PROTOCOL_VERSION``, so it cross-checks the
recorded pid against a bare TCP connect-probe to the endpoint (``is_pid_alive``
*and* something still accepting) — a stale handshake naming a recycled pid fails
the connect because its port is unbound. See
:func:`~memtomem_stm.cli.daemon_cmd._live_foreign_daemons`.

A config change (or a ``PROTOCOL_VERSION`` bump) leaves the old fingerprint's
handshake behind as an orphan. No reader keys to it anymore, so it is harmless
to *new* callers, and a daemon with a finite ``idle_timeout_seconds`` removes
its own handshake on graceful teardown / idle timeout. Caveat: a daemon pinned
with ``idle_timeout_seconds=0`` never idle-shuts-down, so after a fingerprint
change it lingers and is not reachable by ``mms daemon stop`` (which keys to the
*current* fingerprint) — ``mms daemon status`` reports such a foreign-fingerprint
daemon and ``mms daemon stop --all`` terminates it (see
:func:`iter_foreign_handshakes`).

File shape::

    {"v": 1, "pid": 1234, "host": "127.0.0.1", "port": 53412,
     "token": "<hex>", "created_at": 1716600000.0, "config_fingerprint": "<sha>"}
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memtomem_stm.daemon.protocol import PROTOCOL_VERSION
from memtomem_stm.utils.fileio import atomic_write_text
from memtomem_stm.utils.json_out import dumps as _json_dumps

if TYPE_CHECKING:
    from memtomem_stm.config import STMConfig

logger = logging.getLogger(__name__)

HANDSHAKE_VERSION = 1
# Filenames are ``stm-daemon-<fingerprint>.{json,lock}`` (the lock half lives in
# ``locking.py`` with the same prefix). The fingerprint is a 16-hex digest, so
# it is always a filesystem-safe filename component.
HANDSHAKE_PREFIX = "stm-daemon"


def handshake_path(data_dir: Path, fingerprint: str) -> Path:
    """Per-config handshake file ``stm-daemon-<fingerprint>.json`` under ``data_dir``.

    Keyed by the config fingerprint so daemons under different configs publish to
    distinct files and coexist (see the module docstring). Callers pass
    :func:`config_fingerprint` of their own effective config.
    """
    return (data_dir / f"{HANDSHAKE_PREFIX}-{fingerprint}.json").expanduser()


def config_fingerprint(config: STMConfig) -> str:
    """Stable digest of the config that determines daemon/engine behavior.

    A running daemon froze its config at start; if a caller's effective config
    differs *in a way that changes daemon behavior*, the daemon would serve
    stale behavior and the caller must treat it as stale. So we fingerprint the
    daemon-behavior-relevant surface: the full ``surfacing`` model (min_score,
    max_results, min_response_chars, default_namespace, result_format,
    include_session_context, persist_query_text, the LTM command, …), the bind
    host, the flat ``MEMTOMEM_STM_HOOK_SURFACE_TOOLS`` env that ``cli.hook_cmd``
    reads directly (so it isn't in any model), and exactly the one ``hook``
    field the daemon engine wiring consumes: ``record_feedback_events``.

    Deliberately **excluded**: every other ``hook`` field — the client-only
    ``use_daemon``, ``fallback``, ``daemon_timeout_seconds`` and ``auto_spawn``,
    plus the whole ``compression`` sub-block (it runs in the hook process,
    never in the daemon). They govern how the *hook* behaves or talks to the
    daemon, never what the daemon does — the daemon's behavior is independent
    of them (``mms daemon start`` warms the same daemon whether or not the
    hook has opted out via ``MEMTOMEM_STM_HOOK__USE_DAEMON=0``). Including
    them would make a live daemon look stale and the hook would reject it
    (then, under the default ``fallback=skip``, return ``{}`` forever).
    ``mode="json"`` makes ``Path``/enum values serializable.

    ``protocol_version`` is also folded in so a wire-incompatible daemon is
    treated as a *different config*: a hook and a daemon built at different
    :data:`~memtomem_stm.daemon.protocol.PROTOCOL_VERSION` values key to distinct
    handshake/lock paths and coexist instead of exchanging frames one side can't
    parse (the stale one idle-times-out under a finite ``idle_timeout_seconds``;
    a pinned ``idle_timeout_seconds=0`` daemon is reported by ``mms daemon
    status`` and stopped by ``mms daemon stop --all`` — see the module docstring
    and :func:`iter_foreign_handshakes`). This is the structural half of the
    version guard; the explicit per-frame ``v`` check is the belt-and-suspenders.
    """
    material = {
        # ``use_daemon`` chooses the standalone client's route; it does not
        # change the daemon-owned engine or LTM connection and therefore must
        # not split discovery identity.
        "surfacing": config.surfacing.model_dump(mode="json", exclude={"use_daemon"}),
        "record_feedback_events": config.hook.record_feedback_events,
        "host": config.daemon.host,
        "idle_timeout_seconds": config.daemon.idle_timeout_seconds,
        "max_pending_requests": config.daemon.max_pending_requests,
        "surface_tools_env": os.environ.get("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", ""),
        "protocol_version": PROTOCOL_VERSION,
    }
    # ``json_out.dumps`` rather than ``json.dumps``: ``surface_tools_env`` is an
    # environment variable, and on POSIX one holding a byte that is not valid
    # UTF-8 is decoded with ``surrogateescape``, so a lone surrogate reaches the
    # ``.encode`` below with no config file involved. That raise lands in
    # ``DaemonServer.__init__`` and in ``client._live_handshake_candidate``,
    # neither of which guards it — no daemon starts and every daemon-touching
    # CLI command prints a traceback (#761). Escaping keeps the digest total;
    # the escaped form is still distinct per input, so the fingerprint keeps
    # separating configs (#757's "only hashed" is not a clearance).
    blob = _json_dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def write_handshake(
    path: Path,
    *,
    pid: int,
    host: str,
    port: int,
    token: str,
    config_fingerprint: str,
    created_at: float,
) -> None:
    """Atomically publish the handshake file at ``0o600`` (token is sensitive)."""
    payload = {
        "v": HANDSHAKE_VERSION,
        "pid": pid,
        "host": host,
        "port": port,
        "token": token,
        "created_at": created_at,
        "config_fingerprint": config_fingerprint,
    }
    atomic_write_text(path, json.dumps(payload), mode=0o600)


def read_handshake(path: Path) -> dict[str, Any] | None:
    """Parse the handshake file; ``None`` if absent, unreadable, or malformed."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def iter_foreign_handshakes(
    data_dir: Path, current_fingerprint: str
) -> list[tuple[str, dict[str, Any]]]:
    """``(fingerprint, handshake)`` for every published daemon under a config
    fingerprint *other than* ``current_fingerprint``.

    Globs the per-config handshake files (``stm-daemon-<fp>.json``) under
    ``data_dir`` and parses each with :func:`read_handshake`, skipping the
    current config's own file and any that are missing/unreadable/malformed. The
    fingerprint is taken from the *filename* — the authoritative routing key that
    ``mms daemon status``/``stop`` derive from the config — so a corrupted
    in-file ``config_fingerprint`` that disagrees does not change which file a
    daemon owns.

    Pure enumeration: it does **not** probe liveness (a left-behind file may name
    a long-dead pid). The caller decides how to confirm a daemon is alive — for a
    foreign daemon that may speak an older :data:`PROTOCOL_VERSION` a socket
    ``ping`` would fail the version gate, so :func:`is_pid_alive` is the
    available signal. Sorted by fingerprint for deterministic output. Returns an
    empty list if ``data_dir`` is absent or unreadable (ops callers degrade).
    """
    base = data_dir.expanduser()
    try:
        paths = sorted(base.glob(f"{HANDSHAKE_PREFIX}-*.json"))
    except OSError:
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for path in paths:
        fingerprint = path.stem.removeprefix(f"{HANDSHAKE_PREFIX}-")
        if fingerprint == current_fingerprint:
            continue
        data = read_handshake(path)
        if data is None:
            continue
        out.append((fingerprint, data))
    return out


def remove_handshake_if_owner(path: Path, *, pid: int, token: str) -> None:
    """Unlink the handshake file only if it still describes *this* daemon.

    Compares ``pid`` and ``token`` so a daemon tearing down does not delete a
    successor's file (e.g. after a fast restart). Best-effort and quiet.
    """
    data = read_handshake(path)
    if data is None:
        return
    if data.get("pid") == pid and data.get("token") == token:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("failed to remove handshake file %s", path, exc_info=True)


def is_pid_alive(pid: int) -> bool:
    """Best-effort liveness probe for a PID. Liveness for *daemon* decisions
    should prefer a socket ``ping``; this is a secondary signal for status/stop.
    """
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI only
        # No portable signal-0 on Windows; assume alive and let the ping decide.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    except OverflowError:
        # A pid beyond the platform's pid_t range (e.g. a huge integer from
        # a hand-edited handshake) raises OverflowError before ESRCH — it
        # cannot name a live process. Not an OSError subclass, so it needs
        # its own arm.
        return False
    return True
