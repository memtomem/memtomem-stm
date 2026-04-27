"""Budget arithmetic for proxied tool names against the 64-char MCP limit.

When an MCP client renders a proxied tool to an LLM API, it composes a
fully-qualified name from the client-side server name and the upstream
tool we registered. The composition shape varies slightly per client:

* Claude Code:     ``mcp__<client-server>__<prefix>__<tool>``  (3 × ``__``)
* Antigravity:     ``mcp_<client-server>_<prefix>__<tool>``    (2 × ``_`` + 1 × ``__``)

Anthropic's tool name regex requires ``^[a-zA-Z0-9_-]{1,64}$``; clients
silently drop tools whose composed name overflows. Issue #261 tracks the
user-facing failure mode (one missing tool buried in a list of working
ones, no upfront error).

We always assume the **stricter Claude Code template** so any false
positives we report are conservative — a more lenient client format would
never reject a tool we cleared.

The composed-name byte budget breaks down as::

    mcp__<server>__<prefix>__<tool>
    └┬─┘└──┬───┘  └──┬───┘  └──┬─┘
     fixed  server   prefix    tool
     overhead

    fixed = "mcp" + 3 × "__"  =  9 bytes  (one pair before each segment)
    overhead = 9 + len(server)
    composed = overhead + len(prefix) + len(tool)

So for the package-default client server name ``memtomem-stm`` (12 chars),
``overhead = 21`` and ``len(prefix) + len(tool)`` must stay under
``64 - 21 = 43``. Registering STM as ``mms`` instead saves 9 bytes and
loosens the budget to 52.
"""

from __future__ import annotations

import os

# Anthropic / MCP spec — tool names must match ``^[a-zA-Z0-9_-]{1,64}$``.
TOOL_NAME_LIMIT = 64

# Most users follow the package's docs and register STM in their client
# config under the package name. ``mms`` (3 chars) is the recommended
# short alternative — its 9-byte savings is exactly what gets a
# ``query_docs_filesystem_docs_by_lang_chain``-class upstream tool to fit.
_DEFAULT_CLIENT_SERVER_NAME = "memtomem-stm"

# Set MMS_CLIENT_SERVER_NAME if the user actually registered STM under a
# different name in their MCP client config — STM has no other way to
# learn what name the client picked.
_OVERRIDE_ENV_VAR = "MMS_CLIENT_SERVER_NAME"

# Number of fixed bytes per Claude Code template: literal ``mcp`` (3) +
# three ``__`` separators (one before server, one before prefix, one
# before tool). Anything else is server / prefix / tool length.
_FIXED_TEMPLATE_BYTES = 9


def client_server_name() -> str:
    """Effective client-side MCP server name used for budget arithmetic.

    Treats an empty env var the same as unset — exporting
    ``MMS_CLIENT_SERVER_NAME=""`` would otherwise drop the overhead to 9
    bytes, which never matches a real client config and silently
    permits overflows the user expected to be rejected.
    """
    return os.environ.get(_OVERRIDE_ENV_VAR) or _DEFAULT_CLIENT_SERVER_NAME


def overhead() -> int:
    """Bytes consumed by everything except the prefix and tool name."""
    return _FIXED_TEMPLATE_BYTES + len(client_server_name())


def composed_length(prefix: str, tool: str) -> int:
    """Length of the tool name as the strictest client (Claude Code) renders it."""
    return overhead() + len(prefix) + len(tool)


def overflows(prefix: str, tool: str) -> bool:
    return composed_length(prefix, tool) > TOOL_NAME_LIMIT


def prefix_hard_limit() -> int:
    """Maximum prefix length such that *some* upstream tool name still
    fits. Anything longer guarantees overflow even for a 1-char tool."""
    return TOOL_NAME_LIMIT - overhead() - 1  # leave 1 char for the tool


# Empirically tuned: budget kept aside for the upstream tool name when
# deciding whether a prefix deserves a warning. Median observed tool
# name across public MCP servers is 15-25 chars, so 22 leaves room for
# everything but the longest outliers. Once a prefix eats into this
# budget, moderately-named tools start to overflow.
_WARN_TOOL_NAME_BUDGET = 22


def prefix_warn_threshold() -> int:
    """Soft threshold — above this, prefix + a median-length (~22 char)
    tool name starts to overflow under the strictest client format.

    Derived from the current ``client_server_name()`` so the threshold
    follows the same dynamic basis as ``prefix_hard_limit()`` — keeping
    the helper consistent across the env-var override path. For the
    default 12-char ``memtomem-stm`` server (overhead 21), this is 21;
    for ``mms`` (overhead 12), it relaxes to 30.
    """
    return TOOL_NAME_LIMIT - overhead() - _WARN_TOOL_NAME_BUDGET
