"""STM's logging calls pass a literal message and their values as arguments (#1425).

``DisplayEscapingFormatter`` escapes a record's *arguments* as it interpolates
them. A value formatted into the message before the call — an f-string, a
``+``, a string built a few lines up — never reaches ``record.args``, so the
formatter cannot tell it from the message's own text and writes it as is.

This guard walks every ``.py`` under ``src/memtomem_stm`` and rejects a logging
call whose message is not a string literal. It also rejects a ``%c``
conversion: ``%c`` of an ``int`` writes that code point raw, and ``%c`` of a
string that needed escaping is not written at all.

What counts as a logging call is deliberately wide: any ``<receiver>.<level>(...)``
with at least one positional argument, ``math.log`` aside. A new helper that
happens to have an ``error(...)`` method and takes a computed first argument
fails here and gets a named exemption, rather than a logger under an unusual
name slipping through.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "memtomem_stm"

LEVEL_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)

# One ``%`` conversion: optional mapping key, flags, width, precision, length
# modifier, then the conversion character (``%%`` comes out as ``%``).
_CONVERSION = re.compile(r"%(?:\([^)]*\))?[#0\- +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?(.)")


def _logging_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in LEVEL_METHODS or not node.args:
            continue  # ``future.exception()`` takes no argument
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id == "math":
            continue  # ``math.log(x)``
        calls.append(node)
    return calls


def violations(source: str, filename: str = "<source>") -> list[str]:
    """Each logging call in *source* that breaks the rule, as ``file:line: why``."""
    found = []
    for call in _logging_calls(ast.parse(source, filename=filename)):
        index = 1 if call.func.attr == "log" else 0  # type: ignore[attr-defined]
        where = f"{filename}:{call.lineno}"
        if len(call.args) <= index:
            found.append(f"{where}: no message argument")
            continue
        message = call.args[index]
        if not (isinstance(message, ast.Constant) and isinstance(message.value, str)):
            found.append(f"{where}: message is not a string literal")
            continue
        if any(match.group(1) == "c" for match in _CONVERSION.finditer(message.value)):
            found.append(f"{where}: %c conversion")
    return found


@pytest.mark.parametrize(
    "source",
    [
        'logger.warning(f"Upstream {server} failed")',
        "logger.warning(message)",
        'logger.warning("Upstream " + server)',
        'logger.warning("Upstream {}".format(server))',
        'logger.warning("Upstream %s" % server)',
        'logger.info(PREFIX + "%s", server)',
        'logging.getLogger(__name__).error(f"{server}")',
        'logger.log(logging.INFO, f"{server}")',
        "logger.log(logging.INFO)",
        "log.exception(str(exc))",
        'logger.info("char %c", code)',
        'logger.info("%(key)c", {"key": code})',
        'logger.info("%-3c|", code)',
    ],
)
def test_the_check_rejects(source):
    assert len(violations(source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        'logger.warning("Upstream %s failed", server)',
        'logger.warning("Upstream %s "\n    "failed after %d tries", server, tries)',
        'logger.info("100%% of %(count)d", {"count": n})',
        'logger.log(logging.INFO, "level %s", name)',
        'logging.getLogger(__name__).debug("plain")',
        "error = future.exception()",
        "score = math.log(count)",
        'logger.info("%5.1f%%c is not a conversion", ratio)',
    ],
)
def test_the_check_accepts(source):
    assert violations(source) == []


def test_every_logging_call_in_src_passes_a_literal_message():
    files = sorted(SRC_ROOT.rglob("*.py"))
    assert files, f"no source files under {SRC_ROOT}"

    calls = 0
    found = []
    for path in files:
        source = path.read_text(encoding="utf-8")
        calls += len(_logging_calls(ast.parse(source)))
        found += violations(source, str(path.relative_to(SRC_ROOT)))

    assert calls > 100, f"the walk found only {calls} logging calls; has the receiver rule rotted?"
    assert found == []
