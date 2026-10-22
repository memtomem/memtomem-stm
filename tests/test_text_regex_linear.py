"""Text-path scans of upstream responses run in linear time.

Each site below used a regex that rescans the same region from many starts, so
a crafted response held the event loop for seconds to minutes. The rewrites
must find exactly what the old regexes found — checked against the old regex,
kept here as the oracle — and must finish on the inputs that used to stall,
checked in a child process so a regression fails instead of hanging the suite.
"""

from __future__ import annotations

import random
import re
import subprocess
import sys
import textwrap

import pytest

from memtomem_stm.proxy import cleaning, compression, extraction
from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.compression import TruncateCompressor
from memtomem_stm.proxy.progressive import ProgressiveChunker

# The pre-fix regexes, as the oracle for what the rewrites must still find.
OLD_LINK_RE = re.compile(r"\[.*?\]\(.*?\)")
OLD_LIST_ITEM_RE = re.compile(r"(?:^|\n)\s*[-*]\s")
OLD_CODE_BOUNDARY_RE = re.compile(
    r"(?:^|\n)(\s*(?:def |class |async def |function |func |export |pub fn )\S.*)"
)
OLD_PROGRESSIVE_CODE_RE = re.compile(
    r"(?:^|\n)\s*(?:def |class |async def |function |func |import |from )"
)
OLD_DECISION_RE = re.compile(
    r"^[\s*\-]*"
    r"(?:Decision|Decided|Resolved|Conclusion|Agreed|We\s+will|We'll|We\s+chose)"
    r"[:\s]+(.+)$",
    re.MULTILINE | re.IGNORECASE,
)
OLD_ACTION_RE = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"(?:TODO|FIXME|HACK|XXX|ACTION)[:\s]+(.+)|"
    r"[-*]\s*\[\s*\]\s+(.+)|"
    r"(?:Action\s+item)[:\s]+(.+)"
    r")",
    re.MULTILINE | re.IGNORECASE,
)
OLD_LINK_LINE_RE = re.compile(r"^\s*[-*]\s*\[.*?\]\(https?://\S+\)")
OLD_BARE_URL_LINE_RE = re.compile(r"^\s*[-*]?\s*https?://\S+\s*$")

# Whitespace the old ``\s`` classes matched that a newline-free class must
# still treat the same way.
_ODD_SPACE = ["\t", "\r", "\x0b", "\x0c", "\x1c", "\x85", " ", "　"]

LINK_ALPHABET = [*"[]()\n x-*", "](", "[](", "http://", "https://", " - [", *_ODD_SPACE]
LINE_ALPHABET = [*"\n -*xd:", "def ", "class ", "import ", "TODO", "todo ", "Decision", "We will"]
LINE_ALPHABET += ["Action item", "[ ]", "[]", "export ", "y", *_ODD_SPACE]
TAG_ALPHABET = [*"<>/ ax\n\"='`", "<script>", "</script>", "<style>", "</style>", "<SCRIPT>"]
TAG_ALPHABET += [
    "</Script>",
    "<ſcript>",
    "</ſcript>",
    "<script ",
    "A<",
    "Map",
    "\x00",
    "<a ",
    "<b/>",
]
TAG_ALPHABET += ["```", "</a>", "\t"]

FUZZ_CASES = 4000


def _fuzz(
    alphabet: list[str], seed: int, max_len: int = 30, prefixes: tuple[str, ...] = ("",)
) -> list[str]:
    rng = random.Random(seed)
    return [
        rng.choice(prefixes) + "".join(rng.choice(alphabet) for _ in range(rng.randint(0, max_len)))
        for _ in range(FUZZ_CASES)
    ]


# Random strings rarely open like a link-flood line, so those tests start most
# of theirs with one of these.
LINE_OPENERS = ("", "- [", " * [", "-[", "\t- [", "- ", "-", "* ", "- [a](http")


def _spans(matches) -> list[tuple[int, int]]:
    return [match.span() for match in matches]


# Structured inputs the random strings may not reach: hit-dense, all-failing,
# interleaved kinds and case variants.
STRUCTURED = [
    "[]()" * 20,
    "[](x)\n" * 5,
    "[" * 40,
    "[a](b) [c](d\n[e](f)",
    "- [](http://" * 5,
    "\n" * 30 + "- x\n  * y",
    "\n \n\t\n" * 5 + "def f(): pass",
    "<a>" * 10,
    "<a " * 10 + ">",
    "<script></script>" * 5,
    "<script>" * 5 + "</script>",
    "<style>" * 5 + "</script>",
    "<script><style></script></style>",
    "<style><script></style></script>",
    "<SCRIPT>a</script><Script>b</SCRIPT>",
    "<ſcript></script>",
    "<ſcript>a</ſcript><script>b</script>",
    "<scrİpt></script>",
    "<scrİpt>" * 3 + "</script><script>",
    "<scrİpt>a</script><script><scrİpt>b</scrİpt>",
    "A<" * 10 + ">",
    "Map<`K`, V> and List<T>",
    "- [a](http://)",
    "- [a](http://))",
    "- [a](https://x)",
    "- [a](x) [b](https://y(z))",
    "- [a](http://x ](http://y)",
    "- http://x",
    "-  https://y ",
    "* http://x y",
    "\t-\thttp://x\t",
]


class TestSameResultsAsTheOldRegex:
    def test_link_count(self):
        for text in _fuzz(LINK_ALPHABET, 1) + STRUCTURED:
            assert compression._count_links(text) == len(OLD_LINK_RE.findall(text)), repr(text)

    def test_list_item_count(self):
        for text in _fuzz(LINE_ALPHABET, 2) + STRUCTURED:
            new = len(compression._LIST_ITEM_RE.findall(text))
            assert new == len(OLD_LIST_ITEM_RE.findall(text)), repr(text)

    def test_progressive_code_detection(self):
        for text in _fuzz(LINE_ALPHABET, 3) + STRUCTURED:
            if text.strip()[:1] in ("{", "[") or re.search(r"(?:^|\n)#{1,6}\s", text):
                continue  # decided before the code check
            detected = ProgressiveChunker.detect_content_type(text) == "code"
            assert detected == bool(OLD_PROGRESSIVE_CODE_RE.search(text)), repr(text)

    def test_extraction_groups_and_ends(self):
        for text in _fuzz(LINE_ALPHABET, 4) + STRUCTURED:
            for new_re, old_re in (
                (extraction._DECISION_RE, OLD_DECISION_RE),
                (extraction._ACTION_RE, OLD_ACTION_RE),
            ):
                new = [(m.groups(), m.end()) for m in new_re.finditer(text)]
                old = [(m.groups(), m.end()) for m in old_re.finditer(text)]
                assert new == old, repr(text)

    def test_code_boundary_ends_and_signatures(self):
        boundary_re = TruncateCompressor._CODE_BOUNDARY_RE
        for text in _fuzz(LINE_ALPHABET, 5) + STRUCTURED:
            new = [(m.end(), m.group(1).strip()) for m in boundary_re.finditer(text)]
            old = [(m.end(), m.group(1).strip()) for m in OLD_CODE_BOUNDARY_RE.finditer(text)]
            assert new == old, repr(text)

    def test_link_flood_line(self):
        for text in _fuzz(LINK_ALPHABET, 6, prefixes=LINE_OPENERS) + STRUCTURED:
            new = cleaning._is_link_line(text)
            assert new == bool(OLD_LINK_LINE_RE.match(text)), repr(text)
            new = bool(cleaning._BARE_URL_LINE_RE.match(text))
            assert new == bool(OLD_BARE_URL_LINE_RE.match(text)), repr(text)

    def test_tag_spans(self):
        for text in _fuzz(TAG_ALPHABET, 7) + STRUCTURED:
            for pattern in (cleaning._HTML_TAG_RE, cleaning._GENERIC_RE):
                new = _spans(cleaning._gt_closed_matches(pattern, text))
                assert new == _spans(pattern.finditer(text)), repr(text)
            new = _spans(cleaning._script_style_matches(text))
            assert new == _spans(cleaning._SCRIPT_STYLE_RE.finditer(text)), repr(text)

    def test_long_s_script_does_not_close_on_ascii_tag(self):
        # The \1 backreference compares lower(): "ſ" is not "s", so this
        # opener has no closer — and must not take the later <script> down.
        text = "<ſcript>a</script><script>b</script>"
        assert _spans(cleaning._script_style_matches(text)) == [(18, 36)]
        assert _spans(cleaning._SCRIPT_STYLE_RE.finditer(text)) == [(18, 36)]

    def test_whole_cleaner(self, monkeypatch):
        # The helpers feed _strip_html_jsx's masked copy, alignment walk and
        # segment walk; swapping the plain scans back in gives the old output.
        texts = (
            _fuzz(TAG_ALPHABET, 8, max_len=40)
            + _fuzz(LINK_ALPHABET, 9, prefixes=LINE_OPENERS)
            + STRUCTURED
        )
        new = [DefaultContentCleaner().clean(text) for text in texts]
        monkeypatch.setattr(cleaning, "_script_style_matches", cleaning._SCRIPT_STYLE_RE.finditer)
        monkeypatch.setattr(cleaning, "_html_tag_matches", cleaning._HTML_TAG_RE.finditer)
        monkeypatch.setattr(cleaning, "_gt_closed_matches", lambda p, t: p.finditer(t))
        monkeypatch.setattr(cleaning, "_is_link_line", lambda ln: bool(OLD_LINK_LINE_RE.match(ln)))
        monkeypatch.setattr(cleaning, "_BARE_URL_LINE_RE", OLD_BARE_URL_LINE_RE)
        for text, cleaned in zip(texts, new, strict=True):
            assert cleaned == DefaultContentCleaner().clean(text), repr(text)


def _code_blocks(blank_lines: int) -> str:
    body = "    a = {i}\n    b = a + 1\n    return b\n"
    return "".join(f"def f{i}(x):\n" + body.format(i=i) + "\n" * blank_lines for i in range(12))


class TestCodeBoundaryAfterBlankLines:
    """Behavior change: a code boundary after blank lines starts at the last of
    them, so the blank lines stay with the block before it (whose body is
    rstripped) instead of opening the next one."""

    def test_boundary_starts_at_the_last_blank_line(self):
        text = "x = 1\n\n \n\t\n    def f():\n        pass\n"
        starts = [m.start() for m in TruncateCompressor._CODE_BOUNDARY_RE.finditer(text)]
        assert starts == [text.index("\n    def")]
        assert [m.start() for m in OLD_CODE_BOUNDARY_RE.finditer(text)] == [text.index("\n")]

    def test_signature_snippets_survive_leading_blank_lines(self):
        # The snippet is the body's first line plus up to 3 more non-empty
        # ones; blank lines opening the body used to fill that window, which
        # left the later blocks with no signature at all.
        out = TruncateCompressor().compress(_code_blocks(3), max_chars=300)
        assert "... (9 more blocks)" in out
        assert "def f3(x):\n    a = 3" in out


# A consumer given an input that took tens of seconds before the fix; each
# must now finish in well under the bound.
STALL_CASES = {
    "summary-bracket-run": (
        "from memtomem_stm.proxy.compression import _content_summary as run",
        "'[' * 128_000",
    ),
    "summary-cubic-link-run": (
        "from memtomem_stm.proxy.compression import _content_summary as run",
        "'- [](http://' * 1_500",
    ),
    "truncate-blank-line-run": (
        "from memtomem_stm.proxy.compression import TruncateCompressor\n"
        "run = lambda s: TruncateCompressor().compress(s, max_chars=500)",
        "'intro\\n' + '\\n ' * 48_000 + '\\nend'",
    ),
    "clean-unclosed-tags": (
        "from memtomem_stm.proxy.cleaning import DefaultContentCleaner\n"
        "run = DefaultContentCleaner().clean",
        "'<a ' * 32_000",
    ),
    "clean-unclosed-scripts": (
        "from memtomem_stm.proxy.cleaning import DefaultContentCleaner\n"
        "run = DefaultContentCleaner().clean",
        "'<script>' * 32_000",
    ),
    "clean-unclosed-generics": (
        "from memtomem_stm.proxy.cleaning import DefaultContentCleaner\n"
        "run = DefaultContentCleaner().clean",
        "'A<' * 256_000",
    ),
    "clean-space-line-in-flood": (
        "from memtomem_stm.proxy.cleaning import DefaultContentCleaner\n"
        "run = DefaultContentCleaner().clean",
        "'\\n'.join(['x'] * 5 + [' ' * 256_000] + ['x'] * 5)",
    ),
    "clean-link-target-run": (
        "from memtomem_stm.proxy.cleaning import DefaultContentCleaner\n"
        "run = DefaultContentCleaner().clean",
        "'\\n'.join(['- [a](http://x)'] * 9 + ['- [' + '](http://x' * 50_000])",
    ),
    "extraction-blank-line-run": (
        "from memtomem_stm.proxy.extraction import _extract_heuristic\n"
        "run = lambda s: _extract_heuristic(s, max_facts=10)",
        "'\\n' * 32_000",
    ),
    "progressive-blank-line-run": (
        "from memtomem_stm.proxy.progressive import ProgressiveChunker\n"
        "run = ProgressiveChunker.detect_content_type",
        "'x' + '\\n' * 96_000",
    ),
}

_CHILD = """
import sys, time
{setup}
text = {payload}
start = time.perf_counter()
run(text)
elapsed = time.perf_counter() - start
print(f"{{elapsed:.3f}}s")
sys.exit(0 if elapsed < {bound} else 1)
"""


@pytest.mark.parametrize("case", sorted(STALL_CASES))
def test_crafted_input_does_not_stall(case):
    setup, payload = STALL_CASES[case]
    code = textwrap.dedent(_CHILD).format(setup=setup, payload=payload, bound=2.0)
    try:
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{case}: still scanning after 60 s", pytrace=False)
    assert done.returncode == 0, f"{case}: {done.stdout.strip()} {done.stderr.strip()[-500:]}"
