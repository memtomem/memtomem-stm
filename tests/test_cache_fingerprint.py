"""Tests for ``compression_fingerprint`` — the settings hash in the
response-cache key.

The cache stores the shaped+compressed response body, so any setting that
changes those bytes must rotate the cache key; otherwise a config change (hot
reload included) keeps serving bodies produced under the old settings.
"""

from __future__ import annotations

import dataclasses

import pytest

from memtomem_stm.proxy.config import (
    CleaningConfig,
    CompressionStrategy,
    HybridConfig,
    LLMCompressorConfig,
    ProgressiveConfig,
    RelevanceScorerConfig,
    SelectiveConfig,
)
from memtomem_stm.proxy.manager import (
    _FINGERPRINT_EXCLUDED_FIELDS,
    _FINGERPRINT_FIELDS,
    ToolConfig,
    compression_fingerprint,
)

_MIN_RETENTION = 0.65
_MAX_UPSTREAM = 10_000_000
_SCORER = RelevanceScorerConfig()


def _tc(**overrides) -> ToolConfig:
    base = dict(
        compression=CompressionStrategy.TRUNCATE,
        max_chars=2000,
        llm=None,
        auto_index_enabled=False,
        selective=None,
        cleaning=CleaningConfig(),
        hybrid=None,
        extraction_enabled=False,
        progressive=None,
        retention_floor=None,
    )
    base.update(overrides)
    return ToolConfig(**base)


def _fp(
    tc: ToolConfig,
    *,
    min_retention=_MIN_RETENTION,
    max_upstream=_MAX_UPSTREAM,
    scorer=_SCORER,
) -> str:
    return compression_fingerprint(tc, min_retention, max_upstream, scorer)


class TestFieldClassification:
    def test_every_tool_config_field_is_classified(self):
        """Adding a ``ToolConfig`` field must force a decision: does it change
        the compressed bytes stored in the cache (fingerprint it) or not
        (exclude it)? An unclassified field would silently reintroduce the
        stale-body bug this fingerprint exists to prevent."""
        all_fields = {f.name for f in dataclasses.fields(ToolConfig)}
        assert _FINGERPRINT_FIELDS | _FINGERPRINT_EXCLUDED_FIELDS == all_fields
        assert not _FINGERPRINT_FIELDS & _FINGERPRINT_EXCLUDED_FIELDS

    def test_fingerprint_payload_covers_declared_fields(self):
        """The function body and the declared classification cannot drift:
        every fingerprinted field name changes the fingerprint."""
        fp_default = _fp(_tc())
        assert isinstance(fp_default, str) and len(fp_default) == 64
        for field in _FINGERPRINT_FIELDS:
            mutated = _mutate(field)
            assert _fp(mutated) != fp_default, (
                f"mutating fingerprinted field {field!r} did not change the fingerprint"
            )


def _mutate(field: str) -> ToolConfig:
    """A ToolConfig whose ``field`` differs from ``_tc()``'s default."""
    values = {
        "compression": _tc(compression=CompressionStrategy.SKELETON),
        "max_chars": _tc(max_chars=4000),
        "retention_floor": _tc(retention_floor=0.5),
        "cleaning": _tc(cleaning=CleaningConfig(strip_html=False)),
        "hybrid": _tc(hybrid=HybridConfig(head_chars=1234)),
        "selective": _tc(selective=SelectiveConfig(json_depth=3)),
        "progressive": _tc(progressive=ProgressiveConfig(chunk_size=1111)),
        "llm": _tc(llm=LLMCompressorConfig(model="gpt-x", api_key="sk-test")),
    }
    return values[field]


class TestFingerprintBehavior:
    def test_deterministic(self):
        assert _fp(_tc()) == _fp(_tc())

    def test_min_result_retention_changes_fingerprint(self):
        assert _fp(_tc(), min_retention=0.65) != _fp(_tc(), min_retention=0.3)

    def test_max_upstream_chars_changes_fingerprint(self):
        """``max_upstream_chars`` truncates ``original_text`` at Stage-3 SHAPE
        (before cleaning/compression), so an oversized response is cached under
        this budget — lowering it must rotate the key."""
        assert _fp(_tc(), max_upstream=10_000_000) != _fp(_tc(), max_upstream=5_000)

    def test_scorer_type_changes_fingerprint(self):
        """The query-aware compressors (TRUNCATE/SCHEMA_PRUNING/SKELETON)
        allocate budget with the relevance scorer, so switching bm25↔embedding
        changes the cached bytes for a query-bearing call — the key must
        rotate."""
        bm25 = RelevanceScorerConfig(scorer="bm25")
        embed = RelevanceScorerConfig(scorer="embedding")
        assert _fp(_tc(), scorer=bm25) != _fp(_tc(), scorer=embed)

    def test_embedding_model_changes_fingerprint(self):
        a = RelevanceScorerConfig(scorer="embedding", embedding_model="nomic-embed-text")
        b = RelevanceScorerConfig(scorer="embedding", embedding_model="mxbai-embed-large")
        assert _fp(_tc(), scorer=a) != _fp(_tc(), scorer=b)

    def test_api_key_does_not_change_fingerprint(self):
        """``llm.api_key`` never affects the compressed bytes, it is secret
        material, and the config validator injects it from the environment —
        including it would make the same config fingerprint differently per
        machine."""
        a = _tc(llm=LLMCompressorConfig(model="m", api_key="sk-one"))
        b = _tc(llm=LLMCompressorConfig(model="m", api_key="sk-two"))
        assert _fp(a) == _fp(b)

    def test_llm_model_changes_fingerprint(self):
        a = _tc(llm=LLMCompressorConfig(model="m1", api_key="sk-test"))
        b = _tc(llm=LLMCompressorConfig(model="m2", api_key="sk-test"))
        assert _fp(a) != _fp(b)

    def test_excluded_fields_do_not_change_fingerprint(self):
        base = _fp(_tc())
        assert _fp(_tc(auto_index_enabled=True)) == base
        assert _fp(_tc(extraction_enabled=True)) == base

    def test_path_and_enum_fields_serialize(self):
        """``SelectiveConfig.pending_store_path`` is a ``Path`` and several
        sub-configs carry enums — the fingerprint must dump them via
        ``mode="json"`` instead of raising ``TypeError`` on the hot path."""
        tc = _tc(
            compression=CompressionStrategy.SELECTIVE,
            selective=SelectiveConfig(pending_store="sqlite"),
            hybrid=HybridConfig(),
            llm=LLMCompressorConfig(api_key="sk-test"),
            progressive=ProgressiveConfig(),
        )
        assert len(_fp(tc)) == 64

    def test_none_sub_configs_distinct_from_defaults(self):
        """``hybrid=None`` (resolver found no config) and an explicit default
        ``HybridConfig()`` hash differently — the compressor behaves
        differently in the two cases, so they must not share rows."""
        assert _fp(_tc(hybrid=None)) != _fp(_tc(hybrid=HybridConfig()))


class TestManagerWiring:
    @pytest.mark.asyncio
    async def test_unknown_server_yields_empty_fingerprint(self, tmp_path):
        """Mirrors the unknown-server posture of ``_resolve_cache_ttl`` /
        ``_tool_cache_eligible``: direct dispatch against an unregistered
        server must not raise out of ``_resolve_tool_config``."""
        from memtomem_stm.proxy.config import ProxyConfig
        from memtomem_stm.proxy.manager import ProxyManager
        from memtomem_stm.proxy.metrics import TokenTracker

        cfg = ProxyConfig(config_path=tmp_path / "p.json", upstream_servers={})
        mgr = ProxyManager(cfg, TokenTracker(metrics_store=None))
        assert mgr._cache_key_fingerprint("ghost", "tool", cfg_snap=cfg) == ""


class TestCanonicalization:
    def test_payload_is_canonical_json(self):
        """Regression guard for accidental dict-order sensitivity: the
        fingerprint is a hash of sorted-key JSON, so two semantically equal
        ToolConfigs always agree."""
        a = _tc(cleaning=CleaningConfig(strip_html=True, deduplicate=True))
        b = _tc(cleaning=CleaningConfig(deduplicate=True, strip_html=True))
        assert _fp(a) == _fp(b)

    def test_fingerprint_is_hex_sha256(self):
        fp = _fp(_tc())
        int(fp, 16)  # raises if not hex
        assert len(fp) == 64
