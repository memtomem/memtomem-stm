"""Unit tests for the UTF-8-safe JSON writer behind every ``--json`` leg (#757).

The end-to-end counterparts — that ``mms remove --yes --json`` exits 0 with a
parsable document after deleting a surrogate-bearing entry — live with the
commands in ``test_proxy_cli.py``. This module pins the serializer's own
contract: valid JSON, round-trip for lone surrogates, no collateral escaping,
and the one documented asymmetry it inherits from Python's JSON decoder.
"""

from __future__ import annotations

import json

import pytest

from memtomem_stm.cli._json_out import dumps, has_lone_surrogate

# One from each end of the high and low surrogate blocks, so a range that is
# off by one at either boundary fails.
SURROGATES = ["\ud800", "\udbff", "\udc00", "\udfff"]


class TestDumps:
    def test_identity_on_clean_input(self):
        """No surrogate present → byte-identical to ``json.dumps``.

        This is what keeps the fix invisible to every existing payload: the
        substitution matches nothing, so output is unchanged rather than
        merely equivalent.
        """
        payload = {"name": "🚀 서버 漢字", "n": 1, "ok": True, "z": None, "l": ["a", "b"]}
        assert dumps(payload, indent=2, ensure_ascii=False) == json.dumps(
            payload, indent=2, ensure_ascii=False
        )

    @pytest.mark.parametrize("surrogate", SURROGATES)
    def test_surrogate_value_is_escaped_and_round_trips(self, surrogate):
        payload = {"name": f"sr{surrogate}v"}
        out = dumps(payload, indent=2, ensure_ascii=False)

        assert f"\\u{ord(surrogate):04x}" in out
        out.encode("utf-8")  # the encode that used to raise
        assert json.loads(out) == payload

    @pytest.mark.parametrize("surrogate", SURROGATES)
    def test_surrogate_key_is_escaped_and_round_trips(self, surrogate):
        """Config server names are dict *keys*, which is how they reach the
        ``list``/``status`` payloads — the substitution is text-level, so it
        covers keys and values alike."""
        payload = {f"sr{surrogate}v": {"prefix": "sx"}}
        out = dumps(payload, indent=2, ensure_ascii=False)

        assert f"\\u{ord(surrogate):04x}" in out
        out.encode("utf-8")
        assert json.loads(out) == payload

    def test_astral_neighbours_stay_raw(self):
        """A surrogate between two emoji is escaped without touching them.

        Emoji are single code points above U+FFFF, never surrogate *pairs*,
        in a Python ``str`` — the reason a blanket character-class
        substitution cannot corrupt them.
        """
        out = dumps({"name": "🚀\ud800🚀"}, ensure_ascii=False)

        assert "🚀" in out and out.count("🚀") == 2
        assert "\\ud800" in out
        assert json.loads(out) == {"name": "🚀\ud800🚀"}

    def test_literal_backslash_u_text_is_not_double_escaped(self):
        """A value carrying the six ASCII characters ``\\ud800`` is data, not
        a surrogate, and must survive untouched.

        Not hypothetical: several ``--json`` ``message`` fields interpolate a
        ``repr()``, which renders a surrogate as exactly this ASCII text.
        """
        payload = {"message": "name is '\\ud800'"}
        out = dumps(payload, ensure_ascii=False)

        assert json.loads(out) == payload

    def test_kwargs_pass_through(self):
        """The compact ``sort_keys`` sites (``mms gateway status``/``explain``)
        must keep their formatting."""
        out = dumps({"b": 1, "a": "x\ud800"}, ensure_ascii=False, sort_keys=True)

        assert out == '{"a": "x\\ud800", "b": 1}'

    def test_escaped_backslash_before_a_surrogate_keeps_the_escape_fresh(self):
        """The safety argument is about *parity*, not absence, of backslashes.

        ``dumps`` renders one backslash as two, so the substituted character is
        always preceded by an even number of them and the inserted ``\\u``
        opens a new escape rather than continuing one.
        """
        payload = {"n": "\\\ud800"}
        out = dumps(payload, ensure_ascii=False)

        assert json.loads(out) == payload

    def test_adjacent_pair_collapses_exactly_as_plain_json_dumps_does(self):
        """The documented round-trip caveat, pinned so it is not mistaken for a
        regression: an adjacent high+low pair decodes as the astral character
        it encodes. Python's decoder does this, not us — ``json.dumps`` with
        ``ensure_ascii=True`` produces the same text and the same asymmetry.
        """
        payload = {"n": "\ud83d\ude80"}  # two code units, not the astral char

        out = dumps(payload, ensure_ascii=False)

        assert out == json.dumps(payload)  # identical to the stdlib's own escaping
        assert json.loads(out) == {"n": "\U0001f680"}

    def test_surrogate_survives_a_dumps_loads_dumps_cycle(self):
        """Idempotent: re-serializing a decoded document produces the same
        text, so a config rewritten by ``_save`` does not accumulate escapes.
        """
        payload = {"name": "s\ud800x"}
        once = dumps(payload, indent=2, ensure_ascii=False)

        assert dumps(json.loads(once), indent=2, ensure_ascii=False) == once


class TestHasLoneSurrogate:
    """The gate on names entering the config — serializable is not usable."""

    @pytest.mark.parametrize("surrogate", SURROGATES)
    def test_detects_each_boundary_surrogate(self, surrogate):
        assert has_lone_surrogate(f"sr{surrogate}v") is True

    @pytest.mark.parametrize("value", ["", "fs", "서버 🚀", "a\\ud800b", "sr\tv"])
    def test_clean_and_merely_unusual_names_pass(self, value):
        """Encodable is the whole test: non-ASCII names, a tab, and the literal
        ASCII text ``\\ud800`` are all writable, so none is refused."""
        assert has_lone_surrogate(value) is False
        value.encode("utf-8")

    def test_an_adjacent_pair_is_refused_too(self):
        """Recombination is the *JSON decoder's* behaviour, not Python's
        encoder: a ``str`` holding the two code units still raises on
        ``.encode()``, so such a name is exactly as unusable as a lone one and
        the predicate must not carve out an exception for it.
        """
        pair = "ok" + chr(0xD83D) + chr(0xDE80)
        with pytest.raises(UnicodeEncodeError):
            pair.encode("utf-8")

        assert has_lone_surrogate(pair) is True
