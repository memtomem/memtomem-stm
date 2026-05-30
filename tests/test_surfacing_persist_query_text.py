"""Issue #352 part 3 — ``SurfacingConfig.persist_query_text`` opt-in
hashing of the persisted query column.

Three layers exercised:

1. **Engine boundary** — when the flag is off, both the cache-miss and
   cache-hit ``record_surfacing`` call sites must pass
   ``"sha256:" + sha256(query)[:16]`` rather than the raw text. The
   default (``True``) preserves the legacy raw-text contract.
2. **Store stats** — ``FeedbackStore.get_stats`` ``query_preview`` must
   pass the 23-char hash form through verbatim (no 80-char clip) so
   the digest tail survives, while keeping the legacy clip for raw
   text.
3. **Server formatter** — ``stm_surfacing_stats`` renders a one-line
   legend when any recent row carries the ``sha256:`` prefix so
   operators reading the output don't mistake the digest for a
   malformed query.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem_stm.surfacing.engine import _QUERY_HASH_PREFIX, SurfacingEngine
from memtomem_stm.surfacing.feedback_store import FeedbackStore


# ── Test fixtures (lightweight reuse of existing patterns) ────────────


@dataclass
class FakeChunkMeta:
    namespace: str = "default"


@dataclass
class FakeChunk:
    id: str = "m1"
    content: str = "memory body"
    source: str = "source.md"
    meta: FakeChunkMeta = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.meta is None:
            self.meta = FakeChunkMeta()


@dataclass
class FakeSearchResult:
    chunk: FakeChunk
    score: float = 0.5


def _make_config(**overrides):
    from memtomem_stm.surfacing.config import SurfacingConfig

    defaults = {
        "enabled": True,
        "min_response_chars": 10,
        "timeout_seconds": 5.0,
        "min_score": 0.02,
        "max_results": 3,
        "cooldown_seconds": 0.0,
        "max_surfacings_per_minute": 1000,
        "auto_tune_enabled": False,
        "include_session_context": False,
        "fire_webhook": False,
        "cache_ttl_seconds": 60.0,
        "query_retention_days": 0,
    }
    defaults.update(overrides)
    return SurfacingConfig(**defaults)


def _make_mcp_adapter(results: list[FakeSearchResult]):
    """Same 3-tuple shape as ``tests/test_surfacing_engine._make_mcp_adapter``
    — outcome string is the third element. Kept local to this file to
    avoid cross-test imports."""
    adapter = AsyncMock()
    adapter.search = AsyncMock(
        return_value=(list(results), [], "ok" if results else "empty_results")
    )
    return adapter


def _make_tracker():
    tracker = MagicMock()
    tracker.record_surfacing = MagicMock()
    tracker.store = MagicMock()
    tracker.store.mark_surfaced = MagicMock()
    tracker.store.get_seen_ids = MagicMock(return_value=set())
    tracker.store.cleanup_expired = MagicMock(return_value=0)
    return tracker


VALID_ARGS = {"path": "src/app.py", "_context_query": "Flask web framework architecture"}
LONG_RESPONSE = "x" * 200


# ── 1. Engine boundary ────────────────────────────────────────────────


class TestEngineHashesPersistedQuery:
    async def test_default_persists_raw_text(self):
        """``persist_query_text`` defaults to ``True`` — the legacy raw
        query text must reach ``record_surfacing`` unchanged so existing
        deployments see no behavior change until they explicitly opt
        out."""
        tracker = _make_tracker()
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter([FakeSearchResult(FakeChunk(), 0.9)]),
            feedback_tracker=tracker,
        )
        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)

        tracker.record_surfacing.assert_called_once()
        assert tracker.record_surfacing.call_args.kwargs["query"] == VALID_ARGS["_context_query"]

    async def test_disabled_persists_sha256_prefix(self):
        """With ``persist_query_text=False`` the engine substitutes
        ``sha256:`` + 16-hex-char digest for the query argument."""
        tracker = _make_tracker()
        engine = SurfacingEngine(
            config=_make_config(persist_query_text=False),
            mcp_adapter=_make_mcp_adapter([FakeSearchResult(FakeChunk(), 0.9)]),
            feedback_tracker=tracker,
        )
        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)

        tracker.record_surfacing.assert_called_once()
        persisted = tracker.record_surfacing.call_args.kwargs["query"]
        assert persisted.startswith(_QUERY_HASH_PREFIX)
        expected = (
            _QUERY_HASH_PREFIX
            + hashlib.sha256(VALID_ARGS["_context_query"].encode("utf-8")).hexdigest()[:16]
        )
        assert persisted == expected
        assert len(persisted) == len(_QUERY_HASH_PREFIX) + 16
        # And — critically — the raw query string never appears in the
        # persisted form, even as a substring.
        assert VALID_ARGS["_context_query"] not in persisted

    async def test_cache_hit_also_hashes(self):
        """The cache-hit ``record_surfacing`` call site is a parallel code
        path — both must apply the hash. Without this the in-process
        cache would leak raw queries on every cache hit even with the
        flag off."""
        tracker = _make_tracker()
        engine = SurfacingEngine(
            config=_make_config(persist_query_text=False, cooldown_seconds=0),
            mcp_adapter=_make_mcp_adapter([FakeSearchResult(FakeChunk(), 0.9)]),
            feedback_tracker=tracker,
        )
        # Miss + hit on the same query.
        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)

        assert tracker.record_surfacing.call_count == 2
        for call in tracker.record_surfacing.call_args_list:
            persisted = call.kwargs["query"]
            assert persisted.startswith(_QUERY_HASH_PREFIX)
            assert VALID_ARGS["_context_query"] not in persisted

    async def test_hash_is_deterministic_across_calls(self):
        """Same raw query → same digest. Lets operators cluster events by
        query without ever seeing the underlying text."""
        tracker = _make_tracker()
        engine = SurfacingEngine(
            config=_make_config(persist_query_text=False, cooldown_seconds=0),
            mcp_adapter=_make_mcp_adapter([FakeSearchResult(FakeChunk(), 0.9)]),
            feedback_tracker=tracker,
        )
        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)

        a = tracker.record_surfacing.call_args_list[0].kwargs["query"]
        b = tracker.record_surfacing.call_args_list[1].kwargs["query"]
        assert a == b


class TestSecretGuard:
    """A secret-bearing query is hashed even when ``persist_query_text=True``.

    Closes the proxy-path gap (#352 covered the opt-in knob, not secrets):
    a Bash ``command`` argument or a tokenized URL carrying an inline
    credential must never reach ``surfacing_events.query`` verbatim. The
    literals below are each verified to match ``proxy.privacy``'s default
    patterns; the clean queries are verified not to.
    """

    _SECRETS = [
        "git clone repo then sk-ABCDEFGHIJKLMNOPQRSTUVWX",
        "fetch url with api_key=abc12345",
        "token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "psql password=hunter2secret",
        "aws AKIAIOSFODNN7EXAMPLE",
        "gh ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
        "-----BEGIN RSA PRIVATE KEY-----",
    ]
    _CLEAN = [
        "find the authentication bug in login",
        "refactor the token parser module",
        "reset the user password screen please",
    ]

    @pytest.mark.parametrize("secret", _SECRETS)
    async def test_secret_query_hashed_when_persist_true(self, secret):
        # Default config → persist_query_text=True; the secret guard must
        # still hash it before it reaches record_surfacing.
        tracker = _make_tracker()
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter([FakeSearchResult(FakeChunk(), 0.9)]),
            feedback_tracker=tracker,
        )
        await engine.surface("s", "Bash", {"_context_query": secret}, LONG_RESPONSE)

        tracker.record_surfacing.assert_called_once()
        persisted = tracker.record_surfacing.call_args.kwargs["query"]
        assert persisted.startswith(_QUERY_HASH_PREFIX)
        assert secret not in persisted
        expected = _QUERY_HASH_PREFIX + hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]
        assert persisted == expected

    @pytest.mark.parametrize("clean", _CLEAN)
    async def test_clean_query_literal_when_persist_true(self, clean):
        # A non-secret query is unaffected by the guard — persisted raw so
        # feedback ratings keyed on (tool, query) keep correlating.
        tracker = _make_tracker()
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter([FakeSearchResult(FakeChunk(), 0.9)]),
            feedback_tracker=tracker,
        )
        await engine.surface("s", "read_file", {"_context_query": clean}, LONG_RESPONSE)

        tracker.record_surfacing.assert_called_once()
        assert tracker.record_surfacing.call_args.kwargs["query"] == clean

    async def test_secret_guard_unit_level(self):
        # Direct unit assertion on the chokepoint, independent of surface().
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter([]),
            feedback_tracker=_make_tracker(),
        )
        out = engine._persistable_query("sk-ABCDEFGHIJKLMNOPQRSTUVWX")
        assert out.startswith(_QUERY_HASH_PREFIX)
        assert engine._persistable_query("ordinary search text") == "ordinary search text"


class TestPersistSitesRouteThroughGuard:
    """Both persistence sites must funnel through ``_persistable_query`` so
    the guard cannot be bypassed by one path. The cache-hit
    (``_render_cached``) and miss (``_do_surface_miss``) call sites are
    shape-identical, so pin them by source inspection."""

    def test_both_persist_sites_wrap_query(self):
        import inspect

        s = inspect.getsource(SurfacingEngine)
        assert s.count("query=self._persistable_query(query)") >= 2


# ── 2. Store stats query_preview ──────────────────────────────────────


class TestStatsPreviewPreservesHash:
    def test_hash_passes_through_verbatim(self, tmp_path: Path) -> None:
        """The 23-char ``sha256:<16-hex>`` form must not be clipped — the
        digest tail is the disambiguating bit and clipping it defeats
        the opaque-ID purpose."""
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()
        hashed = _QUERY_HASH_PREFIX + "0123456789abcdef"
        store.record_surfacing(
            surfacing_id="evt-hashed",
            server="s",
            tool="read_file",
            query=hashed,
            memory_ids=["m1"],
            scores=[0.7],
        )
        stats = store.get_stats(limit=5)
        assert len(stats["recent"]) == 1
        assert stats["recent"][0]["query_preview"] == hashed
        store.close()

    def test_raw_text_still_clipped_at_80_chars(self, tmp_path: Path) -> None:
        """Raw text must keep the legacy 80-char clip — the hash branch
        is additive, not a replacement."""
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()
        long_query = "x" * 200
        store.record_surfacing(
            surfacing_id="evt-raw",
            server="s",
            tool="read_file",
            query=long_query,
            memory_ids=["m1"],
            scores=[0.7],
        )
        stats = store.get_stats(limit=5)
        preview = stats["recent"][0]["query_preview"]
        assert preview.endswith("...")
        assert len(preview) == 80
        store.close()

    def test_raw_query_with_sha256_prefix_is_clipped(self, tmp_path: Path) -> None:
        """Prefix-only matching would misclassify a legitimate raw query
        that happens to start with ``sha256:`` (e.g. an agent searching
        for a checksum, or a tool argument carrying a literal hash) and
        bypass the 80-char clip, leaking unbounded text under the default
        ``persist_query_text=True``. The bypass must require the exact
        23-char ``sha256:<16-hex>`` shape, not the bare prefix."""
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()
        # 200-char raw query that begins with the sentinel prefix. Could
        # easily come from an LLM agent searching for "sha256:abc... why
        # does that hash appear in two places" or similar.
        adversarial = "sha256:" + ("user typed text with extra context " * 6)
        assert adversarial.startswith("sha256:")
        assert len(adversarial) > 80
        store.record_surfacing(
            surfacing_id="evt-prefix",
            server="s",
            tool="read_file",
            query=adversarial,
            memory_ids=["m1"],
            scores=[0.7],
        )
        stats = store.get_stats(limit=5)
        preview = stats["recent"][0]["query_preview"]
        # Clipped to 80 chars and ends with the ellipsis — must NOT pass
        # through verbatim.
        assert len(preview) == 80
        assert preview.endswith("...")
        # And the legitimate-hash regex must not match this clipped form
        # (the formatter relies on this to suppress the legend).
        from memtomem_stm.server import _HASHED_QUERY_PREVIEW_RE

        assert _HASHED_QUERY_PREVIEW_RE.fullmatch(preview) is None
        store.close()

    def test_invalid_hash_shape_is_clipped(self, tmp_path: Path) -> None:
        """``sha256:`` followed by the wrong number of hex chars, or by
        non-hex characters, is not a legitimate engine-written digest
        and must take the raw-text path."""
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()
        # Too short (15 hex chars), too long (17), and non-hex.
        cases = [
            "sha256:" + "0" * 15,
            "sha256:" + "0" * 17,
            "sha256:" + "g" * 16,
        ]
        for i, q in enumerate(cases):
            store.record_surfacing(
                surfacing_id=f"evt-bad-{i}",
                server="s",
                tool="read_file",
                query=q,
                memory_ids=["m1"],
                scores=[0.7],
            )
        stats = store.get_stats(limit=10)
        previews = sorted(row["query_preview"] for row in stats["recent"])
        # All three round-trip through the raw-text branch verbatim
        # (each is under 80 chars so no clip is applied).
        assert previews == sorted(cases)
        # And critically — the legitimate-hash regex matches none of
        # them, so the formatter legend would not fire.
        from memtomem_stm.server import _HASHED_QUERY_PREVIEW_RE

        for q in cases:
            assert _HASHED_QUERY_PREVIEW_RE.fullmatch(q) is None
        store.close()


# ── 3. Server formatter legend ────────────────────────────────────────


@pytest.fixture
def _stm_context():
    """Build a STMContext-like object the way ``_make_ctx`` does in
    tests/test_server_tools.py — bypasses MCP plumbing and lets the
    formatter run against a pre-built stats payload."""
    from types import SimpleNamespace

    from memtomem_stm.proxy.config import ProxyConfig
    from memtomem_stm.proxy.manager import ProxyManager
    from memtomem_stm.server import STMConfig, STMContext
    from memtomem_stm.proxy.metrics import TokenTracker

    def _build(tracker):
        token_tracker = TokenTracker()
        proxy = ProxyManager(
            ProxyConfig(config_path="/tmp/proxy.json", upstream_servers={}), token_tracker
        )
        app = STMContext(
            config=STMConfig(),
            proxy_manager=proxy,
            tracker=token_tracker,
            surfacing_engine=None,
            feedback_tracker=tracker,
            compression_feedback_tracker=None,
            progressive_reads_tracker=None,
        )
        return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))

    return _build


def _stats_payload_with_recent(*previews: str) -> dict:
    return {
        "events_total": len(previews),
        "distinct_tools": 1,
        "date_range": {"first": 1_700_000_000.0, "last": 1_700_000_999.0},
        "per_tool_breakdown": [
            {
                "tool": "t",
                "events": len(previews),
                "avg_memory_count": 1.0,
                "feedback_count": 0,
                "not_relevant_count": 0,
                "negative_count": 0,
            }
        ],
        "rating_distribution": {},
        "total_feedback": 0,
        "recent": [
            {
                "ts": 1_700_000_999.0 - i,
                "tool": "t",
                "query_preview": p,
                "memory_ids": ["m1"],
                "scores": [0.5],
            }
            for i, p in enumerate(previews)
        ],
    }


class TestStatsLegend:
    async def test_legend_fires_when_any_row_is_hashed(self, _stm_context):
        from memtomem_stm.server import stm_surfacing_stats

        tracker = MagicMock()
        tracker.get_stats.return_value = _stats_payload_with_recent(
            "sha256:0123456789abcdef", "plain text query"
        )
        result = await stm_surfacing_stats(ctx=_stm_context(tracker))

        assert "persist_query_text=false" in result
        assert "sha256:0123456789abcdef" in result
        # Plain row still renders alongside.
        assert "plain text query" in result

    async def test_no_legend_when_all_rows_raw(self, _stm_context):
        from memtomem_stm.server import stm_surfacing_stats

        tracker = MagicMock()
        tracker.get_stats.return_value = _stats_payload_with_recent("hello world", "another query")
        result = await stm_surfacing_stats(ctx=_stm_context(tracker))

        assert "persist_query_text=false" not in result
        assert "sha256:" not in result

    async def test_no_legend_for_raw_sha256_prefix_lookalike(self, _stm_context):
        """A raw query that begins with ``sha256:`` but is not the exact
        23-char ``sha256:<16-hex>`` shape (e.g. an 80-char-clipped
        user-typed checksum search) must not trigger the hash legend.
        Pins the formatter's exact-shape gate."""
        from memtomem_stm.server import stm_surfacing_stats

        tracker = MagicMock()
        # 80-char clipped form starting with the sentinel — would slip
        # past a naive ``startswith("sha256:")`` check.
        clipped_lookalike = "sha256:" + "x" * 70 + "..."
        tracker.get_stats.return_value = _stats_payload_with_recent(clipped_lookalike)
        result = await stm_surfacing_stats(ctx=_stm_context(tracker))

        assert "persist_query_text=false" not in result
        assert clipped_lookalike in result

    async def test_legend_suppressed_when_recent_empty(self, _stm_context):
        """No recent rows → no Recent: block → no legend regardless of
        config. Pins that the legend is data-driven."""
        from memtomem_stm.server import stm_surfacing_stats

        tracker = MagicMock()
        payload = _stats_payload_with_recent()
        tracker.get_stats.return_value = payload
        result = await stm_surfacing_stats(ctx=_stm_context(tracker))

        assert "Recent:" not in result
        assert "persist_query_text=false" not in result
