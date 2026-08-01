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


# An inline code span, delimited by a run of backticks that is not backslash
# escaped. Non-greedy so two spans on one line do not merge and swallow a real
# link between them; the lookbehinds keep an escaped ``\``` (a literal backtick,
# not a delimiter) from opening or closing a span. Deliberately line-scoped: a
# code span wrapped across lines is not tracked, which can only make the checker
# noisier — it never hides a link.
_CODE_SPAN = re.compile(r"(?<!\\)(`+).*?(?<!\\)\1")


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
            lines.append(line if line.lstrip().startswith("#") else _CODE_SPAN.sub("", line))
        else:
            lines.append("")
    return "\n".join(lines)


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
