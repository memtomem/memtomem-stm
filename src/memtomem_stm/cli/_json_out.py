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
    substitution over the dumped text because inside ``json.dumps`` output a
    raw surrogate can only appear inside a string literal (all structural
    characters are ASCII) and is never preceded by a backslash (``dumps``
    escapes ``\\`` as ``\\\\``). The result is therefore valid JSON that
    round-trips: ``json.loads(dumps(x)) == x``, surrogates included.

    Lowercase ``\\udxxx`` deliberately matches ``json.dumps``'s own
    ``ensure_ascii=True`` escape style, so the output is byte-identical to
    what ``dumps`` would have produced for that character on its own. That is
    the opposite convention from ``_disp`` in ``proxy``, whose uppercase
    ``\\uXXXX`` marks *terminal prose* — a rendering for humans, not a value
    a consumer decodes back.

    Encoding with ``errors="surrogatepass"`` would be the other way to make
    the write succeed, but it emits bytes that are not valid UTF-8, moving
    the failure to whichever consumer decodes them. Escaping keeps the
    document decodable by anything that reads JSON.

    Clean payloads are unaffected: with no surrogate present the substitution
    matches nothing and the dumped text is returned unchanged.
    """
    return _LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", json.dumps(payload, **kwargs))
