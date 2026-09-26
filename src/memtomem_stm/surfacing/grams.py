"""Keyed hashes of surfaced paths and text, shared by collection and analysis.

Surfacing records *which* file a delivered memory came from and *what* its
rendered preview said, but only as keyed hashes: the rows must support an
offline join against a host transcript without holding the path or the text
itself. Both sides of that join — the collection code in
:mod:`memtomem_stm.surfacing.feedback_store` and any later reader — have to
normalize and hash identically, so every rule that shapes a hash lives here
and nowhere else.

Hashes are HMAC-SHA256 truncated to 16 bytes (32 hex chars) under a random
per-install key kept in ``stm_feedback.db``. The key is readable on the
machine, so this is not secrecy against a local reader; it keeps paths and
text out of the tables and stops a leaked export from being reversed by
dictionary.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sys
from types import ModuleType

KEY_BYTES = 32
"""Length of the per-install HMAC key."""

MAX_SNIPPET_GRAMS = 64
"""Cap on stored 4-gram hashes per memory. The kept subset is the smallest
hashes, so it depends only on the text and the key — never on which arm or
which render produced it."""

_DIGEST_BYTES = 16

_PLATFORM_CASEFOLD = sys.platform in {"darwin", "win32"}
"""Whether path keys are case-folded on this platform.

A platform rule, not a per-volume one: macOS and Windows default to
case-insensitive filesystems. On a case-sensitive APFS volume two files whose
names differ only in case share a key. Collection and analysis fold the same
way, so the rule is consistent; that collision is the documented limit."""

_WORD_RE = re.compile(r"[a-z0-9_]{3,}")

_SENTINEL_SOURCES = frozenset({"", ".", "unknown", "pinned"})
"""``source_file`` values the LTM adapters use when a result has no real file:
``"unknown"`` (compact and structured parsers), ``"pinned"`` (pinned-context
fallback), and ``""`` / ``"."`` (the daemon adapter's empty default, which
``Path("")`` renders as ``"."``). All are relative, so the absolute-path test
already excludes them; they are named so that stays true on purpose."""


def keyed_hash(value: str, key: bytes) -> str:
    """HMAC-SHA256 of *value* under *key*, truncated to 16 bytes, as hex."""
    mac = hmac.new(key, value.encode("utf-8", errors="surrogatepass"), hashlib.sha256)
    return mac.digest()[:_DIGEST_BYTES].hex()


def unsanitize(text: str) -> str:
    """Invert :meth:`SurfacingFormatter._sanitize` on formatter output.

    ``_sanitize`` writes ``<>&``, a backtick, control and structural
    characters as ``\\uXXXX`` / ``\\UXXXXXXXX``, and puts a backslash before
    every Markdown metacharacter — the backslash itself included. Scanning
    left to right, ``\\uXXXX`` / ``\\UXXXXXXXX`` decode and any other ``\\X``
    becomes ``X``, which is exact for everything ``_sanitize`` emits. Only
    formatter output may be passed here: agent-written text such as a literal
    ``\\u0041lpha`` must keep its own tokens, and would be decoded.

    Whitespace runs that ``_sanitize`` collapsed stay collapsed; the word
    tokenizer in :func:`gram_hashes` does not see the difference.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char != "\\" or i + 1 >= n:
            out.append(char)
            i += 1
            continue
        nxt = text[i + 1]
        width = 4 if nxt == "u" else 8 if nxt == "U" else 0
        if width:
            digits = text[i + 2 : i + 2 + width]
            if len(digits) == width and all(c in "0123456789abcdefABCDEF" for c in digits):
                out.append(chr(int(digits, 16)))
                i += 2 + width
                continue
        out.append(nxt)
        i += 2
    return "".join(out)


def gram_hashes(text: str, key: bytes) -> set[str]:
    """Keyed hashes of every run of four consecutive words in *text*.

    Lower-cased; a word is a maximal run matching ``[a-z0-9_]{3,}``; the four
    words are joined by one space before hashing.
    """
    words = _WORD_RE.findall(text.lower())
    return {keyed_hash(" ".join(words[i : i + 4]), key) for i in range(len(words) - 3)}


def snippet_grams(preview: str, key: bytes) -> list[str]:
    """The stored 4-gram hashes of one rendered bullet preview.

    The whole pipeline: :func:`unsanitize` the formatter output, hash every
    4-gram, dedupe, sort ascending, keep the first :data:`MAX_SNIPPET_GRAMS`.
    """
    return sorted(gram_hashes(unsanitize(preview), key))[:MAX_SNIPPET_GRAMS]


def path_key(
    path: str,
    *,
    cwd: str | None = None,
    pathmod: ModuleType = os.path,
    casefold: bool = _PLATFORM_CASEFOLD,
) -> str:
    """The lexical key of *path*: never touches the filesystem or the account database.

    A relative path is joined onto *cwd* when one is given; ``normpath``;
    lower-cased when *casefold*. ``~`` is not expanded: ``expanduser`` can
    consult the account database (``pwd.getpwnam`` for ``~user``), which may
    block, and collection only keys absolute paths anyway. Lower-casing, not
    ``str.casefold``: case-insensitive APFS and NTFS compare names by simple
    per-character case mapping, under which ``Straße`` and ``STRASSE`` are two
    files, and full case folding would give them one key. Collection passes no
    *cwd* (only absolute paths are eligible); a transcript reader passes the
    record's ``cwd`` so a relative argument keys the same as its absolute form.
    *pathmod* is injectable so the Windows rules can be tested on any OS.
    """
    joined = path
    if cwd is not None and not pathmod.isabs(path):
        joined = pathmod.join(cwd, path)
    normalized = pathmod.normpath(joined)
    return normalized.lower() if casefold else normalized


def ancestor_keys(key: str, *, pathmod: ModuleType = os.path) -> list[str]:
    """Every ancestor directory of an already-normalized path key, nearest first."""
    ancestors: list[str] = []
    current = key
    while True:
        parent = pathmod.dirname(current)
        if not parent or parent == current:
            break
        ancestors.append(parent)
        current = parent
    return ancestors


def basename_key(key: str, *, pathmod: ModuleType = os.path) -> str:
    """The final component of an already-normalized path key."""
    return pathmod.basename(key)


def eligible_source(source: str | None, *, pathmod: ModuleType = os.path) -> bool:
    """Whether a delivered memory's ``source_file`` can anchor a file match.

    Decided from the string alone, at render time: an absolute path that is not
    an adapter sentinel. A relative path is ineligible because the only anchor
    available later is the agent's ``cwd``, not the base the LTM indexed it
    against; a ``~`` path is ineligible because expanding it can consult the
    account database, which this hot path must not wait on. Nothing is looked
    up, so a file deleted or moved after delivery cannot change the answer.
    """
    if source is None or source in _SENTINEL_SOURCES:
        return False
    return pathmod.isabs(source)
