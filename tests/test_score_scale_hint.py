"""Direct unit tests for the score-scale hint helpers (#1555).

``KNOWN_SCORE_SCALES``, ``render_score_scale`` and ``read_score_scale_hint``
in ``memtomem_stm.surfacing.mcp_client`` were only exercised indirectly through
the parsers and the engine; these tests pin their contracts directly.
"""

from __future__ import annotations

import pytest

from memtomem_stm.surfacing.mcp_client import (
    KNOWN_SCORE_SCALES,
    read_score_scale_hint,
    render_score_scale,
)


class TestKnownScoreScales:
    def test_exact_label_set(self):
        assert KNOWN_SCORE_SCALES == frozenset({"rrf", "bm25", "dense", "none", "rerank"})

    def test_is_an_immutable_frozenset(self):
        assert isinstance(KNOWN_SCORE_SCALES, frozenset)


class TestRenderScoreScale:
    def test_none_stays_none(self):
        assert render_score_scale(None) is None

    @pytest.mark.parametrize("label", sorted(KNOWN_SCORE_SCALES))
    def test_known_label_is_itself(self, label):
        assert render_score_scale(label) == label

    def test_unknown_string_is_unrecognized(self):
        assert render_score_scale("cosine") == "unrecognized"

    @pytest.mark.parametrize("value", [42, 3.5, True, ["rrf"], {"scale": "rrf"}])
    def test_non_string_is_unrecognized(self, value):
        assert render_score_scale(value) == "unrecognized"


class TestReadScoreScaleHint:
    @pytest.mark.parametrize("label", sorted(KNOWN_SCORE_SCALES))
    def test_known_label_kept_verbatim(self, label):
        assert read_score_scale_hint(label) == label

    def test_unknown_non_empty_string_kept_verbatim(self):
        # Telemetry wants the label the core sent, even an unrecognized one.
        assert read_score_scale_hint("cosine") == "cosine"

    @pytest.mark.parametrize("raw", [None, "", 0, 3.5, True, ["rrf"], {"scale": "rrf"}])
    def test_absent_or_non_string_degrades_to_none(self, raw):
        assert read_score_scale_hint(raw) is None
