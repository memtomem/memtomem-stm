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
        "Model-facing text, not a document: these serialize an upstream "
        "response to measure or reshape it, and the result goes back to the "
        "MCP client through the SDK, which owns the wire encoding.",
    ),
    "proxy/selection_log.py": (
        2,
        "Hashed or appended to a JSONL log; the argument-fingerprint helper "
        "documents that its output is only ever hashed.",
    ),
    "proxy/selection_eval.py": (
        2,
        "The remaining two are a canonical form for hashing and a privacy "
        "scan over an in-memory string; ``to_json``, the one a --json leg "
        "echoes, is routed (#757 round 4).",
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
    "daemon/protocol.py": (1, "Daemon IPC frame, encoded by us — see #761."),
    "daemon/discovery.py": (1, "Daemon fingerprint, encoded by us — see #761."),
    "server.py": (
        1,
        "MCP tool response text; the SDK owns the wire encoding.",
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


def _raw_dumps_sites() -> dict[str, list[int]]:
    """``{relative path: [line, ...]}`` for stdlib ``json.dumps`` calls that
    pass ``ensure_ascii=False``.

    Matches on the call shape, not on text: a ``grep`` for the keyword also
    hits every ``json_out.dumps(..., ensure_ascii=False)``, which is the
    routed form and precisely what this test must not count.
    """
    found: dict[str, list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (
                isinstance(fn, ast.Attribute)
                and fn.attr == "dumps"
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "json"
            ):
                continue
            if any(
                kw.arg == "ensure_ascii"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is False
                for kw in node.keywords
            ):
                rel = str(path.relative_to(SRC))
                found.setdefault(rel, []).append(node.lineno)
    return found


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
