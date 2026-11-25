"""Versioned STM delivery, distinct from upstream MCP structured content.

Only transient chunk responses use this representation. Ordinary response
caches continue to contain pre-surfacing upstream text, never STM envelopes.
"""

from __future__ import annotations

import contextvars
import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Literal, TYPE_CHECKING

from memtomem_stm.utils.json_out import dumps

if TYPE_CHECKING:
    from mcp.types import CallToolResult

DeliveryFormat = Literal["legacy", "structured"]
_FORMAT: contextvars.ContextVar[DeliveryFormat] = contextvars.ContextVar(
    "stm_delivery_format", default="legacy"
)
_BUDGET: contextvars.ContextVar[int] = contextvars.ContextVar("stm_delivery_budget", default=16000)
_CONTINUATION_BUDGET: contextvars.ContextVar[int] = contextvars.ContextVar(
    "stm_continuation_budget", default=16000
)
_READ_FORMAT: contextvars.ContextVar[DeliveryFormat | None] = contextvars.ContextVar(
    "stm_read_format", default=None
)
_TEXT_BLOCK: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "stm_text_block", default=None
)
_WIRE_PROJECTOR: contextvars.ContextVar[Callable[[dict[str, Any]], dict[str, Any]] | None] = (
    contextvars.ContextVar("stm_wire_projector", default=None)
)


@contextmanager
def wire_projection_scope(
    projector: Callable[[dict[str, Any]], dict[str, Any]],
) -> Iterator[None]:
    token = _WIRE_PROJECTOR.set(projector)
    try:
        yield
    finally:
        _WIRE_PROJECTOR.reset(token)


def wire_json(result: CallToolResult) -> str:
    """Measure the negotiated SDK wire surface, excluding JSON-RPC framing.

    The SDK may add serverInfo/resultType for modern clients, or remove fields
    for legacy clients. Request middleware supplies that projection; direct
    Python/library calls retain their existing model-serialization basis.
    """
    projector = _WIRE_PROJECTOR.get()
    if projector is None:
        return result.model_dump_json(by_alias=True, exclude_none=True)
    data = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    return dumps(projector(data), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class DeliveryError(ValueError):
    code = "invalid_delivery_metadata"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class UnsupportedDelivery(DeliveryError):
    code = "unsupported_delivery_version"


class DeliveryBudgetExceeded(DeliveryError):
    code = "budget_exceeded"

    def __init__(self, message: str, *, code: str | None = None, excess: int | None = None) -> None:
        super().__init__(message, code=code)
        # Serialized characters the smallest attempted frame still needed, when
        # a fitter measured it. Lets a caller decide how much source to move
        # out of a fixed part (a hybrid head) before trying again.
        self.excess = excess


class BlockText(str):
    """Continuation text that carries the upstream text block's metadata (#1529).

    ``text_block`` is the ``annotations`` / ``_meta`` of the text block the
    continuation is a part of, in its JSON form; ``None`` for a block that had
    none and for STM's own messages. The continuation formatter re-applies it.
    """

    text_block: dict[str, Any] | None = None


def with_text_block(value: str, block: dict[str, Any] | None) -> str:
    """*value* carrying *block*; *value* itself when there is nothing to carry."""
    if not block:
        return value
    carried = value if isinstance(value, BlockText) else BlockText(value)
    carried.text_block = block
    return carried


class DeliveryText(BlockText):
    """Legacy-compatible compressor result carrying its typed origin, never parsed.

    The descriptor is created at the key publisher. Its presence is also the
    out-of-band cache exclusion signal, independent of presentation markers.
    """

    descriptor: dict[str, Any]
    response_format: DeliveryFormat = "legacy"

    def __new__(cls, text: str, descriptor: dict[str, Any]) -> DeliveryText:
        value = super().__new__(cls, text)
        value.descriptor = descriptor
        return value


@contextmanager
def delivery_scope(
    value: DeliveryFormat,
    budget: int = 16000,
    *,
    continuation_budget: int | None = None,
    text_block: dict[str, Any] | None = None,
) -> Iterator[None]:
    """Delivery settings for one response's publishers.

    *text_block* is the metadata of the upstream text block being published,
    as :func:`text_block_overhead` and :func:`stored_text_block` read it; the
    publishers store it with the pending entry so continuations repeat it.
    """
    token = _FORMAT.set(value)
    budget_token = _BUDGET.set(budget)
    continuation_token = _CONTINUATION_BUDGET.set(
        budget if continuation_budget is None else continuation_budget
    )
    block_token = _TEXT_BLOCK.set(text_block or None)
    try:
        yield
    finally:
        _FORMAT.reset(token)
        _BUDGET.reset(budget_token)
        _CONTINUATION_BUDGET.reset(continuation_token)
        _TEXT_BLOCK.reset(block_token)


def delivery_budget() -> int:
    return _BUDGET.get()


def continuation_budget() -> int:
    """Full tool limit, as stored with a pending entry.

    Result-level upstream metadata is not repeated on owned reads. The text
    block's metadata is, and a structured reader subtracts its size
    (:func:`text_block_overhead`) from this limit when it fits a frame.
    """
    return _CONTINUATION_BUDGET.get()


def text_block_metadata() -> dict[str, Any] | None:
    """The text block metadata of the response being published, if any."""
    return _TEXT_BLOCK.get()


def stored_text_block(meta: dict[str, Any]) -> dict[str, Any] | None:
    """The ``text_block`` a pending entry's metadata stores, validated (#1529).

    ``None`` when *meta* has no such key. A present value — ``null`` included —
    that is not a non-empty object, or that the SDK's ``TextContent`` refuses,
    is a :class:`DeliveryError`, never a miss: a miss deletes the live row.
    Lone surrogates are escaped here, as the rest of the row's metadata is by
    its parser. This is a structural check: publishers only write metadata the
    first response already carried, and a row corrupted past what the SDK can
    serialize fails at send like any other corrupted field.
    """
    if "text_block" not in meta:
        return None
    from mcp.types import TextContent

    from memtomem_stm.utils.json_out import scrub_lone_surrogates

    try:
        raw = scrub_lone_surrogates(meta["text_block"])
        if not isinstance(raw, dict) or not raw or "type" in raw or "text" in raw:
            raise TypeError("text block metadata must be a non-empty object")
        TextContent.model_validate({**raw, "type": "text", "text": ""})
    except (ValueError, TypeError, RecursionError) as exc:
        raise DeliveryError("Invalid text block metadata.") from exc
    return raw


def progressive_text_block(metadata_json: str | None) -> dict[str, Any] | None:
    """The text block metadata a progressive row keeps in ``metadata_json``.

    Kept out of ``__meta__``: every publication's expiry pass decodes the
    ``__meta__`` of each live row, and block metadata can be large, so only
    the read of the requested key decodes it. ``None`` for a row without it
    (every row written before #1529). Only this code writes the column for
    progressive rows, so one that does not decode is a :class:`DeliveryError`,
    which keeps the row, and never a miss.
    """
    if metadata_json is None:
        return None
    try:
        data = json.loads(metadata_json)
    except (TypeError, ValueError, RecursionError) as exc:
        raise DeliveryError("Invalid text block metadata.") from exc
    if not isinstance(data, dict):
        raise DeliveryError("Invalid text block metadata.")
    return stored_text_block(data)


def apply_text_block(result: CallToolResult, block: dict[str, Any] | None) -> CallToolResult:
    """*result* with its first content block carrying *block* (#1529)."""
    if not block:
        return result
    from mcp.types import TextContent

    first = result.content[0]
    assert isinstance(first, TextContent)
    result.content[0] = TextContent.model_validate({**block, "type": "text", "text": first.text})
    return result


def text_block_overhead(block: dict[str, Any] | None) -> int:
    """Serialized characters *block* adds to a result's text block.

    The larger of :func:`wire_json`'s count, the unit the structured fitters
    budget, and the SDK serializer's: under a projector ``wire_json`` writes
    with ``json``, which spells some floats shorter than the SDK sends them
    (``1e-05`` against ``0.00001``). The difference does not depend on the
    block's text.
    """
    if not block:
        return 0
    from mcp.types import CallToolResult, TextContent

    bare = CallToolResult(content=[TextContent(type="text", text="")])
    carried = apply_text_block(CallToolResult(content=[TextContent(type="text", text="")]), block)

    def sdk(result: CallToolResult) -> int:
        return len(result.model_dump_json(by_alias=True, exclude_none=True))

    return max(0, len(wire_json(carried)) - len(wire_json(bare)), sdk(carried) - sdk(bare))


@contextmanager
def read_delivery_scope(value: DeliveryFormat | None) -> Iterator[None]:
    token = _READ_FORMAT.set(value)
    try:
        yield
    finally:
        _READ_FORMAT.reset(token)


def progressive_metadata_format(meta: dict[str, Any]) -> DeliveryFormat:
    if _READ_FORMAT.get() == "legacy":
        return "legacy"
    try:
        raw = (
            dumps(meta["delivery"], ensure_ascii=True, allow_nan=False)
            if "delivery" in meta
            else None
        )
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise DeliveryError("Invalid delivery metadata.") from exc
    return metadata_format(raw, _READ_FORMAT.get())


def stored_progressive_format(meta: dict[str, Any]) -> object:
    """Read the stored declaration without applying a per-read override.

    Validation remains the responsibility of ``progressive_metadata_format``.
    """
    presentation = meta.get("delivery")
    return (
        presentation.get("delivery_format", "legacy")
        if isinstance(presentation, dict)
        else "legacy"
    )


def delivery_format() -> DeliveryFormat:
    return _FORMAT.get()


def metadata_format(raw: str | None, override: DeliveryFormat | None = None) -> DeliveryFormat:
    """Validate presentation metadata independently of expiry. Never deletes rows."""
    if override == "legacy":
        return override
    if raw is None:
        return override or "legacy"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, RecursionError) as exc:
        raise DeliveryError("Invalid delivery metadata.") from exc
    if not isinstance(data, dict):
        raise DeliveryError("Invalid delivery metadata.")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise UnsupportedDelivery("Unsupported delivery metadata version.")
    mode = data.get("delivery_format")
    if mode not in ("legacy", "structured"):
        raise UnsupportedDelivery("Unsupported delivery format.")
    if "budget" in data and (type(data["budget"]) is not int or not 0 < data["budget"] < 2**63):
        raise DeliveryError("Invalid delivery budget.")
    if "metadata_complete" in data and type(data["metadata_complete"]) is not bool:
        raise DeliveryError("Invalid completeness flag.")
    if "source_offset" in data and (
        type(data["source_offset"]) is not int or data["source_offset"] < 0
    ):
        raise DeliveryError("Invalid source offset.")
    records = data.get("chunks", {})
    if not isinstance(records, dict):
        raise DeliveryError("Invalid chunk metadata.")
    for key, record in records.items():
        if not isinstance(key, str) or not isinstance(record, dict):
            raise DeliveryError("Invalid chunk metadata.")
        for field in ("chunk_id", "label"):
            if not isinstance(record.get(field), str) or (
                field == "chunk_id" and not record[field]
            ):
                raise DeliveryError("Invalid chunk identity.")
            try:
                record[field].encode("utf-8")
            except UnicodeEncodeError as exc:
                raise DeliveryError("Invalid chunk identity.") from exc
        path = record.get("json_path", [])
        if not isinstance(path, list) or any(type(part) not in (str, int) for part in path):
            raise DeliveryError("Invalid JSON path.")
        if any(
            isinstance(part, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in part) for part in path
        ):
            raise DeliveryError("Invalid JSON path.")
        for field in ("start", "end"):
            if field in record and (type(record[field]) is not int or record[field] < 0):
                raise DeliveryError("Invalid source range.")
        if "start" in record and "end" in record and record["start"] > record["end"]:
            raise DeliveryError("Invalid source range.")
        heading = record.get("heading")
        if heading is not None and (
            not isinstance(heading, dict)
            or not isinstance(heading.get("title"), str)
            or type(heading.get("level")) is not int
            or not 1 <= heading["level"] <= 6
        ):
            raise DeliveryError("Invalid heading metadata.")
    return override or mode


def envelope(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": kind,
        "offset_unit": "unicode_codepoint",
        "payload": payload,
    }


def result_object(value: dict[str, Any]) -> dict[str, Any]:
    return {"result": f"STM {value['kind']} delivery", "stm": value}


def delivery_json(value: dict[str, Any]) -> str:
    return dumps(result_object(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def delivery_result(value: dict[str, Any], *, owned: bool = True) -> CallToolResult:
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(
        content=[TextContent(type="text", text=delivery_json(value))],
        structured_content=result_object(value) if owned else None,
        is_error=value["kind"] == "error",
    )


def plain_result_fits(text: str) -> bool:
    """Whether *text* returned unchanged fits the structured base budget.

    Measured as the single-text MCP result the passthrough becomes, so JSON
    escaping and the result wrapper count, as they do for STM envelopes.
    """
    from mcp.types import CallToolResult, TextContent

    result = CallToolResult(content=[TextContent(type="text", text=text)])
    return len(wire_json(result)) <= delivery_budget()


def delivery_size(value: dict[str, Any], *, owned: bool) -> int:
    """Serialized base MCP-result characters, before optional supplements."""
    return len(wire_json(delivery_result(value, owned=owned)))


def error_result(code: str, message: str, *, owned: bool = True) -> CallToolResult:
    return delivery_result(envelope("error", {"code": code, "message": message}), owned=owned)


def progressive_envelope(
    content: str, *, key: str, offset: int, end: int, source_format: str, ttl: float
) -> dict[str, Any]:
    return envelope(
        "progressive",
        {
            "key": key,
            "source_format": source_format,
            "content": content[offset:end],
            "offset": offset,
            "next_offset": end if end < len(content) else None,
            "total_chars": len(content),
            "has_more": end < len(content),
            "ttl_seconds_remaining": ttl,
        },
    )


def fit_progressive(
    content: str,
    *,
    key: str,
    offset: int,
    limit: int,
    source_format: str,
    ttl: float,
    budget: int,
    owned: bool,
) -> dict[str, Any]:
    def build(end: int) -> dict[str, Any]:
        return progressive_envelope(
            content, key=key, offset=offset, end=end, source_format=source_format, ttl=ttl
        )

    maximum = min(len(content), offset + limit)
    full = build(maximum)
    if delivery_size(full, owned=owned) <= budget:
        return full
    lo, hi, best = offset + 1, maximum - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = build(mid)
        if delivery_size(candidate, owned=owned) <= budget:
            best, lo = candidate, mid + 1
        else:
            hi = mid - 1
    if best is None:
        raise DeliveryBudgetExceeded("Budget cannot hold a progressing delivery envelope.")
    return best


def require_progressive_continuation(
    content: str, *, key: str, source_format: str, ttl: float, budget: int
) -> None:
    """Preflight a one-character owned reply before publishing a non-final key.

    Use the source's worst JSON escaping cost and widest cursor fields. Checking
    both final and non-final shapes avoids a key that works once but gets stuck
    on a later newline, digit-width transition, or final null cursor.
    """
    if re.search(r"[\x00-\x07\x0b\x0e-\x1f]", content):
        worst = "\x00"
    elif '"' in content or "\\" in content:
        worst = '"'
    elif re.search(r"[\b\f\n\r\t]", content):
        worst = "\n"
    else:
        worst = "x"
    descriptor = progressive_envelope(
        content,
        key=key,
        offset=len(content) - 1,
        end=len(content),
        source_format=source_format,
        ttl=ttl,
    )
    payload = descriptor["payload"]
    payload["content"] = worst
    for has_more in (False, True):
        payload["has_more"] = has_more
        payload["next_offset"] = len(content) if has_more else None
        if delivery_size(descriptor, owned=True) > budget:
            raise DeliveryBudgetExceeded("Budget cannot hold a progressing continuation envelope.")


def fit_toc(value: dict[str, Any], budget: int, *, owned: bool = False) -> dict[str, Any]:
    """Shrink non-inline previews; keep complete inline contents and all keys."""
    if delivery_size(value, owned=owned) <= budget:
        return value
    entries = value["payload"]["entries"]
    lo, hi, best = (
        0,
        max((len(e["preview"]) for e in entries if not e.get("inline")), default=0),
        None,
    )
    while lo <= hi:
        cap = (lo + hi) // 2
        payload = {
            **value["payload"],
            "entries": [
                e if e.get("inline") else {**e, "preview": e["preview"][:cap]} for e in entries
            ],
        }
        candidate = {**value, "payload": payload}
        if delivery_size(candidate, owned=owned) <= budget:
            best, lo = candidate, cap + 1
        else:
            hi = cap - 1
    if best is None:
        smallest = {
            **value,
            "payload": {
                **value["payload"],
                "entries": [e if e.get("inline") else {**e, "preview": ""} for e in entries],
            },
        }
        raise DeliveryBudgetExceeded(
            "Budget cannot hold the complete selection table.",
            excess=delivery_size(smallest, owned=owned) - budget,
        )
    return best
