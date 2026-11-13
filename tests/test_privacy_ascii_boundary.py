"""A token written against non-ASCII text is still found (memtomem#3298).

Python's ``\\b`` counts every Unicode letter and digit as a word character, so a
credential with a Korean particle attached, or sitting inside Chinese or
Japanese prose, had no boundary on that side and ten of the token rules missed
it. The rules now spell their boundaries as ASCII lookarounds, as memtomem LTM
does. STM's email rule, which LTM does not have, ended in ``\\b`` too and takes
the same spelling.

What that must and must not change is checked against two references built
from the patterns themselves: the set as it was spelled before (the same
strings with ``\\b``), and that spelling with the boundary made ASCII by a
scoped inline flag, which is the behaviour the lookarounds are meant to have.
"""

from __future__ import annotations

import hashlib
import re
import re._parser as re_parser
from re._constants import AT, AT_BOUNDARY, AT_NON_BOUNDARY

import pytest

from memtomem_stm.proxy import cache as cache_module
from memtomem_stm.proxy import privacy
from test_privacy_pkcs8 import _open, _rows, _stamp

_LEADING = "(?<![A-Za-z0-9_])"
_TRAILING = "(?![A-Za-z0-9_])"
_TRAILING_AFTER_HYPHEN_CLASS = "(?<=[A-Za-z0-9_])(?![A-Za-z0-9_])"

# What ``_SECRET_CLASS_SET_SHA256`` pinned before the boundaries were rewritten.
_SET_WITH_WORD_BOUNDARIES_SHA256 = (
    "631d79e538a2000424dd3493737bb60580f7516a20bdacddd12df9432caa99d8"
)

# One token per ``CREDENTIAL_PATTERNS`` rule, by index, as in LTM's fixtures.
_TOKENS: tuple[str, ...] = (
    "API_KEY: abc123",
    "Password = hunter2",
    '"password": "hunter2"',
    '"SessionToken": "FAKEFwoGZXIvYXdzFAKE"',
    "x-amz-security-token: FAKEFwoGZXIvYXdzFAKE",
    "sk-" + "a" * 30,
    "github_pat_" + "x" * 30,
    "sk_live_" + "a" * 25,
    "npm_" + "a" * 30,
    "AKIAIOSFODNN7EXAMPLE",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIicm9vdA.SflKxwRJSMeK",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN ENCRYPTED PRIVATE KEY-----",
    "sk-proj-FAKE0aaaa1111bbbb2222cccc3333T3Blbk" + "FJEXAMPLE0dddd4444eeee5555ffff6666",
    "sk-ant-api03-" + ("FAKEfake0123456789" * 6)[:93] + "AA",
    "ghs_FAKEfake0123456789FAKEfake0123456789",
    "AIzaSy0_-BcdSy0_-BcdSy0_-BcdSy0_-BcdSy0",
    "glpat-" + ("FAKEfake0123456789" * 2)[:20],
    "hf" + "_FAKEfake0123456789FAKEfake01234567",
    "pypi-AgEIcHlwaS5vcmcFAKEfake0123456789FAKEfake0123456789FAKEfake0123456789",
)

_NON_ASCII = ("가", "を", "密", "é")
_NEIGHBOURS = ("", " ", "x", "7", "_", "-", ".", "/", *_NON_ASCII, "٢")
_ASCII_NEIGHBOURS = tuple(n for n in _NEIGHBOURS if n.isascii())

_JWT_HEAD = "eyJAAAAAAAA.BBBBBBBB."
_JWT_TAILS = ("abcdefgh", "abcdefgh-", "abcdefg-", "--------", "a---------", "abcdefg----")


def _with_word_boundaries(pattern: str) -> str:
    """``pattern`` with its ASCII lookarounds spelled ``\\b`` again."""
    return (
        pattern.replace(_TRAILING_AFTER_HYPHEN_CLASS, r"\b")
        .replace(_LEADING, r"\b")
        .replace(_TRAILING, r"\b")
    )


def _ascii_boundary(pattern: str) -> re.Pattern[str]:
    """The ``\\b`` spelling with ``\\b`` meaning the ASCII boundary, nothing else changed."""
    return re.compile(_with_word_boundaries(pattern).replace(r"\b", r"(?a:\b)"))


_REWRITTEN = tuple(
    i for i, p in enumerate(privacy.CREDENTIAL_PATTERNS) if _with_word_boundaries(p) != p
)


def _rule_with(fragment: str) -> int:
    (index,) = [i for i, p in enumerate(privacy.CREDENTIAL_PATTERNS) if fragment in p]
    return index


# Rules whose last class holds ASCII word characters only, so an underscore
# behind the token is not consumed and has to block the match. Named here, not
# read off the patterns: a rule that lost its guard would drop out of a list
# derived from them.
_TRAILING_GUARDED = tuple(
    _rule_with(fragment) for fragment in ("npm_", "(?:AKIA|ASIA)", "gh[ousr]_", "hf_(")
)
_JWT_INDEX = _rule_with("eyJ(?=")


def _bodies(index: int) -> list[str]:
    bodies = [_TOKENS[index]]
    if index == _JWT_INDEX:
        bodies += [_JWT_HEAD + tail for tail in _JWT_TAILS]
    return bodies


def _texts(index: int, neighbours: tuple[str, ...]) -> list[str]:
    return [
        f"{before}{body}{after}"
        for body in _bodies(index)
        for before in neighbours
        for after in neighbours
    ]


def _spans(compiled: re.Pattern[str], text: str) -> list[tuple[int, int]]:
    return [m.span() for m in compiled.finditer(text)]


def _hit(text: str, index: int) -> bool:
    # Through the scanner, so the rule is compiled the way the gates compile it.
    return privacy.contains_sensitive_content(text, [privacy.CREDENTIAL_PATTERNS[index]])


def _boundary_assertions(pattern: str) -> int:
    """How many ``\\b`` / ``\\B`` assertions ``re`` parses out of ``pattern``."""
    found = 0
    stack: list[object] = [re_parser.parse(pattern)]
    while stack:
        node = stack.pop()
        if isinstance(node, re_parser.SubPattern):
            for op, av in node:
                if op is AT and av in (AT_BOUNDARY, AT_NON_BOUNDARY):
                    found += 1
                else:
                    stack.append(av)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return found


def test_tokens_cover_every_rule():
    assert len(_TOKENS) == len(privacy.CREDENTIAL_PATTERNS)
    for index, token in enumerate(_TOKENS):
        assert _hit(token, index), (index, token)


def test_the_set_differs_from_the_one_pinned_before_only_in_how_boundaries_are_spelled():
    as_before = [_with_word_boundaries(p) for p in privacy.CREDENTIAL_PATTERNS]
    joined = "\n".join(as_before).encode()
    assert hashlib.sha256(joined).hexdigest() == _SET_WITH_WORD_BOUNDARIES_SHA256
    assert len(_REWRITTEN) == 10


@pytest.mark.parametrize("index", _REWRITTEN)
def test_a_rewritten_rule_matches_what_an_ascii_word_boundary_matches(index):
    rule = re.compile(privacy.CREDENTIAL_PATTERNS[index])
    reference = _ascii_boundary(privacy.CREDENTIAL_PATTERNS[index])
    for text in _texts(index, _NEIGHBOURS):
        assert _spans(rule, text) == _spans(reference, text), repr(text)


@pytest.mark.parametrize("index", _REWRITTEN)
def test_nothing_changes_between_ascii_neighbours(index):
    rule = re.compile(privacy.CREDENTIAL_PATTERNS[index])
    before = re.compile(_with_word_boundaries(privacy.CREDENTIAL_PATTERNS[index]))
    for text in _texts(index, _ASCII_NEIGHBOURS):
        assert _spans(rule, text) == _spans(before, text), repr(text)


@pytest.mark.parametrize("neighbour", _NON_ASCII)
@pytest.mark.parametrize("index", range(len(_TOKENS)))
def test_every_rule_finds_its_token_against_non_ascii_text(index, neighbour):
    token = _TOKENS[index]
    assert _hit(f"{neighbour}{token}", index)
    assert _hit(f"{token}{neighbour}", index)
    assert _hit(f"{neighbour}{token}{neighbour}", index)


@pytest.mark.parametrize("index", _REWRITTEN)
@pytest.mark.parametrize("neighbour", ["x", "7", "_"])
def test_an_ascii_word_character_in_front_is_still_not_a_boundary(index, neighbour):
    assert not _hit(f"{neighbour}{_TOKENS[index]}", index)


@pytest.mark.parametrize("index", _TRAILING_GUARDED)
def test_an_underscore_behind_is_still_not_a_boundary(index):
    assert not _hit(f"{_TOKENS[index]}_", index)


def test_no_rule_uses_a_word_boundary_escape():
    # A rule synced in with ``\b`` reintroduces the miss; write the boundary as
    # the ASCII lookarounds described above ``CREDENTIAL_PATTERNS``.
    rules = privacy.DEFAULT_PATTERNS
    assert {rule: _boundary_assertions(rule) for rule in rules if _boundary_assertions(rule)} == {}


_EMAIL_RULE = privacy.PII_PATTERNS[0]
_ADDRESSES = ("user@example.com", "a.b+c@sub.example.co.uk", "git@github.com:org/repo.git")


def test_the_email_rule_matches_what_an_ascii_word_boundary_matches():
    rule = re.compile(_EMAIL_RULE)
    reference = _ascii_boundary(_EMAIL_RULE)
    assert reference.pattern != rule.pattern
    for body in _ADDRESSES:
        for before in _NEIGHBOURS:
            for after in _NEIGHBOURS:
                text = f"{before}{body}{after}"
                assert _spans(rule, text) == _spans(reference, text), repr(text)


@pytest.mark.parametrize("neighbour", _NON_ASCII)
def test_an_address_with_non_ascii_text_behind_is_pii(neighbour):
    text = f"메일 user@example.com{neighbour}"
    assert privacy.contains_sensitive_content(text, privacy.PII_PATTERNS)
    assert not privacy.contains_sensitive_content(text, privacy.CREDENTIAL_PATTERNS)


def test_the_boundary_check_sees_what_it_is_looking_for():
    assert _boundary_assertions(r"\bnpm_[a-z]+\b") == 2
    assert _boundary_assertions(r"(?:a|(?=\Bx))(?<=\by)") == 2
    assert _boundary_assertions(r"[\b]\\b") == 0


@pytest.mark.parametrize("tail", ["--------가", "a---------가", "abcdefg----٢", "abcdefg-가"])
def test_a_jwt_tail_of_hyphens_before_a_non_ascii_word_character_is_not_matched(tail):
    # Accepted with the rewrite: the Unicode boundary between the last ``-`` and
    # that character used to end the match, and the ASCII boundary has none there.
    assert not _hit(_JWT_HEAD + tail, _JWT_INDEX)


def test_a_jwt_match_ends_at_its_last_word_character():
    rule = re.compile(privacy.CREDENTIAL_PATTERNS[_JWT_INDEX])
    assert _spans(rule, _JWT_HEAD + "abcdefgh-가") == [(0, len(_JWT_HEAD) + 8)]
    assert _hit(_JWT_HEAD + "abcdefgh가", _JWT_INDEX)


def test_a_label_still_matches_across_an_ideographic_space():
    # Why the patterns are not simply compiled with ``re.ASCII``.
    assert _hit("pwd\u3000= x", _rule_with("(password|passwd|pwd)"))


@pytest.mark.parametrize(
    "text",
    [
        f"배포 키 {_TOKENS[9]}를 사용한다",
        f"토큰은 {_TOKENS[15]}입니다",
        f"設定ファイルの{_TOKENS[16]}を使う",
        f"密钥{_TOKENS[13]}",
    ],
)
def test_the_credential_scan_sees_a_token_inside_cjk_prose(text):
    assert privacy.contains_sensitive_content(text, privacy.CREDENTIAL_PATTERNS)
    assert privacy.contains_sensitive_content(text)


def test_a_response_cached_before_the_fix_is_swept_on_upgrade(tmp_path, monkeypatch):
    # The rewrite changes DEFAULT_PATTERNS, so it changes the cache's policy
    # fingerprint, and the first open under it drops the rows the old spelling
    # let through. Checked straight after the open, before a read could evict.
    path = tmp_path / "c.db"
    with monkeypatch.context() as old_build:
        old_build.setattr(
            privacy,
            "DEFAULT_PATTERNS",
            [*map(_with_word_boundaries, privacy.CREDENTIAL_PATTERNS), *privacy.PII_PATTERNS],
        )
        old_stamp = cache_module._privacy_policy_fingerprint()
        seed = _open(path)
        try:
            seed.set("s", "keyed", {}, f"배포 키 {_TOKENS[9]}를 사용한다", ttl_seconds=None)
            seed.set("s", "clean", {}, "배포 키를 사용한다", ttl_seconds=None)
        finally:
            seed.close()
    assert set(_rows(path)) == {"keyed", "clean"}

    _open(path).close()
    assert _rows(path) == {"clean": "배포 키를 사용한다"}
    assert _stamp(path) == cache_module._privacy_policy_fingerprint() != old_stamp
