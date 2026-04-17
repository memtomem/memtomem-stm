"""Memory operations — auto-indexing responses and extracting facts into LTM."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from memtomem_stm.proxy.protocols import FileIndexer

from memtomem_stm.proxy.config import AutoIndexConfig, ExtractionConfig
from memtomem_stm.proxy.extraction import ExtractedFact, FactExtractor
from memtomem_stm.utils.fileio import atomic_write_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AutoIndexOutcome:
    """Structured result of a single ``auto_index_response`` call.

    ``summary`` is the composed ``[Indexed] … \\n\\n <agent_summary>`` string
    the caller returns to the agent — identical to the pre-outcome return
    value, preserved verbatim so existing surfaces don't change.

    The other fields feed ``CallMetrics`` so operators watching
    ``proxy_metrics.db`` see indexing failures that were previously
    swallowed: the pre-outcome implementation caught every exception,
    reported ``0 chunks`` in the summary, and left the metrics row
    looking healthy even when the LTM pipeline was fully broken.
    """

    summary: str
    ok: bool
    chunks_indexed: int
    error: str | None = None


def compose_index_footer(
    server: str,
    tool: str,
    original_chars: int | None,
    compressed_chars: int | None,
    text: str,
    agent_summary: str,
    ns: str,
    chunks: int | None,
) -> str:
    """Compose the ``[Indexed]`` / ``[Indexing…]`` summary footer.

    ``chunks=None`` is the background-scheduled placeholder: emits
    ``[Indexing…] … · scheduled`` with the namespace dropped, since
    the chunk count and final namespace binding are both unknown until
    the background indexing task runs.
    """
    size_part = f"{original_chars or len(text)}→{compressed_chars or len(agent_summary)} chars"
    if chunks is None:
        tag = "Indexing…"
        tail = "scheduled"
    else:
        tag = "Indexed"
        tail = f"{chunks} chunks in `{ns}` namespace"
    return f"[{tag}] `{server}/{tool}` ({size_part}) · {tail}.\n\n{agent_summary}"


@dataclass(frozen=True, slots=True)
class ExtractOutcome:
    """Structured result of a single ``extract_and_store`` call.

    ``ok=True`` means the extraction phase completed without the outer
    exception path firing. Individual fact-indexing failures inside the
    loop are logged and counted against ``facts_stored`` (they reduce the
    count) but do NOT flip ``ok`` — matching the pre-outcome contract
    where partial failure still returned normally.
    """

    ok: bool
    facts_stored: int
    error: str | None = None


async def auto_index_response(
    index_engine: FileIndexer,
    ai_cfg: AutoIndexConfig,
    server: str,
    tool: str,
    arguments: dict[str, Any],
    text: str,
    agent_summary: str,
    compression_strategy: str | None = None,
    original_chars: int | None = None,
    compressed_chars: int | None = None,
    context_query: str | None = None,
) -> AutoIndexOutcome:
    """Write a response to disk and index it via the file indexer.

    Returns an ``AutoIndexOutcome`` carrying the summary string plus the
    indexing status (``ok``, ``chunks_indexed``, ``error``). Callers that
    only need the summary can read ``outcome.summary``; callers that feed
    metrics consume the status fields. Failure is still logged via
    ``logger.warning`` as before — the outcome adds a second observability
    channel for operators who can't scrape logs.
    """
    memory_dir = ai_cfg.memory_dir.expanduser().resolve()
    memory_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    safe_tool = tool.replace("/", "_")
    fname = f"{server}__{safe_tool}__{ts}.md"
    file_path = memory_dir / fname

    args_str = ", ".join(f"{k}={v!r}" for k, v in arguments.items()) if arguments else "(none)"

    frontmatter_lines = [
        "---",
        f"source: proxy/{server}/{tool}",
        f"timestamp: {datetime.now(timezone.utc).isoformat()}",
    ]
    if compression_strategy is not None:
        frontmatter_lines.append(f"compression: {compression_strategy}")
    if original_chars is not None:
        frontmatter_lines.append(f"original_chars: {original_chars}")
    if compressed_chars is not None:
        frontmatter_lines.append(f"compressed_chars: {compressed_chars}")
    frontmatter_lines.append("---")

    intent_section = ""
    if context_query:
        intent_section = f"## Agent Intent\n\n> {context_query}\n\n"

    md_content = (
        f"{chr(10).join(frontmatter_lines)}\n\n"
        f"# Proxy Response: {server}/{tool}\n\n"
        f"- **Source**: `{server}/{tool}({args_str})`\n"
        f"- **Original size**: {original_chars or len(text)} chars\n\n"
        f"{intent_section}"
        f"## Content\n\n{text}\n"
    )
    # Atomic so the auto-indexer (called next) can't observe a partial file
    # if the writer is killed mid-flush. Caller has already mkdir'd memory_dir.
    atomic_write_text(file_path, md_content, ensure_parent=False)

    ns = ai_cfg.namespace.format(server=server, tool=tool)

    ok = True
    error: str | None = None
    try:
        stats = await index_engine.index_file(file_path, namespace=ns)
        chunks = stats.indexed_chunks
    except Exception as exc:
        logger.warning(
            "Auto-index failed for %s/%s: %s",
            server,
            tool,
            exc,
            exc_info=True,
        )
        chunks = 0
        ok = False
        error = f"{type(exc).__name__}: {exc}"

    summary = compose_index_footer(
        server=server,
        tool=tool,
        original_chars=original_chars,
        compressed_chars=compressed_chars,
        text=text,
        agent_summary=agent_summary,
        ns=ns,
        chunks=chunks,
    )
    return AutoIndexOutcome(summary=summary, ok=ok, chunks_indexed=chunks, error=error)


async def extract_and_store(
    index_engine: FileIndexer | None,
    extractor: FactExtractor,
    ext_cfg: ExtractionConfig,
    server: str,
    tool: str,
    arguments: dict[str, Any],
    text: str,
    *,
    context_query: str | None = None,
) -> ExtractOutcome:
    """Extract facts from response and store as individual memory entries.

    Returns an ``ExtractOutcome`` — ``ok=False`` only when the outer
    extraction phase itself raises (e.g., ``extractor.extract()`` fails).
    Per-fact indexing failures are logged and counted against
    ``facts_stored`` but do NOT flip ``ok``.
    """
    indexed_count = 0
    try:
        facts = await extractor.extract(text, server=server, tool=tool)
        if not facts:
            return ExtractOutcome(ok=True, facts_stored=0)

        memory_dir = ext_cfg.memory_dir.expanduser().resolve()
        memory_dir.mkdir(parents=True, exist_ok=True)
        ns = ext_cfg.namespace.format(server=server, tool=tool)

        # Dedup: skip facts already in the index
        dedup = ext_cfg.dedup_threshold > 0 and hasattr(index_engine, "is_duplicate")

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        safe_tool = tool.replace("/", "_")

        for i, fact in enumerate(facts[: ext_cfg.max_facts]):
            if dedup and index_engine is not None:
                try:
                    is_dup = await index_engine.is_duplicate(
                        fact.content,
                        namespace=ns,
                        threshold=ext_cfg.dedup_threshold,
                    )
                    if is_dup:
                        logger.debug("Skipping duplicate fact: %s", fact.content[:60])
                        continue
                except Exception:
                    pass  # on dedup failure, proceed with indexing

            fname = f"{server}__{safe_tool}__fact__{ts}__{i:02d}.md"
            file_path = memory_dir / fname
            md_content = format_fact_md(fact, server, tool, arguments)
            # Atomic so a kill between write and index_file leaves no partial.
            atomic_write_text(file_path, md_content, ensure_parent=False)

            if index_engine is None:
                continue
            try:
                await index_engine.index_file(file_path, namespace=ns)
                indexed_count += 1
            except Exception as exc:
                logger.warning("Fact indexing failed: %s", exc, exc_info=True)

        if indexed_count:
            logger.info(
                "Extracted %d facts from %s/%s into namespace '%s'",
                indexed_count,
                server,
                tool,
                ns,
            )
        return ExtractOutcome(ok=True, facts_stored=indexed_count)
    except Exception as exc:
        logger.warning("Fact extraction failed for %s/%s", server, tool, exc_info=True)
        return ExtractOutcome(
            ok=False,
            facts_stored=indexed_count,
            error=f"{type(exc).__name__}: {exc}",
        )


def format_fact_md(
    fact: ExtractedFact,
    server: str,
    tool: str,
    arguments: dict[str, Any],
) -> str:
    """Format an extracted fact as a Markdown file with frontmatter."""
    tags_str = ", ".join(fact.tags) if fact.tags else ""
    args_str = ", ".join(f"{k}={v!r}" for k, v in arguments.items()) if arguments else "(none)"
    title = fact.content[:80].rstrip(".")
    lines = [
        "---",
        f"source: extracted/{server}/{tool}",
        f"timestamp: {datetime.now(timezone.utc).isoformat()}",
        f"category: {fact.category}",
        f"confidence: {fact.confidence}",
        "---",
        "",
        f"## {title}",
        "",
        fact.content,
        "",
    ]
    if tags_str:
        lines.append(f"tags: [{tags_str}]")
    lines.append(f"extracted_from: {server}/{tool}({args_str})")
    lines.append("")
    return "\n".join(lines)
