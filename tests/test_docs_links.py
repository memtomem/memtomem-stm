"""Local link and anchor checks for the tracked public documentation surface."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest
from _pytest.outcomes import Failed
from markdown_it import MarkdownIt
from markdown_it.token import Token

REPO_ROOT = Path(__file__).resolve().parents[1]


def _public_markdown() -> list[Path]:
    roots = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CHANGELOG.md",
        REPO_ROOT / "CLA.md",
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / "SECURITY.md",
        REPO_ROOT / "notebooks" / "README.md",
    ]
    docs = [path for path in (REPO_ROOT / "docs").rglob("*.md") if "reports" not in path.parts]
    return sorted([*roots, *docs])


_PARSER = MarkdownIt("commonmark")


def _tokens(text: str) -> list[Token]:
    """Flatten the parsed document, descending into inline children.

    An ``image`` token's children are its alt text. Markdown-it still parses
    link and HTML syntax in there so a renderer can flatten it to a string, but
    none of it is live: ``![foo [bar](x.md)](pic.png)`` renders one image and no
    clickable link. Descending into it would resurrect exactly the class of
    false link this module exists to avoid, so alt text is not walked.
    """
    flat: list[Token] = []

    def walk(tokens: list[Token]) -> None:
        for token in tokens:
            flat.append(token)
            if token.children and token.type != "image":
                walk(list(token.children))

    walk(_PARSER.parse(text))
    return flat


def _link_targets(text: str) -> list[str]:
    """Every live link target in ``text``, in document order.

    Delegating to a CommonMark parser rather than blanking code spans by hand.
    Three review rounds of a hand-rolled stripper each shipped a case where a
    real link was deleted before it could be checked — spans paired across a
    blank line or a fenced block, a stripped span fusing ``!`` onto the link
    that followed it (making it an image, which is skipped), and a backtick
    consumed by an autolink still acting as a delimiter. The parser settles all
    of them by construction: code spans become ``code_inline`` tokens, fenced
    blocks never reach inline parsing, images are ``image`` tokens rather than
    links, and autolinks are resolved with the same precedence a renderer uses.
    """
    return [token.attrGet("href") or "" for token in _tokens(text) if token.type == "link_open"]


def _headings(text: str) -> list[str]:
    """Rendered text of each ATX/setext heading, in document order.

    GitHub slugs what a heading *renders* to, not its source, so a heading
    containing a link or emphasis must contribute only its visible text —
    ``## [X](y.md) title`` is ``x-title``, not ``xymd-title``.
    """
    titles: list[str] = []
    tokens = _PARSER.parse(text)
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or index + 1 >= len(tokens):
            continue
        inline = tokens[index + 1]
        titles.append(
            "".join(
                child.content
                for child in (inline.children or [])
                if child.type in ("text", "code_inline")
            )
            or inline.content
        )
    return titles


_CANONICAL_PREFIXES = (
    "https://github.com/memtomem/memtomem-stm/blob/main/",
    "https://github.com/memtomem/memtomem-stm/tree/main/",
)
_BARE_CANONICAL = re.compile(
    r"https://github\.com/memtomem/memtomem-stm/(?:blob|tree)/main/[^\s)<>\]`\"']*"
)


def _bare_canonical_urls(text: str) -> list[str]:
    """Canonical repo URLs GitHub linkifies but CommonMark leaves as text.

    The parser is a CommonMark one and GitHub renders GFM, whose autolink
    extension turns a bare URL into a link. Only canonical repo URLs matter:
    every other absolute URL is skipped by the checker anyway.

    Scans the ``text`` tokens rather than the raw source. Code spans, fenced
    and indented blocks are their own token types, so they are excluded by
    construction and *by position* — subtracting matched substrings instead
    would let one sample occurrence inside a code span silence every live
    occurrence of the same URL elsewhere in the file.
    """
    return [
        match.group(0)
        for token in _tokens(text)
        if token.type == "text"
        for match in _BARE_CANONICAL.finditer(token.content)
    ]


def _github_slug(title: str) -> str:
    title = re.sub(r"<[^>]+>", "", title).strip().lower()
    title = re.sub(r"[^\w\- ]", "", title, flags=re.UNICODE)
    return title.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    # Hand-written targets come from HTML the parser hands back verbatim; a
    # fenced block never becomes an html token, so these are live anchors.
    html = "".join(
        token.content for token in _tokens(text) if token.type in ("html_block", "html_inline")
    )
    # ...but an HTML *comment* is also an html token, and an anchor commented
    # out inside one renders nothing. Counting it would let a fragment pointing
    # at a deleted anchor pass.
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    anchors = set(re.findall(r"<a\s+(?:name|id)=[\"']([^\"']+)", html, re.IGNORECASE))
    counts: dict[str, int] = {}
    for title in _headings(text):
        base = _github_slug(title)
        count = counts.get(base, 0)
        counts[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


# Every case in this table cost a review round against a hand-rolled stripper.
# Each is markdown where the delimiter structure decides whether a link is real.
_LINK_EXTRACTION_CASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "a code span on the same line as a real link",
        "See `mms init` then [CLI](cli.md#init) for details.",
        ("cli.md#init",),
    ),
    ("a link written inside a code span", "Write `[x](nope.md)` to link.", ()),
    ("a double-backtick span closing on its own run length", "Use ``[x](nope.md)`` verbatim.", ()),
    ("two spans that must not merge", "`a` [CLI](cli.md#init) `b`", ("cli.md#init",)),
    ("escaped backticks, which cannot open a span", r"\`[CLI](cli.md#init)\`", ("cli.md#init",)),
    (
        "an escape inside a span, which is inert, so the span closes there",
        "`code\\` [bad](missing.md) `",
        ("missing.md",),
    ),
    (
        "a span wrapping lines: the middle backticks close one and open the next",
        "`first span\n` [bad](missing.md) `second span\n`",
        ("missing.md",),
    ),
    (
        "unclosed backticks in different blocks, which cannot pair",
        "`open\n\n[bad](missing.md) `",
        ("missing.md",),
    ),
    (
        "the same, separated by a fenced block",
        "`open\n```\nsample\n```\n[bad](missing.md) `",
        ("missing.md",),
    ),
    (
        "a bang before a span, which must not fuse into an image",
        "!`sample`[bad](missing.md)",
        ("missing.md",),
    ),
    ("an image, which is not a link", "![img](pic.png)", ()),
    (
        "a link inside image alt text, which renders no clickable link",
        "![foo [bar](missing.md)](pic.png)",
        (),
    ),
    (
        "a backtick consumed by an autolink, which cannot also open a span",
        "<https://example.com/x>\n[bad](missing.md) `",
        ("https://example.com/x", "missing.md"),
    ),
    ("an unclosed run, which is literal text", "see ` and [CLI](cli.md#init)", ("cli.md#init",)),
    ("a fenced block, whose contents never reach inline parsing", "```\n[x](nope.md)\n```", ()),
    ("an indented code block", "    [x](nope.md)\n", ()),
)


@pytest.mark.parametrize(("label", "markdown", "expected"), _LINK_EXTRACTION_CASES)
def test_link_targets_match_commonmark(
    label: str, markdown: str, expected: tuple[str, ...]
) -> None:
    """Only links a renderer would emit are checked — no more, no fewer."""
    assert tuple(_link_targets(markdown)) == expected, label


def test_anchors_come_from_rendered_headings_and_live_html(tmp_path: Path) -> None:
    """Anchor sources must match what GitHub would generate for the same file."""
    source = tmp_path / "doc.md"
    source.write_text(
        "# Title `code`\n"
        "\n"
        '<a name="manual"></a>\n'
        "\n"
        "## Repeat\n"
        "\n"
        "## Repeat\n"
        "\n"
        "```\n"
        "## Fenced heading\n"
        '<a name="fenced"></a>\n'
        "```\n"
        "\n"
        "Setext\n"
        "------\n",
        encoding="utf-8",
    )
    anchors = _anchors(source)
    # Backticks drop out of the slug but their content stays.
    assert "title-code" in anchors
    assert "manual" in anchors
    # A repeated heading gets GitHub's -1 suffix, and setext headings count.
    assert {"repeat", "repeat-1", "setext"} <= anchors
    # Nothing inside a fenced block is a heading or a live anchor.
    assert "fenced-heading" not in anchors
    assert "fenced" not in anchors

    # An anchor commented out renders nothing, so a fragment pointing at it is
    # broken and must not be accepted.
    commented = tmp_path / "commented.md"
    commented.write_text('<!-- <a name="ghost"></a> -->\n\n# Real\n', encoding="utf-8")
    assert "ghost" not in _anchors(commented)
    assert "real" in _anchors(commented)

    # Image alt text is not live HTML either.
    alt = tmp_path / "alt.md"
    alt.write_text('![<a name="alt-ghost"></a>](pic.png)\n', encoding="utf-8")
    assert "alt-ghost" not in _anchors(alt)


def _check_links_of(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> str | None:
    """Run the real link check over one synthetic doc; return its failure text."""
    module = sys.modules[__name__]
    source = tmp_path / "doc.md"
    source.write_text(body, encoding="utf-8")
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "_public_markdown", lambda: [source])
    try:
        test_public_markdown_relative_links_and_anchors_resolve()
    except Failed as exc:
        # Only the checker's own ``pytest.fail`` counts as "it caught it" — an
        # unrelated crash must propagate, not read as a detected broken link.
        return str(exc)
    return None


def test_link_checker_catches_breakage_the_delimiter_traps_used_to_hide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end positive control: each trap must still surface real breakage.

    ``_link_targets`` is unit-tested above; this drives the whole checker, so a
    future change that extracts the right targets but stops reporting them
    cannot pass.
    """
    assert _check_links_of(tmp_path, monkeypatch, "`x` [gone](missing.md)\n") is not None
    assert _check_links_of(tmp_path, monkeypatch, "# Title\n\n`x` [bad](doc.md#nope)\n") is not None
    # ...while the same link inside a code span is correctly ignored.
    assert _check_links_of(tmp_path, monkeypatch, "`[gone](missing.md)`\n") is None
    # An escaped backtick does not open a span, so this breakage is still caught.
    assert _check_links_of(tmp_path, monkeypatch, "\\`[gone](missing.md)\\`\n") is not None
    assert _check_links_of(tmp_path, monkeypatch, "`a\n` [gone](missing.md) `b\n`\n") is not None, (
        "a link between two multiline spans is real and its breakage must be caught"
    )
    assert _check_links_of(tmp_path, monkeypatch, "`code\\` [gone](missing.md) `\n") is not None, (
        "escapes are inert inside a span, so the following link is real"
    )
    assert _check_links_of(tmp_path, monkeypatch, "`open\n\n[gone](missing.md) `\n") is not None, (
        "unclosed backticks in different blocks cannot pair"
    )
    assert _check_links_of(tmp_path, monkeypatch, "!`x`[gone](missing.md)\n") is not None, (
        "a bang beside a span must not turn the link into an ignored image"
    )
    # GitHub linkifies a bare canonical URL; a CommonMark parser does not, so
    # the checker adds them back rather than leaving them unvalidated.
    canonical = "https://github.com/memtomem/memtomem-stm/blob/main/"
    assert _check_links_of(tmp_path, monkeypatch, f"See {canonical}nope.md today\n") is not None, (
        "a bare canonical URL is a live link on GitHub and must be checked"
    )
    assert _check_links_of(tmp_path, monkeypatch, f"Run `{canonical}nope.md` today\n") is None, (
        "...but the same URL inside a code span is sample text"
    )
    assert (
        _check_links_of(
            tmp_path, monkeypatch, f"Sample `{canonical}nope.md`\n\nLive {canonical}nope.md\n"
        )
        is not None
    ), "one sample occurrence must not silence the same URL used live elsewhere"


def test_public_markdown_avoids_constructs_this_checker_cannot_model() -> None:
    """Refuse the two GFM shapes a CommonMark parser reads differently.

    Neither appears in the docs today, and the checker would not merely miss
    them — it would extract the *wrong* target:

    * ``See[^1].`` with ``[^1]: note`` is a GitHub footnote, but CommonMark
      reads it as a shortcut reference link whose destination is ``note``.
    * ``| [text | continued](x.md) |`` is split at the unescaped pipe by GFM
      and renders no link at all, while CommonMark returns ``x.md``.

    Failing loudly beats silently checking something GitHub does not render.
    Escape the pipe as ``\\|`` inside a table cell; footnotes have no
    workaround here, so adding one means teaching this module GFM first.
    """
    footnote = re.compile(r"^\s{0,3}\[\^[^\]]+\]:", re.MULTILINE)
    pipe_in_link_text = re.compile(r"^\s*\|.*\[[^\]]*(?<!\\)\|[^\]]*\]\(", re.MULTILINE)
    failures: list[str] = []
    for source in _public_markdown():
        body = source.read_text(encoding="utf-8")
        name = source.relative_to(REPO_ROOT)
        if footnote.search(body):
            failures.append(f"{name}: GFM footnote definition")
        if pipe_in_link_text.search(body):
            failures.append(f"{name}: unescaped pipe inside a table-cell link text")
    if failures:
        pytest.fail(
            "Public docs use markdown this CommonMark-based checker misreads:\n"
            + "\n".join(failures)
        )


def test_public_markdown_relative_links_and_anchors_resolve() -> None:
    failures: list[str] = []
    for source in _public_markdown():
        # The parser already resolved which targets are live links: code spans,
        # fenced blocks and images never reach this loop, and an angle-bracket
        # destination arrives unwrapped.
        body = source.read_text(encoding="utf-8")
        for raw in [*_link_targets(body), *_bare_canonical_urls(body)]:
            raw = raw.strip()
            if not raw:
                continue
            canonical = next(
                (prefix for prefix in _CANONICAL_PREFIXES if raw.startswith(prefix)),
                None,
            )
            if canonical is not None:
                target_part, _, fragment = raw[len(canonical) :].partition("#")
                target = REPO_ROOT / unquote(target_part)
                if not target.exists():
                    failures.append(
                        f"{source.relative_to(REPO_ROOT)} missing canonical target: {raw}"
                    )
                elif (
                    fragment
                    and target.suffix.lower() == ".md"
                    and unquote(fragment) not in _anchors(target)
                ):
                    failures.append(
                        f"{source.relative_to(REPO_ROOT)} broken canonical anchor: {raw}"
                    )
                continue
            if re.match(r"^(?:https?|mailto):", raw) or raw.startswith("#"):
                if raw.startswith("#") and raw[1:] not in _anchors(source):
                    failures.append(f"{source.relative_to(REPO_ROOT)} -> {raw}")
                continue
            target_part, _, fragment = raw.partition("#")
            target = (source.parent / unquote(target_part)).resolve()
            try:
                target.relative_to(REPO_ROOT)
            except ValueError:
                failures.append(f"{source.relative_to(REPO_ROOT)} escapes repo: {raw}")
                continue
            if not target.exists():
                failures.append(f"{source.relative_to(REPO_ROOT)} missing: {raw}")
                continue
            if (
                fragment
                and target.suffix.lower() == ".md"
                and unquote(fragment) not in _anchors(target)
            ):
                failures.append(f"{source.relative_to(REPO_ROOT)} broken anchor: {raw}")
    if failures:
        pytest.fail("Broken public documentation links:\n" + "\n".join(failures))


def test_public_markdown_has_no_duplicate_generated_anchors() -> None:
    failures: list[str] = []
    for source in _public_markdown():
        if source.name == "CHANGELOG.md":
            # Keep-a-Changelog intentionally repeats category headings.
            continue
        seen: set[str] = set()
        for title in _headings(source.read_text(encoding="utf-8")):
            slug = _github_slug(title)
            if slug in seen:
                failures.append(f"{source.relative_to(REPO_ROOT)} duplicate base anchor: {slug}")
            seen.add(slug)
    if failures:
        pytest.fail("Duplicate public documentation heading slugs:\n" + "\n".join(failures))
