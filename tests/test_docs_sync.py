"""Cross-file invariants between docs and the source of truth.

Pins drift between ``CONTRIBUTING.md`` / ``docs/`` and the things they
quote (``.github/workflows/ci.yml``, ``src/memtomem_stm/cli/proxy.py``).
A contributor updating one side without the other is otherwise invisible
in local testing and only surfaces through user confusion.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_contributing_pytest_command_matches_ci() -> None:
    """CONTRIBUTING.md's ``pytest -m`` filter must match the one in CI.

    The CI ``test`` job filters out ``bench_qa_meta`` (intentional-failure
    self-tests — see ``pyproject.toml`` markers table) and
    ``bench_qa_llm_judge`` (requires ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``)
    in addition to ``ollama``. A shorter filter in CONTRIBUTING leads new
    contributors straight into those expected failures.
    """
    ci = _read(".github/workflows/ci.yml")
    contributing = _read("CONTRIBUTING.md")

    # Double-quoted ``pytest -m "…"`` is what the workflow uses today. If the
    # form ever changes (single quotes, ``run: |`` block, matrix variable),
    # this regex falls through to zero matches and we want a loud failure
    # pointing operators back here rather than a cryptic IndexError.
    ci_filters = re.findall(r'pytest -m "([^"]+)"', ci)
    test_job_filters = [f for f in ci_filters if "not ollama" in f]

    if not test_job_filters:
        pytest.fail(
            "Could not locate CI's pytest filter — expected a double-quoted "
            '`pytest -m "…not ollama…"` in .github/workflows/ci.yml. '
            "The workflow was likely refactored; update this test and "
            "CONTRIBUTING.md together."
        )
    if len(test_job_filters) > 1:
        pytest.fail(
            "Multiple CI jobs now use `not ollama` — this test picks the "
            "first match, which may not be the one CONTRIBUTING should "
            f"mirror. Filters found: {test_job_filters!r}. Parse by job "
            "name or pin the canonical one explicitly."
        )
    canonical = test_job_filters[0]

    contributing_filters = re.findall(r'pytest -m "([^"]+)"', contributing)
    assert canonical in contributing_filters, (
        f"CONTRIBUTING.md must quote the CI pytest filter verbatim.\n"
        f"  CI uses: {canonical!r}\n"
        f"  CONTRIBUTING has: {contributing_filters!r}"
    )


def test_cli_docs_flag_desktop_discovery_is_macos_only() -> None:
    """``docs/cli.md`` must warn that Claude Desktop discovery is macOS-only.

    ``_desktop_config_path()`` in ``src/memtomem_stm/cli/proxy.py`` returns
    only the macOS path (``~/Library/Application Support/Claude/...``);
    Linux/Windows callers of ``mms add --import`` silently see zero
    Claude Desktop candidates. If that helper ever learns OS-aware
    variants, relax this pin and drop the caveat from the docs.
    """
    proxy_src = _read("src/memtomem_stm/cli/proxy.py")

    func_match = re.search(
        r"def _desktop_config_path\b.*?(?=\ndef |\nclass |\Z)",
        proxy_src,
        re.DOTALL,
    )
    assert func_match, "_desktop_config_path helper not found — update this test"
    desktop_func = func_match.group(0)
    # Sanity: the helper is still macOS-only (no Windows/Linux paths embedded).
    assert "Library/Application Support/Claude" in desktop_func
    assert "APPDATA" not in desktop_func
    assert ".config/Claude" not in desktop_func

    cli_md = _read("docs/cli.md")
    assert re.search(r"macOS[- ]only", cli_md, re.IGNORECASE), (
        "docs/cli.md must call out that Claude Desktop discovery via "
        "`mms add --import` is macOS-only — Linux/Windows users otherwise "
        "see a silent zero-result import"
    )
