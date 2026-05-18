"""POSIX-only stdlib imports must not run at module load in ``src/``.

Python evaluates ``import`` statements eagerly. A bare ``import fcntl`` at the
top of a module raises ``ModuleNotFoundError`` on Windows the moment the
module is loaded — even when no call site uses it.

This guard walks every ``.py`` under ``src/`` and rejects POSIX-only stdlib
imports that execute at module-load time. An import is exempt only when nested
inside one of two recognized patterns whose runtime behavior actually prevents
the Windows crash:

* a platform guard: ``if sys.platform != "win32":``, ``if os.name != "nt":``,
  or ``if os.name == "posix":``
* an ``except ImportError`` wrapper (also ``ModuleNotFoundError``, broader
  catchall families, or bare ``except:``)

Plain ``if True:`` / ``if some_var:`` / ``try: ... except OSError:`` do **not**
exempt — the import still runs at load time and still crashes on Windows.

Function- and class-body imports don't run at module load and are out of scope
for this guard (they would fail at call time on Windows, a separate bug class).

``stm`` has zero POSIX-only imports today (verified during the #302 Windows
triage); this is preventive insurance against regressions during ongoing
Windows-correctness work. Mirrors memtomem #657.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

# Stdlib modules that exist only on POSIX builds of CPython. A module-load
# import of any of these crashes on Windows. ``pty`` and ``tty`` are included
# because both transitively import ``termios``; CPython docs mark each as
# "Availability: Unix" in its own right.
POSIX_ONLY_STDLIB_MODULES = frozenset(
    {
        "fcntl",
        "grp",
        "nis",
        "ossaudiodev",
        "posix",
        "pty",
        "pwd",
        "resource",
        "spwd",
        "syslog",
        "termios",
        "tty",
    }
)

# Exception types whose presence in an ``except`` clause genuinely protects
# against POSIX-only import failure.
IMPORT_ERROR_CATCHERS = frozenset(
    {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
)


def _is_posix_platform_guard(test: ast.expr) -> bool:
    """Recognize the canonical ``if`` tests that confine code to POSIX.

    Only the three documented forms exempt nested imports. Anything else
    (``if True``, ``if some_var``, ``if sys.platform == "darwin"``) leaves
    the import running at module-load time on Windows runners.
    """
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    left, op, right = test.left, test.ops[0], test.comparators[0]
    if not isinstance(right, ast.Constant) or not isinstance(right.value, str):
        return False

    def _is_attr(node: ast.expr, owner: str, attr: str) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == attr
            and isinstance(node.value, ast.Name)
            and node.value.id == owner
        )

    if _is_attr(left, "sys", "platform") and isinstance(op, ast.NotEq) and right.value == "win32":
        return True
    if _is_attr(left, "os", "name") and isinstance(op, ast.NotEq) and right.value == "nt":
        return True
    if _is_attr(left, "os", "name") and isinstance(op, ast.Eq) and right.value == "posix":
        return True
    return False


def _try_catches_import_error(handlers: list[ast.ExceptHandler]) -> bool:
    """True if any handler catches ``ImportError`` (or a superclass / bare except)."""
    for handler in handlers:
        if handler.type is None:
            return True  # bare ``except:``
        types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        for node in types:
            if isinstance(node, ast.Name) and node.id in IMPORT_ERROR_CATCHERS:
                return True
    return False


def _find_unguarded_posix_imports(
    body: list[ast.stmt], *, exempt: bool = False
) -> list[tuple[int, str]]:
    """Return ``(lineno, top_level_module_name)`` for offending imports.

    Recurses into ``If`` / ``Try`` (those run at module load time), flipping
    ``exempt=True`` for the protected sub-tree only when the construct matches
    a recognized POSIX-only guard or ``ImportError``-catching wrapper. Does
    not recurse into function/class bodies — those don't execute at load time.
    """
    offenses: list[tuple[int, str]] = []
    for node in body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in POSIX_ONLY_STDLIB_MODULES and not exempt:
                    offenses.append((node.lineno, top))
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            if top in POSIX_ONLY_STDLIB_MODULES and not exempt:
                offenses.append((node.lineno, top))
        elif isinstance(node, ast.If):
            body_exempt = exempt or _is_posix_platform_guard(node.test)
            offenses.extend(_find_unguarded_posix_imports(node.body, exempt=body_exempt))
            # ``orelse`` (the False branch) inherits only the outer ``exempt``.
            # We don't recognize the inverse ``if sys.platform == "win32":``
            # form because no src/ file uses it today; if one ever does, the
            # canonical fix is to flip the condition rather than expand here.
            offenses.extend(_find_unguarded_posix_imports(node.orelse, exempt=exempt))
        elif isinstance(node, (ast.Try, ast.TryStar)):
            body_exempt = exempt or _try_catches_import_error(node.handlers)
            offenses.extend(_find_unguarded_posix_imports(node.body, exempt=body_exempt))
            for handler in node.handlers:
                offenses.extend(_find_unguarded_posix_imports(handler.body, exempt=exempt))
            offenses.extend(_find_unguarded_posix_imports(node.orelse, exempt=exempt))
            offenses.extend(_find_unguarded_posix_imports(node.finalbody, exempt=exempt))
    return offenses


def test_no_posix_only_load_time_imports_in_src() -> None:
    paths = sorted(SRC_ROOT.rglob("*.py"))
    assert paths, f"no python sources discovered under {SRC_ROOT}"

    offenses: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, name in _find_unguarded_posix_imports(tree.body):
            offenses.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: import {name}")

    assert not offenses, (
        "POSIX-only stdlib module(s) imported at module load (will crash on "
        "Windows). Wrap in `if sys.platform != 'win32':` or "
        "`try/except ImportError`:\n  " + "\n  ".join(offenses)
    )


# --- walker self-tests -------------------------------------------------------
# Pin the contract: any change that loosens these is a guard regression.


def _names(src: str) -> set[str]:
    return {name for _, name in _find_unguarded_posix_imports(ast.parse(src).body)}


def test_guard_flags_bare_top_level_posix_import() -> None:
    assert "fcntl" in _names("import fcntl\n")


def test_guard_flags_pty_and_tty() -> None:
    assert "pty" in _names("import pty\n")
    assert "tty" in _names("import tty\n")


def test_guard_exempts_recognized_platform_guards() -> None:
    for guard in (
        "if sys.platform != 'win32':\n    import fcntl\n",
        "if os.name != 'nt':\n    import fcntl\n",
        "if os.name == 'posix':\n    import fcntl\n",
    ):
        assert "fcntl" not in _names(guard), guard


def test_guard_rejects_unrecognized_if_block() -> None:
    """``if True:`` and arbitrary tests still execute on Windows at load time."""
    for body in (
        "if True:\n    import fcntl\n",
        "if some_var:\n    import fcntl\n",
        "if sys.platform == 'darwin':\n    import fcntl\n",
    ):
        assert "fcntl" in _names(body), body


def test_guard_exempts_try_except_import_error() -> None:
    for body in (
        "try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n",
        "try:\n    import fcntl\nexcept ModuleNotFoundError:\n    fcntl = None\n",
        "try:\n    import fcntl\nexcept (ImportError, OSError):\n    fcntl = None\n",
        "try:\n    import fcntl\nexcept:\n    fcntl = None\n",
    ):
        assert "fcntl" not in _names(body), body


def test_guard_rejects_try_with_non_import_handler() -> None:
    """``try`` whose handlers don't catch ImportError still lets the crash through."""
    body = "try:\n    import fcntl\nexcept OSError:\n    fcntl = None\n"
    assert "fcntl" in _names(body)


def test_guard_skips_function_body_imports() -> None:
    """Function-body imports don't run at module load; out of scope for this guard."""
    body = "def f():\n    import fcntl\n    return fcntl\n"
    assert "fcntl" not in _names(body)
