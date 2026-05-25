"""Daemon discovery — the ``stm-daemon.json`` handshake file.

The daemon publishes its loopback endpoint + auth token to a single file under
``data_dir`` (default ``~/.memtomem``). ``mms hook`` and ``mms daemon
status/stop`` read it to find a running daemon; the daemon writes it
**atomically at ``0o600`` only after a successful port bind**, so the file
never advertises an endpoint that isn't accepting yet. The write is
last-writer-wins, not a mutual-exclusion primitive (two daemons binding
distinct ephemeral ports would overwrite each other here) — preventing a second
daemon is the spawn lock's job (see :mod:`~memtomem_stm.daemon.locking`).
Liveness is proven by a successful ``ping`` over the socket — not by the
recorded ``pid`` — so a recycled PID can't be mistaken for the daemon.

File shape::

    {"v": 1, "pid": 1234, "host": "127.0.0.1", "port": 53412,
     "token": "<hex>", "created_at": 1716600000.0, "config_fingerprint": "<sha>"}

``config_fingerprint`` lets a reader detect a daemon started under stale config
(different LTM command, feedback DB, etc.) and treat it as stale.
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
HANDSHAKE_FILENAME = "stm-daemon.json"


def handshake_path(data_dir: Path) -> Path:
    """Absolute path to the handshake file under ``data_dir``."""
    return (data_dir / HANDSHAKE_FILENAME).expanduser()


def config_fingerprint(config: STMConfig) -> str:
    """Stable digest of the config that determines daemon/engine behavior.

    A running daemon froze its config at start; if a caller's effective config
    differs, the daemon would serve stale behavior, so the caller must treat it
    as stale. We therefore fingerprint the *whole* daemon-relevant surface
    rather than a hand-picked subset: the full ``surfacing`` model (min_score,
    max_results, min_response_chars, default_namespace, result_format,
    include_session_context, persist_query_text, the LTM command, …), the full
    ``hook`` model (record_feedback_events, …), the bind host, and the flat
    ``MEMTOMEM_STM_HOOK_SURFACE_TOOLS`` env that ``cli.hook_cmd`` reads directly
    (so it isn't in any model). ``mode="json"`` makes ``Path``/enum values
    serializable. Over-inclusion only costs an extra restart on a config
    change, which is the safe direction.
    """
    material = {
        "surfacing": config.surfacing.model_dump(mode="json"),
        "hook": config.hook.model_dump(mode="json"),
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
