"""Installation-local secret keys kept in a sidecar file next to a store.

Some stores derive an identifier from inputs that can hold user or credential
text — the response-cache row key hashes the call arguments, the tool-graph
provider fingerprint hashes the backend's command line. A plain hash of such
input is a guess oracle: anyone holding a copy of the file can confirm a guessed
input by recomputing it. Keying the derivation with a secret removes that, but
only if the secret does not travel with the file. So the key lives in its own
file beside the store (``<store>.key``), never inside it: a copied database, a
single-file backup or a file attached to a bug report arrives without its key.

The key is created on first use and never rewritten. Losing it is harmless for
the stores using it today — every identifier it keyed becomes unreachable, which
for a cache is a miss — and each store notices the change and purges its rows.

Creation is exclusive. The key is written in full to a temporary file and then
hard-linked into place, which fails if the name already exists, so two processes
creating at once end up reading the same key and no reader ever sees a partial
file. A filesystem without hard links gets an error rather than a weaker
fallback: a non-exclusive create could hand two first-run processes different
keys.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path

__all__ = ["LocalKeyError", "key_path_for", "load_key", "load_or_create_key"]

_KEY_BYTES = 32
_KEY_HEX_CHARS = _KEY_BYTES * 2
_HEX_DIGITS = frozenset("0123456789abcdef")


class LocalKeyError(Exception):
    """The key file exists but cannot be used, or cannot be created.

    Deliberately not an :class:`OSError`: callers that treat ``OSError`` as
    "transient, try again later" would turn a key that can never load into a
    silent retry loop. The message names the file so an operator can act on it.
    """


def key_path_for(store_path: Path) -> Path:
    """The sidecar key path for the store at *store_path*: ``<name>.key``.

    The store path is expanded and resolved first, so a store reached through
    ``~``, a relative path or a symlink maps to one key file. Two spellings of
    one database with two keys would each see the other's rows as written under
    a foreign key and purge them on every open.
    """
    try:
        resolved = store_path.expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise LocalKeyError(f"cannot resolve the store path {store_path}: {exc}") from exc
    return resolved.with_name(resolved.name + ".key")


def load_key(path: Path) -> bytes | None:
    """Read the key at *path*; ``None`` only if no file exists there.

    Anything else that prevents using the file raises :class:`LocalKeyError` and
    leaves it untouched: an unreadable file, a dangling symlink, or content that
    is not exactly one key. "Absent" and "unusable" are kept apart because
    folding them would let a caller overwrite a key that some other process is
    using.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LocalKeyError(f"cannot inspect the key file {path}: {exc}") from exc
    # Non-blocking so a FIFO (or another special file) at this name fails
    # below instead of hanging the open; a regular file ignores the flag.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise LocalKeyError(f"cannot read the key file {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise LocalKeyError(f"the key file {path} is not a regular file")
        _tighten_mode(fd)
        raw = _read_all(fd, _KEY_HEX_CHARS + 2)
    except OSError as exc:
        raise LocalKeyError(f"cannot read the key file {path}: {exc}") from exc
    finally:
        os.close(fd)
    return _parse(raw, path)


def load_or_create_key(path: Path) -> bytes:
    """Return the key at *path*, creating it first if no file exists there."""
    existing = load_key(path)
    if existing is not None:
        return existing
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise LocalKeyError(f"cannot create the directory for {path}: {exc}") from exc
    _publish_new_key(path)
    key = load_key(path)
    if key is None:
        # Created a moment ago and gone already: something removed it between
        # the link and this read. Say so rather than loop.
        raise LocalKeyError(f"the key file {path} disappeared right after it was created")
    return key


def _publish_new_key(path: Path) -> None:
    """Write a fresh key to a temp file and hard-link it to *path*.

    Losing the race to another creator is success: that process's key is the one
    every later reader will see.
    """
    content = (secrets.token_bytes(_KEY_BYTES).hex() + "\n").encode("ascii")
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    except OSError as exc:
        raise LocalKeyError(f"cannot create the key file {path}: {exc}") from exc
    try:
        try:
            if sys.platform != "win32":
                os.fchmod(fd, 0o600)
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            return
        except OSError as exc:
            hint = ""
            if exc.errno in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV):
                hint = " (this filesystem may not support hard links)"
            raise LocalKeyError(f"cannot create the key file {path}: {exc}{hint}") from exc
    except OSError as exc:
        raise LocalKeyError(f"cannot create the key file {path}: {exc}") from exc
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _tighten_mode(fd: int) -> None:
    """Drop group/other permission bits if present. POSIX only.

    Checked before changing, so the common read leaves the file's ctime alone.
    """
    if sys.platform == "win32":  # pragma: no cover - POSIX permission model
        return
    mode = os.fstat(fd).st_mode
    if stat.S_IMODE(mode) & 0o077:
        os.fchmod(fd, 0o600)


def _read_all(fd: int, limit: int) -> bytes:
    """Up to *limit* + 1 bytes from *fd*, so an oversized file is detectable."""
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _parse(raw: bytes, path: Path) -> bytes:
    text = raw.decode("ascii", errors="replace")
    if text.endswith("\n"):
        text = text[:-1]
    if len(text) != _KEY_HEX_CHARS or not set(text) <= _HEX_DIGITS:
        raise LocalKeyError(
            f"the key file {path} does not hold a {_KEY_BYTES}-byte hex key; "
            "move it aside to have a new one created"
        )
    return bytes.fromhex(text)
