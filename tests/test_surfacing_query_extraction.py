"""Query-extraction improvements for surfacing.

Three corrections in ContextExtractor:
1. Grep/Glob ``pattern`` (and ``glob``) values are tokenized into searchable
   words, so those tools produce a query that can clear ``min_query_tokens``
   instead of one opaque regex/glob token that suppressed surfacing.
2. Arguments are read in sorted-key order, so two identical calls built in a
   different order yield the same query (stable surfacing cache key + dedup).
3. The identifier filter applies to semantic-keyed values too — a uuid/hex id
   under a key like ``name`` no longer leaks into the query.
"""

from __future__ import annotations

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.context_extractor import ContextExtractor

_UUID = "550e8400-e29b-41d4-a716-446655440000"


def _extract(tool: str, arguments: dict) -> str | None:
    return ContextExtractor().extract_query("builtin", tool, arguments, SurfacingConfig())


class TestPatternTokenization:
    def test_tokenize_pattern_splits_metacharacters(self) -> None:
        assert ContextExtractor._tokenize_pattern("auth.*token_handler") == "auth token handler"
        assert ContextExtractor._tokenize_pattern("**/*.jwt_handler.py") == "jwt handler py"

    def test_tokenize_pattern_drops_short_and_numeric(self) -> None:
        # single chars, empties, and pure numbers are dropped
        assert ContextExtractor._tokenize_pattern("a.*b/123/name") == "name"

    def test_grep_pattern_produces_searchable_query(self) -> None:
        # Pre-fix: "auth.*token_handler" was one token → below min_query_tokens
        # (3) → surfacing suppressed. Now it tokenizes and surfaces.
        q = _extract("Grep", {"pattern": "auth.*token_handler", "path": "src/auth"})
        assert q is not None
        assert "auth" in q and "token" in q and "handler" in q

    def test_glob_pattern_tokenized(self) -> None:
        q = _extract("Glob", {"pattern": "**/*.jwt_handler.py"})
        assert q == "jwt handler py"


class TestArgumentOrderDeterminism:
    def test_reordered_args_produce_same_query(self) -> None:
        a = _extract("tool", {"zebra": "alpha bravo charlie", "apple": "delta echo foxtrot"})
        b = _extract("tool", {"apple": "delta echo foxtrot", "zebra": "alpha bravo charlie"})
        assert a == b
        assert a is not None


class TestSemanticKeyIdentifierFilter:
    def test_uuid_under_semantic_key_is_filtered(self) -> None:
        # A uuid under "name" must not become the query (it is an opaque id).
        assert _extract("tool", {"name": _UUID}) is None

    def test_real_semantic_value_still_used(self) -> None:
        assert _extract("tool", {"name": "jwt token handler"}) == "jwt token handler"

    def test_hex_under_semantic_key_is_filtered(self) -> None:
        assert _extract("tool", {"title": "abcdef0123456789abcdef0123456789"}) is None
