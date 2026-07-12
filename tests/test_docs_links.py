"""Local link and anchor checks for the tracked public documentation surface."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _public_markdown() -> list[Path]:
    roots = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CLA.md",
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / "SECURITY.md",
        REPO_ROOT / "notebooks" / "README.md",
    ]
    docs = [path for path in (REPO_ROOT / "docs").rglob("*.md") if "reports" not in path.parts]
    return sorted([*roots, *docs])


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


def test_public_markdown_relative_links_and_anchors_resolve() -> None:
    failures: list[str] = []
    for source in _public_markdown():
        text = _outside_fences(source.read_text(encoding="utf-8"))
        for match in re.finditer(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", text):
            raw = match.group(1).strip()
            if raw.startswith("<") and raw.endswith(">"):
                raw = raw[1:-1]
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
