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


class DeliveryText(str):
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
    value: DeliveryFormat, budget: int = 16000, *, continuation_budget: int | None = None
) -> Iterator[None]:
    token = _FORMAT.set(value)
    budget_token = _BUDGET.set(budget)
    continuation_token = _CONTINUATION_BUDGET.set(
        budget if continuation_budget is None else continuation_budget
    )
    try:
        yield
    finally:
        _FORMAT.reset(token)
        _BUDGET.reset(budget_token)
        _CONTINUATION_BUDGET.reset(continuation_token)


def delivery_budget() -> int:
    return _BUDGET.get()


def continuation_budget() -> int:
    """Full tool limit; initial upstream metadata is not repeated on owned reads."""
    return _CONTINUATION_BUDGET.get()


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
    """Shrink only previews. Never silently omit keys or truncate JSON."""
    if delivery_size(value, owned=owned) <= budget:
        return value
    entries = value["payload"]["entries"]
    lo, hi, best = 0, max((len(e["preview"]) for e in entries), default=0), None
    while lo <= hi:
        cap = (lo + hi) // 2
        payload = {
            **value["payload"],
            "entries": [{**e, "preview": e["preview"][:cap]} for e in entries],
        }
        candidate = {**value, "payload": payload}
        if delivery_size(candidate, owned=owned) <= budget:
            best, lo = candidate, cap + 1
        else:
            hi = cap - 1
    if best is None:
        raise DeliveryBudgetExceeded("Budget cannot hold the complete selection table.")
    return best
