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

``risk_penalty`` demotes a tool without removing it: ``final_score =
relevance_score * (1 - risk_penalty)`` and the ordering follows
``final_score``, so a penalized tool sinks without leaving the record.
Multiplicative because BM25 scores are unbounded — an absolute penalty
would mean nothing across queries. Two independent sources feed the penalty,
composed via :func:`compose_risk_penalty`: the #465 hard filter's
``review``-profile demotion (a flagged-but-advertised tool), and the #493
external tool-graph ``risk_score`` mapped onto eligible-but-risky tools in
*every* profile. Each candidate also carries a ``risk_penalty_source`` tag
(:func:`penalty_source`) recording which source(s) shaped it, so #468 replay
can attribute the demotion. Calls stamp :data:`RANKER_VERSION_BM25_GRAPH_RISK`
when any graph-derived penalty contributed, :data:`RANKER_VERSION_BM25_RISK`
when only native review demotions did, else :data:`RANKER_VERSION_BM25` — the
math is identical, but the cohorts must not pool. The penalties themselves
are session-stable (health flags + the graph consult both run once at
startup), making records deterministic within a session and self-describing
across sessions (each candidate carries the penalty and source that shaped
its score). A hard-rejected tool never reaches this module at all — ranking
runs over the filter's output, so it can never resurrect a reject.

Privacy: the derived query is used in memory for scoring only — callers
persist its sha256/length/source via ``build_candidate_features``, never the
text, matching the selection log's structural-redaction contract.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from memtomem_stm.proxy.relevance import BM25Scorer

if TYPE_CHECKING:
    from memtomem_stm.proxy.manager import ProxyToolInfo

# Stamped on selection/execution events whose ranking ran; events where it
# did not (no query signal, ranking disabled, empty candidate set) keep the
# selection log's "v0-passthrough" default, so replay can split cohorts by
# this field alone.
RANKER_VERSION_BM25 = "v1-bm25-tool-relevance"

# Stamped instead of RANKER_VERSION_BM25 when at least one candidate carried
# a nonzero risk penalty — the scoring function then differs from plain v1
# (final_score = relevance * (1 - penalty)), and replay must not pool the
# two. With an all-zero penalty map the math degenerates to v1 exactly, so
# such calls keep the v1 stamp. v2 is the NATIVE-only cohort: every nonzero
# penalty came from the #465 ``review``-profile demotion.
RANKER_VERSION_BM25_RISK = "v2-bm25-risk-penalty"

# Stamped instead of RANKER_VERSION_BM25_RISK when at least one candidate's
# penalty includes a component derived from the external tool-graph's
# per-candidate ``risk_score`` (#493). The scoring math is unchanged (still
# final = relevance * (1 - penalty)), but the penalty PROVENANCE differs — a
# graph-risk demotion applies to eligible-but-risky tools in EVERY profile,
# whereas the native v2 demotion is review-profile-only — so #468 replay must
# split the cohorts. Per-candidate ``risk_penalty_source`` records which
# source(s) shaped each tool's penalty within the call.
RANKER_VERSION_BM25_GRAPH_RISK = "v3-bm25-graph-risk-penalty"

# ``risk_penalty_source`` values — the provenance of a candidate's composed
# penalty, for #468 replay attribution. Every ranked record carries one
# (``"none"`` when unpenalized), keeping records self-describing.
PENALTY_SOURCE_NONE = "none"
PENALTY_SOURCE_REVIEW = "review"  # #465 review-profile demotion only
PENALTY_SOURCE_GRAPH = "graph"  # #493 tool-graph risk_score only
PENALTY_SOURCE_BOTH = "review+graph"  # both stacked via compose_risk_penalty

# Bound the schema text folded into each candidate document. Schemas are
# advertised (possibly distilled) client-facing artifacts, but a pathological
# upstream schema should cost bounded scoring work; the cap is deterministic
# so truncation never varies between identical calls.
_MAX_SCHEMA_CHARS = 2000

# Bound the args-derived fallback query the same way.
_MAX_QUERY_CHARS = 512


def compose_risk_penalty(native: float, graph: float) -> float:
    """Combine two independent demotions into one penalty (complement-product).

    The ranker scores ``final = relevance * (1 - penalty)``. Two independent
    multiplicative demotions therefore stack: ``relevance * (1 - native) *
    (1 - graph) = relevance * (1 - combined)`` with ``combined = 1 -
    (1 - native)(1 - graph)``. This composition is the one consistent with the
    ranking math itself — commutative, monotone in each input, bounded to
    ``[0, 1]`` for inputs in ``[0, 1]``, and degenerating to whichever input is
    nonzero when the other is ``0`` (so an unflagged tool with a graph risk,
    or a flagged tool the graph scored clean, behaves exactly as before).

    *native* is the #465 ``review``-profile demotion (``review_risk_penalty``);
    *graph* is the #493 tool-graph ``risk_score`` scaled by
    ``risk_penalty_scale``.
    """
    return 1.0 - (1.0 - native) * (1.0 - graph)


def penalty_source(native: float, graph: float) -> str:
    """Provenance tag for a composed penalty (one of ``PENALTY_SOURCE_*``).

    Lets #468 replay attribute each candidate's demotion to the native review
    rule, the tool-graph risk signal, or both — the combined ``risk_penalty``
    value alone cannot distinguish a ``0.5`` that is native-only from one that
    is graph-derived.
    """
    if native > 0.0 and graph > 0.0:
        return PENALTY_SOURCE_BOTH
    if graph > 0.0:
        return PENALTY_SOURCE_GRAPH
    if native > 0.0:
        return PENALTY_SOURCE_REVIEW
    return PENALTY_SOURCE_NONE


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

    def rank(
        self,
        query: str,
        candidates: list[ProxyToolInfo],
        risk_penalties: Mapping[str, float] | None = None,
        risk_penalty_sources: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Rank *candidates* against *query*; order follows ``final_score``.

        *risk_penalties* maps prefixed names to the already-composed demotion
        (native #465 ``review`` demotion stacked with the #493 tool-graph risk
        via :func:`compose_risk_penalty`); absent tools carry ``0.0`` and the
        math degenerates to plain relevance order. *risk_penalty_sources* maps
        the same names to the provenance tag (:func:`penalty_source`) echoed
        into each record for #468 replay attribution; absent tools record
        :data:`PENALTY_SOURCE_NONE`.
        """
        if not candidates:
            return []
        penalties = risk_penalties or {}
        sources = risk_penalty_sources or {}
        sections = [_candidate_document(c) for c in candidates]
        scores = self._scorer.score_sections(query, sections)
        finals = [
            scores[i] * (1.0 - penalties.get(c.prefixed_name, 0.0))
            for i, c in enumerate(candidates)
        ]
        order = sorted(
            range(len(candidates)),
            key=lambda i: (-finals[i], candidates[i].prefixed_name),
        )
        return [
            {
                "tool": candidates[i].prefixed_name,
                "rank": rank,
                "relevance_score": round(scores[i], 6),
                "risk_penalty": round(penalties.get(candidates[i].prefixed_name, 0.0), 6),
                "risk_penalty_source": sources.get(
                    candidates[i].prefixed_name, PENALTY_SOURCE_NONE
                ),
                "final_score": round(finals[i], 6),
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
