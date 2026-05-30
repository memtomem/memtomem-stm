"""Query-aware wiring for SCHEMA_PRUNING / SKELETON structural compressors.

Extends the #386 SELECTIVE / Hybrid-TOC query-awareness to the two structural
compressors that previously ranked purely by position. Both now accept a
``context_query`` and the manager's relevance scorer:

* ``SchemaPruningCompressor`` keeps every key (the schema invariant) but spends
  the value budget on the keys most relevant to the query — relevant subtrees
  keep longer strings / more array items, irrelevant ones are pruned harder.
* ``SkeletonCompressor`` keeps every heading but weights the per-section content
  budget toward the most relevant sections so they retain more body lines.

The load-bearing invariant is byte-for-byte identity when there is no query (or
no BM25 signal): the compressors fall back to the exact pre-query behavior.
``field_extract`` is intentionally left position-ranked (BM25 over short keys is
a weak signal) — reopen if a user reports a dropped query-relevant key.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from memtomem_stm.proxy.compression import (
    BM25Scorer,
    SchemaPruningCompressor,
    SkeletonCompressor,
)
from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics import TokenTracker


class _RecordingScorer:
    """A RelevanceScorer stub that records calls and scores by token overlap.

    Used to prove a compressor actually *consults* the injected scorer (not just
    stores it), without any network dependency.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def score_sections(self, query: str, sections: list[tuple[str, str]]) -> list[float]:
        self.calls.append((query, len(sections)))
        terms = query.lower().split()
        return [
            float(sum(t in f"{title} {body}".lower() for t in terms)) for title, body in sections
        ]


# A dict-of-arrays payload (the dominant SCHEMA_PRUNING routing trigger): three
# top-level keys, each an array of records, only one of which matches the query.
_PRUNE_PAYLOAD = json.dumps(
    {
        "users": [{"id": i, "name": "Alice", "bio": "long biography text " * 4} for i in range(22)],
        "orders": [
            {"oid": i, "total": 12.5, "desc": "order description text " * 4} for i in range(22)
        ],
        "logs": ["log line %d verbose detail text here and more" % i for i in range(22)],
    }
)


# A multi-section markdown doc with multi-line bodies so the per-section budget
# actually governs how many body lines survive.
def _skeleton_doc() -> str:
    def section(name: str, term: str) -> str:
        lines = "\n".join(f"{term} detail line {i} explains more" for i in range(8))
        return f"## {name}\n{lines}\n\n"

    return (
        section("Auth", "jwt oauth")
        + section("Billing", "invoice payment")
        + section("Logging", "log rotation")
        + section("Metrics", "counter gauge")
    )


def _block(rendered: str, heading: str) -> str:
    """Return the rendered text of a single section (heading → next heading)."""
    return rendered.split(heading, 1)[1].split("##", 1)[0]


class TestSchemaPruningQueryAware:
    def test_no_query_is_byte_identical(self) -> None:
        c = SchemaPruningCompressor()
        base = c.compress(_PRUNE_PAYLOAD, max_chars=900)
        assert c.compress(_PRUNE_PAYLOAD, max_chars=900, context_query=None) == base

    def test_irrelevant_query_is_byte_identical(self) -> None:
        # No top-level key scores → falls back to the uniform path verbatim.
        c = SchemaPruningCompressor()
        base = c.compress(_PRUNE_PAYLOAD, max_chars=900)
        assert c.compress(_PRUNE_PAYLOAD, max_chars=900, context_query="zzzzz qqqqq") == base

    def test_query_keeps_more_of_the_relevant_key(self) -> None:
        c = SchemaPruningCompressor()
        base = json.loads(c.compress(_PRUNE_PAYLOAD, max_chars=900))
        out = json.loads(c.compress(_PRUNE_PAYLOAD, max_chars=900, context_query="orders total"))
        # Under budget pressure the relevant subtree retains strictly more detail.
        assert len(json.dumps(out["orders"])) > len(json.dumps(base["orders"]))

    def test_all_keys_survive_with_query(self) -> None:
        c = SchemaPruningCompressor()
        out = json.loads(c.compress(_PRUNE_PAYLOAD, max_chars=900, context_query="orders total"))
        assert set(out.keys()) == {"users", "orders", "logs"}

    def test_output_respects_budget(self) -> None:
        c = SchemaPruningCompressor()
        assert len(c.compress(_PRUNE_PAYLOAD, max_chars=900, context_query="orders total")) <= 900

    def test_full_payload_fits_means_no_needless_pruning(self) -> None:
        # When the whole object fits at default detail, the query must not prune
        # irrelevant keys harder than the uniform path — both are identical.
        c = SchemaPruningCompressor()
        base = c.compress(_PRUNE_PAYLOAD, max_chars=4000)
        assert c.compress(_PRUNE_PAYLOAD, max_chars=4000, context_query="orders total") == base

    def test_query_relevance_follows_the_query(self) -> None:
        # A query for a different key shifts the retained detail to that key.
        c = SchemaPruningCompressor()
        users_q = json.loads(
            c.compress(_PRUNE_PAYLOAD, max_chars=900, context_query="users name bio")
        )
        orders_q = json.loads(
            c.compress(_PRUNE_PAYLOAD, max_chars=900, context_query="orders total desc")
        )
        assert len(json.dumps(users_q["users"])) > len(json.dumps(orders_q["users"]))
        assert len(json.dumps(orders_q["orders"])) > len(json.dumps(users_q["orders"]))

    def test_non_object_root_is_byte_identical(self) -> None:
        # Top-level arrays have no keys to weight — query-awareness is deferred,
        # so a query must not change the output.
        list_payload = json.dumps([{"k": "v " * 20, "n": i} for i in range(30)])
        c = SchemaPruningCompressor()
        base = c.compress(list_payload, max_chars=300)
        assert c.compress(list_payload, max_chars=300, context_query="anything here") == base

    def test_single_key_dict_is_byte_identical(self) -> None:
        # A single-key object has nothing to weight (len < 2) — the query path is
        # skipped and the output matches the no-query path exactly.
        payload = json.dumps(
            {"orders": [{"oid": i, "total": 12.5, "desc": "order text " * 6} for i in range(25)]}
        )
        c = SchemaPruningCompressor()
        base = c.compress(payload, max_chars=400)
        assert c.compress(payload, max_chars=400, context_query="orders total") == base

    def test_tight_budget_delegates_to_uniform_path(self) -> None:
        # When the budget is too tight for the weighted tiers, _compress_weighted
        # returns None and the query path falls through to the uniform minimal
        # tier instead of carrying a divergent copy of the overflow handling —
        # so the output is identical to the no-query path.
        c = SchemaPruningCompressor()
        base = c.compress(_PRUNE_PAYLOAD, max_chars=100)
        assert c.compress(_PRUNE_PAYLOAD, max_chars=100, context_query="orders total") == base


class TestSkeletonQueryAware:
    def test_no_query_is_byte_identical(self) -> None:
        doc = _skeleton_doc()
        c = SkeletonCompressor()
        base = c.compress(doc, max_chars=400)
        assert c.compress(doc, max_chars=400, context_query=None) == base

    def test_irrelevant_query_is_byte_identical(self) -> None:
        doc = _skeleton_doc()
        c = SkeletonCompressor()
        base = c.compress(doc, max_chars=400)
        assert c.compress(doc, max_chars=400, context_query="zzzzz qqqqq") == base

    def test_query_keeps_more_body_in_the_relevant_section(self) -> None:
        doc = _skeleton_doc()
        c = SkeletonCompressor()
        base = c.compress(doc, max_chars=400)
        out = c.compress(doc, max_chars=400, context_query="invoice payment billing")
        assert _block(out, "## Billing").count("invoice") > _block(base, "## Billing").count(
            "invoice"
        )

    def test_all_headings_survive_with_query(self) -> None:
        doc = _skeleton_doc()
        out = SkeletonCompressor().compress(
            doc, max_chars=400, context_query="invoice payment billing"
        )
        assert all(h in out for h in ["## Auth", "## Billing", "## Logging", "## Metrics"])

    def test_section_order_is_preserved(self) -> None:
        doc = _skeleton_doc()
        out = SkeletonCompressor().compress(doc, max_chars=400, context_query="invoice payment")
        positions = [out.index(h) for h in ["## Auth", "## Billing", "## Logging", "## Metrics"]]
        assert positions == sorted(positions)

    def test_output_respects_budget(self) -> None:
        doc = _skeleton_doc()
        out = SkeletonCompressor().compress(doc, max_chars=400, context_query="invoice payment")
        assert len(out) <= 400

    def test_relevance_follows_the_query(self) -> None:
        # Querying for Auth surfaces Auth's body instead of Billing's.
        doc = _skeleton_doc()
        c = SkeletonCompressor()
        auth_q = c.compress(doc, max_chars=400, context_query="jwt oauth auth")
        bill_q = c.compress(doc, max_chars=400, context_query="invoice payment billing")
        assert _block(auth_q, "## Auth").count("jwt") > _block(bill_q, "## Auth").count("jwt")
        assert _block(bill_q, "## Billing").count("invoice") > _block(auth_q, "## Billing").count(
            "invoice"
        )


class TestStructuralScorerInjection:
    def test_schema_pruning_defaults_to_bm25(self) -> None:
        assert isinstance(SchemaPruningCompressor()._scorer, BM25Scorer)

    def test_skeleton_defaults_to_bm25(self) -> None:
        assert isinstance(SkeletonCompressor()._scorer, BM25Scorer)

    def test_schema_pruning_consults_injected_scorer(self) -> None:
        # Inject a recording scorer and confirm it is both stored AND used when a
        # query drives compression (not merely held as an attribute).
        scorer = _RecordingScorer()
        c = SchemaPruningCompressor(scorer=scorer)
        assert c._scorer is scorer
        c.compress(_PRUNE_PAYLOAD, max_chars=900, context_query="orders total")
        assert scorer.calls, "injected scorer was never consulted"

    def test_skeleton_consults_injected_scorer(self) -> None:
        scorer = _RecordingScorer()
        c = SkeletonCompressor(scorer=scorer)
        assert c._scorer is scorer
        c.compress(_skeleton_doc(), max_chars=400, context_query="invoice payment billing")
        assert scorer.calls, "injected scorer was never consulted"


@pytest.mark.asyncio
class TestManagerStructuralForwardsQuery:
    @pytest.mark.parametrize(
        "strategy, class_name",
        [
            (CompressionStrategy.SCHEMA_PRUNING, "SchemaPruningCompressor"),
            (CompressionStrategy.SKELETON, "SkeletonCompressor"),
        ],
    )
    async def test_forwards_query_and_injects_scorer(
        self, tmp_path, monkeypatch, strategy, class_name
    ) -> None:
        proxy_cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={"srv": UpstreamServerConfig(prefix="test", compression=strategy)},
        )
        mgr = ProxyManager(proxy_cfg, TokenTracker(metrics_store=None))

        captured: dict[str, object] = {}

        def fake_factory(*, scorer=None):
            captured["scorer"] = scorer

            def compress(text, *, max_chars, context_query=None):
                captured["context_query"] = context_query
                return "OUT"

            return SimpleNamespace(compress=compress)

        monkeypatch.setattr(f"memtomem_stm.proxy.manager.{class_name}", fake_factory)

        out, fallback = await mgr._apply_compression(
            "x" * 500,
            strategy,
            100,
            None,  # sel_cfg
            None,  # llm_cfg
            None,  # hybrid_cfg
            "srv",
            "tool",
            context_query="orders total",
        )
        assert out == "OUT"
        assert fallback is None
        assert captured["context_query"] == "orders total"
        # The manager injects its own (hot-reload-aware) relevance scorer.
        assert captured["scorer"] is mgr._relevance_scorer
