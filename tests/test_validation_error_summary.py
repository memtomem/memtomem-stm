"""What a validation error location may carry (#1085).

``validation_error_summary`` renders ``loc (type)`` and never the rejected
value. A location, though, holds the dict key of the failing entry, so these
tests pin the two classes of key its callers can reach — config key names and
upstream-authored keys — and an inventory of every reference to the helper in
``src/``, so a new caller (which might validate data keyed by secrets) shows up
in review.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest
from mcp.types import InitializeResult
from pydantic import ValidationError

from memtomem_stm.proxy.config import UpstreamServerConfig, validation_error_summary
from memtomem_stm.proxy.staged_status import ProbeStage

_SRC = Path(__file__).resolve().parents[1] / "src" / "memtomem_stm"


def _summary(model: type, data: dict) -> str:
    with pytest.raises(ValidationError) as info:
        model.model_validate(data)
    return validation_error_summary(info.value)


@pytest.mark.parametrize(
    ("field", "value", "expected", "absent"),
    [
        ("env", {"cnryEnvKey": 1}, "env.cnryEnvKey (string_type)", None),
        ("headers", {"cnryHdrKey": 1}, "headers.cnryHdrKey (string_type)", None),
        ("args", ["a", {"cnryArgKey": "cnryArgVal"}], "args.1 (string_type)", "cnryArgVal"),
    ],
    ids=["env-name", "header-name", "args-index"],
)
def test_config_keys_reach_the_location_and_values_do_not(field, value, expected, absent):
    """An env or header *name* is a config key and appears; an ``args`` item
    is a list entry, so only its index appears, not the value inside it."""
    summary = _summary(UpstreamServerConfig, {"prefix": "p", field: value})
    assert summary == expected
    if absent is not None:
        assert absent not in summary


def test_upstream_authored_key_reaches_the_probe_line():
    """On the probe's ``invalid server response`` line the key comes from the
    upstream's initialize reply, not from STM's config."""
    from memtomem_stm.cli.proxy import _probe_failure_message

    with pytest.raises(ValidationError) as info:
        InitializeResult.model_validate(
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"experimental": {"cnryKey": 1}},
                "serverInfo": {"name": "x", "version": "1"},
            }
        )
    message = _probe_failure_message(info.value, ProbeStage.TRANSPORT_CONNECTED)
    assert message == ("invalid server response: capabilities.experimental.cnryKey (dict_type)")


def _references(root: Path = _SRC) -> tuple[Counter[tuple[str, str]], list[str]]:
    """Every load of the name ``validation_error_summary`` under *root*, keyed by
    file and enclosing function, plus any import that renames it.

    Counting loads rather than calls catches a second call in a listed
    function, a call inside a lambda, and an alias made by assignment (the
    assignment is itself a load). An ``import ... as`` rename is reported
    separately, since the renamed calls would not match by name.
    """
    refs: Counter[tuple[str, str]] = Counter()
    renames: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stack: list[str] = []

        class _Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                for alias in node.names:
                    if alias.name == "validation_error_summary" and alias.asname:
                        renames.append(f"{rel}: as {alias.asname}")

            def visit_Name(self, node: ast.Name) -> None:
                if node.id == "validation_error_summary" and isinstance(node.ctx, ast.Load):
                    refs[(rel, ".".join(stack))] += 1

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if node.attr == "validation_error_summary" and isinstance(node.ctx, ast.Load):
                    refs[(rel, ".".join(stack))] += 1
                self.generic_visit(node)

        _Visitor().visit(tree)
    return refs, renames


@pytest.mark.parametrize(
    ("source", "expected_refs", "expected_renames"),
    [
        ("def f(e):\n    return validation_error_summary(e)\n", {("m.py", "f"): 1}, []),
        (
            "def f(e):\n    validation_error_summary(e)\n    return validation_error_summary(e)\n",
            {("m.py", "f"): 2},
            [],
        ),
        ("g = lambda e: validation_error_summary(e)\n", {("m.py", ""): 1}, []),
        ("vs = validation_error_summary\n", {("m.py", ""): 1}, []),
        ("def f(e):\n    return config.validation_error_summary(e)\n", {("m.py", "f"): 1}, []),
        (
            "from memtomem_stm.proxy.config import validation_error_summary as vs\n",
            {},
            ["m.py: as vs"],
        ),
    ],
    ids=["call", "second-call", "lambda", "alias", "attribute", "renamed-import"],
)
def test_inventory_sees_each_reference_form(tmp_path, source, expected_refs, expected_renames):
    """Positive controls for the scanner below: each form a new caller could
    take is counted, so an unchanged live inventory means no new reference."""
    (tmp_path / "m.py").write_text(source, encoding="utf-8")
    refs, renames = _references(tmp_path)
    assert dict(refs) == expected_refs
    assert renames == expected_renames


def test_references_are_the_reviewed_set():
    """Each caller validates STM's config, ``stm_admin`` parameters, or an
    upstream's reply — none keyed by secrets. A new reference must be checked
    against the rule in the helper's docstring, then added here."""
    refs, renames = _references()
    assert renames == []
    assert dict(refs) == {
        ("cli/proxy.py", "_runtime_proxy_read"): 1,
        ("cli/proxy.py", "_probe_failure_message"): 1,
        ("cli/proxy.py", "_surfacing_bootstrap_error"): 1,
        ("cli/proxy.py", "doctor"): 1,
        ("proxy/config.py", "config_load_error_summary"): 1,
        ("server.py", "stm_admin"): 1,
    }
