"""Tracing utilities for STM.

The mechanism, in the three terms the rest of this module uses:

- A **consumer factory** (``_langfuse_leg()`` today) is called once per
  ``traced()`` call and returns either a **leg** — a context manager for that
  one consumer — or ``None``, which means that consumer contributes nothing to
  this call. A factory decides on its own what makes it return ``None``.
- ``traced()`` calls every factory, discards the ``None``s, and returns
  ``nullcontext()`` when no factory produced a leg. Otherwise it returns a
  ``_FanOut`` over the legs it did get.
- A leg is entered and exited by ``_FanOut``; a leg that was built can still
  fail to start a span, which is the leg's own business, not ``traced()``'s.

The factory list is deliberately hard-wired here — no registry, no adapter
base class, no shared event format. Each consumer keeps its own construction,
sampling and failure policy. This is the per-boundary shape ADR 0001
prescribes, not the generic provider registry it defers. Langfuse is the only
consumer today; the fan-out exists so a second one can be added without
touching the call sites.

Every leg must be *self-safe*: entering or exiting it may never raise, because
``traced()`` wraps the proxy and surfacing hot paths and tracing must never
break the traced call. ``init_langfuse()`` likewise returns ``None`` rather
than raising when the SDK is missing or the config disables it.
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
# per call — the first failure warns, repeats go to DEBUG. The latch is
# per-consumer (this one is Langfuse's): a second consumer going bad must
# still get its own first WARNING rather than being silenced by this one.
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


class _FanOut:
    """Enter several tracing legs as one context manager.

    The legs are entered in order and exited in reverse. ``__enter__``
    yields the *first* leg's value, so ``with traced(...) as span`` keeps
    meaning "the primary consumer's span object". For the same reason
    ``__exit__`` reports the first leg's result: with a single leg this
    preserves the context-manager semantics of entering that leg directly,
    and a secondary consumer can never suppress an exception raised by the
    traced body.

    Legs are assumed self-safe (see the module docstring) — this class adds
    ordering, not error handling. Entering a leg is not proof that the leg
    started a span; a self-safe leg may degrade to a no-op internally.
    """

    def __init__(self, legs: list[Any]) -> None:
        self._legs = legs
        self._entered: list[Any] = []

    def __enter__(self) -> Any:
        primary: Any = None
        for index, leg in enumerate(self._legs):
            value = leg.__enter__()
            self._entered.append(leg)
            if index == 0:
                primary = value
        return primary

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        results = [leg.__exit__(exc_type, exc, tb) for leg in reversed(self._entered)]
        # results is in reverse-entry order, so the primary leg's result is last.
        return bool(results[-1]) if results else False


def _langfuse_leg(name: str, **kwargs: Any) -> Any:
    """The Langfuse consumer factory: return a leg, or None for this call.

    Returns ``None`` in exactly three cases — no client is available, the
    call was sampled out (``sampling_rate < 1.0``), or the SDK raised while
    constructing the observation. All three mean "no Langfuse leg", never an
    error.
    """
    client = _langfuse_client
    if client is None:
        return None
    if _sampling_rate < 1.0 and random.random() >= _sampling_rate:  # noqa: S311
        return None
    try:
        return _SafeObservation(client.start_as_current_observation(name=name, **kwargs))
    except Exception:
        _log_observation_failure("creation")
        return None


def traced(name: str, **kwargs: Any) -> Any:
    """Return a context manager over every leg the consumer factories built.

    Returns ``nullcontext()`` when no factory produced a leg, so no exporter
    work happens when tracing is off. Nested calls within an existing
    ``traced()`` block automatically create parent-child relationships via
    OpenTelemetry context propagation (Langfuse ≥ 4.x). A raising SDK
    degrades to a no-op — per the module contract, tracing failures must
    never break the traced call.
    """
    legs = [leg for leg in (_langfuse_leg(name, **kwargs),) if leg is not None]
    if not legs:
        return nullcontext()
    return _FanOut(legs)
