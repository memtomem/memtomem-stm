"""Langfuse tracing utilities for STM.

If langfuse is not installed or disabled, all functions gracefully return None/nullcontext.
"""

from __future__ import annotations

import os
import random
from contextlib import nullcontext
from typing import Any

_langfuse_client: Any = None
_sampling_rate: float = 1.0
_SERVICE_NAME = "memtomem-stm"


def init_langfuse(config: object, *, service_name: str = _SERVICE_NAME) -> Any:
    """Initialize Langfuse client if enabled and installed. Returns client or None."""
    global _langfuse_client, _sampling_rate

    if not getattr(config, "enabled", False):
        return None

    try:
        from langfuse import Langfuse
    except ImportError:
        return None

    if not os.environ.get("OTEL_SERVICE_NAME"):
        os.environ["OTEL_SERVICE_NAME"] = service_name

    _sampling_rate = getattr(config, "sampling_rate", 1.0)

    kwargs: dict[str, Any] = {}
    if getattr(config, "public_key", ""):
        kwargs["public_key"] = config.public_key  # type: ignore[union-attr]
    if getattr(config, "secret_key", ""):
        kwargs["secret_key"] = config.secret_key  # type: ignore[union-attr]
    if getattr(config, "host", ""):
        kwargs["host"] = config.host  # type: ignore[union-attr]

    _langfuse_client = Langfuse(**kwargs)
    return _langfuse_client


def get_langfuse() -> Any:
    """Return the current Langfuse client, or None."""
    return _langfuse_client


def shutdown_langfuse(client: Any = None) -> None:
    """Flush and shutdown the Langfuse client."""
    c = client or _langfuse_client
    if c is not None and hasattr(c, "shutdown"):
        c.shutdown()


def traced(name: str, **kwargs: Any) -> Any:
    """Return a Langfuse observation context manager.

    Returns nullcontext if Langfuse is unavailable or this call was
    sampled out (``sampling_rate < 1.0``).  Nested calls within an
    existing ``traced()`` block automatically create parent-child
    relationships via OpenTelemetry context propagation (Langfuse
    ≥ 4.x).
    """
    client = _langfuse_client
    if client is None:
        return nullcontext()
    if _sampling_rate < 1.0 and random.random() >= _sampling_rate:  # noqa: S311
        return nullcontext()
    return client.start_as_current_observation(name=name, **kwargs)
