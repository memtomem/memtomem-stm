"""Local link and anchor checks for the tracked public documentation surface."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest
from _pytest.outcomes import Failed

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


def _strip_code_spans(text: str) -> str:
    """Blank CommonMark inline code spans, preserving line structure.

    A regex cannot do this correctly. A code span opens on a backtick run and
    closes on the next run of *exactly* the same length, the span may contain a
    line ending, and backslash escapes do not apply inside one. Any line-scoped
    or escape-aware pattern therefore mispairs delimiters and can delete a real
    link — both directions were observed:

    * ``\\`x\\`` on line 1, ``\\`` then a link then ``\\`y`` on line 2, ``\\``
      on line 3: the middle line's two backticks close span one and open span
      two, so the link between them is real, but a line-scoped matcher pairs
      them and removes it.
    * ``\\`code\\\\\\` [link](x) \\``: the backtick after the backslash closes
      the span (escapes are inert inside it), so the link is real, but an
      escape-aware lookbehind skips that closer and pairs the outermost pair.

    Newlines inside a span are preserved so the caller's line-oriented passes
    keep their alignment. An unclosed run stays literal text, as CommonMark
    specifies.
    """

    def escaped(position: int) -> bool:
        """True when the character at ``position`` is backslash escaped."""
        backslashes = 0
        while position - backslashes - 1 >= 0 and text[position - backslashes - 1] == "\\":
            backslashes += 1
        return backslashes % 2 == 1

    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text[index] != "`":
            out.append(text[index])
            index += 1
            continue
        # Escapes are honoured in ordinary text, so an escaped backtick cannot
        # OPEN a span. They are inert inside a span, so the closer scan below
        # deliberately does not consult this.
        if escaped(index):
            out.append(text[index])
            index += 1
            continue
        opener_end = index
        while opener_end < length and text[opener_end] == "`":
            opener_end += 1
        run = opener_end - index

        closer: tuple[int, int] | None = None
        cursor = opener_end
        while cursor < length:
            if text[cursor] != "`":
                cursor += 1
                continue
            candidate_end = cursor
            while candidate_end < length and text[candidate_end] == "`":
                candidate_end += 1
            if candidate_end - cursor == run:
                closer = (cursor, candidate_end)
                break
            cursor = candidate_end

        if closer is None:
            out.append("`" * run)
            index = opener_end
            continue
        # Drop the span's text so sample link syntax is not checked as a live
        # link, but leave a space and the interior newlines behind. Stripping to
        # zero width would let the neighbours fuse into a different token: in
        # ``!`x`[bad](y)`` the surviving ``!`` would turn a real link into an
        # image, which the link regex skips — a hidden broken link.
        out.append(" " + "\n" * text.count("\n", opener_end, closer[0]))
        index = closer[1]
    return "".join(out)


_HEADING_LINE = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")

# An autolink or raw HTML tag. These have the same precedence as code spans and
# are resolved left to right, so a backtick they consume cannot also open a
# span. Masking those backticks is enough to keep the scanner from pairing them;
# the construct itself is left in place because _anchors() reads ``<a name=...>``
# out of this same output.
_INLINE_ANGLE_CONSTRUCT = re.compile(r"<[^<>\n]*>")
_MASKED_BACKTICK = "\x00"


def _outside_fences(text: str) -> str:
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = re.match(r"^\s*(```+|~~~+)", line)
        if marker:
            token = marker.group(1)[0]
            if fence is None:
                fence = token
            elif token == fence:
                fence = None
            lines.append("")
        elif fence is None:
            lines.append(line)
        else:
            lines.append("")

    # Heading lines keep their backticks: _anchors() slugs them, and blanking a
    # span inside a heading would change the generated anchor. Code spans are
    # stripped from maximal runs of non-heading lines, so a span may still wrap
    # across lines within such a run.
    #
    # Known limitation, verified to fail only in the safe direction: a code span
    # that wraps across a line which *looks* like an ATX heading is split here,
    # so both halves become unclosed runs and nothing inside is stripped. That
    # can only surface a link the checker would otherwise skip (a spurious
    # failure someone must look at), never hide a broken one.
    #
    # CommonMark resolves blocks before inlines, so a span can never span a
    # block boundary. Flushing at blank lines is what enforces that — and it is
    # also what stops the blanked lines of a fenced block from letting stray
    # backticks on either side of the fence pair with each other.
    result: list[str] = []
    block: list[str] = []

    def flush() -> None:
        if block:
            masked = _INLINE_ANGLE_CONSTRUCT.sub(
                lambda match: match.group(0).replace("`", _MASKED_BACKTICK),
                "\n".join(block),
            )
            stripped = _strip_code_spans(masked).replace(_MASKED_BACKTICK, "`")
            result.extend(stripped.split("\n"))
            block.clear()

    for line in lines:
        if _HEADING_LINE.match(line) or not line.strip():
            flush()
            result.append(line)
        else:
            block.append(line)
    flush()
    return "\n".join(result)


def _github_slug(title: str) -> str:
    title = re.sub(r"<[^>]+>", "", title).strip().lower()
    title = re.sub(r"[^\w\- ]", "", title, flags=re.UNICODE)
    return title.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = _outside_fences(path.read_text(encoding="utf-8"))
    anchors = set(re.findall(r"<a\s+(?:name|id)=[\"']([^\"']+)", text, re.IGNORECASE))
    counts: dict[str, int] = {}
    for line in text.splitlines():
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not heading:
            continue
        base = _github_slug(heading.group(1))
        count = counts.get(base, 0)
        counts[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def test_outside_fences_strips_inline_code_without_eating_real_links() -> None:
    """``_outside_fences`` narrows what the checker sees, so pin both directions."""
    stripped = _outside_fences("See `mms init` then [CLI](cli.md#init) for details.")
    # Positive control: the real link on a code-span line is still visible.
    assert "[CLI](cli.md#init)" in stripped
    assert "mms init" not in stripped

    # A link written *inside* a code span is documentation of syntax, not a link.
    assert "[x](nope.md)" not in _outside_fences("Write `[x](nope.md)` to link.")
    # Double-backtick spans close on their own run length.
    assert "[x](nope.md)" not in _outside_fences("Use ``[x](nope.md)`` verbatim.")

    # Two code spans must not merge into one greedy match that eats the link
    # between them.
    between = _outside_fences("`a` [CLI](cli.md#init) `b`")
    assert "[CLI](cli.md#init)" in between
    assert "a" not in between and "b" not in between

    # A backslash-escaped backtick is a literal character, not a delimiter, so
    # the link between two of them is real and must survive.
    escaped = _outside_fences(r"\`[CLI](cli.md#init)\`")
    assert "[CLI](cli.md#init)" in escaped

    # ...but escapes are inert INSIDE a span, so the backtick after the
    # backslash closes it and the link that follows is outside and real.
    assert "[bad](missing.md)" in _outside_fences("`code\\` [bad](missing.md) `")

    # A span may wrap across lines. The middle line's two backticks close the
    # first span and open the second, so the link between them is real — a
    # line-scoped matcher pairs them and silently deletes it.
    wrapped = _outside_fences("`first span\n` [bad](missing.md) `second span\n`")
    assert "[bad](missing.md)" in wrapped
    assert wrapped.count("\n") == 2, "line alignment must survive span stripping"

    # A span cannot cross a block boundary, so two unclosed backticks in
    # different blocks are literal text and the link between them is real.
    assert "[bad](missing.md)" in _outside_fences("`open\n\n[bad](missing.md) `")
    assert "[bad](missing.md)" in _outside_fences("`open\n```\nsample\n```\n[bad](missing.md) `"), (
        "a fenced block's blanked lines must not let backticks pair across it"
    )

    # A stripped span must not fuse its neighbours into a different token: the
    # surviving `!` would make this an image, which the link regex skips.
    assert "[bad](missing.md)" in _outside_fences("!`sample`[bad](missing.md)")
    assert "![bad](missing.md)" not in _outside_fences("!`sample`[bad](missing.md)")

    # An autolink consumes its own backtick, so it cannot open a span.
    assert "[bad](missing.md)" in _outside_fences(
        "<https://example.com/`tick>\n[bad](missing.md) `"
    )
    # ...while the HTML the anchor scanner reads out of this text is untouched.
    assert '<a name="x">' in _outside_fences('<a name="x"></a> text')

    # An unclosed run is literal text, not an open span swallowing the rest.
    assert "[CLI](cli.md#init)" in _outside_fences("see ` and [CLI](cli.md#init)")

    # A span wrapping across a heading-looking line is split, so nothing inside
    # is stripped. Pin the direction of that miss: it must expose the link (a
    # visible spurious failure), never hide one.
    across_heading = _outside_fences("`code\n# not a heading\n` [x](nope.md)")
    assert "[x](nope.md)" in across_heading

    # Headings keep their backticks so heading slugs stay byte-faithful.
    assert _outside_fences("## `mms doctor`") == "## `mms doctor`"

    # Fenced blocks are still blanked wholesale.
    assert _outside_fences("```\n[x](nope.md)\n```") == "\n\n"


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


def test_link_checker_still_catches_breakage_after_code_span_stripping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control for the inline-code strip: real breakage must still fail."""
    assert _check_links_of(tmp_path, monkeypatch, "`x` [gone](missing.md)\n") is not None
    assert _check_links_of(tmp_path, monkeypatch, "# Title\n\n`x` [bad](doc.md#nope)\n") is not None
    # ...while the same link inside a code span is correctly ignored.
    assert _check_links_of(tmp_path, monkeypatch, "`[gone](missing.md)`\n") is None
    # An escaped backtick does not open a span, so this breakage is still caught.
    assert _check_links_of(tmp_path, monkeypatch, "\\`[gone](missing.md)\\`\n") is not None
    # End to end, both delimiter-pairing traps must still surface real breakage.
    assert _check_links_of(tmp_path, monkeypatch, "`a\n` [gone](missing.md) `b\n`\n") is not None, (
        "a link between two multiline spans is real and its breakage must be caught"
    )
    assert _check_links_of(tmp_path, monkeypatch, "`code\\` [gone](missing.md) `\n") is not None, (
        "escapes are inert inside a span, so the following link is real"
    )


def test_public_markdown_relative_links_and_anchors_resolve() -> None:
    failures: list[str] = []
    for source in _public_markdown():
        text = _outside_fences(source.read_text(encoding="utf-8"))
        for match in re.finditer(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", text):
            raw = match.group(1).strip()
            if raw.startswith("<") and raw.endswith(">"):
                raw = raw[1:-1]
            canonical_prefixes = (
                "https://github.com/memtomem/memtomem-stm/blob/main/",
                "https://github.com/memtomem/memtomem-stm/tree/main/",
            )
            canonical = next(
                (prefix for prefix in canonical_prefixes if raw.startswith(prefix)),
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
        text = _outside_fences(source.read_text(encoding="utf-8"))
        seen: set[str] = set()
        for line in text.splitlines():
            heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
            if not heading:
                continue
            slug = _github_slug(heading.group(1))
            if slug in seen:
                failures.append(f"{source.relative_to(REPO_ROOT)} duplicate base anchor: {slug}")
            seen.add(slug)
    if failures:
        pytest.fail("Duplicate public documentation heading slugs:\n" + "\n".join(failures))
