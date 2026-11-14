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
for the daemon. The foreign-daemon path (:func:`iter_foreign_handshakes`) can't
``ping`` a daemon at an incompatible ``PROTOCOL_VERSION``, so it cross-checks the
recorded pid against a bare TCP connect-probe to the endpoint (``is_pid_alive``
*and* something still accepting) — a stale handshake naming a recycled pid fails
the connect because its port is unbound. See
:func:`~memtomem_stm.cli.daemon_cmd._live_foreign_daemons`.

A handshake under another fingerprint is not necessarily stale. Processes on
one machine can run with different ``MEMTOMEM_STM_*`` environments on purpose —
an MCP-launched server and a shell hook, say — and each then keys to its own
live daemon (#1195). A config change (or a ``PROTOCOL_VERSION`` bump) is the
other way to get one: the old fingerprint's handshake stays behind for as long
as its daemon runs, and a daemon with an idle shutdown removes it on teardown
once nothing uses it. Caveat: a daemon whose idle shutdown is off
(``idle_timeout_seconds`` 0 or ``inf``) never exits on its own, so it can
outlive every caller of its fingerprint, and ``mms daemon stop`` (which keys
to the *current* fingerprint) cannot reach it — ``mms daemon status`` reports
every foreign-fingerprint daemon, with ``idle_shutdown_seconds`` saying whether
it can linger (not whether anything still uses it), and ``mms daemon stop
--all`` terminates them (see :func:`iter_foreign_handshakes`).

The fingerprint is keyed. It covers the whole ``surfacing`` model — the LTM
command line and ``ltm_mcp_headers`` among it — and it is written into a file
name and into the handshake, so a plain hash would let anyone holding a
handshake (attached to a bug report, say) confirm a guessed header by
recomputing it. It is an HMAC under ``stm-discovery.key``, a random key kept
beside the handshakes in ``data_dir`` (:func:`discovery_key_path`), which every
STM process sharing that directory reads. The key is created by whoever first
needs a daemon to exist — the daemon, a spawn request, the standalone surfacing
adapter — and never by a command that only looks (``status``, ``stop``, a
``ping``): with no key there is no fingerprint, so those report no daemon for
this config without building a handshake or lock path. A daemon can still be
running in the directory then — one an earlier build started, or one whose key
was removed while it ran — and is found through its published handshake, as a
daemon under a different config (:func:`iter_foreign_handshakes`). Losing the
key loses nothing stored.

File shape::

    {"v": 1, "pid": 1234, "host": "127.0.0.1", "port": 53412,
     "token": "<hex>", "created_at": 1716600000.0, "config_fingerprint": "<hex>",
     "idle_shutdown_seconds": 900.0}

``idle_shutdown_seconds`` is ``null`` when the daemon never shuts down on idle,
and absent from handshakes written by builds that predate it. It was added
without bumping :data:`HANDSHAKE_VERSION`: readers select fields with ``.get``,
and a bump would make an older hook at the same fingerprint reject a newer
daemon's handshake.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload

from memtomem_stm.daemon.protocol import PROTOCOL_VERSION
from memtomem_stm.utils import child_reaper
from memtomem_stm.utils.digest import keyed_framed_digest
from memtomem_stm.utils.fileio import atomic_write_text
from memtomem_stm.utils.json_out import dumps as _json_dumps
from memtomem_stm.utils.local_key import load_key, load_or_create_key

if TYPE_CHECKING:
    from memtomem_stm.config import STMConfig

logger = logging.getLogger(__name__)

HANDSHAKE_VERSION = 1
# Filenames are ``stm-daemon-<fingerprint>.{json,lock}`` (the lock half lives in
# ``locking.py`` with the same prefix). The fingerprint is a 16-hex digest, so
# it is always a filesystem-safe filename component.
HANDSHAKE_PREFIX = "stm-daemon"
# Outside the ``stm-daemon-*`` names on purpose: the handshake glob and the
# lock names must never pick the key up.
DISCOVERY_KEY_NAME = "stm-discovery.key"
_FINGERPRINT_DOMAIN = "stm-discovery-v1"


def handshake_path(data_dir: Path, fingerprint: str) -> Path:
    """Per-config handshake file ``stm-daemon-<fingerprint>.json`` under ``data_dir``.

    Keyed by the config fingerprint so daemons under different configs publish to
    distinct files and coexist (see the module docstring). Callers pass
    :func:`config_fingerprint` of their own effective config.
    """
    return (data_dir / f"{HANDSHAKE_PREFIX}-{fingerprint}.json").expanduser()


def discovery_key_path(data_dir: Path) -> Path:
    """The key every fingerprint under ``data_dir`` is derived with.

    Normalized like :func:`handshake_path`. Not resolved: the key sits inside
    the directory, so every spelling that reaches the directory reaches the
    same file.
    """
    return (data_dir / DISCOVERY_KEY_NAME).expanduser()


@overload
def config_fingerprint(config: STMConfig, *, create_key: Literal[True]) -> str: ...


@overload
def config_fingerprint(config: STMConfig, *, create_key: bool) -> str | None: ...


def config_fingerprint(config: STMConfig, *, create_key: bool) -> str | None:
    """Stable keyed digest of the config that determines daemon/engine behavior.

    *create_key* has no default, so every caller says which side it is on. A
    caller that brings a daemon into existence passes ``True`` and always gets
    a fingerprint. A caller that only looks for one passes ``False`` and gets
    ``None`` when ``data_dir`` holds no key: there is no fingerprint to look
    under, and the caller must report "no daemon for this config" without
    building a handshake or lock path. A key that exists but cannot be used raises
    :class:`~memtomem_stm.utils.local_key.LocalKeyError` either way — falling
    back to an unkeyed value would give this process a different identity from
    every other one sharing the directory.

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
    parse (the other one idle-times-out once unused under a finite
    ``idle_timeout_seconds``; a pinned ``idle_timeout_seconds=0`` daemon is
    reported by ``mms daemon status`` and stopped by ``mms daemon stop --all``
    — see the module docstring and :func:`iter_foreign_handshakes`). This is the structural half of the
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
        # Sibling of the line above and required for the same reason: a caller
        # that asked for the serialized daemon must not silently reuse a
        # running four-way one, and vice versa (#874).
        "max_concurrent_ltm_ops": config.daemon.max_concurrent_ltm_ops,
        "surface_tools_env": os.environ.get("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", ""),
        "protocol_version": PROTOCOL_VERSION,
    }
    # ``json_out.dumps`` rather than ``json.dumps``: ``surface_tools_env`` is an
    # environment variable, and on POSIX one holding a byte that is not valid
    # UTF-8 is decoded with ``surrogateescape``, so a lone surrogate reaches the
    # digest's ``.encode`` with no config file involved. That raise lands in
    # ``DaemonServer.__init__`` and in ``client._live_handshake_candidate``,
    # neither of which guards it — no daemon starts and every daemon-touching
    # CLI command prints a traceback (#761). Escaping keeps the digest total;
    # the escaped form is still distinct per input, so the fingerprint keeps
    # separating configs (#757's "only hashed" is not a clearance).
    blob = _json_dumps(material, sort_keys=True, ensure_ascii=False)
    key_path = discovery_key_path(config.data_dir)
    key = load_or_create_key(key_path) if create_key else load_key(key_path)
    if key is None:
        return None
    return keyed_framed_digest((blob,), key=key, domain=_FINGERPRINT_DOMAIN)[:16]


def write_handshake(
    path: Path,
    *,
    pid: int,
    host: str,
    port: int,
    token: str,
    config_fingerprint: str,
    created_at: float,
    idle_shutdown_seconds: float | None,
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
        "idle_shutdown_seconds": idle_shutdown_seconds,
    }
    atomic_write_text(path, json.dumps(payload), mode=0o600)


def read_handshake(path: Path) -> dict[str, Any] | None:
    """Parse the handshake file; ``None`` if absent, unreadable, or malformed."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


@overload
def iter_foreign_handshakes(
    data_dir: Path, current_fingerprint: str | None, *, include_unreadable: Literal[False] = False
) -> list[tuple[str, dict[str, Any]]]: ...


@overload
def iter_foreign_handshakes(
    data_dir: Path, current_fingerprint: str | None, *, include_unreadable: Literal[True]
) -> list[tuple[str, dict[str, Any] | None]]: ...


def iter_foreign_handshakes(
    data_dir: Path, current_fingerprint: str | None, *, include_unreadable: bool = False
) -> list[tuple[str, dict[str, Any]]] | list[tuple[str, dict[str, Any] | None]]:
    """``(fingerprint, handshake)`` for every published daemon under a config
    fingerprint *other than* ``current_fingerprint``.

    ``None`` for ``current_fingerprint`` means the caller has none — no
    discovery key, or one it cannot use — and then every handshake is foreign.

    Globs the per-config handshake files (``stm-daemon-<fp>.json``) under
    ``data_dir`` and parses each with :func:`read_handshake`, skipping the
    current config's own file and any that are missing/unreadable/malformed —
    or, with ``include_unreadable``, yielding ``(fingerprint, None)`` for an
    unreadable/malformed one, so a caller can still ask whether a daemon holds
    that fingerprint's lock (#1245). The
    fingerprint is taken from the *filename* — the authoritative routing key that
    ``mms daemon status``/``stop`` derive from the config — so a corrupted
    in-file ``config_fingerprint`` that disagrees does not change which file a
    daemon owns.

    Pure enumeration: it does **not** probe liveness (a left-behind file may name
    a long-dead pid). The caller decides how to confirm a daemon is alive — for a
    foreign daemon that may speak an older :data:`PROTOCOL_VERSION` a socket
    ``ping`` would fail the version gate, so :func:`is_pid_alive` is the
    available signal. Sorted by fingerprint for deterministic output.

    A missing ``data_dir`` returns an empty list. Any other failure to list it
    (no read permission, not a directory, …) raises ``OSError``: those leave the
    foreign daemons unknown, not absent (#1249). ``Path.glob`` returns nothing
    for a directory it cannot list, so ``os.listdir`` checks that first; the
    matching and order stay glob's.
    """
    base = data_dir.expanduser()
    try:
        os.listdir(base)
    except FileNotFoundError:
        return []
    paths = sorted(base.glob(f"{HANDSHAKE_PREFIX}-*.json"))
    out: list[tuple[str, dict[str, Any] | None]] = []
    for path in paths:
        fingerprint = path.stem.removeprefix(f"{HANDSHAKE_PREFIX}-")
        if fingerprint == current_fingerprint:
            continue
        data = read_handshake(path)
        if data is None and not include_unreadable:
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


# Re-exported: the probe moved to ``utils.child_reaper`` with the leaked-child
# sweep that is its main consumer (#906). Callers here keep importing it from
# this module.
is_pid_alive = child_reaper.is_pid_alive
