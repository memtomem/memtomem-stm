"""Enumerate every raw ``json.dumps(..., ensure_ascii=False)`` in ``src/``.

``ensure_ascii=False`` keeps CJK and emoji readable and is the right default
here, but it also leaves a lone surrogate raw in the returned ``str`` — the
``UnicodeEncodeError`` then lands at whatever encodes that string later
(#757). Which sites need the surrogate-safe writer is a judgement call about
what happens to the result, and the four review rounds on #758 kept finding
another one because "every JSON document the CLI emits" was asserted from
reading ``cli/`` alone. It could not have been verified that way: a ``--json``
leg can serialize in ``proxy/``, which is exactly how
``selection_eval.to_json`` was missed.

So enumerate mechanically instead of claiming. Each file below carries the
reason its sites do *not* go through ``utils.json_out``. A new site — or one
removed — fails this test, which forces the same judgement to be made and
recorded rather than assumed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "memtomem_stm"

# path relative to src/memtomem_stm → (count, why it is not routed)
#
# "Hashed or measured" — the string is consumed in-process (length, digest,
# substring scan) and never encoded to bytes by us, so a surrogate cannot
# surface. "Encoded by us" entries are the ones that *would* raise; each is
# either already guarded at the call site or recorded as unaudited in #761.
ALLOWLIST: dict[str, tuple[int, str]] = {
    "proxy/compression.py": (
        26,
        "Reshapes an upstream response for the model. NOT safe and NOT fixed "
        "here — deferred to #761. 'The SDK owns the encoding' was wrong: "
        "``TextContent(...).model_dump_json()`` raises "
        "``PydanticSerializationError`` on a raw surrogate, and "
        "``_mm_json_loads`` decodes one out of a legal ``\\ud800`` escape in "
        "upstream content. Routing these also changes the lengths the "
        "compression budgets count, which is why it is not a one-line fix.",
    ),
    "proxy/selection_eval.py": (
        1,
        "The one left is a privacy scan over an in-memory string, which is "
        "searched and discarded. ``to_json`` (echoed by a --json leg) and "
        "the canonical hash input are both routed — the latter encodes on "
        "the very next expression, so 'it is only hashed' did not make it "
        "safe (#757 round 5).",
    ),
    "proxy/cache.py": (
        1,
        "Response-envelope column in the SQLite cache. sqlite3 encodes text "
        "parameters to UTF-8, so a surrogate raises at execute time; the "
        "value is upstream response content rather than anything from a "
        "config, and this path is unaudited — see #761.",
    ),
    "proxy/pending_store.py": (
        1,
        "Chunk payload column in the SQLite pending store, same encode-at-"
        "execute exposure as the response cache and the same unaudited "
        "upstream-content origin — see #761.",
    ),
    "proxy/progressive.py": (
        1,
        "Metadata column written through the same pending store; carries the "
        "server and tool names alongside upstream content — see #761.",
    ),
    "proxy/toolgraph_bundle.py": (
        1,
        "Contract fingerprint, encoded to bytes by us. A surrogate raises "
        "here, which is one of the reasons the CLI refuses to create such a "
        "name at all (#758); reaching it another way is #761.",
    ),
    "mms/drift.py": (
        1,
        "Canonical form of a registry server, hashed by its only caller with "
        "an explicit ``.encode('utf-8')`` — the encode this class of bug "
        "lands on. Registry entries come from the same host configs the "
        "CLI reads, so this is a reachable path and is unaudited — see #761.",
    ),
    "server.py": (
        1,
        "MCP tool response text, serializing values parsed out of Core's "
        "nested candidate JSON. Same false 'the SDK owns it' reasoning as "
        "``compression.py`` and the same real exposure — deferred to #761.",
    ),
    "cli/hook_cmd.py": (
        1,
        "Renders a tool response into hook text; the hook's own emit fails "
        "open on an unencodable reply (#758).",
    ),
    "cli/proxy.py": (
        4,
        "All four are the ``claude mcp`` surfaces rather than documents: the "
        "add-json argv and the two manual-hint renderings, plus the probe in "
        "``_argv_is_encodable`` itself. The argv is checked before either "
        "verb spawns and both hint branches go through ``_shell_join`` or "
        "``_disp`` (#754/#756/#758).",
    ),
}


def _stdlib_dumps_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names in *tree* that resolve to the stdlib ``json`` module and to
    ``json.dumps`` directly.

    Both spellings are already in this repository — ``import json`` and
    ``import json as _json`` — so matching the literal ``json.dumps`` would
    let a site in through the alias that is right there in two modules.
    """
    modules = set()
    directs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "json":
                    modules.add(alias.asname or "json")
        elif isinstance(node, ast.ImportFrom) and node.module == "json":
            for alias in node.names:
                if alias.name == "dumps":
                    directs.add(alias.asname or "dumps")
    return modules, directs


def _dumps_lines_in(tree: ast.Module) -> list[int]:
    """Sorted line numbers of stdlib ``dumps`` calls passing
    ``ensure_ascii=False``, under any binding the module established."""
    modules, directs = _stdlib_dumps_names(tree)
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        is_stdlib = (
            isinstance(fn, ast.Attribute)
            and fn.attr == "dumps"
            and isinstance(fn.value, ast.Name)
            and fn.value.id in modules
        ) or (isinstance(fn, ast.Name) and fn.id in directs)
        if not is_stdlib:
            continue
        if any(
            kw.arg == "ensure_ascii"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is False
            for kw in node.keywords
        ):
            lines.append(node.lineno)
    return sorted(lines)


def _raw_dumps_sites() -> dict[str, list[int]]:
    """``{relative path: [line, ...]}`` for stdlib ``json.dumps`` calls that
    pass ``ensure_ascii=False``.

    Matches on the resolved call, not on text: a search for the keyword also
    hits every ``json_out.dumps(..., ensure_ascii=False)``, which is the
    routed form and precisely what this test must not count.
    """
    found: dict[str, list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        lines = _dumps_lines_in(ast.parse(path.read_text(encoding="utf-8")))
        if lines:
            # ``as_posix``, not ``str``: on Windows the latter renders
            # ``proxy\cache.py`` and matches nothing in the allowlist, so
            # every site would read as unclassified there.
            found[path.relative_to(SRC).as_posix()] = lines
    return found


def test_the_matcher_resolves_aliases_and_ignores_the_routed_writer() -> None:
    """The guard is only as good as what it can see.

    Asserts the *detected lines*, not the import bindings: checking the
    bindings alone would still pass with the call-matching half deleted, and
    there is no production ``from json import dumps`` site to catch that.

    Pins the three spellings that matter — the alias this repository already
    uses, a direct ``from json import dumps``, and the routed
    ``json_out.dumps``, which must never be counted (miscounting it would
    make every routed site read as a violation).
    """
    src = (
        "import json as _json\n"  # 1
        "from json import dumps as _d\n"  # 2
        "from memtomem_stm.utils import json_out\n"  # 3
        "a = _json.dumps({}, ensure_ascii=False)\n"  # 4  <- counted
        "b = _d({}, ensure_ascii=False)\n"  # 5  <- counted
        "c = json_out.dumps({}, ensure_ascii=False)\n"  # 6  <- routed, ignored
        "d = _json.dumps({})\n"  # 7  <- default ensure_ascii, ignored
    )
    assert _dumps_lines_in(ast.parse(src)) == [4, 5]


def test_every_unrouted_emitter_is_accounted_for() -> None:
    """A new ``json.dumps(..., ensure_ascii=False)`` must be classified.

    Failing here is not "you did something wrong" — it is a prompt to decide
    what happens to that string. If anything encodes it to bytes (a file, a
    socket, a SQLite column, a subprocess argument), route it through
    ``utils.json_out``; if it is only measured, hashed, or handed to a
    library that owns the encoding, add it below with that reason.
    """
    found = _raw_dumps_sites()

    unexpected = sorted(set(found) - set(ALLOWLIST))
    named = ", ".join(f"{f}:{found[f]}" for f in unexpected)
    assert not unexpected, f"unclassified raw json.dumps(ensure_ascii=False) site(s): {named}"

    stale = sorted(set(ALLOWLIST) - set(found))
    assert not stale, f"allowlist names file(s) with no such site left: {stale}"

    drifted = {
        f: (len(found[f]), ALLOWLIST[f][0]) for f in found if len(found[f]) != ALLOWLIST[f][0]
    }
    assert not drifted, f"site count changed (actual, allowed): {drifted}"


@pytest.mark.parametrize("path", sorted(ALLOWLIST))
def test_every_allowlist_entry_states_a_reason(path: str) -> None:
    """The allowlist is the record, so an entry without a reason is worthless
    — it would let the next site in on the strength of a number alone."""
    _, reason = ALLOWLIST[path]
    assert len(reason) > 40, path
