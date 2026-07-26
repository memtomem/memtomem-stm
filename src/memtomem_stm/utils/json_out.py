"""UTF-8-safe JSON serialization and lone-surrogate scrubbing.

Lives in its own leaf module (imports nothing from the package) because
every JSON emitter needs it: ``proxy``, ``mms_host``, ``mms_project``,
``config_cmd`` and ``_write_lock`` — and ``_write_lock`` is imported *by*
``proxy``, so the helper cannot live there without a cycle. The daemon and
the proxy pipeline import it for the same reason.

The boundary policy has three distinct shapes (#761, #783):

- :func:`dumps` for an emitter — a value we are *serializing now*, where the
  escape belongs in the emitted text and a consumer decodes it back.
- :func:`escape_lone_surrogates` / :func:`scrub_lone_surrogates` for an
  *ingest* point — content arriving from an upstream server, the daemon wire
  or Core, where the surrogate is escaped once on the way in so that every
  later encode (a ``TextContent`` serialization, a SQLite parameter, a
  fingerprint) is total without each of them having to know about this.
- :func:`require_utf8_identifier` for an exact-match identifier — refuse the
  value instead of non-injectively rewriting its identity. Digest-only inputs
  use ``errors="surrogatepass"`` at their call site so hashing remains total
  and injective without emitting invalid UTF-8.
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
    callers pass** — ``indent``, ``sort_keys``, and ``separators`` holding only
    ASCII. Under those, every structural character ``dumps`` emits is ASCII, so
    a raw surrogate can only appear inside a string literal; and ``dumps``
    escapes ``\\`` as ``\\\\``, so the substituted character is always preceded
    by an *even* number of backslashes and the inserted ``\\u`` therefore opens
    a fresh escape rather than continuing one. The result is valid JSON, and
    ``json.loads(dumps(x)) == x`` for every *lone* surrogate — see the pair
    caveat two paragraphs down for the one input where it does not hold.

    Both halves depend on that argument contract, and the ASCII qualifier on
    ``separators`` is the whole of it: a caller passing a surrogate *inside*
    ``separators`` would have it escaped **structurally**, yielding text that no
    longer parses. Compact separators (``(",", ":")``, which the daemon frame,
    the fingerprints and the cache envelope all pass) are ASCII and so change
    nothing about the argument above. A ``default=`` returning a surrogate is
    fine, since its result is escaped as data like any other. Do not widen the
    contract further without revisiting this.

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


def escape_lone_surrogates(text: str) -> str:
    """``text`` with every lone surrogate replaced by its ``\\udxxx`` literal.

    The ingest-side counterpart to :func:`dumps`. Where ``dumps`` escapes into
    text a consumer will decode back, this escapes into the *value itself*: the
    six ASCII characters ``\\ud800`` replace one unencodable code unit, so
    everything downstream — a ``json.dumps``, a ``TextContent`` serialization, a
    SQLite text parameter, a ``.encode()`` for a digest — is total without
    knowing this happened. Crucially the result is inert under a later
    dumps/loads round trip: ``dumps`` renders the backslash as ``\\\\`` and
    ``loads`` decodes it back to the same six characters, so the surrogate
    cannot re-materialize downstream the way it does when only the emitter
    escapes (#761).

    Returns the input object UNCHANGED (same identity) when there is nothing to
    escape, so the overwhelmingly common case allocates nothing.

    Lowercase to match :func:`dumps` and ``json.dumps``'s own
    ``ensure_ascii=True`` style. Like the display escaping in ``cli/_display``
    this is non-injective — text that already held the six literal characters
    ``\\ud800`` is indistinguishable from text that held the code unit — which
    is the accepted trade for a value that otherwise cannot be delivered at all.
    """
    if _LONE_SURROGATE.search(text) is None:
        return text
    return _LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def scrub_lone_surrogates(value: Any) -> Any:
    """:func:`escape_lone_surrogates` applied to every string in a parsed tree.

    For ingest points that receive a decoded structure rather than one string —
    a daemon frame, an upstream JSON payload, Core's nested candidate object.
    Dict *keys* are scrubbed too: a key encodes exactly like a value does.

    Returns the input object UNCHANGED (same identity) when it holds no lone
    surrogate, mirroring ``compression._sanitize_nonfinite``'s no-copy fast path
    so this can be folded into the same walk without costing an allocation on
    the clean path.

    Two consequences of scrubbing a key, both confined to the already-broken
    input and both acceptable for a JSON object, whose member order carries no
    meaning: a rewritten key moves to the end of its object, and if it collides
    with a key that already held those literal characters the two merge, last
    one winning. Values are rewritten in place, so an object whose keys are all
    clean keeps its order exactly.
    """
    if isinstance(value, str):
        return escape_lone_surrogates(value)
    if isinstance(value, dict):
        replaced: dict[Any, Any] | None = None
        for key, item in value.items():
            new_key = escape_lone_surrogates(key) if isinstance(key, str) else key
            new_item = scrub_lone_surrogates(item)
            if new_key is not key or new_item is not item:
                if replaced is None:
                    replaced = dict(value)
                if new_key is not key:
                    del replaced[key]
                replaced[new_key] = new_item
        return replaced if replaced is not None else value
    if isinstance(value, list):
        replaced_seq: list[Any] | None = None
        for index, item in enumerate(value):
            new_item = scrub_lone_surrogates(item)
            if new_item is not item:
                if replaced_seq is None:
                    replaced_seq = list(value)
                replaced_seq[index] = new_item
        return replaced_seq if replaced_seq is not None else value
    return value


IDENTITY_FIELD_NAMES: frozenset[str] = frozenset({"id", "chunk_id", "block_id"})
"""Keys under which Core nests a value that can become a chunk identity.

Scoped to the names that actually reach ``chunk.id``, not every id-shaped key.
``omitted_block_ids`` is joined into a rendered hint and Core's adjacent-context
``id`` only crosses the daemon wire — neither is ever exact-matched, so both are
content by destination and keep the ordinary escape. Matching is by key NAME at
any depth, so an exempted name in a *new* nesting level arrives raw and its
reader must decide refuse-or-escape explicitly; that is the safe direction to
fail, but it is why the exemption list stays this short.
"""


def scrub_content_preserving_identity(
    value: Any, identity_keys: frozenset[str] = IDENTITY_FIELD_NAMES
) -> Any:
    """:func:`scrub_lone_surrogates`, except identity values are left RAW.

    Escaping is non-injective, so applying it to an identifier destroys the
    thing an identifier is for: a raw ``\\ud800`` code unit and the six literal
    characters ``\\ud800`` — two identities Core can legitimately mint
    separately — both come out as the latter, and every later exact match
    (feedback demotion, cross-session dedup, cache invalidation, the
    ``increment_access`` boost) then acts on whichever arrived last (#783).

    Content still gets escaped once at ingest exactly as before, so the
    ``TextContent`` / SQLite / digest totality that #761 bought is unchanged.
    Identity values instead arrive *unmodified*, which is what lets the
    refusal boundaries downstream see the truth and decline. Callers that
    assign an identity MUST therefore check it with :func:`has_lone_surrogate`
    — the value they get here can be unencodable, which is the point.

    Keys are scrubbed like ``scrub_lone_surrogates`` does; an unencodable
    *key* is a malformed document, not an identity Core meant to mint.
    """
    if isinstance(value, str):
        return escape_lone_surrogates(value)
    if isinstance(value, dict):
        replaced: dict[Any, Any] | None = None
        for key, item in value.items():
            new_key = escape_lone_surrogates(key) if isinstance(key, str) else key
            if isinstance(key, str) and key in identity_keys:
                new_item = item
            else:
                new_item = scrub_content_preserving_identity(item, identity_keys)
            if new_key is not key or new_item is not item:
                if replaced is None:
                    replaced = dict(value)
                if new_key is not key:
                    del replaced[key]
                replaced[new_key] = new_item
        return replaced if replaced is not None else value
    if isinstance(value, list):
        replaced_seq2: list[Any] | None = None
        for index, item in enumerate(value):
            new_item = scrub_content_preserving_identity(item, identity_keys)
            if new_item is not item:
                if replaced_seq2 is None:
                    replaced_seq2 = list(value)
                replaced_seq2[index] = new_item
        return replaced_seq2 if replaced_seq2 is not None else value
    return value


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


def require_utf8_identifier(value: str | None, field: str) -> None:
    """Raise a sanitized ``ValueError`` for an unencodable identifier.

    Unlike content, identifiers participate in equality, cache keys, and
    relational joins. Escaping one side would change that identity and can
    make an exact-match safety decision disappear. ``field`` is a trusted
    ASCII schema label; the offending value is deliberately never exposed.
    """
    if value is not None and has_lone_surrogate(value):
        raise ValueError(f"{field} must be a valid UTF-8 identifier")
