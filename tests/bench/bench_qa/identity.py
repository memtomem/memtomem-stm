"""Fixture-derived compact IDs shared by replay, recall and sweep oracles."""

from collections.abc import Mapping
from typing import Any

from memtomem_stm.utils.digest import framed_digest


def compact_seed_id(seed: Mapping[str, Any]) -> str:
    """Expected ID after STM compact parsing, distinct from Core's real IDs."""
    return "stm:compact:v2:" + framed_digest(
        (seed.get("namespace", "default"), seed["source"], seed["content"])
    )
