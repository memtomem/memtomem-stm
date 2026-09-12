"""Lossless, generation-pinned metadata pages with a whole-MCP-result budget."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict
from uuid import uuid4

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from memtomem_stm.utils.json_out import dumps, escape_lone_surrogates

MAX_RECOVERY_RESPONSE_BYTES = 16_384
DEFAULT_RECOVERY_PAGE_CHARS = 4_000
MAX_RECOVERY_PAGE_CHARS = 16_000

RecoveryPart = Literal["description", "input_schema", "upstream_description"]


class RecoveryPage(TypedDict):
    name: str
    part: RecoveryPart
    text: str
    format: Literal["text", "json"]
    generation: str
    offset: int
    next_offset: int | None
    total_chars: int
    response_hint: str


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """Detached, serialized sources; equal metadata keeps its registration token."""

    name: str
    description: str
    input_schema: str
    response_hint: str
    upstream_description: str | None
    generation: str = field(default_factory=lambda: uuid4().hex, compare=False)

    @classmethod
    def from_details(cls, details: dict[str, Any]) -> RecoverySnapshot:
        return cls(
            name=escape_lone_surrogates(details["name"]),
            description=escape_lone_surrogates(details["description"]),
            input_schema=dumps(
                details["input_schema"], ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ),
            response_hint=escape_lone_surrogates(details["response_hint"]),
            upstream_description=(
                escape_lone_surrogates(details["upstream_description"])
                if "upstream_description" in details
                else None
            ),
        )


def recovery_result(page: RecoveryPage) -> CallToolResult:
    """Build exactly the envelope budgeted below, bypassing SDK auto-conversion.

    Both representations are intentional for text-only and structured clients.
    Their duplication and JSON escaping count against the SAME byte budget.
    """
    payload = dict(page)
    return CallToolResult(
        content=[
            TextContent(type="text", text=dumps(payload, ensure_ascii=False, separators=(",", ":")))
        ],
        structured_content=payload,
    )


def recovery_result_size(page: RecoveryPage) -> int:
    # Matches MCP's result serialization; the JSON-RPC id/envelope is transport
    # bookkeeping, not part of the tool result that reaches a model.
    return len(
        recovery_result(page).model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
    )


def metadata_page(
    snapshot: RecoverySnapshot,
    part: RecoveryPart,
    offset: int,
    limit: int,
    generation: str | None,
) -> RecoveryPage:
    if part not in ("description", "input_schema", "upstream_description"):
        raise ToolError("Unknown metadata part.")
    if type(offset) is not int or offset < 0:
        raise ToolError("offset must be a nonnegative integer.")
    if type(limit) is not int or not 1 <= limit <= MAX_RECOVERY_PAGE_CHARS:
        raise ToolError("limit must be an integer between 1 and 16000.")
    if offset > 0 and generation is None:
        raise ToolError("generation is required to continue metadata; restart at offset 0.")
    if generation is not None and generation != snapshot.generation:
        raise ToolError("Tool metadata changed; restart at offset 0 without generation.")
    source = getattr(snapshot, part)
    if source is None:
        raise ToolError("Tool metadata is unavailable for this part.")
    total = len(source)
    if offset > total:
        raise ToolError("offset exceeds the metadata length.")

    def page_at(end: int) -> RecoveryPage:
        return RecoveryPage(
            name=snapshot.name,
            part=part,
            text=source[offset:end],
            format="json" if part == "input_schema" else "text",
            generation=snapshot.generation,
            offset=offset,
            next_offset=end if end < total else None,
            total_chars=total,
            response_hint=snapshot.response_hint,
        )

    end = min(total, offset + limit)
    page = page_at(end)
    if recovery_result_size(page) <= MAX_RECOVERY_RESPONSE_BYTES:
        return page

    # Check the final page first above: next_offset=null can be shorter than
    # its preceding numeric offset. Below EOF, size is monotone in end.
    low, high = offset, end - 1
    while low < high:
        middle = (low + high + 1) // 2
        if recovery_result_size(page_at(middle)) <= MAX_RECOVERY_RESPONSE_BYTES:
            low = middle
        else:
            high = middle - 1
    page = page_at(low)
    if low == offset or recovery_result_size(page) > MAX_RECOVERY_RESPONSE_BYTES:
        # Never echo a pathological tool name or yield a non-progressing page.
        raise ToolError("Tool metadata envelope exceeds the recovery response budget.")
    return page
