"""POSIX-only stdlib imports must not appear at module level in ``src/``.

Python evaluates ``import`` statements eagerly. A bare ``import fcntl`` at the
top of a module raises ``ModuleNotFoundError`` on Windows the moment the
module is loaded — even when no call site uses it. Conditional imports nested
under a platform guard (``if sys.platform != "win32":``) or wrapped in
``try/except ImportError`` only execute on POSIX and are safe.

``stm`` has zero such imports today (verified during the #302 Windows triage).
This guard exists to keep it that way; mirrors memtomem #657.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

# Stdlib modules that exist only on POSIX builds of CPython. A module-level
# import of any of these crashes on Windows at import time.
POSIX_ONLY_STDLIB_MODULES = frozenset(
    {
        "fcntl",
        "grp",
        "nis",
        "ossaudiodev",
        "posix",
        "pwd",
        "resource",
        "spwd",
        "syslog",
        "termios",
    }
)


def _module_level_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """Return ``(lineno, top_level_module_name)`` for imports at module scope.

    Imports nested inside ``If`` / ``Try`` / function / class bodies are
    excluded — those are the legitimate way to depend on a POSIX-only module
    without breaking Windows.
    """
    found: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name.split(".")[0]))
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module.split(".")[0]))
    return found


def test_no_posix_only_top_level_imports_in_src() -> None:
    paths = sorted(SRC_ROOT.rglob("*.py"))
    assert paths, f"no python sources discovered under {SRC_ROOT}"

    offenses: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, name in _module_level_imports(tree):
            if name in POSIX_ONLY_STDLIB_MODULES:
                offenses.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: import {name}")

    assert not offenses, (
        "POSIX-only stdlib module(s) imported at module level (will crash on "
        "Windows import). Wrap in `if sys.platform != 'win32':` or "
        "`try/except ImportError`:\n  " + "\n  ".join(offenses)
    )


def test_guard_detects_top_level_posix_import() -> None:
    """Self-test: the walker must flag a bare top-level POSIX import."""
    tree = ast.parse("import fcntl\n")
    assert ("fcntl") in {name for _, name in _module_level_imports(tree)}


def test_guard_ignores_conditional_posix_import() -> None:
    """Self-test: a platform-guarded import must not be flagged."""
    src = "import sys\nif sys.platform != 'win32':\n    import fcntl\n"
    names = {name for _, name in _module_level_imports(ast.parse(src))}
    assert "fcntl" not in names
    assert "sys" in names


def test_guard_ignores_try_wrapped_posix_import() -> None:
    """Self-test: a ``try/except ImportError`` wrapper must not be flagged."""
    src = "try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n"
    names = {name for _, name in _module_level_imports(ast.parse(src))}
    assert "fcntl" not in names
