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

    # Split on blank lines (markdown paragraph boundaries) and scope the
    # caveat check to paragraphs that actually describe ``--from-clients`` /
    # ``--import``. Checking the whole file would pass even if someone moved
    # the warning to an unrelated section (install guide, release notes)
    # while deleting it from where a reader of ``--import`` docs looks.
    # Paragraph scope rather than a tight ±N-line window so prose
    # restructuring inside the same paragraph doesn't false-fail.
    paragraphs = re.split(r"\n\s*\n", cli_md)
    import_paragraphs = [p for p in paragraphs if "--from-clients" in p or "--import" in p]
    if not import_paragraphs:
        pytest.fail(
            "docs/cli.md no longer mentions `--from-clients` / `--import` — "
            "the flag was renamed or removed. Update this test alongside "
            "the docs change."
        )

    has_caveat = any(re.search(r"macOS[- ]only", p, re.IGNORECASE) for p in import_paragraphs)
    assert has_caveat, (
        "docs/cli.md must call out that Claude Desktop discovery is "
        "macOS-only in a paragraph that mentions `--from-clients` / "
        "`--import`. Without this caveat, Linux/Windows callers silently "
        "see zero Claude Desktop candidates from `mms add --import`."
    )


def test_configuration_full_example_documents_upstream_timeouts() -> None:
    """docs/configuration.md's full-example upstream block must list the
    per-upstream timeout fields added in v0.1.12 (#206).

    ``UpstreamServerConfig`` in ``src/memtomem_stm/proxy/config.py`` exposes
    ``call_timeout_seconds`` and ``overall_deadline_seconds`` as the
    bounded-hang knobs for upstream tool calls. If the "Full example"
    block omits them, users who don't read CHANGELOG have no surface to
    discover these tuning knobs and a silently-hung upstream looks like
    a proxy bug rather than a tunable timeout. Scope the assertion to
    the ``## Config File`` section so moving the fields into release
    notes or env-var prose cannot satisfy the check.
    """
    config_md = _read("docs/configuration.md")
    section_match = re.search(
        r"##\s+Config File[^\n]*\n(.*?)(?=\n##\s|\Z)",
        config_md,
        re.DOTALL,
    )
    if not section_match:
        pytest.fail(
            "docs/configuration.md lost its `## Config File` H2 section — "
            "either restructure the test or restore the section heading."
        )
    section_body = section_match.group(1)
    block_match = re.search(r"```json\n(.*?)\n```", section_body, re.DOTALL)
    if not block_match:
        pytest.fail(
            "docs/configuration.md `## Config File` section lost its "
            "```json fenced example — either restructure the test or "
            "restore the full-example block."
        )
    example = block_match.group(1)

    required = ("call_timeout_seconds", "overall_deadline_seconds")
    missing = [field for field in required if field not in example]
    if missing:
        pytest.fail(
            f"docs/configuration.md full-example is missing upstream "
            f"timeout field(s): {missing!r}. These exist on "
            "UpstreamServerConfig (src/memtomem_stm/proxy/config.py, "
            "defaults 90s / 180s) and were added in v0.1.12 (#206) to "
            "bound silently-hung upstreams. Keep them visible next to "
            "`max_retries` / `reconnect_delay_seconds` so operators "
            "discover them when scanning the example."
        )


def test_compression_md_llm_section_documents_timeout_fallback() -> None:
    """docs/compression.md's ``## LLM Compression`` section must surface
    ``llm_timeout_seconds`` in the config example or fallback prose.

    ``LLMCompressorConfig.llm_timeout_seconds``
    (``src/memtomem_stm/proxy/config.py``, default 60.0) bounds the LLM
    call; on timeout the compressor falls back to ``TruncateCompressor``
    and records ``llm_summary→timeout_fallback`` in ``proxy_metrics``
    (v0.1.12, #207/#210). Without this documented, users cannot tune the
    timeout, cannot interpret the fallback label in metrics, and assume
    "API failure (circuit breaker protection)" is the only fallback path.
    """
    comp_md = _read("docs/compression.md")
    section_match = re.search(
        r"##\s+LLM Compression[^\n]*\n(.*?)(?=\n##\s|\Z)",
        comp_md,
        re.DOTALL,
    )
    if not section_match:
        pytest.fail(
            "docs/compression.md lost its `## LLM Compression` H2 section — "
            "either restructure the test or restore the section heading."
        )
    section_body = section_match.group(1)
    if "llm_timeout_seconds" not in section_body:
        pytest.fail(
            "docs/compression.md `## LLM Compression` section must "
            "mention `llm_timeout_seconds` — either in the JSON example "
            "or the fallback prose. Without it, the timeout-bound LLM "
            "compression path introduced in v0.1.12 (#207/#210) is "
            "invisible to users reading the LLM section, and the "
            "`llm_summary→timeout_fallback` metric label has no "
            "documented origin."
        )
