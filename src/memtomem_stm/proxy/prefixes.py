"""Shared upstream-prefix validation rules.

Single source of truth for what makes an upstream prefix acceptable and
for detecting collisions across a set of upstreams. Both the runtime
config model (``ProxyConfig`` validators in ``proxy/config.py``) and the
CLI save paths (``mms add`` / ``mms init`` / ``add --from-clients`` in
``cli/proxy.py``) call these helpers, so the CLI can never write a
config file the runtime would refuse to load.

Deliberately dependency-light — stdlib only, no pydantic/click — because
the CLI keeps pydantic imports lazy for startup latency and this module
sits on both sides of that boundary.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# Prefixes become the ``<prefix>__<tool>`` namespace segment of proxied
# tool names, so they must be valid tool-name characters and must not
# contain the ``__`` separator itself (it would break prefix/tool
# splitting).
PREFIX_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


def prefix_format_error(prefix: str) -> str | None:
    """Format-rule violation message for ``prefix``, or ``None`` if valid."""
    if not PREFIX_RE.match(prefix) or "__" in prefix:
        return (
            f"invalid prefix '{prefix}'. "
            "Must start with a letter, contain only letters/digits/underscores, "
            "and must not contain '__'."
        )
    return None


def empty_prefix_keys(prefixes: Mapping[str, str]) -> list[str]:
    """Sorted server keys whose prefix is empty or whitespace-only."""
    return sorted(server_key for server_key, prefix in prefixes.items() if not prefix.strip())


def prefix_collisions(prefixes: Mapping[str, str]) -> dict[str, list[str]]:
    """``{prefix: sorted(server_keys)}`` for prefixes used by more than one upstream."""
    by_prefix: dict[str, list[str]] = {}
    for server_key, prefix in prefixes.items():
        by_prefix.setdefault(prefix, []).append(server_key)
    return {prefix: sorted(keys) for prefix, keys in by_prefix.items() if len(keys) > 1}


def format_collision_error(collisions: Mapping[str, list[str]]) -> str:
    """One-line collision report shared verbatim by runtime and CLI errors."""
    details = "; ".join(
        f"prefix '{prefix}' used by upstreams: {keys}"
        for prefix, keys in sorted(collisions.items())
    )
    return f"Duplicate upstream prefixes detected: {details}"
