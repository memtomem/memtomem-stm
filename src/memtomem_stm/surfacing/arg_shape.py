"""The shape of a tool call's arguments, for the surfacing opportunity log.

An opportunity row has to say what kind of call surfacing saw without holding
anything the call carried. So it keeps argument key *names* — and only names
that look like identifiers and do not name a credential — plus a few counts,
and never an argument value. The two facts taken from a value are reduced
before they are stored: a path contributes its depth and, when its suffix is
one of a fixed set of common file types, that type; any other suffix is stored
as ``"other"``, so a user-chosen extension cannot reach the row.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import PurePath
from typing import Any

from memtomem_stm.proxy.privacy import contains_sensitive_content

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,31}")
_MAX_KEYS = 32

# ``contains_sensitive_content`` finds secrets in text: its patterns need an
# assignment or a token shape, so a bare field name such as ``password`` or
# ``api_key`` passes it. Key names are matched against this list instead, after
# lower-casing and dropping underscores. It over-drops on purpose (``author``,
# ``bypass``): a dropped key only costs a count, a kept one is disclosure.
_SENSITIVE_KEY_FRAGMENTS = (
    "pass",
    "secret",
    "token",
    "auth",
    "cred",
    "cookie",
    "private",
    "apikey",
    "accesskey",
    "signature",
    "session",
)

_PATH_KEYS = ("file_path", "path")

_KNOWN_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".ipynb",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".sh",
        ".sql",
        ".md",
        ".rst",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".xml",
        ".html",
        ".css",
        ".csv",
        ".lock",
        ".log",
    }
)
"""Suffixes an opportunity row may name. Anything else is stored as ``"other"``."""


def _key_is_storable(key: object) -> bool:
    if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
        return False
    folded = key.lower().replace("_", "")
    if any(fragment in folded for fragment in _SENSITIVE_KEY_FRAGMENTS):
        return False
    return not contains_sensitive_content(key)


def _path_value(arguments: Mapping[Any, Any]) -> str | None:
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def arg_shape_json(arguments: object, query_tokens: int | None) -> str:
    """Return the JSON shape of *arguments* for one opportunity row.

    ``keys`` lists the storable top-level key names, sorted and capped at 32;
    ``dropped_keys`` counts every other key. ``path_depth`` and ``ext`` come
    from the ``file_path`` (else ``path``) argument when it is a string.
    ``query_tokens`` is the whitespace token count of the extracted query,
    passed in as a count so no query text reaches this module.
    """
    keys: list[str] = []
    dropped = 0
    path_depth: int | None = None
    ext: str | None = None
    if isinstance(arguments, Mapping):
        storable = sorted(k for k in arguments if _key_is_storable(k))
        keys = storable[:_MAX_KEYS]
        dropped = len(arguments) - len(keys)
        path = _path_value(arguments)
        if path is not None:
            pure = PurePath(path)
            path_depth = len(pure.parts)
            suffix = pure.suffix.lower()
            if suffix:
                ext = suffix if suffix in _KNOWN_EXTENSIONS else "other"
    shape = {
        "keys": keys,
        "dropped_keys": dropped,
        "path_depth": path_depth,
        "ext": ext,
        "query_tokens": query_tokens,
    }
    return json.dumps(shape, sort_keys=True, separators=(",", ":"))
