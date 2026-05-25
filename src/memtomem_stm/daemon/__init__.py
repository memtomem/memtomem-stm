"""Local short-term-memory surfacing daemon (``mms daemon``) — Stage 2.

A long-lived loopback server that keeps one warm :class:`SurfacingEngine` and
one warm LTM MCP connection so ``mms hook`` no longer pays the ~6s cold start
(embedding + reranker model load) on every built-in tool call. See
``docs`` / the project plan for the full design; the per-module docstrings here
are the source of truth for the wire protocol, discovery, and lifecycle
contracts.

Submodules:

- :mod:`~memtomem_stm.daemon.protocol` — newline-delimited JSON framing + token.
- :mod:`~memtomem_stm.daemon.discovery` — the ``stm-daemon.json`` handshake file.
- :mod:`~memtomem_stm.daemon.locking` — cross-platform single-owner lifetime lock.
- :mod:`~memtomem_stm.daemon.spawn` — lock-probed, fire-and-forget detached spawn.
- :mod:`~memtomem_stm.daemon.server` — the asyncio server loop + warm engine.
"""

from __future__ import annotations
