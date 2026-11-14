"""Injective digest over a tuple of string components.

Both SQLite caches in ``proxy/`` key their rows on a SHA-256 of several
components joined together, and both got the join wrong the same way: a bare
separator is ambiguous the moment a component can contain it, so two distinct
component tuples hashed to one key and one call's cached row answered the
other's (#784 for the response cache, #794 for the tool-graph consult cache).

Shared rather than written twice on purpose. The usual bar in this repository
is four sites before extracting a helper, and this is two — but the duplication
here is what produced the second bug, and the property being preserved
(injectivity) is not one a reader can check by eye at the call site. A third
cache should get this for free instead of re-deriving it.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Protocol

__all__ = ["framed_digest", "keyed_framed_digest"]


def framed_digest(components: tuple[str, ...]) -> str:
    """Hex SHA-256 over *components*, injective over the tuple.

    Each component is length-prefixed netstring-style (``len:data``) and folded
    into the digest separately, so the concatenation parses left-to-right
    unambiguously and no component boundary can shift into another. A joined
    string cannot promise that: with a bare ``\\x00`` separator,
    ``("a\\x00b", "c")`` and ``("a", "b\\x00c")`` produce the same bytes.

    The length prefix counts **bytes, not characters** — it is taken after the
    encode below, which is the only version of this that works once a component
    is non-ASCII.

    Encoded with ``surrogatepass`` so a lone surrogate cannot raise here.
    Nothing decodes these bytes — they exist only to be hashed — so the usual
    objection to ``surrogatepass`` (it emits byte sequences that are not valid
    UTF-8) has no consumer to bite. Note this makes the digest injective over
    the *strings it is handed*: a caller that renders structured data down to a
    string first (``json.dumps`` and friends) inherits that serialization's
    equivalence classes and must decide for itself whether they are the ones it
    wants.
    """
    digest = hashlib.sha256()
    _feed(digest, components)
    return digest.hexdigest()


def keyed_framed_digest(components: tuple[str, ...], *, key: bytes, domain: str) -> str:
    """Hex HMAC-SHA256 over *components*, framed exactly as :func:`framed_digest`.

    For identifiers derived from input that can hold user or credential text:
    without *key*, nobody can recompute the value to confirm a guessed input.

    *domain* names the use. The HMAC runs under a subkey derived from *key* and
    *domain*, so one key shared between two uses, or a file moved next to a
    different store, cannot make a value from one use match the other's.
    """
    subkey = hmac.new(key, domain.encode("utf-8"), hashlib.sha256).digest()
    mac = hmac.new(subkey, digestmod=hashlib.sha256)
    _feed(mac, components)
    return mac.hexdigest()


class _Updatable(Protocol):
    def update(self, data: bytes, /) -> None: ...


def _feed(digest: _Updatable, components: tuple[str, ...]) -> None:
    for component in components:
        data = component.encode("utf-8", errors="surrogatepass")
        digest.update(f"{len(data)}:".encode())
        digest.update(data)
