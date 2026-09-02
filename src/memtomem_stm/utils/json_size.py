"""Bounded UTF-8 JSON size accounting without materializing one giant string."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import re
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel


_STRING_CHUNK_CHARS = 64 * 1024
_JSON_ESCAPE_RE = re.compile(r'["\\\x00-\x1f]')
_SHORT_JSON_ESCAPES = frozenset({'"', "\\", "\b", "\f", "\n", "\r", "\t"})

# Budget for the on-loop first pass. Accounting is native code (``str.encode``
# plus one regex scan per 64 KiB chunk), so measuring this much costs less than
# the scheduling round trip a thread hop adds — and the overwhelming majority of
# inbound MCP messages (progress notifications, ``list_changed``, small tool
# results) are far below it.
_SYNC_MEASURE_BYTES = 64 * 1024

_EXECUTOR_THREAD_PREFIX = "stm-json-size"
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _size_executor() -> ThreadPoolExecutor:
    """The sizer's own worker, never the shared default executor.

    A message over the sync budget is measured off the event loop, and the one
    caller that matters is ``BoundedReadStream.receive`` — the dispatcher's only
    read loop. Sending that work to ``asyncio.to_thread``'s default pool put it
    behind every other ``to_thread`` user (the proxy's own ``compress()`` among
    them), so a saturated pool delayed prompt upstream responses until the call
    timeout (#956). A single worker suffices: the only work here is our own
    bounded accounting, and serializing two large measurements costs
    milliseconds. The idle thread is reclaimed at interpreter shutdown like any
    other executor's, so there is no lifecycle to plumb through callers.
    """
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=_EXECUTOR_THREAD_PREFIX
            )
        return _executor


class _LimitExceeded(Exception):
    pass


class _Sizer:
    def __init__(self, limit: int, *, approximate: bool = False) -> None:
        self.limit = limit
        self.total = 0
        # Routing mode: never materialize a model's dump (see
        # ``_approx_json_size``). Only ``value()``'s ``BaseModel`` branch reads
        # it; every byte rule below is shared, so the two modes agree except
        # where a custom serializer or a computed field applies.
        self.approximate = approximate

    def add(self, size: int) -> None:
        self.total += size
        if self.total > self.limit:
            raise _LimitExceeded

    def string(self, value: str) -> None:
        self.add(2)  # quotes
        for offset in range(0, len(value), _STRING_CHUNK_CHARS):
            chunk = value[offset : offset + _STRING_CHUNK_CHARS]
            try:
                # CPython's UTF-8 encoder and regex engine account for the
                # common path in native code. Keep the slice bounded so a
                # permitted multi-megabyte string does not require another
                # full-size encoded copy merely to measure it.
                chunk_size = len(chunk.encode("utf-8"))
            except UnicodeEncodeError:
                # A JSON string may contain a lone surrogate. Its JSON wire
                # form is the six-byte ``\\udxxx`` escape, while UTF-8 rejects
                # it. Limit the rare scalar fallback to one bounded chunk.
                self._string_chunk_with_surrogates(chunk)
                continue

            for match in _JSON_ESCAPE_RE.finditer(chunk):
                # Quotes, backslashes, and the five short control escapes grow
                # from one UTF-8 byte to two. Other C0 controls grow to the
                # six-byte ``\\u00xx`` form.
                chunk_size += 1 if match.group() in _SHORT_JSON_ESCAPES else 5
            self.add(chunk_size)

    def _string_chunk_with_surrogates(self, chunk: str) -> None:
        for char in chunk:
            code = ord(char)
            if char in _SHORT_JSON_ESCAPES:
                self.add(2)
            elif code < 0x20 or 0xD800 <= code <= 0xDFFF:
                self.add(6)
            elif code <= 0x7F:
                self.add(1)
            elif code <= 0x7FF:
                self.add(2)
            elif code <= 0xFFFF:
                self.add(3)
            else:
                self.add(4)

    def _model_fields(self, model: BaseModel) -> None:
        """Walk a model's fields without dumping it (routing mode only).

        ``model_dump`` materializes the whole subtree before a single byte is
        counted, which is precisely the work the caller is trying to keep off
        the event loop. Recursing over the raw attribute values keeps the walk
        bounded: a field the limit is already past is never reached.

        A model whose serialized form this walk cannot predict — a custom field
        or model serializer, a computed field, or extras carrying data no
        declared field names — is reported as over the limit instead. Routing
        may err either way in principle, but only one direction is harmless:
        overstating sends the payload to the worker, while understating would
        hand a large model back to the event loop to dump, which is the stall
        this whole path exists to avoid.
        """
        cls = type(model)
        if not _model_walk_is_faithful(cls) or getattr(model, "__pydantic_extra__", None):
            raise _LimitExceeded
        self.add(2)  # braces
        first = True
        for name, field in cls.model_fields.items():
            item = getattr(model, name, None)
            if item is None:
                continue  # mirrors exclude_none
            if not first:
                self.add(1)
            first = False
            self.string(field.serialization_alias or field.alias or name)
            self.add(1)
            self.value(item)

    def value(self, value: Any) -> None:
        if isinstance(value, BaseModel):
            if self.approximate:
                self._model_fields(value)
                return
            value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
        elif isinstance(value, Enum):
            value = value.value
        elif isinstance(value, Path):
            value = str(value)

        if value is None:
            self.add(4)
        elif value is True:
            self.add(4)
        elif value is False:
            self.add(5)
        elif isinstance(value, str):
            self.string(value)
        elif isinstance(value, int | float):
            self.add(len(json.dumps(value, allow_nan=True, separators=(",", ":"))))
        elif isinstance(value, Mapping):
            self.add(2)
            for index, (key, item) in enumerate(value.items()):
                if index:
                    self.add(1)
                self.string(str(key))
                self.add(1)
                self.value(item)
        elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
            self.add(2)
            for index, item in enumerate(value):
                if index:
                    self.add(1)
                self.value(item)
        elif isinstance(value, bytes | bytearray):
            # MCP JSON models encode binary fields as base64 strings. Unknown
            # raw bytes are conservatively counted as their hexadecimal form.
            self.string(bytes(value).hex())
        elif isinstance(value, SimpleNamespace):
            self.value({k: v for k, v in vars(value).items() if not k.startswith("_")})
        else:
            self.string(str(value))


def json_utf8_size(value: Any, *, limit: int) -> int:
    """Return encoded JSON bytes, or ``limit + 1`` once the cap is exceeded."""
    sizer = _Sizer(limit)
    try:
        sizer.value(value)
    except _LimitExceeded:
        return limit + 1
    return sizer.total


# Model families whose serialized form a field walk can stand in for. Pydantic
# has many ways to make a model serialize as something other than its declared
# fields — decorators, ``Annotated`` functional serializers, root models,
# ``model_config`` — and enumerating them is a game the estimate keeps losing,
# so vouch for named packages instead. These are the ones this proxy actually
# measures: the MCP SDK's wire models. Anything else routes off-loop, where the
# authoritative sizer serializes the model itself.
# ``mcp_types`` is where the 2.0 SDK's wire models live; ``mcp`` covers the
# session/shared models that reach the sizer through ``SessionMessage``.
_WALKABLE_MODEL_PACKAGES = ("mcp.", "mcp_types.")


@functools.lru_cache(maxsize=512)
def _model_walk_is_faithful(cls: type[BaseModel]) -> bool:
    """Whether a field walk can stand in for *cls*'s own serialization.

    Two conditions, and the allowlist is the load-bearing one: an unknown model
    may serialize through a mechanism this walk cannot see (an ``Annotated``
    ``PlainSerializer`` is not recorded in ``__pydantic_decorators__`` at all),
    and a routing estimate that trusts it would hand a large payload back to
    the event loop to dump. The decorator check then covers a vouched-for
    package growing a serializer later.
    """
    module = f"{getattr(cls, '__module__', '')}."
    if not module.startswith(_WALKABLE_MODEL_PACKAGES):
        return False
    decorators = getattr(cls, "__pydantic_decorators__", None)
    if decorators is None:
        return False
    return not (
        decorators.field_serializers or decorators.model_serializers or decorators.computed_fields
    )


def _approx_json_size(value: Any, *, limit: int) -> int:
    """Bounded size estimate used only to route a payload, never to enforce.

    Differs from ``json_utf8_size`` by whatever a custom serializer or computed
    field would have changed, because it walks a pydantic model's fields rather
    than dumping it. That is safe here: the number decides which thread does the
    authoritative measurement, and both branches then call ``json_utf8_size``
    itself. A model this walk cannot inspect is reported as over the limit, so
    the payload takes the off-loop branch.
    """
    sizer = _Sizer(limit, approximate=True)
    try:
        sizer.value(value)
    except _LimitExceeded:
        return limit + 1
    except Exception:  # noqa: BLE001 - routing must never fail the caller
        return limit + 1
    return sizer.total


async def json_utf8_size_async(value: Any, *, limit: int) -> int:
    """Measure JSON size, hopping to a thread only for a large payload.

    MCP responses can legitimately approach the configured multi-megabyte
    ceiling, and accounting for one is CPU work worth keeping off the event
    loop. Most inbound messages are nowhere near it, though, and this helper is
    called from the dispatcher's read loop on *every* one of them — where an
    unconditional hop cost more than the measurement and queued the loop behind
    unrelated executor work (#956).

    So route first, then measure. ``_approx_json_size`` walks the value under
    ``_SYNC_MEASURE_BYTES`` without dumping any pydantic model, so the routing
    decision itself stays bounded even for a 40 MiB response. A payload it
    reports as small is then measured exactly on this thread, where both the
    dump and the byte walk are cheap because the payload really is small. Any
    other payload is measured exactly on the sizer's dedicated worker, so a
    large message never materializes its dump on the event loop.

    The on-loop measurement's answer is final when it lands at or under the
    budget (the exact size), or when the budget is the caller's own ``limit``
    (a settled over-cap verdict). An estimate that proved badly wrong falls
    through to the worker rather than finishing a big walk here.
    """
    sync_limit = min(limit, _SYNC_MEASURE_BYTES)
    if _approx_json_size(value, limit=sync_limit) <= sync_limit:
        size = json_utf8_size(value, limit=sync_limit)
        if size <= sync_limit or sync_limit == limit:
            return size
    # ``run_in_executor`` does not carry context, while the ``asyncio.to_thread``
    # this replaced did. A serializer reading a ``ContextVar`` would otherwise
    # produce one size on the loop and another on the worker, so pass the
    # caller's context explicitly and keep the two paths interchangeable.
    context = contextvars.copy_context()
    return await asyncio.get_running_loop().run_in_executor(
        _size_executor(),
        functools.partial(context.run, json_utf8_size, value, limit=limit),
    )
