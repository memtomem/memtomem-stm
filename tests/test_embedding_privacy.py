"""The embedding relevance scorer's credential screen (#1382).

``EmbeddingScorer`` sends the context query and each section's title and first
500 body characters to ``embedding_base_url``. A pass where any of that — or
the response the sections were rebuilt from — matches a credential pattern is
scored with BM25 and sends nothing, the rule the LLM compressor applies (#289).
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from memtomem_stm.proxy.privacy import CREDENTIAL_PATTERNS, contains_sensitive_content
from memtomem_stm.proxy.relevance import BM25Scorer, EmbeddingScorer, scoring_source

_SECRET = "password=hunter2"


def _scorer(privacy_scan: bool = True) -> EmbeddingScorer:
    return EmbeddingScorer(
        provider="ollama",
        model="test-model",
        base_url="http://ollama:11434",
        cache_size=0,
        privacy_scan=privacy_scan,
    )


def _post():
    """Patch ``httpx.post`` with an endpoint that embeds any batch it is sent."""

    def respond(url, *, json, **_kwargs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(
            return_value={"embeddings": [[1.0, float(i)] for i in range(len(json["input"]))]}
        )
        return resp

    return patch("httpx.post", side_effect=respond)


def _sent(post: MagicMock) -> list[str]:
    return [text for call in post.call_args_list for text in call.kwargs["json"]["input"]]


class TestScorerScreen:
    def test_a_clean_pass_is_sent(self):
        """Positive control: without a match the endpoint is asked."""
        sections = [("## A", "alpha beta"), ("## B", "gamma")]
        with _post() as post:
            _scorer().score_sections("alpha", sections)
        assert post.call_count == 1
        assert _sent(post) == ["alpha", "## A\nalpha beta", "## B\ngamma"]

    def test_a_credential_in_a_section_sends_nothing(self):
        scorer = _scorer()
        sections = [("## A", "alpha beta"), ("## Config", f"set {_SECRET} here")]
        with _post() as post:
            scores = scorer.score_sections("alpha", sections)
        assert post.call_count == 0
        assert scores == BM25Scorer().score_sections("alpha", sections)
        # The endpoint was never asked, so nothing failed.
        assert scorer.fallback_count == 0

    def test_a_credential_in_the_query_sends_nothing(self):
        sections = [("## A", "alpha beta"), ("## B", "gamma")]
        with _post() as post:
            scores = _scorer().score_sections(f"alpha {_SECRET}", sections)
        assert post.call_count == 0
        assert scores == BM25Scorer().score_sections(f"alpha {_SECRET}", sections)

    def test_a_credential_past_the_cut_still_withholds_the_pass(self):
        """The whole section is screened, not only the 500 characters sent: a
        credential straddling the cut leaves part of itself in what is sent."""
        body = "x " * 249 + f" {_SECRET}"
        assert len(body) > 500 and _SECRET not in body[:500]
        with _post() as post:
            _scorer().score_sections("alpha", [("## A", body), ("## B", "alpha")])
        assert post.call_count == 0

    def test_a_match_the_cut_creates_withholds_the_pass(self):
        """The exact text sent is screened too: an AWS key ID whose run of
        characters continues in the full body does not match there, but ends at
        the cut and matches in what would be sent."""
        body = " " * 480 + "AKIA" + "A" * 16 + "Z"
        assert not contains_sensitive_content(f"## A\n{body}", CREDENTIAL_PATTERNS)
        assert contains_sensitive_content(f"## A\n{body[:500]}", CREDENTIAL_PATTERNS)
        with _post() as post:
            _scorer().score_sections("alpha", [("## A", body), ("## B", "alpha")])
        assert post.call_count == 0

    def test_an_email_alone_is_sent(self):
        """Credentials only, as on the LLM path: an address is ordinary content."""
        with _post() as post:
            _scorer().score_sections("alpha", [("## A", "mail ops@example.com"), ("## B", "b")])
        assert post.call_count == 1

    def test_the_scan_can_be_turned_off(self):
        with _post() as post:
            _scorer(privacy_scan=False).score_sections(
                "alpha", [("## A", f"set {_SECRET} here"), ("## B", "b")]
            )
        assert post.call_count == 1
        assert f"## A\nset {_SECRET} here" in _sent(post)

    def test_a_credential_in_the_bound_source_withholds_clean_sections(self):
        """A source bound by the compressor is screened as well; the sections
        rebuilt from it may have split the match apart."""
        sections = [("password", '"hunter2"'), ("## B", "alpha")]
        assert not any(
            contains_sensitive_content(f"{t}\n{b}", CREDENTIAL_PATTERNS) for t, b in sections
        )
        with _post() as post:
            with scoring_source('{"password": "hunter2"}'):
                _scorer().score_sections("alpha", sections)
            assert post.call_count == 0
            # The binding ends with its block.
            _scorer().score_sections("alpha", sections)
        assert post.call_count == 1

    def test_nested_sources_add_to_the_outer_ones(self):
        """HYBRID binds the whole response and a sub-compressor binds only the
        tail; a label left in the head must still count."""
        with _post() as post:
            with scoring_source("password: "), scoring_source("hunter2 and more"):
                _scorer().score_sections("alpha", [("## A", "hunter2"), ("## B", "alpha")])
        assert post.call_count == 0

    def test_the_withheld_pass_is_logged_without_its_text(self, caplog):
        with caplog.at_level(logging.INFO, logger="memtomem_stm.proxy.relevance"):
            with _post():
                _scorer().score_sections("alpha", [("## A", _SECRET), ("## B", "b")])
        assert "scoring with BM25 instead" in caplog.text
        assert "hunter2" not in caplog.text


class TestCompressorsBindTheirSource:
    """End to end through compressors whose sections split a label from its
    value, so only the bound response shows the match."""

    _DOC = {"password": "hunter2", "notes": "alpha " * 60, "other": "beta " * 60}

    def test_selective_withholds_a_json_secret_split_across_title_and_body(self):
        from memtomem_stm.proxy.compression import SelectiveCompressor

        compressor = SelectiveCompressor(scorer=_scorer())
        with _post() as post:
            compressor.compress(json.dumps(self._DOC), max_chars=200, context_query="alpha")
        assert post.call_count == 0

    def test_selective_still_sends_a_clean_document(self):
        """Positive control for the test above: the same shape without the
        secret reaches the endpoint, so the zero is the screen's doing."""
        from memtomem_stm.proxy.compression import SelectiveCompressor

        doc = {k: v for k, v in self._DOC.items() if k != "password"}
        compressor = SelectiveCompressor(scorer=_scorer())
        with _post() as post:
            compressor.compress(json.dumps(doc), max_chars=200, context_query="alpha")
        assert post.call_count == 1

    @staticmethod
    def _markdown(preamble: str) -> str:
        """Three multi-line sections after a preamble. TRUNCATE and SKELETON
        score only the sections, so a credential in the preamble reaches the
        screen through the bound response alone."""

        def para(word: str) -> str:
            return "\n".join(f"{word} line {i} " + word * 3 for i in range(30))

        return (
            f"{preamble}\n\n## Intro\nintro first\n{para('alpha')}\n\n"
            f"## Config\nconfig first\n{para('beta')}\n\n"
            f"## Tail\ntail first\n{para('gamma')}"
        )

    @pytest.mark.parametrize("kind", ["truncate", "skeleton"])
    @pytest.mark.parametrize(("preamble", "posts"), [(_SECRET, 0), ("plain preamble", 1)])
    def test_markdown_compressors_bind_their_response(self, kind, preamble, posts):
        from memtomem_stm.proxy.compression import SkeletonCompressor, TruncateCompressor

        cls = TruncateCompressor if kind == "truncate" else SkeletonCompressor
        with _post() as post:
            cls(scorer=_scorer()).compress(
                self._markdown(preamble), max_chars=900, context_query="alpha"
            )
        assert post.call_count == posts
        assert not any("hunter2" in text for text in _sent(post))

    def test_the_text_keyword_still_binds(self):
        """The decorator keeps ``compress``'s signature: ``text=`` is accepted
        and bound like a positional argument."""
        from memtomem_stm.proxy.compression import TruncateCompressor

        assert TruncateCompressor().compress(text="hello", max_chars=100) == "hello"
        with _post() as post:
            TruncateCompressor(scorer=_scorer()).compress(
                text=self._markdown(_SECRET), max_chars=900, context_query="alpha"
            )
        assert post.call_count == 0

    @pytest.mark.parametrize("kind", ["selective", "schema_pruning"])
    @pytest.mark.parametrize(
        "doc",
        [
            '{"\\u0070assword": "hunter2", "notes": "%s", "other": "%s"}',
            '{"db": {"\\u0070assword": "hunter2"}, "notes": "%s", "other": "%s"}',
        ],
        ids=["top-level", "nested"],
    )
    def test_a_json_escaped_label_is_seen_decoded(self, kind, doc):
        """The parser decodes the key before splitting it from its value, so
        the endpoint would get ``password`` and ``hunter2`` while the raw
        response matches no pattern; the source is screened decoded."""
        from memtomem_stm.proxy.compression import SchemaPruningCompressor, SelectiveCompressor

        text = doc % ("alpha " * 60, "beta " * 60)
        assert not contains_sensitive_content(text, CREDENTIAL_PATTERNS)
        cls = SelectiveCompressor if kind == "selective" else SchemaPruningCompressor
        with _post() as post:
            cls(scorer=_scorer()).compress(text, max_chars=200, context_query="alpha")
        assert post.call_count == 0

    def test_schema_pruning_withholds_a_top_level_secret(self):
        from memtomem_stm.proxy.compression import SchemaPruningCompressor

        compressor = SchemaPruningCompressor(scorer=_scorer())
        with _post() as post:
            compressor.compress(json.dumps(self._DOC), max_chars=200, context_query="alpha")
        assert post.call_count == 0


class TestManagerWiring:
    @staticmethod
    def _config(**scorer: object):
        from memtomem_stm.proxy.config import ProxyConfig, RelevanceScorerConfig

        return ProxyConfig(relevance_scorer=RelevanceScorerConfig(scorer="embedding", **scorer))

    def test_the_flag_reaches_the_scorer(self):
        from memtomem_stm.proxy.manager import ProxyManager

        scorer = ProxyManager._create_scorer(self._config(privacy_scan_enabled=False))
        assert isinstance(scorer, EmbeddingScorer)
        assert scorer._privacy_scan is False
        assert ProxyManager._create_scorer(self._config())._privacy_scan is True

    @staticmethod
    def _unscanned_lines(caplog) -> list[str]:
        return [r.getMessage() for r in caplog.records if "UNSCANNED" in r.getMessage()]

    @pytest.mark.parametrize(
        ("base_url", "scan", "warns"),
        [
            ("https://user:pw@embed.example/v1?token=t", False, True),
            ("http://127.0.0.1:11434", False, False),
            ("http://localhost:11434", False, False),
            ("https://embed.example/v1", True, False),
        ],
    )
    def test_construction_warns_only_for_an_unscanned_external_endpoint(
        self, caplog, tmp_path, base_url, scan, warns
    ):
        from memtomem_stm.proxy.config import ProxyConfig, RelevanceScorerConfig
        from memtomem_stm.proxy.manager import ProxyManager
        from memtomem_stm.proxy.metrics import TokenTracker

        cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            relevance_scorer=RelevanceScorerConfig(
                scorer="embedding", embedding_base_url=base_url, privacy_scan_enabled=scan
            ),
        )
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            ProxyManager(cfg, TokenTracker())
        lines = self._unscanned_lines(caplog)
        assert len(lines) == (1 if warns else 0)
        if warns:
            assert "relevance_scorer.privacy_scan_enabled=false" in lines[0]
            # Through ``diagnostic_url``: no userinfo, query or token.
            assert "embed.example" in lines[0]
            assert "pw" not in lines[0] and "token" not in lines[0]

    def _reload_target(self, tmp_path):
        from memtomem_stm.proxy.config import ProxyConfig, RelevanceScorerConfig
        from memtomem_stm.proxy.manager import ProxyManager
        from memtomem_stm.proxy.metrics import TokenTracker

        mgr = ProxyManager(ProxyConfig(config_path=tmp_path / "proxy.json"), TokenTracker())
        unsafe = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            relevance_scorer=RelevanceScorerConfig(
                scorer="embedding",
                embedding_base_url="https://embed.example",
                privacy_scan_enabled=False,
            ),
        )
        return mgr, unsafe

    def test_a_hot_reload_into_the_unscanned_state_warns_once(self, caplog, tmp_path):
        from types import SimpleNamespace

        mgr, unsafe = self._reload_target(tmp_path)
        mgr._config_loader = SimpleNamespace(current=unsafe)  # the live generation
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            mgr._relevance_scorer_for(unsafe)
            mgr._relevance_scorer_for(unsafe)
        # Once per rebuild: the unchanged second read reuses the instance.
        assert len(self._unscanned_lines(caplog)) == 1

    def test_a_stale_pinned_request_does_not_repeat_it(self, caplog, tmp_path):
        """A request whose config is no longer the live one gets a scorer of
        its own on every call; warning there would repeat per request."""
        from types import SimpleNamespace

        mgr, unsafe = self._reload_target(tmp_path)
        mgr._config_loader = SimpleNamespace(current=object())
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            first = mgr._relevance_scorer_for(unsafe)
            mgr._relevance_scorer_for(unsafe)
        assert isinstance(first, EmbeddingScorer) and first._privacy_scan is False
        assert self._unscanned_lines(caplog) == []


class TestExternalDestination:
    @pytest.mark.parametrize(
        ("base_url", "external"),
        [
            (None, False),  # the ollama default, localhost
            ("http://[::1]:11434", False),
            ("https://api.openai.com", True),
            ("http://10.0.0.5:11434", True),
            ("http://[::1", True),  # does not parse: warn rather than raise
        ],
    )
    def test_judged_by_host(self, base_url, external):
        from memtomem_stm.proxy.config import RelevanceScorerConfig

        cfg = RelevanceScorerConfig(embedding_base_url=base_url)
        assert cfg.is_external_destination() is external
