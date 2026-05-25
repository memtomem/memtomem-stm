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
Liveness is proven by a successful ``ping`` over the socket — not by the
recorded ``pid`` — so a recycled PID can't be mistaken for the daemon.

A config change leaves the old fingerprint's handshake behind as an orphan; it
is harmless (no reader keys to it anymore) and the old daemon removes its own on
graceful teardown / idle timeout.

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

from memtomem_stm.utils.fileio import atomic_write_text

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

    Deliberately **excluded**: the client-only ``hook`` fields ``use_daemon``,
    ``fallback`` and ``daemon_timeout_seconds``. They govern how the *hook*
    talks to the daemon, never what the daemon does — the daemon's behavior is
    independent of them (``mms daemon start`` warms the same daemon whether or
    not the hook has opted out via ``MEMTOMEM_STM_HOOK__USE_DAEMON=0``).
    Including them would make a live daemon look stale and the hook would reject
    it (then, under the default ``fallback=skip``, return ``{}`` forever).
    ``mode="json"`` makes ``Path``/enum values serializable.
    """
    material = {
        "surfacing": config.surfacing.model_dump(mode="json"),
        "record_feedback_events": config.hook.record_feedback_events,
        "host": config.daemon.host,
        "surface_tools_env": os.environ.get("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", ""),
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
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
    return True
