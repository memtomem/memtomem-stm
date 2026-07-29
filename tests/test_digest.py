"""Tests for ``utils.digest.framed_digest`` — the shared injective key primitive.

Extracted in #794 from ``proxy/cache._make_key`` (#784) once the tool-graph
consult cache turned out to have the same unframed-join defect. The property
these tests exist for is injectivity over the component tuple: a collision
means one cache row answers for a different call.
"""

from __future__ import annotations

import hashlib
import itertools

from memtomem_stm.utils.digest import framed_digest


class TestFramedDigestShape:
    def test_matches_the_pinned_netstring_layout(self):
        """Spelled out literally — this layout is stored in two SQLite caches.

        A drift here silently orphans every existing row in both, so the
        expected bytes are written out rather than recomputed with the same
        helper under test.
        """
        raw = b"5:alpha4:beta"
        assert framed_digest(("alpha", "beta")) == hashlib.sha256(raw).hexdigest()

    def test_length_prefix_counts_bytes_not_characters(self):
        """The only version that works once a component is non-ASCII.

        Pinned with a 3-byte CJK character and a lone surrogate, so a
        regression to ``len(component)`` frames both short and fails here.
        """
        component = "中\ud800"
        data = component.encode("utf-8", errors="surrogatepass")
        assert len(data) != len(component)
        assert framed_digest((component,)) == hashlib.sha256(b"6:" + data).hexdigest()

    def test_lone_surrogate_does_not_raise(self):
        framed_digest(("\ud800", "\udfff"))  # the call that would raise without surrogatepass


class TestFramedDigestInjectivity:
    def test_empty_tuple_and_empty_component_differ(self):
        assert framed_digest(()) != framed_digest(("",))
        assert framed_digest(("",)) != framed_digest(("", ""))

    def test_no_collision_over_a_framing_adversarial_alphabet(self):
        """Brute force over the framing characters themselves.

        The alphabet is built from what a length prefix is made of — digits,
        ``:``, the empty string — plus NUL and combinations that imitate a
        prefix (``"1:"``, ``":1"``, ``"1:a"``), because a framing scheme's
        failure mode is a component that can be mistaken for its own framing.
        """
        alphabet = ["", "a", ":", "0", "1", "12", "\x00", "1:", ":1", "a:b", "0:", "1:a"]
        seen: dict[str, tuple[str, ...]] = {}
        for combo in itertools.product(alphabet, repeat=3):
            key = framed_digest(combo)
            assert seen.setdefault(key, combo) == combo, f"collision: {seen[key]} vs {combo}"
        assert len(seen) == len(alphabet) ** 3

    def test_astral_scalar_does_not_alias_a_lone_surrogate_pair(self):
        """Distinct under ``surrogatepass``: 4 bytes versus two 3-byte forms.

        ``chr()`` because a JSON boundary merges the pair on decode — the two
        code units have to be built in-process.
        """
        assert framed_digest((chr(0x10000),)) != framed_digest((chr(0xD800) + chr(0xDC00),))
