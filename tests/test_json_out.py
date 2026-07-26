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

from memtomem_stm.utils.json_out import (
    IDENTITY_FIELD_NAMES,
    dumps,
    escape_lone_surrogates,
    has_lone_surrogate,
    scrub_content_preserving_identity,
    scrub_lone_surrogates,
)

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


class TestSeparatorsContract:
    """Compact separators are inside the documented contract (#761).

    ``encode_line``, the two fingerprints and the cache envelope all pass
    ``(",", ":")``; the contract's ASCII qualifier is what makes the blanket
    substitution safe for them, so pin it rather than leaving it to prose.
    """

    @pytest.mark.parametrize("surrogate", SURROGATES)
    def test_compact_separators_still_produce_parsable_json(self, surrogate):
        payload = {"b": f"sr{surrogate}v", "a": [1, {"k": surrogate}]}
        out = dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

        assert " " not in out  # the separators actually took effect
        out.encode("utf-8")  # the encode that used to raise
        assert json.loads(out) == payload

    def test_identity_on_clean_input_with_compact_separators(self):
        payload = {"name": "🚀 서버 漢字", "l": ["a", "b"]}
        assert dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ) == json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class TestEscapeLoneSurrogates:
    """The ingest-side escape: the surrogate is replaced in the value itself."""

    @pytest.mark.parametrize("value", ["", "fs", "서버 🚀", "a\\ud800b", "sr\tv"])
    def test_identity_on_clean_input(self, value):
        """Same *object* back, not merely an equal one — the no-copy fast path
        is what lets this sit in ``_sanitize_nonfinite``'s per-node walk."""
        assert escape_lone_surrogates(value) is value

    @pytest.mark.parametrize("surrogate", SURROGATES)
    def test_each_boundary_surrogate_becomes_its_literal(self, surrogate):
        out = escape_lone_surrogates(f"sr{surrogate}v")

        assert out == f"sr\\u{ord(surrogate):04x}v"
        out.encode("utf-8")  # the encode every downstream consumer performs

    @pytest.mark.parametrize("surrogate", SURROGATES)
    def test_the_escape_is_inert_under_a_dumps_loads_round_trip(self, surrogate):
        """The property ``dumps``-alone does NOT have, and the reason the
        daemon's read side scrubs as well as its write side: ``dumps`` emits an
        escape that ``loads`` decodes *back* into a raw surrogate, so escaping
        only at the emitter relocates the failure into the receiving process.
        Escaping the value itself survives the trip as inert text.
        """
        escaped = escape_lone_surrogates(f"sr{surrogate}v")
        decoded = json.loads(json.dumps({"t": escaped}, ensure_ascii=False))["t"]

        assert decoded == escaped
        assert not has_lone_surrogate(decoded)

        # Contrast: the emitter-only route hands back the raw code unit.
        via_dumps = json.loads(dumps({"t": f"sr{surrogate}v"}, ensure_ascii=False))["t"]
        assert has_lone_surrogate(via_dumps)

    def test_an_adjacent_pair_is_escaped_as_two_code_units(self):
        """``.encode()`` raises on a pair just as it does on a lone surrogate,
        so the pair must be escaped rather than carved out — the same call
        ``has_lone_surrogate`` makes for the config gate."""
        out = escape_lone_surrogates("ok" + chr(0xD83D) + chr(0xDE80))

        assert out == "ok\\ud83d\\ude80"
        out.encode("utf-8")


class TestScrubLoneSurrogates:
    def test_identity_on_a_clean_tree(self):
        tree = {"a": ["x", {"b": "서버 🚀"}], "n": 1, "f": 1.5, "z": None, "t": True}
        assert scrub_lone_surrogates(tree) is tree

    def test_nested_values_are_escaped(self):
        tree = {"a": ["x", {"b": "sr\ud800v"}], "n": 1}
        out = scrub_lone_surrogates(tree)

        assert out == {"a": ["x", {"b": "sr\\ud800v"}], "n": 1}
        json.dumps(out, ensure_ascii=False).encode("utf-8")

    def test_a_clean_branch_keeps_its_identity(self):
        """Copy-on-write is per node: only the containers on the path to a
        surrogate are rebuilt, so scrubbing a large upstream payload with one
        bad leaf does not copy the whole tree."""
        clean_branch = {"deep": ["untouched"]}
        tree = {"clean": clean_branch, "dirty": "sr\ud800v"}
        out = scrub_lone_surrogates(tree)

        assert out is not tree
        assert out["clean"] is clean_branch

    def test_dict_keys_are_scrubbed_too(self):
        """A key encodes exactly like a value does, so leaving keys raw would
        leave the very next ``json.dumps(...).encode()`` raising."""
        out = scrub_lone_surrogates({"k\ud800": 1, "fine": 2})

        assert out == {"k\\ud800": 1, "fine": 2}
        json.dumps(out, ensure_ascii=False).encode("utf-8")

    def test_clean_keys_keep_their_order(self):
        out = scrub_lone_surrogates({"z": "sr\ud800v", "a": 1, "m": 2})
        assert list(out) == ["z", "a", "m"]

    def test_non_string_leaves_are_untouched(self):
        tree = {"n": 1, "f": 1.5, "b": True, "z": None, "s": "sr\ud800v"}
        out = scrub_lone_surrogates(tree)

        assert out["n"] == 1 and out["f"] == 1.5
        assert out["b"] is True and out["z"] is None


class TestScrubContentPreservingIdentity:
    """#783 — the ingest scrub must not rewrite the values it is asked to
    keep injective. Escaping is many-to-one, so applying it to an identifier
    merges two identities Core can mint separately; content has no such
    requirement and keeps the ordinary escape."""

    @pytest.mark.parametrize("key", sorted(IDENTITY_FIELD_NAMES))
    @pytest.mark.parametrize("surrogate", ["\ud800", "\udbff", "\udc00", "\udfff"])
    def test_identity_values_arrive_raw(self, key, surrogate):
        out = scrub_content_preserving_identity({key: f"m{surrogate}"})

        assert out[key] == f"m{surrogate}"
        assert has_lone_surrogate(out[key])

    @pytest.mark.parametrize("key", sorted(IDENTITY_FIELD_NAMES))
    def test_raw_and_literal_identity_stay_distinct(self, key):
        """The whole point: blanket scrubbing collapsed these two onto the
        literal, so a demotion/dedup/invalidation match landed on the wrong
        memory. They must survive as different strings."""
        raw = scrub_content_preserving_identity({key: "m\ud800"})[key]
        literal = scrub_content_preserving_identity({key: r"m\ud800"})[key]

        assert raw != literal
        assert literal == r"m\ud800"

    def test_content_beside_an_identity_is_still_escaped(self):
        out = scrub_content_preserving_identity(
            {"chunk_id": "m\ud800", "content": "c\ud800", "source": "s\ud800"}
        )

        assert out["chunk_id"] == "m\ud800"
        assert out["content"] == r"c\ud800"
        assert out["source"] == r"s\ud800"

    def test_identity_exemption_applies_at_any_depth(self):
        out = scrub_content_preserving_identity(
            {"results": [{"nested": {"chunk_id": "m\ud800", "content": "c\ud800"}}]}
        )
        leaf = out["results"][0]["nested"]

        assert leaf["chunk_id"] == "m\ud800"
        assert leaf["content"] == r"c\ud800"

    def test_non_identity_id_shaped_keys_are_still_escaped(self):
        """``omitted_block_ids`` is joined into a rendered hint and is never
        exact-matched, so it is content by destination."""
        out = scrub_content_preserving_identity({"omitted_block_ids": ["b\ud800"]})

        assert out["omitted_block_ids"] == [r"b\ud800"]

    def test_unencodable_keys_are_still_escaped(self):
        """An unencodable KEY is a malformed document, not an identity Core
        meant to mint — and it would break the very next encode."""
        out = scrub_content_preserving_identity({"k\ud800": {"id": "m\ud800"}})

        assert list(out) == [r"k\ud800"]
        json.dumps(out[r"k\ud800"]["id"], ensure_ascii=True).encode("utf-8")

    def test_clean_tree_is_returned_unchanged(self):
        tree = {"results": [{"chunk_id": "m1", "content": "c"}]}

        assert scrub_content_preserving_identity(tree) is tree
