"""Unit tests for the UTF-8-safe JSON writer behind every ``--json`` leg (#757).

The end-to-end counterparts — that ``mms remove --yes --json`` exits 0 with a
parsable document after deleting a surrogate-bearing entry — live with the
commands in ``test_proxy_cli.py``. This module pins the serializer's own
contract: valid JSON, exact round-trip, and no collateral escaping.
"""

from __future__ import annotations

import json

import pytest

from memtomem_stm.cli._json_out import dumps

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

    def test_surrogate_survives_a_dumps_loads_dumps_cycle(self):
        """Idempotent: re-serializing a decoded document produces the same
        text, so a config rewritten by ``_save`` does not accumulate escapes.
        """
        payload = {"name": "s\ud800x"}
        once = dumps(payload, indent=2, ensure_ascii=False)

        assert dumps(json.loads(once), indent=2, ensure_ascii=False) == once
