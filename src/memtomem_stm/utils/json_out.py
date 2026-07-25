"""UTF-8-safe JSON serialization for the CLI's ``--json`` legs and config
writers.

Lives in its own leaf module (imports nothing from the package) because
every JSON emitter needs it: ``proxy``, ``mms_host``, ``mms_project``,
``config_cmd`` and ``_write_lock`` — and ``_write_lock`` is imported *by*
``proxy``, so the helper cannot live there without a cycle.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Every character in this range is, in a Python ``str``, a *lone* surrogate:
# astral characters are single code points above U+FFFF, so a well-formed
# string never contains one. Two ways they reach us (#757). From a config
# file, where ``"\ud800"`` is a legal JSON escape that ``json.loads`` decodes
# without complaint and no character validation follows. And, on POSIX, from
# argv: a command-line argument carrying a byte that is not valid UTF-8 is
# decoded with ``surrogateescape``, which yields one in U+DC80–U+DCFF — so
# ``mms add`` alone could produce a name it then could not write.
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


def dumps(payload: Any, **kwargs: Any) -> str:
    """``json.dumps`` whose result is always encodable to UTF-8 (#757).

    ``ensure_ascii=False`` — which every caller passes, so CJK and emoji
    stay readable rather than escaping into ``\\uXXXX`` soup (the #750/#754
    line) — leaves a lone surrogate *raw* in the returned ``str``. ``dumps``
    itself does not raise; the ``UnicodeEncodeError`` lands later, when
    ``click.echo`` or ``atomic_write_text`` encodes that string. On a
    mutating command that is the worst possible timing: the config write has
    already happened, so the caller exits non-zero having emitted no JSON for
    an operation that in fact succeeded.

    So re-escape those code units after the fact. This is safe as a blanket
    substitution over the dumped text **for the keyword arguments this module's
    callers pass** — ``indent``, ``sort_keys``, and nothing else. Under those,
    every structural character ``dumps`` emits is ASCII, so a raw surrogate can
    only appear inside a string literal; and ``dumps`` escapes ``\\`` as
    ``\\\\``, so the substituted character is always preceded by an *even*
    number of backslashes and the inserted ``\\u`` therefore opens a fresh
    escape rather than continuing one. The result is valid JSON, and
    ``json.loads(dumps(x)) == x`` for every *lone* surrogate — see the pair
    caveat two paragraphs down for the one input where it does not hold.

    Both halves depend on that argument contract. A caller passing a surrogate
    inside ``separators`` would have it escaped *structurally*, yielding text
    that no longer parses; a ``default=`` returning one is fine, since it is
    escaped as data like any other. Do not widen the contract without
    revisiting this.

    One inherited caveat on the round-trip: a string holding an *adjacent*
    high-then-low pair decodes back as the single astral character they encode,
    so it is not returned unchanged. That is Python's JSON decoder, not this
    function — plain ``json.dumps`` with ``ensure_ascii=True`` produces exactly
    the same text and the same asymmetry. Only *lone* surrogates, the ones this
    exists for, survive identically.

    Lowercase ``\\udxxx`` deliberately matches ``json.dumps``'s own
    ``ensure_ascii=True`` escape style, so the output is byte-identical to what
    ``dumps`` would have produced for that character on its own. Note what this
    is *not*: an escape for humans reading a terminal. What comes out here is a
    value a consumer decodes back, so the JSON convention is the right one.

    Encoding with ``errors="surrogatepass"`` would be the other way to make
    the write succeed, but it emits bytes that are not valid UTF-8, moving
    the failure to whichever consumer decodes them. Escaping keeps the
    document decodable by anything that reads JSON.

    Clean payloads are unaffected: with no surrogate present the substitution
    matches nothing and the dumped text is returned unchanged.
    """
    return _LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", json.dumps(payload, **kwargs))


def unencodable_field(entry: object, path: str = "") -> str | None:
    """Path of the first string in *entry* that cannot encode, or ``None``.

    The server *name* is not the only field that has to survive being used:
    a ``command`` is spawned, a ``url`` is dialled, ``env`` values are handed
    to a child process — all of which encode. Making the config writable
    (#757) is what let those reach disk at all, so the create paths gate the
    whole entry rather than the name alone.

    Returns a *path* (``env['TOKEN']``, ``args[1]``) and never the offending
    value: env and header values are routinely secrets, and this string goes
    into stderr and into ``--json`` payloads that get piped to CI logs.
    """
    if isinstance(entry, str):
        return path or "value" if has_lone_surrogate(entry) else None
    if isinstance(entry, dict):
        for key, value in entry.items():
            if isinstance(key, str) and has_lone_surrogate(key):
                return f"{path}[key]" if path else "key"
            found = unencodable_field(value, f"{path}[{key!r}]" if path else str(key))
            if found:
                return found
        return None
    if isinstance(entry, (list, tuple)):
        for idx, value in enumerate(entry):
            found = unencodable_field(value, f"{path}[{idx}]")
            if found:
                return found
    return None


def has_lone_surrogate(value: str) -> bool:
    """True when ``value`` holds a code unit that cannot be encoded to UTF-8.

    Serializing such a string is now safe, but *storing* one as an upstream
    server name is not: the name is the cache key's first component
    (``proxy/cache.py``) and part of the Toolgraph contract fingerprint, both
    of which hash ``.encode()``d bytes and raise on it. TOML cannot represent
    it at all, so `mms import`'s registry could never hold it either. Such a
    name is unusable end to end, and a config that merely *writes* is a worse
    outcome than a refusal — so the commands that create entries call this and
    decline, while the commands that inspect and delete them do not, which is
    what lets an already-broken config be repaired: ``mms remove`` clears such
    an entry in either output mode, this escaping covering its ``--json``
    report and #756's ``_disp`` covering its printed line. The text-mode
    renderings of ``list`` / ``health`` / ``doctor`` print the name escaped
    rather than raising as of #759, which closed the prose sites #756
    deferred; a test pins all three.
    """
    return _LONE_SURROGATE.search(value) is not None
