"""Deterministic task-relevance ranking over advertised proxy tools (#466 v0).

v0 contract — telemetry input only, never an exposure change: the ranking is
computed per proxied call from the *advertised* tool snapshot and recorded
into the #467 selection event's reserved ``candidate_features`` field. It
does not reorder ``tools/list``, does not emit ``list_changed``, and does not
gate anything. Whether it is worth acting on is exactly what the offline
replay stage (#468) decides from these records; dynamic exposure mechanisms
are deferred until then.

Determinism is an acceptance criterion, so v0 is BM25-only
(:class:`~memtomem_stm.proxy.relevance.BM25Scorer` — zero dependencies, same
score for the same inputs forever). The embedding scorer is deliberately not
offered here: its scores drift across providers/model versions and its
availability varies, which would make #468 replay comparisons meaningless.

The scored document per candidate is what the client actually saw: the
prefixed tool name (BM25 heading position, 3x weight) plus the advertised —
post-truncation, post-distill — description and stable-serialized schema.
``risk_penalty`` is a ``0.0`` placeholder: no risk table exists in this repo
(the one in the toolgraph report is not a repo artifact); a real penalty
input arrives with #465's STM-native filter signals.

Privacy: the derived query is used in memory for scoring only — callers
persist its sha256/length/source via ``build_candidate_features``, never the
text, matching the selection log's structural-redaction contract.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from memtomem_stm.proxy.relevance import BM25Scorer

if TYPE_CHECKING:
    from memtomem_stm.proxy.manager import ProxyToolInfo

# Stamped on selection/execution events whose ranking ran; events where it
# did not (no query signal, ranking disabled, empty candidate set) keep the
# selection log's "v0-passthrough" default, so replay can split cohorts by
# this field alone.
RANKER_VERSION_BM25 = "v1-bm25-tool-relevance"

# Bound the schema text folded into each candidate document. Schemas are
# advertised (possibly distilled) client-facing artifacts, but a pathological
# upstream schema should cost bounded scoring work; the cap is deterministic
# so truncation never varies between identical calls.
_MAX_SCHEMA_CHARS = 2000

# Bound the args-derived fallback query the same way.
_MAX_QUERY_CHARS = 512


def derive_query(arguments: dict[str, Any] | None) -> tuple[str, str] | None:
    """Derive the per-call task signal: ``(query, source)`` or ``None``.

    ``_context_query`` — the explicit task text some harnesses attach — wins
    outright. Otherwise fall back to the call's top-level string argument
    values, sorted by key for determinism (dict iteration order is caller
    noise) and capped. No usable signal returns ``None`` and the caller
    skips ranking entirely rather than ranking against an empty query.
    """
    if not arguments:
        return None
    ctx = arguments.get("_context_query")
    if isinstance(ctx, str) and ctx.strip():
        return ctx[:_MAX_QUERY_CHARS], "context_query"
    parts = [
        v.strip()
        for k, v in sorted(arguments.items())
        if not k.startswith("_") and isinstance(v, str) and v.strip()
    ]
    if not parts:
        return None
    return " ".join(parts)[:_MAX_QUERY_CHARS], "args"


def _candidate_document(info: ProxyToolInfo) -> tuple[str, str]:
    """(heading, body) for BM25: name as heading, advertised text as body."""
    schema_text = json.dumps(info.input_schema or {}, sort_keys=True, separators=(",", ":"))
    return info.prefixed_name, f"{info.description} {schema_text[:_MAX_SCHEMA_CHARS]}"


class ToolRelevanceRanker:
    """Rank advertised tools against a query, deterministically.

    Ties (including the all-zero scores of a query that matches nothing)
    break on the prefixed name, never on upstream discovery order — two
    runs over the same advertised set and query must produce byte-identical
    ``ranked_candidates``.
    """

    def __init__(self, *, top_n: int = 20) -> None:
        self._top_n = top_n
        self._scorer = BM25Scorer()

    def rank(self, query: str, candidates: list[ProxyToolInfo]) -> list[dict[str, Any]]:
        if not candidates:
            return []
        sections = [_candidate_document(c) for c in candidates]
        scores = self._scorer.score_sections(query, sections)
        order = sorted(
            range(len(candidates)),
            key=lambda i: (-scores[i], candidates[i].prefixed_name),
        )
        return [
            {
                "tool": candidates[i].prefixed_name,
                "rank": rank,
                "relevance_score": round(scores[i], 6),
                # Placeholder until a repo-local risk signal exists (#465);
                # recorded explicitly so replay code never special-cases
                # its absence.
                "risk_penalty": 0.0,
                "final_score": round(scores[i], 6),
            }
            for rank, i in enumerate(order[: self._top_n], start=1)
        ]


def build_candidate_features(
    query: str, query_source: str, ranked: list[dict[str, Any]]
) -> dict[str, Any]:
    """Assemble the ``candidate_features`` object for the selection event.

    Carries the query only as sha256 + char count + source tag — the raw
    text never enters the telemetry record.
    """
    return {
        "query_source": query_source,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "query_chars": len(query),
        "ranked_candidates": ranked,
    }
