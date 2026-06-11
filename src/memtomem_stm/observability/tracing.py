"""Langfuse tracing utilities for STM.

If langfuse is not installed or disabled, all functions gracefully return None/nullcontext.
"""

from __future__ import annotations

import logging
import os
import random
import threading
from contextlib import nullcontext
from typing import Any

logger = logging.getLogger(__name__)

_langfuse_client: Any = None
_sampling_rate: float = 1.0
_SERVICE_NAME = "memtomem-stm"

# One-shot escalation for SDK failures: traced() sits on the proxy/surfacing
# hot paths, so a persistently broken exporter must not emit a stack trace
# per call — the first failure warns, repeats go to DEBUG.
_warned_observation_failure = False
_warned_observation_failure_lock = threading.Lock()


def _log_observation_failure(stage: str) -> None:
    global _warned_observation_failure
    with _warned_observation_failure_lock:
        first = not _warned_observation_failure
        _warned_observation_failure = True
    if not first:
        logger.debug("Langfuse observation %s failed (repeat, ignored)", stage, exc_info=True)
        return
    logger.warning(
        "Langfuse observation %s failed — proceeding untraced (repeats logged at DEBUG)",
        stage,
        exc_info=True,
    )


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
    if public_key := getattr(config, "public_key", ""):
        kwargs["public_key"] = public_key
    if secret_key := getattr(config, "secret_key", ""):
        kwargs["secret_key"] = secret_key
    if host := getattr(config, "host", ""):
        kwargs["host"] = host

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


class _SafeObservation:
    """Delegate to a Langfuse observation context manager, degrading to a
    no-op when the SDK raises.

    ``traced()`` wraps the proxy and surfacing hot paths, whose contract is
    that tracing must never break the proxied call. The SDK's context
    manager typically does its real work in ``__enter__`` (OTEL context
    attach, exporter I/O), so guarding only the constructor would still let
    a misconfigured exporter fail the call at ``with`` time. Exceptions
    raised by the traced *body* still propagate normally — only the SDK's
    own enter/exit failures are swallowed.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._entered = False

    def __enter__(self) -> Any:
        try:
            result = self._inner.__enter__()
            self._entered = True
            return result
        except Exception:
            _log_observation_failure("start")
            return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if not self._entered:
            return False
        try:
            return bool(self._inner.__exit__(exc_type, exc, tb))
        except Exception:
            _log_observation_failure("exit")
            return False


def traced(name: str, **kwargs: Any) -> Any:
    """Return a Langfuse observation context manager.

    Returns nullcontext if Langfuse is unavailable or this call was
    sampled out (``sampling_rate < 1.0``).  Nested calls within an
    existing ``traced()`` block automatically create parent-child
    relationships via OpenTelemetry context propagation (Langfuse
    ≥ 4.x). A raising SDK degrades to a no-op (``_SafeObservation``) —
    per the module contract, tracing failures must never break the
    traced call.
    """
    client = _langfuse_client
    if client is None:
        return nullcontext()
    if _sampling_rate < 1.0 and random.random() >= _sampling_rate:  # noqa: S311
        return nullcontext()
    try:
        return _SafeObservation(client.start_as_current_observation(name=name, **kwargs))
    except Exception:
        _log_observation_failure("creation")
        return nullcontext()
