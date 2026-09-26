"""The shape of a tool call's arguments, for the surfacing opportunity log.

An opportunity row has to say what kind of call surfacing saw without holding
anything the call carried. So it keeps counts, and never an argument value or
key name: a tool that accepts arbitrary keys lets the caller choose them, which
makes a key name user text. The two facts taken from a value are reduced
before they are stored: a path contributes its depth and, when its suffix is
one of a fixed set of common file types, that type; any other suffix is stored
as ``"other"``, so a user-chosen extension cannot reach the row.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import Any

_PATH_KEYS = ("file_path", "path")

# A path in a tool argument was written on whatever machine the tool runs on,
# not necessarily this one, so its flavour is read from the string: a drive
# letter, a UNC prefix, or backslashes with no forward slash mean Windows.
_WINDOWS_PATH_RE = re.compile(r"^(?:[A-Za-z]:|\\\\)|^[^/]*\\")


def _pure_path(value: str) -> PurePath:
    if _WINDOWS_PATH_RE.match(value):
        return PureWindowsPath(value)
    return PurePosixPath(value)


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


def _path_value(arguments: Mapping[Any, Any]) -> str | None:
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def arg_shape_json(arguments: object, query_tokens: int | None) -> str:
    """Return the JSON shape of *arguments* for one opportunity row.

    ``key_count`` is the number of top-level arguments. ``path_depth`` and
    ``ext`` come from the ``file_path`` (else ``path``) argument when it is a
    string. ``query_tokens`` is the whitespace token count of the extracted
    query, passed in as a count so no query text reaches this module.
    """
    key_count = 0
    path_depth: int | None = None
    ext: str | None = None
    if isinstance(arguments, Mapping):
        key_count = len(arguments)
        path = _path_value(arguments)
        if path is not None:
            pure = _pure_path(path)
            path_depth = len(pure.parts)
            suffix = pure.suffix.lower()
            if suffix:
                ext = suffix if suffix in _KNOWN_EXTENSIONS else "other"
    shape = {
        "key_count": key_count,
        "path_depth": path_depth,
        "ext": ext,
        "query_tokens": query_tokens,
    }
    return json.dumps(shape, sort_keys=True, separators=(",", ":"))
