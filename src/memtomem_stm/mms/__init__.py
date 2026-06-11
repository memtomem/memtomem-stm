"""mms — project-scoped MCP management.

A layer above the STM proxy that decides *which* MCP servers a given
project sees.

W1 ships project state CRUD (``mms project ...``) plus host import
(``mms import``). The two systems — STM proxy bootstrap (``~/.memtomem/``)
and mms project state (``~/.mms/``) — are intentionally disjoint in W1;
``mms add`` writes only ``stm_proxy.json`` and ``mms import --apply``
writes only ``registry.toml``. Unification is a W2+ trigger-driven
follow-up.
"""
