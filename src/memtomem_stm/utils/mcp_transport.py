"""Streamable-HTTP transport helper for the mcp 2.0 client SDK.

1.x's ``streamablehttp_client(url, headers=..., timeout=..., sse_read_timeout=...)``
built its own HTTP client. 2.0's ``streamable_http_client(url, *, http_client=...)``
takes one instead, and does **not** close a client the caller supplied — so
every call site has to own that lifetime. All three of ours (proxy upstreams,
the surfacing LTM client, the CLI probes) already run inside an
``AsyncExitStack`` on a single task, so this wrapper enters both the client
and the transport as one context manager and hands back the streams.

Timeout mapping from the old two-parameter shape:

* ``timeout=X`` (connect budget) and ``sse_read_timeout=Y`` (long-lived read)
  become ``httpx2.Timeout(X, read=Y)`` — ``Timeout(X)`` sets connect, write
  and pool to X, and ``read=`` overrides the read leg.
* Passing ``timeout=None`` keeps the SDK's own defaults (30s connect/write/pool,
  300s read), which is what a call site that passed no timeouts used to get.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2

from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

# The SDK's SSE-friendly read default. Named here because the proxy pins the
# read leg while overriding the connect leg: a long-lived stream must not
# inherit the connect budget.
SSE_READ_TIMEOUT_SECONDS = 300.0


@asynccontextmanager
async def streamable_http_transport(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
) -> AsyncIterator[Any]:
    """Open a streamable-HTTP transport, owning the HTTP client's lifetime.

    Yields the SDK's ``TransportStreams`` (a tuple subclass, so ``streams[0]``
    / ``streams[1]`` indexing keeps working).
    """
    async with create_mcp_http_client(headers=headers, timeout=timeout) as http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            yield streams
