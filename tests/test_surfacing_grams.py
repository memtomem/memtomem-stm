"""The keyed-hash rules shared by surfacing collection and offline readers.

Every rule that shapes a stored hash lives in ``memtomem_stm.surfacing.grams``;
these tests pin the rules themselves (the store-level tests pin that the store
uses them).
"""

from __future__ import annotations

import ntpath
import posixpath

import pytest

from memtomem_stm.surfacing.formatter import SurfacingFormatter
from memtomem_stm.surfacing.grams import (
    MAX_SNIPPET_GRAMS,
    ancestor_keys,
    basename_key,
    eligible_source,
    gram_hashes,
    keyed_hash,
    path_key,
    snippet_grams,
    unsanitize,
)

KEY = b"k" * 32
_sanitize = SurfacingFormatter._sanitize


class TestUnsanitize:
    @pytest.mark.parametrize(
        "plain",
        [
            "use <tag> & `code` with snake_case_name and _leading *stars*",
            "a literal backslash \\ and \\\\ doubled, braces {x} [y] (z) | pipe",
            "tabs\tand\nnewlines  collapse",
            "control \x00 and zero-width \u200b and bidi \u202e chars",
            "astral \U0001f600 and fullwidth \uff1c lookalike",
            "  leading and trailing  ",
            "\\u0041lpha written literally",
        ],
    )
    def test_inverts_sanitize_up_to_whitespace_collapse(self, plain: str) -> None:
        # _sanitize collapses whitespace runs to one space and strips; every
        # other character must come back exactly.
        assert unsanitize(_sanitize(plain)) == " ".join(plain.split())

    def test_rendered_grams_equal_plain_grams(self) -> None:
        plain = "keep <angle> and `tick` with under_score plus *emph* and back\\slash words"
        assert gram_hashes(unsanitize(_sanitize(plain)), KEY) == gram_hashes(plain, KEY)
        # Without unsanitize the escapes split or glue words, so the rendered
        # form alone must NOT already match — otherwise this test proves nothing.
        assert gram_hashes(_sanitize(plain), KEY) != gram_hashes(plain, KEY)

    def test_literal_escape_text_is_not_decoded_in_plain_text(self) -> None:
        # Agent-written text is never unsanitized: its own tokens stand.
        assert "u0041lpha" in _word_list("the \\u0041lpha token stays literal here")
        # The rendered form of the same literal text round-trips to it.
        assert unsanitize(_sanitize("\\u0041lpha")) == "\\u0041lpha"
        assert unsanitize("\\u0041lpha") == "Alpha"


def _word_list(text: str) -> list[str]:
    import re

    return re.findall(r"[a-z0-9_]{3,}", text.lower())


class TestGramHashes:
    def test_four_word_runs_joined_by_one_space(self) -> None:
        assert gram_hashes("One two three four five", KEY) == {
            keyed_hash("one two three four", KEY),
            keyed_hash("two three four five", KEY),
        }

    def test_short_words_are_not_words(self) -> None:
        # "an", "is" are under three characters and vanish from the sequence.
        assert gram_hashes("alpha an beta is gamma delta", KEY) == {
            keyed_hash("alpha beta gamma delta", KEY)
        }

    def test_fewer_than_four_words_gives_nothing(self) -> None:
        assert gram_hashes("only three words", KEY) == set()

    def test_key_changes_every_hash(self) -> None:
        text = "alpha beta gamma delta epsilon"
        assert gram_hashes(text, KEY).isdisjoint(gram_hashes(text, b"j" * 32))

    def test_snippet_grams_keeps_the_smallest_64(self) -> None:
        text = " ".join(f"word{i:03d}" for i in range(100))
        everything = gram_hashes(text, KEY)
        assert len(everything) == 97
        kept = snippet_grams(text, KEY)
        assert len(kept) == MAX_SNIPPET_GRAMS == 64
        assert kept == sorted(everything)[:64]

    def test_snippet_grams_unsanitizes_the_preview(self) -> None:
        plain = "config <value> holds the `api_base` for staging servers"
        assert set(snippet_grams(_sanitize(plain), KEY)) == gram_hashes(plain, KEY)


class TestPathKey:
    def test_posix_normalizes_and_joins_cwd(self) -> None:
        assert (
            path_key("sub/../Notes/A.md", cwd="/proj", pathmod=posixpath, casefold=False)
            == "/proj/Notes/A.md"
        )

    def test_posix_absolute_ignores_cwd(self) -> None:
        assert path_key("/x/y.md", cwd="/proj", pathmod=posixpath, casefold=False) == "/x/y.md"

    def test_casefold_lowercases(self) -> None:
        assert path_key("/Notes/A.md", pathmod=posixpath, casefold=True) == "/notes/a.md"

    def test_expanduser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/home/u")
        assert path_key("~/n/./a.md", pathmod=posixpath, casefold=False) == "/home/u/n/a.md"

    def test_windows_rules(self) -> None:
        assert (
            path_key("C:/Users/X/Notes/../A.md", pathmod=ntpath, casefold=True)
            == "c:\\users\\x\\a.md"
        )
        assert (
            path_key("sub\\..\\B.md", cwd="C:\\Proj", pathmod=ntpath, casefold=True)
            == "c:\\proj\\b.md"
        )

    def test_ancestors_and_basename(self) -> None:
        assert ancestor_keys("/a/b/c.md", pathmod=posixpath) == ["/a/b", "/a", "/"]
        assert ancestor_keys("c:\\a\\b.md", pathmod=ntpath) == ["c:\\a", "c:\\"]
        assert basename_key("/a/b/c.md", pathmod=posixpath) == "c.md"


class TestEligibleSource:
    @pytest.mark.parametrize("source", ["/notes/a.md", "~/notes/a.md"])
    def test_absolute_or_home_is_eligible(self, source: str) -> None:
        assert eligible_source(source, pathmod=posixpath)

    @pytest.mark.parametrize(
        "source", [None, "", ".", "unknown", "pinned", "notes/a.md", "blk_0123abcd"]
    )
    def test_sentinels_and_relative_are_not(self, source: str | None) -> None:
        assert not eligible_source(source, pathmod=posixpath)

    def test_windows_absolute_is_eligible(self) -> None:
        assert eligible_source("C:\\notes\\a.md", pathmod=ntpath)
