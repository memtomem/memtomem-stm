"""Response compression strategies."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    LLMCompressorConfig,
    LLMProvider,
    TailMode,
)
from memtomem_stm.proxy.relevance import BM25Scorer, RelevanceScorer
from memtomem_stm.utils.circuit_breaker import CircuitBreaker as _CircuitBreaker

logger = logging.getLogger(__name__)

# Backward-compatible alias (tests import QueryRelevanceScorer from here)
QueryRelevanceScorer = BM25Scorer


_HEADINGS_RE = re.compile(r"(?:^|\n)#{1,6}\s")
_CODE_FENCE_RE = re.compile(r"```")
_LIST_ITEM_RE = re.compile(r"(?:^|\n)\s*[-*]\s")
_LINK_RE = re.compile(r"\[.*?\]\(.*?\)")


def _content_summary(text: str) -> str:
    """Count structural elements in the original text for truncation metadata."""
    counts: list[str] = []
    headings = len(_HEADINGS_RE.findall(text))
    code_blocks = len(_CODE_FENCE_RE.findall(text)) // 2
    list_items = len(_LIST_ITEM_RE.findall(text))
    links = len(_LINK_RE.findall(text))
    if headings:
        counts.append(f"{headings} headings")
    if code_blocks:
        counts.append(f"{code_blocks} code blocks")
    if list_items:
        counts.append(f"{list_items} list items")
    if links:
        counts.append(f"{links} links")
    return f" [{', '.join(counts)}]" if counts else ""


def count_markdown_headings(text: str) -> int:
    """Count ATX markdown headings with the canonical regex.

    Shared by ``auto_select_strategy`` (HYBRID routing) and the proxy's
    retention-ladder hybrid-fallback gate so the two agree on what a "heading"
    is. Requires ``#``..``######`` followed by whitespace and matches a heading
    at offset 0 — unlike a bare ``text.count("\\n#")``, which both misses an
    offset-0 heading and over-counts a ``#`` that is not a heading (e.g. a shell
    comment line, or ``#`` inside prose without a following space).
    """
    return len(_HEADINGS_RE.findall(text))


def _sanitize_nonfinite(obj: object) -> object:
    """Recursively replace non-finite floats (``NaN`` / ``Infinity`` /
    ``-Infinity``) with ``None`` so the result serializes to valid JSON.

    ``json.dumps`` emits the bareword tokens ``NaN`` / ``Infinity`` /
    ``-Infinity`` for non-finite floats, which RFC 8259 JSON forbids and strict
    parsers (browser ``JSON.parse``, Go ``encoding/json``,
    ``json.loads(parse_constant=...)``) reject. Upstream tool responses that dump
    numpy/pandas/ML metrics routinely carry these tokens. We sanitize ONCE at
    parse time (see ``_mm_json_loads``) so every compression tier that re-dumps
    the parsed payload — including the budget-search probe loops that measure
    ``len(json.dumps(candidate))`` — sees only finite values and never re-emits
    an invalid token.

    Returns the input object UNCHANGED (same identity) when it contains no
    non-finite float, so the common case allocates nothing. ``bool`` is an
    ``int`` subclass (not ``float``) and is left untouched.
    """
    if isinstance(obj, float):
        return None if not math.isfinite(obj) else obj
    if isinstance(obj, dict):
        replaced: dict | None = None
        for key, value in obj.items():
            sanitized = _sanitize_nonfinite(value)
            if sanitized is not value:
                if replaced is None:
                    replaced = dict(obj)
                replaced[key] = sanitized
        return replaced if replaced is not None else obj
    if isinstance(obj, (list, tuple)):
        replaced_seq: list | None = None
        for index, value in enumerate(obj):
            sanitized = _sanitize_nonfinite(value)
            if sanitized is not value:
                if replaced_seq is None:
                    replaced_seq = list(obj)
                replaced_seq[index] = sanitized
        if replaced_seq is not None:
            return replaced_seq
        # A tuple with no non-finite float still round-trips through json as an
        # array; return it unchanged to preserve the no-copy fast path.
        return obj
    return obj


def _mm_json_loads(s: str, **kwargs: object) -> object:
    """``json.loads`` that scrubs non-finite floats from the parsed result.

    Python's ``json.loads`` accepts the ``NaN`` / ``Infinity`` / ``-Infinity``
    extension tokens and turns them into ``float('nan')`` / ``float('inf')``,
    which would later re-serialize as those same invalid tokens. Sanitizing here
    — the single ingest point for every JSON-handling compression tier — keeps
    all downstream output valid JSON without per-serialization overhead.
    """
    return _sanitize_nonfinite(json.loads(s, **kwargs))


class Compressor(Protocol):
    def compress(self, text: str, *, max_chars: int) -> str: ...


class NoopCompressor:
    """No compression — passthrough.

    Example::

        Input  (max_chars=20): "Unchanged response."
        Output:                "Unchanged response."
    """

    def compress(self, text: str, *, max_chars: int) -> str:
        return text


class TruncateCompressor:
    """Character limit with sentence/word boundary awareness.

    For text with markdown headings, prefers to cut at heading boundaries
    and appends a list of remaining section titles. For plain text, cuts
    at the nearest sentence or word boundary.

    Example — markdown with multiple sections (preserves every heading)::

        Input:  "## Setup\\nInstall deps.\\n\\n## API\\nGET /users.\\n\\n## Errors\\n500 means DB down."
        Output: "## Setup\\nInstall deps.\\n\\n## API\\nGET /users.\\n\\n## Errors\\n500 means DB down."

    Example — plain text (sentence-boundary cut with length suffix)::

        Input:  "First sentence. Second sentence explains more. Third sentence adds context."
        Output: "First sentence. Second sentence explains more.\\n... (truncated, original: 76 chars)"

    Note: minimum retention is enforced at the pipeline level
    (ProxyManager / BenchHarness), not in the compressor. The compressor
    trusts the max_chars budget it receives.
    """

    _HEADING_RE = re.compile(r"(?:^|\n)(#{1,6}\s+.+)")

    def __init__(self, scorer: RelevanceScorer | None = None) -> None:
        self._scorer = scorer or BM25Scorer()

    # Patterns for code structure boundaries (function/class/method definitions)
    _CODE_BOUNDARY_RE = re.compile(
        r"(?:^|\n)"
        r"(\s*(?:def |class |async def |function |func |export |pub fn )\S.*)",
    )
    # SQL top-level statement boundaries (non-indented only)
    _SQL_BOUNDARY_RE = re.compile(
        r"(?:^|\n)((?:SELECT|WITH|CREATE|INSERT|UPDATE|DELETE)\s)", re.IGNORECASE
    )
    # Comment-section boundaries (-- Section Header)
    _COMMENT_SECTION_RE = re.compile(r"(?:^|\n)(--\s+\S.+)")

    def compress(self, text: str, *, max_chars: int, context_query: str | None = None) -> str:
        if not text or len(text) <= max_chars:
            return text

        # Try JSON key-aware truncation — only for config-like dicts (all values are dicts)
        stripped = text.strip()
        if stripped and stripped[0] == "{":
            try:
                data = _mm_json_loads(stripped)
                if (
                    isinstance(data, dict)
                    and len(data) >= 2
                    and all(isinstance(v, dict) for v in data.values())
                ):
                    return self._json_key_truncate(data, max_chars, context_query)
            except (json.JSONDecodeError, ValueError):
                pass

        # Try section-aware truncation for markdown with headings
        headings = list(self._HEADING_RE.finditer(text))
        if len(headings) >= 2:
            return self._section_aware_truncate(text, max_chars, headings, context_query)

        # Try code-structure-aware truncation (function/class/SQL boundaries)
        code_boundaries = list(self._CODE_BOUNDARY_RE.finditer(text))
        if len(code_boundaries) >= 2:
            return self._code_aware_truncate(text, max_chars, code_boundaries)

        # Try SQL/comment-section boundaries
        sql_boundaries = list(self._COMMENT_SECTION_RE.finditer(text))
        if len(sql_boundaries) < 2:
            sql_boundaries = list(self._SQL_BOUNDARY_RE.finditer(text))
        if len(sql_boundaries) >= 2:
            return self._code_aware_truncate(text, max_chars, sql_boundaries)

        # Repetitive content: preserve tail anomaly
        result = self._tail_anomaly_truncate(text, max_chars)
        if result:
            return result

        # Fallback: position-based truncation. Reserve the suffix out of the
        # budget so break_at + suffix never exceeds max_chars — the suffix used
        # to be appended *after* a budget-filling break point, overshooting it.
        # When the budget is too small to hold the informative note, fall back
        # to a 1-char marker so the body (the useful part) still gets the room.
        summary = _content_summary(text)
        suffix = f"\n... (truncated, original: {len(text)} chars){summary}"
        if len(suffix) >= max_chars:
            suffix = "…"
        break_at = self._find_break(text, max(1, max_chars - len(suffix)))
        result = text[:break_at] + suffix
        return result if len(result) <= max_chars else result[:max_chars]

    _SUMMARY_RE = re.compile(
        r"summary|conclusion|결론|요약|security|root\s*cause|remediation"
        r"|troubleshoot|보안|원인|조치",
        re.IGNORECASE,
    )

    def _json_key_truncate(
        self, data: dict, max_chars: int, context_query: str | None = None
    ) -> str:
        """Distribute budget across all top-level JSON keys.

        Each key gets a proportional share of the budget based on its
        serialized size. When context_query is provided, budget is weighted
        by BM25 relevance (blended with size-proportional allocation).
        """
        # Serialize each top-level key separately to measure sizes
        key_sizes: list[tuple[str, str, int]] = []
        for k, v in data.items():
            serialized = json.dumps({k: v}, ensure_ascii=False, indent=2)
            key_sizes.append((k, serialized, len(serialized)))

        total_size = sum(s for _, _, s in key_sizes)
        overhead = 10  # {}, commas, newlines
        available = max_chars - overhead

        # Compute query-aware weights if applicable
        relevance_weights: list[float] | None = None
        if context_query and context_query.strip():
            sections = [(k, ser) for k, ser, _ in key_sizes]
            raw = self._scorer.score_sections(context_query, sections)
            if any(s > 0 for s in raw):
                relevance_weights = raw

        # Build output: each key gets budget allocation
        parts: list[str] = []
        for idx, (k, serialized, size) in enumerate(key_sizes):
            # Size-proportional base
            size_share = available * size / total_size if total_size else available / len(key_sizes)
            if relevance_weights is not None:
                total_rel = sum(relevance_weights)
                rel_share = available * relevance_weights[idx] / total_rel if total_rel else 0
                # Blend: 40% size-proportional + 60% relevance
                key_budget = max(40, int(0.4 * size_share + 0.6 * rel_share))
            else:
                key_budget = max(40, int(size_share))

            if size <= key_budget:
                inner = serialized.strip()[1:-1].strip()
                parts.append(inner)
            else:
                v = data[k]
                truncated = self._truncate_json_value(v, max(0, key_budget - len(k) - 6))
                part = json.dumps({k: truncated}, ensure_ascii=False, indent=2)
                inner = part.strip()[1:-1].strip()
                parts.append(inner)

        # Assemble as valid JSON. The old path sliced the assembled string to
        # max_chars and appended a note — producing INVALID JSON and STILL
        # overshooting the budget. Instead drop whole trailing keys (recording
        # the count in a valid ``_truncated`` member) until the object fits, so
        # the result always parses and stays within budget.
        #
        # Contract floor: valid JSON cannot be shorter than ``{}`` (2 chars).
        # For any ``max_chars >= 2`` the result is ``<= max_chars``; at a
        # pathological sub-2-char budget (never produced by config or the
        # manager retention ladder, which raises tiny budgets) JSON validity
        # takes precedence and we still return ``{}``.
        def _assemble(kept: list[str], omitted: int) -> str:
            members = list(kept)
            if omitted:
                members.append(f'"_truncated": "{omitted} of {len(parts)} keys omitted"')
            return "{\n" + ",\n".join(members) + "\n}"

        result = _assemble(parts, 0)
        if len(result) <= max_chars:
            return result
        for keep in range(len(parts) - 1, -1, -1):
            candidate = _assemble(parts[:keep], len(parts) - keep)
            if len(candidate) <= max_chars:
                return candidate
        return "{}"

    def _truncate_json_value(self, value: object, budget: int) -> object:
        """Truncate a JSON value to fit within character budget."""
        if isinstance(value, str):
            if len(value) > budget:
                return value[:budget] + "..."
            return value
        if isinstance(value, dict):
            preview: dict = {}
            per_key = max(20, budget // max(1, len(value)))
            for k, v in value.items():
                preview[k] = self._truncate_json_value(v, per_key)
            return preview
        if isinstance(value, list):
            if not value:
                return value
            n = min(3, len(value))
            items = [self._truncate_json_value(item, budget // max(1, n)) for item in value[:n]]
            if len(value) > n:
                items.append(f"... ({len(value) - n} more)")
            return items
        return value

    # Pattern to strip timestamps/IDs for repetitive content detection
    _TIMESTAMP_RE = re.compile(
        r"\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}[:\d.]*\S*"
        r"|\b\d{10,13}\b"  # unix timestamps
    )

    @classmethod
    def _tail_anomaly_truncate(cls, text: str, max_chars: int) -> str | None:
        """Detect highly repetitive content and preserve tail anomaly.

        If >50% of lines match a repeated pattern (after stripping timestamps)
        and the tail differs, keep a sample + full tail anomaly.
        Returns None if content is not repetitive.
        """
        lines = text.split("\n")
        if len(lines) < 10:
            return None

        # Fingerprint: strip timestamps/numbers, keep structure
        def _fp(line: str) -> str:
            stripped = cls._TIMESTAMP_RE.sub("", line.strip())
            # Also normalize varying numbers (latency, counts)
            stripped = re.sub(r"\d+", "#", stripped)
            return stripped[:40]

        fingerprints: dict[str, int] = {}
        for line in lines:
            fp = _fp(line)
            if fp:
                fingerprints[fp] = fingerprints.get(fp, 0) + 1

        if not fingerprints:
            return None

        top_fp, top_count = max(fingerprints.items(), key=lambda x: x[1])
        if top_count < len(lines) * 0.5:
            return None  # Not repetitive enough

        # Find non-matching tail lines (anomalies)
        tail_lines: list[str] = []
        for line in reversed(lines):
            if _fp(line) != top_fp:
                tail_lines.insert(0, line)
            else:
                break

        if not tail_lines:
            return None

        # Build: first few lines + count + tail anomaly
        tail_text = "\n".join(tail_lines)
        sample_count = 3
        head_lines = lines[:sample_count]
        head_text = "\n".join(head_lines)
        omitted = max(0, len(lines) - sample_count - len(tail_lines))

        # The tail anomaly is the payload this path exists to surface, so it is
        # reserved first. The old code sliced from the START on overflow, which
        # cut the anomaly off entirely — destroying what it set out to preserve.
        # When even the tail overflows the budget, keep its most-recent end
        # (where anomalies surface) together with the marker.
        marker = f"\n... ({omitted} similar lines omitted)\n"
        head_budget = max_chars - len(marker) - len(tail_text)
        if head_budget < 0:
            body = marker + tail_text
            return body[-max_chars:] if len(body) > max_chars else body
        head_kept = head_text if len(head_text) <= head_budget else head_text[:head_budget]
        return head_kept + marker + tail_text

    def _section_aware_truncate(
        self,
        text: str,
        max_chars: int,
        headings: list[re.Match],
        context_query: str | None = None,
    ) -> str:
        """Preserve information from ALL sections — minimum representation first.

        Strategy (eliminates head bias):
        1. Reserve minimum space: heading + first content line for EVERY section
        2. Distribute remaining budget:
           - With context_query: proportional to BM25 relevance scores
           - Without context_query: top-down sequential (original behavior)
        3. Detect and preserve summary/conclusion sections
        """
        # Parse sections: (title, body_text) pairs
        sections: list[tuple[str, str]] = []
        for i, m in enumerate(headings):
            start = m.start()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            title = m.group(1).strip()
            body = text[start:end].rstrip()
            sections.append((title, body))

        # Text before the first heading (preamble)
        preamble = text[: headings[0].start()].rstrip() if headings[0].start() > 0 else ""

        # Detect summary/conclusion as last section
        summary_idx = -1
        if sections:
            last_title = sections[-1][0]
            if self._SUMMARY_RE.search(last_title):
                summary_idx = len(sections) - 1

        # ── Phase 1: Minimum representation for EVERY section ──
        # Heading + first meaningful content line (guarantees tail section visibility)
        minimums: list[str] = []  # one per section
        min_used = 0
        for i, (title, body) in enumerate(sections):
            lines = body.split("\n")
            # Skip leading empty lines (match position may include \n before heading)
            while lines and not lines[0].strip():
                lines = lines[1:]
            kept = [lines[0]] if lines else [title]  # heading
            for line in lines[1:]:
                if line.strip() and not line.strip().startswith("#"):
                    kept.append(line)
                    break
            snippet = "\n".join(kept)
            minimums.append(snippet)
            min_used += len(snippet) + 2

        if preamble:
            min_used += len(preamble) + 2

        # ── Phase 2: Enrich sections with remaining budget ──
        footer_reserve = 80
        enrich_budget = max_chars - min_used - footer_reserve
        enriched: list[str] = list(minimums)  # start from minimums

        if enrich_budget > 0:
            # Compute per-section relevance scores if query is provided
            scores: list[float] | None = None
            if context_query and context_query.strip():
                raw_scores = self._scorer.score_sections(context_query, sections)
                if any(s > 0 for s in raw_scores):
                    scores = raw_scores

            if scores is not None:
                # ── Query-aware: allocate budget proportional to relevance ──
                self._enrich_by_relevance(
                    sections, minimums, enriched, scores, enrich_budget, summary_idx
                )
            else:
                # ── Original: enrich sections top-down ──
                for i, (_title, body) in enumerate(sections):
                    if i == summary_idx:
                        continue
                    extra = len(body) - len(minimums[i])
                    if extra <= 0:
                        continue
                    if extra <= enrich_budget:
                        enriched[i] = body
                        enrich_budget -= extra
                    else:
                        lines = body.split("\n")
                        kept = enriched[i].split("\n")
                        kept_set = set(kept)
                        for line in lines:
                            if line in kept_set:
                                continue
                            if len(line) + 1 > enrich_budget:
                                break
                            kept.append(line)
                            enrich_budget -= len(line) + 1
                        enriched[i] = "\n".join(kept)
                        break

        # ── Phase 3: Summary section enrichment ──
        if summary_idx >= 0:
            remaining = max_chars - sum(len(e) + 2 for e in enriched) - footer_reserve
            if remaining > 50:
                _, summary_body = sections[summary_idx]
                extra = len(summary_body) - len(enriched[summary_idx])
                if extra > 0:
                    if extra <= remaining:
                        enriched[summary_idx] = summary_body
                    else:
                        cut = len(enriched[summary_idx]) + remaining
                        # Find a word boundary near the cut point
                        while (
                            cut > len(enriched[summary_idx])
                            and cut < len(summary_body)
                            and summary_body[cut] not in " \n\t"
                        ):
                            cut -= 1
                        enriched[summary_idx] = summary_body[:cut]

        # ── Assemble ──
        parts: list[str] = []
        if preamble:
            parts.append(preamble)
        for i, section_text in enumerate(enriched):
            parts.append(section_text)

        body = "\n\n".join(parts)
        footer = f"\n(original: {len(text)} chars)"
        # Reserve the footer (and a sentinel when cutting) so neither is sliced
        # off; the old ``result[:max_chars]`` dropped the footer and tail
        # sections with no truncation marker when the minimums overflowed.
        return self._fit_with_footer(body, footer, max_chars)

    def _enrich_by_relevance(
        self,
        sections: list[tuple[str, str]],
        minimums: list[str],
        enriched: list[str],
        scores: list[float],
        budget: int,
        summary_idx: int,
    ) -> None:
        """Distribute enrichment budget proportional to relevance scores.

        Modifies *enriched* in-place. Higher-scored sections get more budget.
        Each section's "extra" is capped by (full body - minimum).
        """
        # Compute per-section extras and weights
        extras: list[int] = []
        weights: list[float] = []
        for i, (_title, body) in enumerate(sections):
            extra = max(0, len(body) - len(minimums[i]))
            extras.append(extra)
            # summary gets enriched in Phase 3; exclude from relevance allocation
            weights.append(scores[i] if i != summary_idx and extra > 0 else 0.0)

        total_weight = sum(weights)
        if total_weight <= 0:
            return  # nothing to distribute

        # Allocate budget proportionally, capped by each section's extra
        allocations = [0] * len(sections)
        remaining = budget
        for i in range(len(sections)):
            if weights[i] <= 0:
                continue
            share = int(budget * weights[i] / total_weight)
            allocations[i] = min(share, extras[i])
            remaining -= allocations[i]

        # Distribute leftover to highest-scored sections with remaining capacity
        if remaining > 0:
            ranked = sorted(range(len(sections)), key=lambda j: -scores[j])
            for i in ranked:
                if remaining <= 0:
                    break
                gap = extras[i] - allocations[i]
                if gap <= 0:
                    continue
                give = min(gap, remaining)
                allocations[i] += give
                remaining -= give

        # Apply allocations: enrich each section up to its allocation
        for i, (_title, body) in enumerate(sections):
            if allocations[i] <= 0:
                continue
            if allocations[i] >= extras[i]:
                enriched[i] = body
            else:
                # Partial: add lines until allocation exhausted
                lines = body.split("\n")
                kept = enriched[i].split("\n")
                kept_set = set(kept)
                alloc_left = allocations[i]
                for line in lines:
                    if line in kept_set:
                        continue
                    if len(line) + 1 > alloc_left:
                        break
                    kept.append(line)
                    alloc_left -= len(line) + 1
                enriched[i] = "\n".join(kept)

    def _code_aware_truncate(self, text: str, max_chars: int, boundaries: list[re.Match]) -> str:
        """Preserve signatures/names from ALL code blocks, not just the first ones.

        For code files: keeps full body of top functions + signature lines of rest.
        For SQL: keeps first query full + signature of remaining queries.
        """
        # Parse blocks: each boundary starts a logical block
        blocks: list[tuple[str, str]] = []  # (signature_line, full_body)
        for i, m in enumerate(boundaries):
            start = m.start()
            end = boundaries[i + 1].start() if i + 1 < len(boundaries) else len(text)
            sig = m.group(1).strip()
            body = text[start:end].rstrip()
            blocks.append((sig, body))

        # Preamble (imports, docstrings before first boundary)
        preamble = text[: boundaries[0].start()].rstrip() if boundaries[0].start() > 0 else ""

        # Phase 1: full blocks from top
        ratio = max_chars / len(text) if text else 1.0
        full_pct = min(0.80, 0.45 + ratio * 0.4)
        full_budget = int(max_chars * full_pct)

        parts: list[str] = []
        used = 0
        full_count = 0

        if preamble:
            # Keep preamble but cap it
            preamble_budget = min(len(preamble), full_budget // 3)
            if len(preamble) > preamble_budget:
                preamble = preamble[:preamble_budget] + "\n..."
            parts.append(preamble)
            used += len(preamble) + 1

        for i, (sig, body) in enumerate(blocks):
            if used + len(body) + 2 <= full_budget:
                parts.append(body)
                used += len(body) + 2
                full_count = i + 1
            else:
                break

        # Phase 2: signatures of remaining blocks
        remaining = [(sig, body) for i, (sig, body) in enumerate(blocks) if i >= full_count]
        if remaining:
            sig_budget = max_chars - used - 60
            sig_parts: list[str] = []
            sig_used = 0
            for sig, body in remaining:
                # Show signature + first non-empty body line
                body_lines = body.split("\n")
                sig_lines = [body_lines[0]]
                for line in body_lines[1:4]:  # up to 3 more lines
                    stripped = line.strip()
                    if stripped:
                        sig_lines.append(line)
                snippet = "\n".join(sig_lines)
                if sig_used + len(snippet) + 2 > sig_budget:
                    if sig_used + len(sig) + 4 <= sig_budget:
                        sig_parts.append(f"# {sig}")
                        sig_used += len(sig) + 4
                else:
                    sig_parts.append(snippet)
                    sig_used += len(snippet) + 2

            if sig_parts:
                parts.append(f"\n... ({len(remaining)} more blocks)\n")
                parts.extend(sig_parts)

        body = "\n\n".join(parts)
        footer = f"\n(original: {len(text)} chars)"
        return self._fit_with_footer(body, footer, max_chars)

    @staticmethod
    def _find_break(text: str, max_chars: int) -> int:
        if max_chars <= 0:
            return 0
        end = min(max_chars, len(text) - 1)
        floor = max(1, int(max_chars * 0.8))
        for i in range(end, floor - 1, -1):
            if i >= 1 and text[i - 1] in ".!?\n。！？" and (i >= len(text) or text[i] in " \n\t"):
                return i
        for i in range(end, floor - 1, -1):
            if i < len(text) and text[i] in " \n\t":
                return i
        return max_chars

    @staticmethod
    def _fit_with_footer(
        body: str, footer: str, max_chars: int, *, sentinel: str = "\n... (truncated)"
    ) -> str:
        """Assemble ``body + footer`` within ``max_chars`` without slicing the footer.

        Fixes the family of defects where ``(body + footer)[:max_chars]`` both
        overshot the budget (the footer was appended after the body already
        filled it) and silently dropped the footer / tail content with no
        marker. The footer — and, when a cut is needed, a truncation
        ``sentinel`` — are reserved out of the budget, and the body is cut at a
        line boundary when one is reasonably close.

        The result is always ``<= max_chars`` for any
        ``max_chars >= len(sentinel) + len(footer)``; for a pathologically tiny
        budget that cannot hold even the footer, it degrades to a hard slice.
        """
        full = body + footer
        if len(full) <= max_chars:
            return full
        reserve = len(sentinel) + len(footer)
        keep = max_chars - reserve
        if keep <= 0:
            return full[:max_chars]
        cut = body[:keep]
        nl = cut.rfind("\n")
        if nl >= keep // 2:
            cut = cut[:nl]
        return cut + sentinel + footer


@dataclass
class PendingSelection:
    """Stores original chunks while waiting for section selection."""

    chunks: dict[str, str]
    format: str
    created_at: float
    total_chars: int


class SelectiveCompressor:
    """2-phase compression: Phase 1 returns a TOC, Phase 2 returns selected sections.

    Phase 1 parses the input into named chunks (JSON keys, markdown sections,
    or text paragraphs), stores them keyed by a UUID, and returns a JSON
    table of contents. Phase 2 (``select(key, sections=[...])``) retrieves
    the selected chunks.

    Example — Phase 1 (markdown → TOC JSON, shape simplified)::

        Input:  "## Users\\n<120 chars>\\n\\n## Orders\\n<95 chars>"
        Output: {"type": "toc", "selection_key": "a1b2c3d4e5f67890", "format": "markdown",
                 "entries": [{"key": "users", "size": 120, "preview": "..."},
                             {"key": "orders", "size": 95, "preview": "..."}],
                 "hint": "Call stm_proxy_select_chunks(key=..., sections=[...]) to retrieve."}

    Example — Phase 2 (caller picks ``['users']``)::

        select(key="a1b2c3d4e5f67890", sections=["users"])
          → "## Users\\n<120 chars of actual content>"
    """

    def __init__(
        self,
        max_pending: int = 100,
        pending_ttl_seconds: float = 300.0,
        json_depth: int = 1,
        min_section_chars: int = 50,
        store: object | None = None,
        scorer: RelevanceScorer | None = None,
    ) -> None:
        self._max_pending = max_pending
        self._ttl = pending_ttl_seconds
        self._json_depth = json_depth
        self._min_section_chars = min_section_chars
        self._scorer = scorer or BM25Scorer()
        if store is not None:
            self._store = store
        else:
            from memtomem_stm.proxy.pending_store import InMemoryPendingStore

            self._store = InMemoryPendingStore()

    def compress(self, text: str, *, max_chars: int, context_query: str | None = None) -> str:
        if not text or len(text) <= max_chars:
            return text

        fmt, chunks = self._detect_and_parse(text)

        if len(chunks) <= 1:
            chunks = self._decompose_single_chunk(chunks, fmt)
            if len(chunks) <= 1:
                return TruncateCompressor().compress(
                    text, max_chars=max_chars, context_query=context_query
                )

        return self._store_and_build_toc(text, fmt, chunks, context_query, max_chars=max_chars)

    def compress_full_toc(
        self, text: str, *, max_chars: int, context_query: str | None = None
    ) -> str | None:
        fmt, chunks = self._detect_and_parse(text)
        if len(chunks) <= 1:
            chunks = self._decompose_single_chunk(chunks, fmt)
            if len(chunks) <= 1:
                return None
        return self._store_and_build_toc(text, fmt, chunks, context_query)

    # Largest per-entry preview length (the historical default).
    _MAX_PREVIEW = 80

    def _store_and_build_toc(
        self,
        text: str,
        fmt: str,
        chunks: dict[str, str],
        context_query: str | None = None,
        max_chars: int | None = None,
    ) -> str:
        selection_key = uuid.uuid4().hex[:16]

        self._store.put(
            selection_key,
            PendingSelection(
                chunks=chunks,
                format=fmt,
                created_at=time.monotonic(),
                total_chars=len(text),
            ),
        )
        self._evict()

        # Query-aware ordering: when a context query is supplied, surface the
        # most relevant sections first so the agent sees them at the top of the
        # TOC. Selection by key is unaffected — ``chunks`` still holds every
        # section. With no query (or no signal) the original insertion order is
        # preserved, which callers and tests rely on. Stable sort keeps ties in
        # insertion order. Order is decided ONCE here so the preview-budget
        # search below rebuilds the same entries in the same order.
        items = list(chunks.items())
        if context_query and context_query.strip():
            scores = self._scorer.score_sections(context_query, items)
            if any(s > 0 for s in scores):
                order = sorted(range(len(items)), key=lambda i: -scores[i])
                items = [items[i] for i in order]

        def build(preview_cap: int) -> str:
            entries = []
            for key, content in items:
                size = len(content)
                is_inline = size < self._min_section_chars
                # Inline short sections show their content in full; only the
                # longer-section preview is capped. ``preview_cap`` bounds that
                # preview so a many-section TOC can honor the char budget without
                # dropping entries — every section stays addressable by key,
                # which the two-phase select() protocol depends on.
                preview = content if is_inline else content[:preview_cap].replace("\n", " ")
                entries.append(
                    {
                        "key": key,
                        "type": self._infer_type(key, content, fmt),
                        "size": size,
                        "preview": preview,
                        "inline": is_inline,
                    }
                )
            toc = {
                "type": "toc",
                "selection_key": selection_key,
                "format": fmt,
                "total_chars": len(text),
                "ttl_seconds_remaining": int(self._ttl),
                "entries": entries,
                "hint": (
                    f"Call stm_proxy_select_chunks(key='{selection_key}', "
                    "sections=[...]) to retrieve."
                ),
            }
            return json.dumps(toc, ensure_ascii=False)

        full = build(self._MAX_PREVIEW)
        if max_chars is None or len(full) <= max_chars:
            return full

        # Over budget: shrink the uniform preview cap to fill as much of the
        # budget as possible while keeping EVERY entry. ``len(build(cap))`` is
        # non-decreasing in ``cap``, so a binary search finds the largest cap
        # that fits; ``build(0)`` (empty previews) is the floor. The entry COUNT
        # and the envelope are never reduced — at extreme section counts even the
        # zero-preview TOC may exceed the budget, which is accepted by design
        # (the manager exempts SELECTIVE from the ratio guard because the agent
        # retrieves full content via stm_proxy_select_chunks).
        lo, hi, best = 0, self._MAX_PREVIEW, build(0)
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = build(mid)
            if len(candidate) <= max_chars:
                best, lo = candidate, mid + 1
            else:
                hi = mid - 1
        return best

    def select(self, key: str, sections: list[str]) -> str:
        self._store.evict_expired(self._ttl)

        pending = self._store.get(key)
        if pending is None:
            return f"Selection key '{key}' not found or expired."

        self._store.touch(key)

        selected_parts: list[str] = []
        for section in sections:
            if section in pending.chunks:
                selected_parts.append(pending.chunks[section])

        if not selected_parts:
            available = list(pending.chunks.keys())
            return f"No matching sections found. Available: {available}"

        return "\n\n".join(selected_parts)

    def _detect_and_parse(self, text: str) -> tuple[str, dict[str, str]]:
        try:
            data = _mm_json_loads(text)
            if isinstance(data, dict):
                return "json", self._parse_json_dict(data, text)
            if isinstance(data, list):
                return "json", self._parse_json_array(data)
        except (json.JSONDecodeError, ValueError):
            pass

        if re.search(r"(?:^|\n)#{1,6}\s", text):
            return "markdown", self._parse_markdown(text)

        return "text", self._parse_text(text)

    def _parse_json_dict(
        self, data: dict[str, object], raw_text: str, prefix: str = "", depth: int = 0
    ) -> dict[str, str]:
        chunks: dict[str, str] = {}
        for key, value in data.items():
            full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
            if isinstance(value, dict) and depth < self._json_depth:
                nested = self._parse_json_dict(value, "", prefix=full_key, depth=depth + 1)
                chunks.update(nested)
            else:
                chunks[full_key] = json.dumps(value, ensure_ascii=False, indent=2)
        return chunks

    def _parse_json_array(self, data: list[object]) -> dict[str, str]:
        chunks: dict[str, str] = {}
        for i, item in enumerate(data):
            chunks[f"[{i}]"] = json.dumps(item, ensure_ascii=False, indent=2)
        return chunks

    def _parse_markdown(self, text: str) -> dict[str, str]:
        chunks: dict[str, str] = {}
        parts = re.split(r"(?:^|\n)(#{1,6}\s+.+)", text)
        current_heading = ""
        current_content: list[str] = []

        for part in parts:
            heading_match = re.match(r"^(#{1,6})\s+(.+)$", part.strip())
            if heading_match:
                if current_heading or current_content:
                    content = "\n".join(current_content).strip()
                    if content:
                        chunks[current_heading or "Preamble"] = content
                current_heading = heading_match.group(2).strip()
                current_content = []
            else:
                current_content.append(part)

        if current_heading or current_content:
            content = "\n".join(current_content).strip()
            if content:
                chunks[current_heading or "Preamble"] = content

        return chunks

    def _parse_text(self, text: str) -> dict[str, str]:
        paragraphs = re.split(r"\n\n+", text)
        chunks: dict[str, str] = {}
        for i, para in enumerate(paragraphs):
            stripped = para.strip()
            if stripped:
                chunks[f"Paragraph {i + 1}"] = stripped
        return chunks

    def _decompose_single_chunk(self, chunks: dict[str, str], fmt: str) -> dict[str, str]:
        if not chunks:
            return chunks
        key, value = next(iter(chunks.items()))
        try:
            parsed = _mm_json_loads(value)
        except (json.JSONDecodeError, ValueError):
            return chunks
        if isinstance(parsed, dict) and len(parsed) > 1:
            return {
                f"{key}.{k}": json.dumps(v, ensure_ascii=False, indent=2) for k, v in parsed.items()
            }
        if isinstance(parsed, list) and len(parsed) > 1:
            return {
                f"{key}[{i}]": json.dumps(item, ensure_ascii=False, indent=2)
                for i, item in enumerate(parsed)
            }
        return chunks

    def _infer_type(self, key: str, content: str, fmt: str) -> str:
        if fmt == "json":
            if content.startswith("{"):
                return "object"
            if content.startswith("["):
                return "array"
            return "string"
        if fmt == "markdown":
            return "heading"
        return "paragraph"

    def _evict(self) -> None:
        self._store.evict_expired(self._ttl)
        self._store.evict_oldest(self._max_pending)


class FieldExtractCompressor:
    """JSON: preserve key structure + truncate values. Text: head + tail lines.

    Example — JSON (keys kept, long values truncated)::

        Input:  {"user": {"name": "Alice",
                          "bio": "A very long biography spanning many sentences..."}}
        Output: {"user": {"name": "Alice", "bio": "A very long biograp..."}}

    Example — plain text (head + tail, middle elided)::

        Input:  "Line 1\\nLine 2\\nLine 3\\nLine 4\\nLine 5"
        Output: "Line 1\\nLine 2\\n...\\nLine 5"
    """

    def compress(self, text: str, *, max_chars: int) -> str:
        if not text or len(text) <= max_chars:
            return text
        try:
            data = _mm_json_loads(text)
            return self._compress_json(data, max_chars)
        except (json.JSONDecodeError, ValueError):
            pass
        return self._compress_text(text, max_chars)

    def _compress_json(self, data: object, max_chars: int) -> str:
        if isinstance(data, dict):
            preview: object = self._extract_dict(data, max_chars)
        elif isinstance(data, list):
            preview = self._preview_list(data)
        else:
            preview = data
        indent = 2 if isinstance(data, (dict, list)) else None
        result = json.dumps(preview, ensure_ascii=False, indent=indent)
        if len(result) <= max_chars:
            return result
        # Final tier: emit VALID JSON within budget. The old
        # ``result[:max_chars] + "\n... (truncated)"`` overshot the budget by the
        # suffix length AND cut the JSON mid-token (invalid output); a top-level
        # array additionally appended a non-JSON text marker. ``_fit_extracted``
        # re-derives a valid, within-budget form from the original data.
        return self._fit_extracted(data, max_chars)

    def _preview_list(self, data: list) -> list[object]:
        """Head preview of a top-level array as VALID JSON: the first items, then
        an in-array omitted-count marker (an extra string element). The pre-fix
        code appended ``"\\n... (N items total ...)"`` as text *after*
        ``json.dumps``, which left the whole output unparseable for any array
        over the 5-item preview."""
        n = len(data)
        preview_n = min(5, n)
        preview: list[object] = [
            self._preview_dict(item) if isinstance(item, dict) else item
            for item in data[:preview_n]
        ]
        if n > preview_n:
            preview.append(f"... ({n - preview_n} of {n} items omitted)")
        return preview

    def _extract_dict(self, data: dict, budget: int) -> dict:
        """Budget-aware recursive dict extraction — preserves all keys with depth.

        Scalar/small values are placed first so they survive truncation.
        Large arrays/dicts are placed after, allowing them to be cut if needed.
        """
        # Separate scalar (small) values from large collections
        scalar_keys: list[str] = []
        collection_keys: list[str] = []
        for key, value in data.items():
            if isinstance(value, (list, dict)):
                collection_keys.append(key)
            else:
                scalar_keys.append(key)

        # Process scalars first (survive truncation), then collections
        summary: dict = {}
        for key in scalar_keys + collection_keys:
            value = data[key]
            if isinstance(value, str) and len(value) > 80:
                if "```" in value:
                    limit = min(len(value), 500)
                    fence_end = value.rfind("```", 0, limit)
                    summary[key] = (
                        value[: fence_end + 3] if fence_end > 80 else value[:limit] + "..."
                    )
                else:
                    summary[key] = value[:80] + "..."
            elif isinstance(value, list):
                preview_n = min(5, len(value))
                items: list[object] = []
                for item in value[:preview_n]:
                    if isinstance(item, str) and len(item) > 80:
                        items.append(item[:80] + "...")
                    elif isinstance(item, dict):
                        items.append(self._preview_dict(item))
                    elif isinstance(item, list):
                        items.append(f"[{len(item)} items]")
                    else:
                        items.append(item)
                remaining = len(value) - preview_n
                if remaining > 0:
                    items.append(f"... ({remaining} more)")
                summary[key] = items
            elif isinstance(value, dict):
                # Recurse one level — preserve all keys of nested dicts
                summary[key] = self._preview_dict(value)
            else:
                summary[key] = value
        return summary

    @staticmethod
    def _preview_dict(d: dict, max_keys: int = 6, max_value_len: int = 80) -> dict:
        """Show first N key-value pairs with truncated values, hint at rest."""
        preview: dict = {}
        keys = list(d.keys())
        for k in keys[:max_keys]:
            v = d[k]
            if isinstance(v, str) and len(v) > max_value_len:
                preview[k] = v[:max_value_len] + "..."
            elif isinstance(v, dict):
                # One more level of preview for nested dicts
                inner: dict = {}
                inner_keys = list(v.keys())
                for ik in inner_keys[:4]:
                    iv = v[ik]
                    if isinstance(iv, str) and len(iv) > 40:
                        inner[ik] = iv[:40] + "..."
                    elif isinstance(iv, (dict, list)):
                        inner[ik] = f"({type(iv).__name__}, {len(iv)})"
                    else:
                        inner[ik] = iv
                if len(inner_keys) > 4:
                    inner[f"...{len(inner_keys) - 4} more"] = "..."
                preview[k] = inner
            elif isinstance(v, list):
                if len(v) <= 3:
                    preview[k] = v
                else:
                    preview[k] = v[:3] + [f"... ({len(v) - 3} more)"]
            else:
                preview[k] = v
        remaining = len(keys) - max_keys
        if remaining > 0:
            preview[f"...{remaining} more"] = "..."
        return preview

    def _take(self, value: object, budget: int) -> object:
        """Greedy prefix-fill: keep the leading, full-detail content of ``value``
        whose serialized cost fits ``budget`` (COMPACT) chars, dropping the tail
        into a valid omitted-count marker (an extra ``_truncated`` dict member or
        an in-array string element, counted against the ORIGINAL length).

        STRICT budget for containers: ``len(json.dumps(_take(value, b))) <= b``
        always — every child (including the first) is gated, and a child is
        accepted only if it still fits, so a caller can hand ``_take`` the hard
        ``max_chars`` directly and trust the result. Leading full-length scalars
        (names, ids, roles) survive untouched — only a boundary string is ever
        truncated. A non-string scalar is returned whole when its literal fits,
        otherwise it becomes the scalar stub ``""`` so sibling keys can still
        survive.
        """
        if isinstance(value, str):
            if len(value) + 2 <= budget:  # +2 for the surrounding quotes
                return value
            keep = budget - 5  # quotes (2) + "..." (3)
            return value[:keep] + "..." if keep > 0 else ""
        if not isinstance(value, (dict, list)):
            literal = json.dumps(value, ensure_ascii=False)
            return value if len(literal) <= budget else ""

        if len(json.dumps(value, ensure_ascii=False)) <= budget:
            return value

        # Collision-safe marker key, checked against ALL original keys (a real
        # "_truncated" key can be in the *dropped* tail, invisible to ``out``).
        marker_key = "_truncated"
        if isinstance(value, dict):
            is_dict = True
            items: list = list(value.items())
            existing = set(value)
            while marker_key in existing:
                marker_key += "_"
        else:
            is_dict = False
            items = list(enumerate(value))
        n = len(items)

        if not is_dict:
            out_list: list = []

            def list_fits(candidate: list) -> bool:
                return len(json.dumps(candidate, ensure_ascii=False)) <= budget

            def is_starved(child: object, source: object) -> bool:
                if child == "" and not isinstance(source, (dict, list, str)):
                    return False
                return child in ({}, [], "") and source not in (None, {}, [], "", 0, False)

            def best_child(source: object, suffix: list[str]) -> object | None:
                lo, hi, best = 0, budget, None
                while lo <= hi:
                    mid = (lo + hi) // 2
                    child = self._take(source, mid)
                    if list_fits(out_list + [child] + suffix):
                        best, lo = child, mid + 1
                    else:
                        hi = mid - 1
                return best

            for i, (_k, v) in enumerate(items):
                marker_after = f"... ({n - i - 1} of {n} items omitted)" if i < n - 1 else None
                child = best_child(v, [marker_after] if marker_after else [])
                if child is None or is_starved(child, v):
                    marker = f"... ({n - i} of {n} items omitted)"
                    if list_fits(out_list + [marker]):
                        out_list.append(marker)
                    else:
                        child = best_child(v, [])
                        if child is not None and not is_starved(child, v):
                            out_list.append(child)
                            continue
                    break
                out_list.append(child)
            return out_list

        out_dict: dict = {}

        def dict_fits(candidate: dict) -> bool:
            return len(json.dumps(candidate, ensure_ascii=False)) <= budget

        def dict_starved(child: object, source: object) -> bool:
            if child == "" and not isinstance(source, (dict, list, str)):
                return False
            return child in ({}, [], "") and source not in (None, {}, [], "", 0, False)

        def best_value(key: object, source: object, suffix: dict[str, str]) -> object | None:
            lo, hi, best = 0, budget, None
            while lo <= hi:
                mid = (lo + hi) // 2
                child = self._take(source, mid)
                candidate = dict(out_dict)
                candidate[key] = child
                candidate.update(suffix)
                if dict_fits(candidate):
                    best, lo = child, mid + 1
                else:
                    hi = mid - 1
            return best

        for i, (k, v) in enumerate(items):
            marker_after = {marker_key: f"{n - i - 1} of {n} keys omitted"} if i < n - 1 else {}
            child = best_value(k, v, marker_after)
            if child is None or dict_starved(child, v):
                marker = f"{n - i} of {n} keys omitted"
                candidate = dict(out_dict)
                candidate[marker_key] = marker
                if dict_fits(candidate):
                    out_dict[marker_key] = marker
                else:
                    child = best_value(k, v, {})
                    if child is not None and not dict_starved(child, v):
                        out_dict[k] = child
                        continue
                break
            out_dict[k] = child
        return out_dict

    @staticmethod
    def _stub_value(value: object) -> object:
        """Smallest placeholder that keeps a key visible: a shape stub for a
        container, an empty string for a string, the value itself for a small
        scalar."""
        if isinstance(value, dict):
            return f"{{{len(value)} keys}}" if value else {}
        if isinstance(value, list):
            return f"[{len(value)} items]" if value else []
        if isinstance(value, str):
            return ""
        if len(json.dumps(value, ensure_ascii=False)) > 80:
            return ""
        return value  # int / float / bool / None

    def _fit_extracted(self, data: object, max_chars: int) -> str:
        """Re-derive a VALID, within-budget form from the ORIGINAL data, filling
        the budget with as much leading full-detail content as fits.

        Output is COMPACT JSON (no indent) so ``_take``'s compact cost
        accounting matches the measured length exactly — the exact match makes
        length monotonic in the soft budget, so the search fills ``max_chars``
        instead of landing on a collapse cliff.

        Top-level dict: first lay down a *skeleton* — every key with a minimal
        stub value — so no top-level key vanishes while the budget allows it
        (FieldExtract's "preserve key structure" contract). Then restore short
        scalar values first before enriching keys with as much full-detail
        content (via ``_take``) as the remaining budget allows — values like
        names/ids/roles survive in full rather than every value being shrunk to
        a stub.
        Only when the stubs themselves overflow are trailing keys dropped into a
        valid collision-safe marker. List / scalar roots binary-search ``_take``
        directly. Floors to ``{}`` / ``[]`` / ``""`` / ``null``.
        """

        def dump(obj: object) -> str:
            return json.dumps(obj, ensure_ascii=False)

        if isinstance(data, dict) and data:
            items = list(data.items())
            skeleton = {k: self._stub_value(v) for k, v in items}
            if len(dump(skeleton)) <= max_chars:
                # Enrich keys to fill the budget, but enrich SCALARS before
                # collections (mirrors _extract_dict's scalar-first ordering):
                # cheap, high-value scalar fields (ids, names, flags) must
                # survive a tight budget even when large nested sections precede
                # them in document order. The output dict keeps the original key
                # order — only the enrichment *priority* is reordered.
                out = dict(skeleton)
                scalars = [(k, v) for k, v in items if not isinstance(v, (dict, list))]
                collections = [(k, v) for k, v in items if isinstance(v, (dict, list))]
                for k, v in sorted(scalars, key=lambda item: len(dump(item[1]))):
                    trial = dict(out)
                    trial[k] = v
                    if len(dump(trial)) <= max_chars:
                        out[k] = v

                for k, v in scalars + collections:
                    if out[k] == v:
                        continue
                    lo, hi, best_v = 0, len(dump(v)), out[k]
                    while lo <= hi:
                        mid = (lo + hi) // 2
                        trial = dict(out)
                        trial[k] = self._take(v, mid)
                        if len(dump(trial)) <= max_chars:
                            best_v, lo = trial[k], mid + 1
                        else:
                            hi = mid - 1
                    out[k] = best_v
                return dump(out)

            # Even the all-stub skeleton overflows — keep the leading keys that
            # fit + a valid marker. ``len`` is monotonic in ``keep``. The marker
            # key is checked against ALL original keys, not just the kept prefix:
            # a real "_truncated" key can sit in the *dropped* tail (invisible to
            # ``kept``), and reusing its name would clobber/shadow it.
            existing = set(data)
            marker = "_truncated"
            while marker in existing:
                marker += "_"
            lo, hi, best = 0, len(items) - 1, None
            while lo <= hi:
                keep = (lo + hi) // 2
                kept = {k: skeleton[k] for k, _ in items[:keep]}
                kept[marker] = f"{len(items) - keep} of {len(items)} keys omitted"
                candidate = dump(kept)
                if len(candidate) <= max_chars:
                    best, lo = candidate, keep + 1
                else:
                    hi = keep - 1
            return best if best is not None else "{}"

        if not isinstance(data, (dict, list, str)):
            # Non-string scalar root (number / bool / null). Emit it verbatim
            # when it fits; otherwise degrade to a truncated STRING of the
            # literal. Nested oversized scalars use ``_take``'s empty-string stub,
            # but root scalars should stay visibly truncated rather than
            # disappearing behind an unlabelled empty value.
            literal = json.dumps(data, ensure_ascii=False)
            if len(literal) <= max_chars:
                return literal
            for k in range(len(literal), -1, -1):
                candidate = json.dumps(literal[:k], ensure_ascii=False)
                if len(candidate) <= max_chars:
                    return candidate
            return '""'

        # List or string root: ``_take`` is strict (<= budget) for containers, so
        # a single call at the hard budget fills it — no search, no cliff.
        candidate = dump(self._take(data, max_chars))
        if len(candidate) <= max_chars:
            return candidate

        # Even the most compact ``_take`` form overflows. Floor to the smallest
        # valid JSON within budget.
        if isinstance(data, dict):
            return "{}"
        if isinstance(data, list):
            return "[]"
        if isinstance(data, str):
            for k in range(len(data), -1, -1):
                candidate = json.dumps(data[:k], ensure_ascii=False)
                if len(candidate) <= max_chars:
                    return candidate
            return '""'
        return '""'

    def _compress_text(self, text: str, max_chars: int) -> str:
        lines = text.split("\n")
        if len(lines) <= 10:
            return self._fit_text(text, max_chars)
        head_count = max(3, len(lines) // 10)
        tail_count = max(3, len(lines) // 10)
        head = "\n".join(lines[:head_count])
        tail = "\n".join(lines[-tail_count:])
        omitted = len(lines) - head_count - tail_count
        summary = _content_summary(text)
        result = f"{head}\n... ({omitted} lines omitted){summary} ...\n{tail}"
        return self._fit_text(result, max_chars)

    @staticmethod
    def _fit_text(text: str, max_chars: int) -> str:
        """Within-budget text fit. Text has no JSON contract, so a boundary cut
        is acceptable — but reserve room for the suffix so the result never
        exceeds ``max_chars`` (the old ``slice + suffix`` overshot by the suffix
        length)."""
        if len(text) <= max_chars:
            return text
        suffix = "\n... (truncated)"
        if max_chars < len(suffix):
            return text[:max_chars]
        return text[: max_chars - len(suffix)] + suffix


class SchemaPruningCompressor:
    """JSON schema-preserving pruner — keeps ALL keys, limits values.

    Strategy: recursively walk JSON tree, preserving the full key structure.
    Arrays are sampled (first 2 + last 1 + count), strings are capped.
    At a normal budget this represents every configuration field, every nested
    key, and every data relationship in the output.

    When the budget physically cannot hold the full schema, the final tier
    (``_fit_minimal``) degrades gracefully and always emits valid JSON within
    budget: the deepest nesting collapses to shape stubs (``{N keys}`` /
    ``[N items]``) first, and only if the top-level keys themselves overflow are
    whole trailing keys/items dropped into a marker. So "all keys / every nested
    key" is the normal-budget guarantee, not an unconditional one.

    Example — nested config with a long array (all keys preserved, array sampled)::

        Input:  {"db": {"host": "prod", "pool": {"min": 2, "max": 20}},
                 "servers": ["s1", "s2", "s3", "s4", "s5"]}
        Output: {"db": {"host": "prod", "pool": {"min": 2, "max": 20}},
                 "servers": ["s1", "s2", "... (2 items omitted)", "s5"]}
    """

    def __init__(
        self,
        max_string: int = 80,
        max_array_items: int = 3,
        scorer: RelevanceScorer | None = None,
    ) -> None:
        self._max_string = max_string
        self._max_array = max_array_items
        self._scorer = scorer or BM25Scorer()

    def compress(self, text: str, *, max_chars: int, context_query: str | None = None) -> str:
        if not text or len(text) <= max_chars:
            return text
        try:
            data = _mm_json_loads(text)
        except (json.JSONDecodeError, ValueError):
            return TruncateCompressor(scorer=self._scorer).compress(
                text, max_chars=max_chars, context_query=context_query
            )

        # Query-aware: for a top-level object, spend the value budget on the
        # keys most relevant to the query — relevant subtrees keep longer
        # strings and more array items, irrelevant ones are pruned harder.
        # Every key is still emitted (the schema invariant) and key order is
        # untouched. With no query, no BM25 signal, a single-key dict, or a
        # non-object root the uniform path below runs and the output is
        # byte-for-byte identical to the pre-query behavior. When the budget is
        # too tight even for the weighted tiers, _compress_weighted returns None
        # and we likewise fall through to the uniform minimal/final tier.
        if context_query and context_query.strip() and isinstance(data, dict) and len(data) >= 2:
            relevant = self._relevant_keys(data, context_query)
            if relevant is not None:
                weighted = self._compress_weighted(data, max_chars, relevant)
                if weighted is not None:
                    return weighted

        # Iteratively reduce detail until output fits budget
        for max_str in (self._max_string, 40, 20):
            pruned = self._prune(data, max_str=max_str)
            result = json.dumps(pruned, ensure_ascii=False, indent=2)
            if len(result) <= max_chars:
                return result

        # Final tier: emit VALID JSON within the budget. The old
        # ``result[:max_chars] + "... (pruned)"`` hard slice cut JSON mid-token
        # (invalid output) and overshot the budget by the suffix length.
        return self._fit_minimal(data, max_chars)

    def _relevant_keys(self, data: dict, context_query: str) -> set[str] | None:
        """Top-level keys whose subtree scores against the query.

        Returns ``None`` when no key scores (no BM25 signal), which tells the
        caller to fall back to the uniform path for byte-identical output.
        """
        sections = [(k, json.dumps(v, ensure_ascii=False)) for k, v in data.items()]
        scores = self._scorer.score_sections(context_query, sections)
        if not any(s > 0 for s in scores):
            return None
        return {k for (k, _), s in zip(sections, scores) if s > 0}

    def _compress_weighted(self, data: dict, max_chars: int, relevant: set[str]) -> str | None:
        """Prune relevant keys gently and irrelevant keys hard, tightening to fit.

        The first tier keeps full default detail everywhere — identical to the
        uniform path — so when the whole object fits there is no needless
        pruning of irrelevant keys. Only under real budget pressure do later
        tiers cap irrelevant subtrees first (relevant keys stay at full detail),
        and finally trim relevant keys too. Every key survives and key order is
        preserved.

        Returns ``None`` when even the tightest tier overflows ``max_chars`` — at
        that point all detail is already minimal and there is no relevance
        preference left to keep, so the caller falls through to the uniform
        minimal/final tier rather than this method carrying its own overflow
        handling (which would duplicate the uniform path's truncation).
        """
        # (relevant_max_str, irrelevant_max_str, irrelevant_max_array)
        # irrelevant_max_array stays >= 2: _prune samples "first 2 + last 1", so
        # max_array=1 on a 2-item array duplicates the tail and emits a bogus
        # "(-1 items omitted)" marker (oversized, not pruned). The value lever
        # for irrelevant keys is max_string, not the array cap.
        tiers = (
            (self._max_string, self._max_string, self._max_array),
            (self._max_string, 40, 2),
            (self._max_string, 20, 2),
            (40, 12, 2),
            (20, 10, 2),
        )
        for rel_str, irr_str, irr_arr in tiers:
            pruned = {
                k: self._prune(
                    v,
                    max_str=(rel_str if k in relevant else irr_str),
                    max_array=(self._max_array if k in relevant else irr_arr),
                )
                for k, v in data.items()
            }
            result = json.dumps(pruned, ensure_ascii=False, indent=2)
            if len(result) <= max_chars:
                return result

        return None

    def _prune(
        self,
        data: object,
        max_str: int = 80,
        max_array: int | None = None,
        max_depth: int | None = None,
        _depth: int = 0,
    ) -> object:
        ma = max_array if max_array is not None else self._max_array
        # Depth cap (final-tier only): collapse a nested container to a compact
        # shape stub so every shallower key still survives. ``max_depth=None``
        # (the default for all earlier tiers) keeps full depth — byte-identical
        # to the pre-depth behavior.
        if max_depth is not None and _depth >= max_depth:
            if isinstance(data, dict):
                return f"{{{len(data)} keys}}"
            if isinstance(data, list):
                return f"[{len(data)} items]"
        if isinstance(data, dict):
            return {k: self._prune(v, max_str, ma, max_depth, _depth + 1) for k, v in data.items()}
        if isinstance(data, list):
            n = len(data)
            if n <= ma:
                return [self._prune(item, max_str, ma, max_depth, _depth + 1) for item in data]
            # First 2 + last 1 + count (preserves head and tail anomalies)
            head = [
                self._prune(data[i], max_str, ma, max_depth, _depth + 1) for i in range(min(2, n))
            ]
            tail = [self._prune(data[-1], max_str, ma, max_depth, _depth + 1)]
            omitted = n - min(2, n) - 1
            return head + [f"... ({omitted} items omitted)"] + tail
        if isinstance(data, str) and len(data) > max_str:
            # Never let the "..." ellipsis make the value LONGER than the
            # original (e.g. a 2-char string at max_str=1). This keeps a very
            # small max_str (used by the final tier to shrink values before
            # dropping keys) from ever inflating short strings.
            truncated = data[:max_str] + "..."
            return truncated if len(truncated) < len(data) else data
        return data

    def _fit_minimal(self, data: object, max_chars: int) -> str:
        """Fit ``data`` into ``max_chars`` as VALID JSON at the final tier.

        Reached when even minimal per-value pruning overflows because ``_prune``
        keeps every key and never drops whole items (the all-keys schema
        invariant). Degrades gracefully, validity and budget always held:

        1. Keep EVERY top-level key, escalating value compression until it fits:
           first collapse the deepest nested levels to shape stubs (``{N keys}``
           / ``[N items]``), then shrink scalar strings — so the agent still sees
           the full top-level schema, with nested/string detail surviving in
           proportion to the budget.
        2. Only when the top-level keys *themselves* cannot fit (a wide object /
           array, where the key names alone exceed the budget) drop whole
           trailing keys/items into a valid marker.
        3. Floor: ``{}`` / ``[]`` / ``""`` / ``null`` — at a sub-token budget
           validity wins over the length cap (a budget the manager retention
           ladder never produces). Keys/items are dropped by position; at this
           tier all detail is uniform, so there is no relevance preference left.

        Scalar roots have no keys to preserve, so they bypass the container
        tiers and shrink the value directly (a string keeps a prefix; any other
        scalar floors to ``null``).
        """
        if isinstance(data, (dict, list)) and data:
            # Tier 1: keep EVERY key/item, escalating value compression until it
            # fits — collapse the deepest nesting to shape stubs, then shrink
            # scalar strings (10 -> 4 -> 0 chars). Dropping a key is a last resort
            # (Tier 2); a smaller stub value is always preferred. (max_str,
            # max_depth); ``max_depth=None`` == full depth == byte-identical to
            # the pre-final-tier behavior.
            for max_str, max_depth in ((10, None), (10, 3), (10, 2), (10, 1), (4, 1), (0, 1)):
                pruned = self._prune(data, max_str=max_str, max_array=2, max_depth=max_depth)
                result = json.dumps(pruned, ensure_ascii=False, indent=2)
                if len(result) <= max_chars:
                    return result

            # Tier 2: even the most compact all-keys form overflows — the
            # top-level keys themselves don't fit, so drop whole trailing ones.
            if isinstance(data, dict):
                # Most compact value form (stubbed nesting + minimal strings) so
                # as many keys as possible survive before any are dropped.
                items = list(self._prune(data, max_str=0, max_array=2, max_depth=1).items())
                total = len(items)
                # Collision-safe marker key: never clobber (or repurpose as the
                # marker) a real top-level "_pruned" field, which would skew the
                # omitted count and replace the user's key.
                marker = "_pruned"
                existing = {k for k, _ in items}
                while marker in existing:
                    marker += "_"

                def _candidate(keep: int) -> str:
                    kept = dict(items[:keep])
                    kept[marker] = f"{total - keep} of {total} keys omitted"
                    return json.dumps(kept, ensure_ascii=False, indent=2)

                # ``len(_candidate(keep))`` is monotonic in ``keep`` (each extra
                # key adds far more than the marker's digit change), so
                # binary-search the largest prefix that fits instead of dumping
                # every prefix — O(log n) serializations, not O(n) (a wide object
                # used to stall here).
                lo, hi, best = 0, total - 1, None
                while lo <= hi:
                    mid = (lo + hi) // 2
                    candidate = _candidate(mid)
                    if len(candidate) <= max_chars:
                        best, lo = candidate, mid + 1
                    else:
                        hi = mid - 1
                return best if best is not None else "{}"

            # list: count against the ORIGINAL length — _prune already samples a
            # long list, so counting the sampled form understates omissions by
            # orders of magnitude. Show head elements (most compact) + an exact
            # marker.
            total = len(data)
            for keep in range(min(2, total), -1, -1):
                shown: list[object] = [
                    self._prune(data[i], max_str=0, max_array=2, max_depth=1) for i in range(keep)
                ]
                shown.append(f"... ({total - keep} of {total} items omitted)")
                candidate = json.dumps(shown, ensure_ascii=False, indent=2)
                if len(candidate) <= max_chars:
                    return candidate
            return "[]"

        # Scalar root or empty container: emit the shortest VALID token that
        # fits. An empty object/array keeps its type; a string keeps a prefix
        # (down to ``""``); any other scalar floors to a valid ``null`` rather
        # than a mid-token slice — validity wins over the cap (as for {}/[]/"").
        if isinstance(data, dict):
            return "{}"
        if isinstance(data, list):
            return "[]"
        pruned = self._prune(data, max_str=10)
        if isinstance(pruned, str):
            result = json.dumps(pruned, ensure_ascii=False)
            if len(result) <= max_chars:
                return result
            for k in range(len(pruned), -1, -1):
                candidate = json.dumps(pruned[:k], ensure_ascii=False)
                if len(candidate) <= max_chars:
                    return candidate
            return '""'
        return "null"


class SkeletonCompressor:
    """Markdown skeleton — preserves ALL headings + structural lines.

    For documents with many parallel sections (API docs, changelogs),
    keeps the full document skeleton so no section is completely lost.
    Body content is aggressively trimmed to heading + first key line only.

    Example — API reference with many endpoints (every heading survives)::

        Input:  "## GET /users\\nReturns list of users.\\n<20 more detail lines>\\n\\n"
                "## POST /users\\nCreates user.\\n<15 more detail lines>\\n\\n"
                "## DELETE /users/:id\\nRemoves user.\\n<10 more detail lines>"
        Output: "## GET /users\\nReturns list of users.\\n\\n"
                "## POST /users\\nCreates user.\\n\\n"
                "## DELETE /users/:id\\nRemoves user."
    """

    _HEADING_RE = re.compile(r"^(#{1,6}\s.+)$", re.MULTILINE)

    def __init__(self, scorer: RelevanceScorer | None = None) -> None:
        self._scorer = scorer or BM25Scorer()

    def compress(self, text: str, *, max_chars: int, context_query: str | None = None) -> str:
        """Keep all headings + first content line per section.

        When a context query is supplied, the per-section content budget is
        weighted toward the sections most relevant to the query so they retain
        more body lines; every heading still survives and section order is
        preserved. With no query (or no BM25 signal) the budget is split evenly
        and the output is byte-for-byte identical to the pre-query behavior.
        """
        if not text or len(text) <= max_chars:
            return text

        headings = list(self._HEADING_RE.finditer(text))
        if len(headings) < 2:
            return TruncateCompressor(scorer=self._scorer).compress(
                text, max_chars=max_chars, context_query=context_query
            )

        # Slice each section once into (heading_line, body_lines).
        sections: list[tuple[str, list[str]]] = []
        for i, m in enumerate(headings):
            sec_start = m.start()
            sec_end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            lines = text[sec_start:sec_end].rstrip().split("\n")
            body_lines = [ln for ln in lines[1:] if ln.strip()]
            sections.append((lines[0], body_lines))

        budgets = self._section_budgets(sections, max_chars, context_query)

        # Build sections: heading + first content lines up to the per-section
        # budget. Keep the per-section line lists (not pre-joined) so the
        # degraded path can shed body and whole heading lines independently.
        kept_sections: list[list[str]] = []
        total_body_trimmed = 0
        total_body_chars = 0
        for (heading, body_lines), budget in zip(sections, budgets):
            kept = [heading]  # always keep the heading
            kept_chars = len(heading)

            # Measure total body content (non-empty, non-heading lines)
            section_body_chars = sum(len(ln) for ln in body_lines)
            total_body_chars += section_body_chars

            # Add content lines until per-section budget
            for line in body_lines:
                if kept_chars + len(line) + 1 > budget:
                    break
                kept.append(line)
                kept_chars += len(line) + 1

            total_body_trimmed += max(0, section_body_chars - (kept_chars - len(heading)))
            kept_sections.append(kept)

        result = "\n\n".join("\n".join(kept) for kept in kept_sections)
        result += self._footer(len(text), total_body_trimmed, len(headings))
        if len(result) <= max_chars:
            return result

        # The assembled skeleton overshoots the budget. Degrade gracefully
        # rather than ``result[:max_chars]`` (which dropped trailing headings,
        # cut a heading mid-line, and sliced the footer marker).
        return self._fit_skeleton(
            sections, budgets, len(text), len(headings), total_body_chars, max_chars
        )

    def _section_budgets(
        self,
        sections: list[tuple[str, list[str]]],
        max_chars: int,
        context_query: str | None,
    ) -> list[int]:
        """Per-section content budget: even split unless a query reweights it.

        Without a query (or with no BM25 signal) every section gets the same
        ``max(60, (max_chars - 80) // n)`` budget as before — byte-identical.
        With a query, every section keeps the 60-char floor and the remaining
        budget is handed out in proportion to BM25 relevance, so the most
        relevant sections retain more body lines. The weighted total never
        exceeds the even-split total, so query-awareness adds no overshoot.
        """
        n = len(sections)
        content_budget = max_chars - 80
        base = max(60, content_budget // n)
        if not (context_query and context_query.strip()):
            return [base] * n

        scored = [(heading, "\n".join(body)) for heading, body in sections]
        # Clamp to non-negative: BM25 scores are always >= 0 (no-op, preserves
        # byte-identity), but an EmbeddingScorer returns cosine similarities that
        # can be negative. Raw negatives would skew the proportional split into
        # negative/huge per-section budgets, overfilling early sections so the
        # final hard slice drops trailing headings — breaking the skeleton's
        # every-heading-survives invariant.
        scores = [max(0.0, s) for s in self._scorer.score_sections(context_query, scored)]
        total = sum(scores)
        if total <= 0:
            return [base] * n

        floor = 60
        remainder = max(0, content_budget - floor * n)
        return [floor + int(remainder * s / total) for s in scores]

    @staticmethod
    def _footer(text_len: int, body_trimmed: int, num_headings: int) -> str:
        """The trailing skeleton-metadata line, kept verbatim in every tier."""
        return (
            f"\n(skeleton — {text_len} chars original"
            f", {body_trimmed} body_trimmed_chars"
            f", {num_headings} sections)"
        )

    @staticmethod
    def _omitted_marker(omitted: int, total: int) -> str:
        """Marker line recording whole sections dropped under budget pressure."""
        return f"\n... ({omitted} of {total} sections omitted)"

    @staticmethod
    def _heading_prefix(headings: list[str], sep: str, max_chars: int, reserve: int) -> int:
        """Count of leading WHOLE heading lines that fit in ``max_chars`` once
        ``reserve`` bytes are set aside for a trailing marker/footer."""
        used = 0
        kept = 0
        for i, heading in enumerate(headings):
            add = len(heading) + (len(sep) if i > 0 else 0)
            if used + add + reserve > max_chars:
                break
            used += add
            kept += 1
        return kept

    def _fit_skeleton(
        self,
        sections: list[tuple[str, list[str]]],
        budgets: list[int],
        text_len: int,
        num_headings: int,
        total_body_chars: int,
        max_chars: int,
    ) -> str:
        """Assemble the skeleton within ``max_chars`` preserving WHOLE headings.

        Replaces the old ``result[:max_chars]`` slice, which dropped trailing
        headings, cut a heading mid-line, and truncated the footer — violating
        the class's "every heading survives" contract. The footer is metadata,
        secondary to heading preservation, so it is best-effort (appended only
        when it still fits); the omission marker — the only piece carrying the
        heading-drop accounting — is reserved with a constant width so the
        kept-heading count is monotonic in the budget (a shrinking budget never
        frees reserved bytes for *more* headings).

        * **All headings fit** (``all_headings_len <= max_chars``): keep every
          heading (the primary contract), refill body lines up to the RAW budget
          (the footer is NOT reserved, so it never preempts a body line — else a
          larger budget that first admits the footer would drop content), then
          append the footer only if it fits in whatever slack is left.
        * **Partial** (the bare heading lines overflow): keep the longest prefix
          of WHOLE heading lines — reserving room for the ``... (N of M sections
          omitted)`` marker but NOT the footer — then append the marker, and the
          footer too if it additionally fits.
        * **Floor** — a budget below one heading + marker: the whole first
          heading when it fits, else a last-resort hard slice (mirroring
          ``TruncateCompressor._fit_with_footer``). Below a single heading's
          width is a budget the retention ladder never produces.

        The marker reserve uses its widest possible width (max-digit counts), so
        the actual — never wider — fits; the footer is always appended by an
        explicit fit check. ``len(output) <= max(0, max_chars)`` holds by
        construction.
        """
        headings = [heading for heading, _ in sections]
        sep = "\n\n"
        marker_reserve = len(self._omitted_marker(num_headings, num_headings))
        all_headings_len = sum(len(h) for h in headings) + len(sep) * (num_headings - 1)

        # All headings fit: keep every one (heading preservation outranks the
        # metadata footer). Refill bodies up to the RAW budget — the footer is
        # best-effort and must never preempt body lines, or a larger budget that
        # first admits the footer would drop content (non-monotonic). The footer
        # is appended afterwards only if it fits in whatever slack remains.
        if all_headings_len <= max_chars:
            kept_lines: list[list[str]] = [[h] for h in headings]
            kept_chars = [len(h) for h in headings]
            running = all_headings_len
            # Layered refill: add EVERY section's first body line before any
            # section's second, so the skeleton keeps "heading + first content
            # line per section" rather than letting early sections spend the
            # global slack on extra lines and starve later sections. A section
            # whose next line does not fit (its per-section budget or the global
            # budget) keeps its contiguous prefix and is skipped thereafter.
            max_body = max((len(body_lines) for _, body_lines in sections), default=0)
            for layer in range(max_body):
                progressed = False
                for idx, (_heading, body_lines) in enumerate(sections):
                    if layer >= len(body_lines) or len(kept_lines[idx]) - 1 != layer:
                        continue
                    line = body_lines[layer]
                    if kept_chars[idx] + len(line) + 1 > budgets[idx]:
                        continue  # this section's per-section budget is full
                    if running + len(line) + 1 > max_chars:
                        continue  # a line this size no longer fits the budget
                    kept_lines[idx].append(line)
                    kept_chars[idx] += len(line) + 1
                    running += len(line) + 1
                    progressed = True
                # A layer that adds nothing means no section can advance to the
                # next layer (a section only becomes eligible by growing here),
                # so every later layer is a no-op — stop instead of scanning them.
                if not progressed:
                    break
            # Same per-section accounting as the fits-case build above
            # (``kept_chars[idx] - len(heading)`` counts each kept body line's
            # joining newline), so body_trimmed_chars is identical across paths.
            body_trimmed = sum(
                max(0, sum(len(ln) for ln in body_lines) - (kept_chars[idx] - len(headings[idx])))
                for idx, (_heading, body_lines) in enumerate(sections)
            )
            body = sep.join("\n".join(lines) for lines in kept_lines)
            footer = self._footer(text_len, body_trimmed, num_headings)
            return body + footer if running + len(footer) <= max_chars else body

        # Partial: keep the longest whole-heading prefix, reserving room ONLY for
        # the omission marker (a constant, budget-independent reserve, so the
        # kept-heading count never rises as the budget falls). Append the marker,
        # then the footer too if it additionally fits.
        kept = self._heading_prefix(headings, sep, max_chars, marker_reserve)
        if kept >= 1:
            out = sep.join(headings[:kept]) + self._omitted_marker(
                num_headings - kept, num_headings
            )
            footer = self._footer(text_len, total_body_chars, num_headings)
            if len(out) + len(footer) <= max_chars:
                out += footer
            return out

        # Floor: not even one heading + marker fits. ``headings[0][:max_chars]``
        # is the whole first heading when it fits, and a hard slice only below a
        # single heading's width — a budget the retention ladder never produces.
        return (headings[0] if headings else "")[: max(0, max_chars)]


class LLMCompressor:
    """Compress by asking an LLM to summarize the text.

    Uses OpenAI / Anthropic / Ollama depending on ``LLMCompressorConfig.provider``.
    Output quality and exact wording depend on the model and system prompt.

    Example (representative; actual output varies by model)::

        Input:  "The Redis cache stores session IDs keyed by user ID with a 24h TTL.
                 Eviction uses LRU and total memory cap is 2GB. Miss rate averages 3%."
        Output: "Redis session cache — 24h TTL, LRU eviction, 2GB cap, ~3% miss rate."
    """

    _OPENAI_URL = "https://api.openai.com/v1/chat/completions"
    _ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
    _ANTHROPIC_VERSION = "2023-06-01"

    _KNOWN_HOSTS = {
        "api.openai.com",
        "api.anthropic.com",
        "localhost",
        "127.0.0.1",
    }

    def __init__(self, config: LLMCompressorConfig) -> None:
        self._cfg = config
        self._cb = _CircuitBreaker(
            max_failures=3, reset_timeout=60.0, name=f"llm-{config.provider.value}"
        )
        self._client: httpx.AsyncClient | None = httpx.AsyncClient(timeout=30) if httpx else None
        self.last_fallback: str | None = None
        # Shutdown gate: close() must wait for in-flight compress() calls to
        # drain before aclose()ing the httpx client. Otherwise a config swap
        # or stop() can tear the client down mid-request. Sister pattern to
        # #125 (extraction), #129 (mcp_client), #130 (proxy conn_stack).
        self._in_flight: int = 0
        self._idle: asyncio.Event = asyncio.Event()
        self._idle.set()
        self._closed: bool = False
        # Warn about non-standard base_url to flag potential credential exfiltration
        if config.base_url:
            from urllib.parse import urlparse

            host = urlparse(config.base_url).hostname or ""
            if host and host not in self._KNOWN_HOSTS and not host.endswith(".local"):
                logger.warning(
                    "LLM compressor base_url points to non-standard host %r — "
                    "verify this is intentional (API key may be sent to this host)",
                    host,
                )

    async def compress(
        self, text: str, *, max_chars: int, privacy_patterns: list[str] | None = None
    ) -> str:
        self.last_fallback = None
        if not text or len(text) <= max_chars:
            return text
        if privacy_patterns:
            from memtomem_stm.proxy.privacy import contains_sensitive_content

            if contains_sensitive_content(text, privacy_patterns):
                logger.info(
                    "Sensitive content detected, skipping LLM compression (strategy=llm/%s)",
                    self._cfg.provider.value,
                )
                self.last_fallback = "privacy"
                return TruncateCompressor().compress(text, max_chars=max_chars)
        if self._cb.is_open:
            self.last_fallback = "circuit_breaker"
            return TruncateCompressor().compress(text, max_chars=max_chars)
        if self._closed:
            self.last_fallback = "closed"
            return TruncateCompressor().compress(text, max_chars=max_chars)
        # Register as in-flight so a concurrent close() blocks on ``_idle``
        # until we finish touching ``_client``. The increment+clear pair is
        # sync — no ``await`` between the ``_closed`` check above and the
        # clear, so close() cannot slip in and aclose() the client between
        # our check and our claim.
        self._in_flight += 1
        self._idle.clear()
        try:
            result = await asyncio.wait_for(
                self._call_api(text, max_chars=max_chars),
                timeout=self._cfg.llm_timeout_seconds,
            )
            self._cb.success()
            return result
        except asyncio.TimeoutError:
            self._cb.failure()
            logger.warning(
                "LLM compression timed out after %.1fs (strategy=llm/%s), falling back to truncate",
                self._cfg.llm_timeout_seconds,
                self._cfg.provider.value,
            )
            self.last_fallback = "timeout"
            return TruncateCompressor().compress(text, max_chars=max_chars)
        except Exception as exc:
            self._cb.failure()
            logger.warning(
                "LLM compression failed (strategy=llm/%s, %s), falling back to truncate: %s",
                self._cfg.provider.value,
                type(exc).__name__,
                exc,
            )
            self.last_fallback = "llm_error"
            return TruncateCompressor().compress(text, max_chars=max_chars)
        finally:
            self._in_flight -= 1
            if self._in_flight == 0:
                self._idle.set()

    async def _call_api(self, text: str, *, max_chars: int) -> str:
        if self._client is None:
            raise RuntimeError("LLMCompressor HTTP client is not initialized (missing httpx?)")
        system_prompt = self._cfg.system_prompt.format(max_chars=max_chars)
        match self._cfg.provider:
            case LLMProvider.OPENAI:
                return await self._openai(text, system_prompt)
            case LLMProvider.ANTHROPIC:
                return await self._anthropic(text, system_prompt)
            case LLMProvider.OLLAMA:
                return await self._ollama(text, system_prompt)

    async def close(self) -> None:
        # Flip the gate first so new compress() calls fall back to truncate
        # instead of entering ``_in_flight``. Then wait for already-registered
        # callers to drain before aclose()ing the client.
        self._closed = True
        await self._idle.wait()
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _openai(self, text: str, system_prompt: str) -> str:
        if self._client is None:
            raise RuntimeError("LLMCompressor HTTP client is not initialized (missing httpx?)")
        url = (
            self._cfg.base_url.rstrip("/") + "/v1/chat/completions"
            if self._cfg.base_url
            else self._OPENAI_URL
        )
        resp = await self._client.post(
            url,
            headers={"Authorization": f"Bearer {self._cfg.api_key}"},
            json={
                "model": self._cfg.model,
                "max_tokens": self._cfg.max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise ValueError("OpenAI response has empty 'choices' (likely quota or content filter)")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("OpenAI response missing 'choices[0].message.content'")
        return content

    async def _anthropic(self, text: str, system_prompt: str) -> str:
        if self._client is None:
            raise RuntimeError("LLMCompressor HTTP client is not initialized (missing httpx?)")
        url = (
            self._cfg.base_url.rstrip("/") + "/v1/messages"
            if self._cfg.base_url
            else self._ANTHROPIC_URL
        )
        resp = await self._client.post(
            url,
            headers={
                "x-api-key": self._cfg.api_key,
                "anthropic-version": self._ANTHROPIC_VERSION,
            },
            json={
                "model": self._cfg.model,
                "max_tokens": self._cfg.max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": text}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content") or []
        if not content:
            raise ValueError(
                "Anthropic response has empty 'content' (likely empty completion or filter)"
            )
        text_block = content[0].get("text")
        if not isinstance(text_block, str):
            raise ValueError("Anthropic response missing 'content[0].text'")
        return text_block

    async def _ollama(self, text: str, system_prompt: str) -> str:
        if self._client is None:
            raise RuntimeError("LLMCompressor HTTP client is not initialized (missing httpx?)")
        base = self._cfg.base_url or "http://localhost:11434"
        url = base.rstrip("/") + "/api/chat"
        resp = await self._client.post(
            url,
            json={
                "model": self._cfg.model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        message = data.get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("Ollama response missing 'message.content'")
        return content


class HybridCompressor:
    """Head preserve + tail compress (TOC or truncate).

    The head (first ``head_chars``) is kept verbatim so the most important
    context survives untouched. The tail is compressed via SelectiveCompressor
    (``tail_mode=toc``) or TruncateCompressor (``tail_mode=truncate``),
    separated by a marker line.

    Example — long markdown (head verbatim, tail → selective TOC)::

        Input:  "## Overview\\n<5000 chars of detail>\\n\\n"
                "## API\\n<1500 chars>\\n\\n## Config\\n<1200 chars>"
        Output: "## Overview\\n<5000 chars of detail>\\n"
                "---\\n"
                "Remaining content (2700 chars) — Table of Contents:\\n\\n"
                "<JSON TOC listing api, config>"
    """

    _SEPARATOR_TEMPLATE = "\n---\nRemaining content ({remaining} chars) — Table of Contents:\n\n"
    _SEPARATOR_TRUNC_TEMPLATE = "\n---\nRemaining content ({remaining} chars, truncated):\n\n"

    def __init__(
        self,
        head_chars: int = 5000,
        tail_mode: TailMode = TailMode.TOC,
        min_toc_budget: int = 200,
        min_head_chars: int = 100,
        head_ratio: float = 0.6,
        selective_compressor: SelectiveCompressor | None = None,
    ) -> None:
        self._head_chars = head_chars
        self._tail_mode = tail_mode
        self._min_toc_budget = min_toc_budget
        self._min_head_chars = min_head_chars
        self._head_ratio = head_ratio
        self._selective = selective_compressor or SelectiveCompressor()

    @property
    def selective_compressor(self) -> SelectiveCompressor:
        return self._selective

    def compress(self, text: str, *, max_chars: int, context_query: str | None = None) -> str:
        if not text or len(text) <= max_chars:
            return text

        separator_overhead = 80
        _MIN_TAIL = 50
        available = max_chars - separator_overhead

        if available <= 0:
            return TruncateCompressor().compress(
                text, max_chars=max_chars, context_query=context_query
            )

        if self._head_chars + self._min_toc_budget <= available:
            head_budget = self._head_chars
        else:
            head_budget = max(self._min_head_chars, int(available * self._head_ratio))

        if head_budget > available or head_budget < self._min_head_chars:
            return TruncateCompressor().compress(
                text, max_chars=max_chars, context_query=context_query
            )

        if available - head_budget < _MIN_TAIL:
            return TruncateCompressor().compress(
                text, max_chars=max_chars, context_query=context_query
            )

        head_end = self._find_head_break(text, head_budget)
        head = text[:head_end]
        tail_text = text[head_end:]
        remaining = len(tail_text)

        if self._tail_mode == TailMode.TOC:
            separator = self._SEPARATOR_TEMPLATE.format(remaining=remaining)
        else:
            separator = self._SEPARATOR_TRUNC_TEMPLATE.format(remaining=remaining)

        toc_budget = max_chars - len(head) - len(separator)
        if toc_budget < _MIN_TAIL:
            return TruncateCompressor().compress(
                text, max_chars=max_chars, context_query=context_query
            )

        if self._tail_mode == TailMode.TOC:
            tail_compressed = self._selective.compress(
                tail_text, max_chars=toc_budget, context_query=context_query
            )
        else:
            tail_compressed = TruncateCompressor().compress(
                tail_text, max_chars=toc_budget, context_query=context_query
            )

        result = head + separator + tail_compressed
        if len(result) > max_chars:
            # A sub-compressor can return more than its requested budget. Avoid
            # raw slicing because it can sever a JSON TOC mid-object, but keep
            # the hybrid shape when possible so the ratio guard still sees a
            # navigable, budget-filling fallback.
            tail_budget = max_chars - len(head) - len(separator)
            fitted_tail = self._fit_toc_tail(tail_compressed, tail_budget, tail_text)
            if fitted_tail is not None:
                return head + separator + fitted_tail

            fallback = TruncateCompressor().compress(
                text, max_chars=max_chars, context_query=context_query
            )
            return fallback[:max_chars]
        return result

    @staticmethod
    def _fit_toc_tail(tail: str, max_chars: int, source_text: str) -> str | None:
        if max_chars < 2:
            return None

        try:
            parsed = json.loads(tail)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict) or parsed.get("type") != "toc":
            return None

        def dumps(obj: object) -> str:
            return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

        def fill_preview(toc: dict) -> str:
            candidate = dumps(toc)
            if len(candidate) >= max_chars or not source_text:
                return candidate
            lo, hi = 0, len(source_text)
            best = candidate
            while lo <= hi:
                mid = (lo + hi) // 2
                probe_toc = dict(toc)
                probe_toc["tail_preview"] = source_text[:mid]
                probe = dumps(probe_toc)
                if len(probe) <= max_chars:
                    best = probe
                    lo = mid + 1
                else:
                    hi = mid - 1
            if len(best) < max_chars:
                # JSON permits trailing whitespace. Use it only as a final
                # single-budget filler so the retention guard does not reject an
                # otherwise valid TOC for being one or two chars below the floor.
                best += " " * (max_chars - len(best))
            return best

        candidate = dumps(parsed)
        if len(candidate) <= max_chars:
            return fill_preview(parsed)

        toc = dict(parsed)
        entries = toc.get("entries")
        if isinstance(entries, list):
            toc["entries"] = [dict(e) if isinstance(e, dict) else e for e in entries]
            for entry in reversed(toc["entries"]):
                if not isinstance(entry, dict):
                    continue
                preview = entry.get("preview")
                if not isinstance(preview, str) or not preview:
                    continue
                original = preview
                lo, hi = 0, len(original)
                best: str | None = None
                while lo <= hi:
                    mid = (lo + hi) // 2
                    entry["preview"] = original[:mid] + ("..." if mid < len(original) else "")
                    probe = dumps(toc)
                    if len(probe) <= max_chars:
                        best = entry["preview"]
                        lo = mid + 1
                    else:
                        hi = mid - 1
                if best is not None:
                    entry["preview"] = best
                    return fill_preview(toc)
                entry["preview"] = "..."
                candidate = dumps(toc)
                if len(candidate) <= max_chars:
                    return fill_preview(toc)

        hint = toc.get("hint")
        selection_key = toc.get("selection_key")
        if isinstance(hint, str) and selection_key:
            toc["hint"] = f"select_chunks key={selection_key} sections=[...]"
            candidate = dumps(toc)
            if len(candidate) <= max_chars:
                return fill_preview(toc)

        if isinstance(entries, list):
            while toc["entries"]:
                original_count = len(entries)
                toc["entries"] = toc["entries"][:-1]
                omitted = original_count - len(toc["entries"])
                toc["truncated_entries"] = omitted
                candidate = dumps(toc)
                if len(candidate) <= max_chars:
                    return fill_preview(toc)

        toc["entries"] = []
        toc["truncated_entries"] = len(entries) if isinstance(entries, list) else 0
        candidate = dumps(toc)
        return fill_preview(toc) if len(candidate) <= max_chars else None

    def _find_head_break(self, text: str, budget: int) -> int:
        floor = int(budget * 0.85)
        for i in range(budget, floor - 1, -1):
            if i + 1 < len(text) and text[i : i + 2] == "\n\n":
                return i
            if i >= 2 and text[i - 2 : i] == "\n\n":
                return i - 2
        for i in range(budget, floor - 1, -1):
            if text[i - 1] in ".!?\n" and (i >= len(text) or text[i] in " \n\t"):
                return i
        for i in range(budget, floor - 1, -1):
            if i < len(text) and text[i] in " \n\t":
                return i
        return budget


def get_compressor(strategy: CompressionStrategy) -> Compressor:
    """Factory for sync compressor instances (excludes LLM_SUMMARY, SELECTIVE, HYBRID)."""
    match strategy:
        case CompressionStrategy.NONE:
            return NoopCompressor()
        case CompressionStrategy.TRUNCATE:
            return TruncateCompressor()
        case CompressionStrategy.EXTRACT_FIELDS:
            return FieldExtractCompressor()
        case CompressionStrategy.SCHEMA_PRUNING:
            return SchemaPruningCompressor()
        case CompressionStrategy.SKELETON:
            return SkeletonCompressor()
        case CompressionStrategy.PROGRESSIVE:
            return NoopCompressor()  # progressive is handled at pipeline level
        case _:
            return TruncateCompressor()


def _json_depth(data: object, _current: int = 0) -> int:
    """Measure max nesting depth of a JSON structure."""
    if isinstance(data, dict):
        if not data:
            return _current + 1
        return max(_json_depth(v, _current + 1) for v in data.values())
    if isinstance(data, list):
        if not data:
            return _current + 1
        return max(_json_depth(v, _current + 1) for v in data[:5])  # sample first 5
    return _current


def auto_select_strategy(text: str, *, max_chars: int = 0) -> CompressionStrategy:
    """Detect content type and return the best compression strategy.

    Principle: information preservation > compression ratio.
    If a pattern is not recognized, prefer NONE (passthrough after cleaning)
    over aggressive compression that may destroy information.

    Args:
        text: cleaned content to analyze
        max_chars: budget hint (0 = unknown). When cleaning already fits
                   within budget, returns NONE to skip compression entirely.
    """
    stripped = text.strip()
    if not stripped:
        return CompressionStrategy.NONE

    # If content already fits within budget after cleaning → passthrough
    if max_chars > 0 and len(stripped) <= max_chars:
        return CompressionStrategy.NONE

    # JSON detection
    if stripped[0] in "{[":
        try:
            data = _mm_json_loads(stripped)
            if isinstance(data, list) and len(data) >= 20:
                return CompressionStrategy.SCHEMA_PRUNING
            if isinstance(data, dict):
                arrays = [v for v in data.values() if isinstance(v, list) and len(v) >= 20]
                if arrays:
                    return CompressionStrategy.SCHEMA_PRUNING
                # Dict-of-dicts (config-like): many top-level keys with nested structure
                # → extract_fields preserves all keys vs truncate losing bottom half
                nested = sum(1 for v in data.values() if isinstance(v, (dict, list)))
                if nested >= 3:
                    return CompressionStrategy.EXTRACT_FIELDS
                # Several smaller arrays whose combined length is large (no single
                # array ≥ 20, nested < 3) → schema_pruning preserves the shared row
                # schema across every array, vs truncate dropping whole arrays once
                # the byte budget runs out. Mirrors the top-level list ≥ 20 cutoff.
                list_values = [v for v in data.values() if isinstance(v, list)]
                if list_values and sum(len(v) for v in list_values) >= 20:
                    return CompressionStrategy.SCHEMA_PRUNING
            return CompressionStrategy.TRUNCATE
        except (json.JSONDecodeError, ValueError):
            pass

    # Markdown detection
    heading_count = count_markdown_headings(stripped)

    if heading_count >= 4:
        # Skeleton for API-docs with HTTP method endpoints
        has_http_methods = bool(re.search(r"(?:POST|GET|PUT|DELETE|PATCH)\s+/", stripped))
        if has_http_methods:
            return CompressionStrategy.SKELETON

        # Large docs with substantial sections → hybrid
        if heading_count >= 5 and len(stripped) >= 5000:
            return CompressionStrategy.HYBRID

    # Code-heavy content — HYBRID only for large code files
    fence_count = stripped.count("```")
    if fence_count >= 6 and len(stripped) >= 5000:
        return CompressionStrategy.HYBRID

    return CompressionStrategy.TRUNCATE
