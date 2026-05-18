"""Cross-file invariants between docs and the source of truth.

Pins drift between ``CONTRIBUTING.md`` / ``docs/`` and the things they
quote (``.github/workflows/ci.yml``, ``src/memtomem_stm/cli/proxy.py``).
A contributor updating one side without the other is otherwise invisible
in local testing and only surfaces through user confusion.
"""

from __future__ import annotations

import ast
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

    ``_desktop_config_path()`` in ``src/memtomem_stm/mms/import_hosts.py``
    returns only the macOS path (``~/Library/Application Support/Claude/...``);
    Linux/Windows callers of ``mms add --import`` silently see zero
    Claude Desktop candidates. If that helper ever learns OS-aware
    variants, relax this pin and drop the caveat from the docs.

    The helper used to live in ``cli/proxy.py``; W1 PR2 moved it to
    ``mms/import_hosts.py`` so both ``mms add --from-clients`` and
    ``mms import`` share a single definition.
    """
    helper_src = _read("src/memtomem_stm/mms/import_hosts.py")

    func_match = re.search(
        r"def _desktop_config_path\b.*?(?=\ndef |\nclass |\Z)",
        helper_src,
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
    three per-upstream timeout knobs on ``UpstreamServerConfig``.

    ``src/memtomem_stm/proxy/config.py`` exposes ``connect_timeout_seconds``
    (bounds ``session.initialize()``, #53), ``call_timeout_seconds`` (bounds
    each ``session.call_tool()`` attempt, #206), and
    ``overall_deadline_seconds`` (wall-clock budget across retries, #206).
    If the "Full example" block omits any of them, users who don't read
    CHANGELOG have no surface to discover these tuning knobs and a
    silently-hung upstream looks like a proxy bug rather than a tunable
    timeout. Scope the assertion to the ``## Config File`` section so
    moving the fields into release notes or env-var prose cannot satisfy
    the check.
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

    required = (
        "connect_timeout_seconds",
        "call_timeout_seconds",
        "overall_deadline_seconds",
    )
    missing = [field for field in required if field not in example]
    if missing:
        pytest.fail(
            f"docs/configuration.md full-example is missing upstream "
            f"timeout field(s): {missing!r}. These exist on "
            "UpstreamServerConfig (src/memtomem_stm/proxy/config.py, "
            "defaults 30s / 90s / 180s) and bound silently-hung upstreams "
            "at the init, per-call, and overall-deadline layers. Keep "
            "them visible next to `max_retries` / `reconnect_delay_seconds` "
            "so operators discover them when scanning the example."
        )


_PROXY_MANAGER_EXPECTED_KWARGS = frozenset(
    {
        "surfacing_engine",
        "cache",
        "env_overrides",
        "progressive_reads_tracker",
    }
)


def test_bundled_server_proxy_manager_omits_index_engine() -> None:
    """The bundled ``mms`` server's ``ProxyManager(...)`` construction in
    ``app_lifespan`` must not pass ``index_engine=``.

    ``docs/configuration.md`` (Stage 4 NOTE block, ~lines 73-87) tells
    operators that ``AUTO_INDEX__*`` / ``EXTRACTION__*`` are inert in the
    bundled server because no index engine is wired. That claim is grounded
    in ``src/memtomem_stm/server.py``'s single ``ProxyManager(...)`` call,
    which today passes only ``config.proxy`` and ``tracker`` positionally
    plus a fixed set of unrelated kwargs (#288/#299, PR #339). If a future
    PR wires an ``index_engine=`` here without also dropping the inert
    NOTE, the docs silently revalidate against stale prose — operators
    keep seeing the no-op warning in the docs while the server now writes
    to LTM.

    Paired with ``test_configuration_md_stage4_inert_note_pinned`` (the
    inverse direction).
    """
    server_src = _read("src/memtomem_stm/server.py")
    tree = ast.parse(server_src)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ProxyManager"
    ]
    if len(calls) != 1:
        pytest.fail(
            f"Could not locate a unique `ProxyManager(...)` construction in "
            f"src/memtomem_stm/server.py (found {len(calls)}). The wiring was "
            "refactored; update this test alongside the refactor before "
            "assuming Stage 4 is now wired."
        )
    call = calls[0]

    kwarg_names = {kw.arg for kw in call.keywords if kw.arg is not None}
    if "index_engine" in kwarg_names:
        pytest.fail(
            "server.py wires `index_engine=` into ProxyManager — Stage 4 is "
            "no longer inert in the bundled `mms` server. Update "
            "`docs/configuration.md` to drop the inert NOTE (lines ~73-87) "
            "and close #299."
        )
    if len(call.args) != 2:
        pytest.fail(
            f"`ProxyManager(...)` construction in server.py has "
            f"{len(call.args)} positional args (expected 2: `config.proxy`, "
            "`tracker`). A 3rd positional would smuggle `index_engine` past "
            "the keyword check — `ProxyManager.__init__`'s 3rd parameter is "
            "`index_engine`. Either update this test alongside the new "
            "signature, or close #299 and drop the inert NOTE in "
            "`docs/configuration.md`."
        )
    extras = kwarg_names - _PROXY_MANAGER_EXPECTED_KWARGS
    if extras:
        pytest.fail(
            f"`ProxyManager(...)` construction in server.py has unexpected "
            f"kwarg(s): {sorted(extras)!r}. If you added a new unrelated "
            "kwarg, append it to `_PROXY_MANAGER_EXPECTED_KWARGS` in this "
            "test. If you wired `index_engine=` (renamed?), also drop the "
            "inert NOTE in `docs/configuration.md` (lines ~73-87) and close "
            "#299."
        )


def test_configuration_md_stage4_inert_note_pinned() -> None:
    """``docs/configuration.md``'s Stage 4 NOTE block must keep the inert-
    state warning that pairs with ``server.py``'s engine-less ``ProxyManager``
    construction.

    PR #339 (resolving the operator-visible half of #288) added a comment
    block immediately above the ``AUTO_INDEX__*`` exports explaining that
    those env vars currently have no runtime effect because the bundled
    server constructs ``ProxyManager`` without an ``index_engine``. If
    that NOTE goes away while the server still has no engine wired
    (``test_bundled_server_proxy_manager_omits_index_engine``), operators
    flipping ``AUTO_INDEX__ENABLED=true`` get zero behaviour and zero
    explanation again — the failure mode #288 originally reported.
    """
    config_md = _read("docs/configuration.md")
    # Locate the contiguous ``^#`` comment paragraph immediately
    # preceding the ``AUTO_INDEX__ENABLED`` export. Paragraph scope (not
    # a tight line window) so prose tweaks inside the same block don't
    # false-fail; anchoring to the actual export keeps the check tied
    # to the operator-facing surface.
    block_match = re.search(
        r"((?:^#[^\n]*\n)+)export MEMTOMEM_STM_PROXY__AUTO_INDEX__ENABLED=",
        config_md,
        re.MULTILINE,
    )
    if not block_match:
        pytest.fail(
            "docs/configuration.md no longer has a `#`-comment paragraph "
            "directly above `export MEMTOMEM_STM_PROXY__AUTO_INDEX__ENABLED=` — "
            "either the export was renamed/removed, or the NOTE was "
            "moved into prose. Update this test alongside the docs "
            "restructure or restore the inert NOTE (see #299)."
        )
    note = block_match.group(1).lower()

    required = ("inert", "index_engine", "#288")
    missing = [kw for kw in required if kw.lower() not in note]
    if missing:
        pytest.fail(
            f"docs/configuration.md Stage 4 NOTE is missing keyword(s): "
            f"{missing!r}. If `mms` now wires `index_engine`, also delete "
            "`test_bundled_server_proxy_manager_omits_index_engine` and "
            "close #299. Otherwise restore the inert NOTE — operators "
            "flipping `AUTO_INDEX__ENABLED=true` need to know the env var "
            "is currently a no-op in the bundled server (PR #339, #288)."
        )


def test_surfacing_md_documents_phase_1_observability_sample() -> None:
    """docs/surfacing.md's ``stm_surfacing_stats`` example must include
    the Phase 1 ``Skip reasons`` / ``Outcomes`` / ``Cache`` sections.

    The header strings ship in ``server.py::_format_observability_sections``
    (v0.1.19 / PR #256). If a future operator regenerates the sample
    output and only captures the legacy event-counts block, readers
    of `docs/surfacing.md` lose the only operator-facing surface that
    explains *why* surfacing skipped, and the RFC's "no more
    DEBUG-log only skips" promise becomes invisible. Scope the
    assertion to the fenced code block immediately after the
    "Check effectiveness with `stm_surfacing_stats`" prose so a
    new section pasted into an unrelated part of the file cannot
    satisfy the check.
    """
    server_src = _read("src/memtomem_stm/server.py")
    # Pin the exact header literals server.py emits — if they ever
    # rename, the test fails loudly here rather than silently passing
    # against stale docs.
    required_headers = ("Skip reasons", "Outcomes", "Cache (since process start)")
    missing_in_source = [h for h in required_headers if h not in server_src]
    if missing_in_source:
        pytest.fail(
            f"server.py no longer emits header(s) {missing_in_source!r} — "
            "_format_observability_sections was likely renamed or its "
            "literals changed. Update this test and docs/surfacing.md "
            "together."
        )

    surfacing_md = _read("docs/surfacing.md")
    # Locate the fenced sample block right after the "Check
    # effectiveness" sentence. Splitting on triple-backtick fences and
    # picking the one preceded by that phrase keeps the check tied to
    # the operator-facing example, not any other code block in the
    # file (e.g. a config snippet that happens to mention surfacing).
    anchor = "Check effectiveness with `stm_surfacing_stats`:"
    if anchor not in surfacing_md:
        pytest.fail(
            f"docs/surfacing.md no longer contains the {anchor!r} "
            "anchor — the section was renamed or removed. Update this "
            "test alongside the docs restructure."
        )
    after_anchor = surfacing_md.split(anchor, 1)[1]
    block_match = re.search(r"```\n(.*?)\n```", after_anchor, re.DOTALL)
    if not block_match:
        pytest.fail(
            "docs/surfacing.md no longer has a fenced sample block "
            "after the 'Check effectiveness' anchor — restore the "
            "stm_surfacing_stats example or update this test."
        )
    block = block_match.group(1)

    missing_in_docs = [h for h in required_headers if h not in block]
    if missing_in_docs:
        pytest.fail(
            f"docs/surfacing.md `stm_surfacing_stats` sample block is "
            f"missing Phase 1 observability header(s): {missing_in_docs!r}. "
            "These ship in server.py's _format_observability_sections "
            "and are the operator-facing surface for skip reasons, "
            "outcomes, and cache hit ratio (v0.1.19, #256). Without "
            "them in the sample, readers cannot tell what the new "
            "sections look like or what counters to expect."
        )


def test_surfacing_md_ltm_connection_distinguishes_from_generic_upstream() -> None:
    """``docs/surfacing.md``'s ``## LTM Connection`` section must not claim
    that LTM responses flow through the proxy pipeline.

    LTM is reached via the dedicated ``McpClientSearchAdapter``
    (``src/memtomem_stm/surfacing/mcp_client.py``) wired into
    ``SurfacingEngine``, not via ``ProxyConfig.upstream_servers``. LTM
    responses feed *into* the surfacing engine to compose context for
    upstream calls; they do not get compressed, cached, or surfaced as
    if they were upstream tool output. The previous wording — "the same
    compression / cache / surfacing pipeline applies" — implied the
    opposite and misled operators tuning compression for LTM-shaped
    responses (#298).

    Pinning the prose against the ``McpClientSearchAdapter`` import in
    ``server.py`` keeps the docs claim anchored to source: if LTM ever
    migrates onto the generic upstream path, the import goes away and
    this test fails loudly so the docs can be revisited.
    """
    server_src = _read("src/memtomem_stm/server.py")
    # Anchor: server.py still imports the special-purpose LTM adapter.
    # If this disappears, LTM may now travel the generic-upstream path
    # and the docs claim that it doesn't would need re-evaluation.
    assert "McpClientSearchAdapter" in server_src, (
        "src/memtomem_stm/server.py no longer imports "
        "`McpClientSearchAdapter` — LTM may have moved onto the generic "
        "upstream proxy path. Update docs/surfacing.md's `## LTM "
        "Connection` section alongside the wiring change (#298)."
    )

    surfacing_md = _read("docs/surfacing.md")
    section_match = re.search(
        r"##\s+LTM Connection[^\n]*\n(.*?)(?=\n##\s|\Z)",
        surfacing_md,
        re.DOTALL,
    )
    if not section_match:
        pytest.fail(
            "docs/surfacing.md lost its `## LTM Connection` H2 section — "
            "either restructure the test or restore the section heading."
        )
    section_body = section_match.group(1)

    # The pre-#298 phrasing must not return: it claimed LTM responses
    # flow through compression / cache / surfacing as if LTM were a
    # generic upstream, which contradicts the McpClientSearchAdapter
    # wiring above.
    deprecated = "same compression / cache / surfacing pipeline applies"
    if deprecated in section_body:
        pytest.fail(
            f"docs/surfacing.md `## LTM Connection` reintroduced the "
            f"deprecated phrase {deprecated!r}. LTM responses bypass the "
            "proxy pipeline (they feed *into* surfacing, not through it). "
            "Restore the #298 wording or, if LTM is now a generic "
            "upstream, also drop the `McpClientSearchAdapter` import "
            "check in this test."
        )

    # The replacement wording must keep the operator-facing claims:
    # (a) LTM bypasses the compression/cache pipeline, (b) it feeds the
    # surfacing engine, (c) crash isolation still holds. Use a lowercased
    # scope so casing tweaks don't false-fail.
    lowered = section_body.lower()
    required = ("bypass", "surfacing engine", "crash")
    missing = [kw for kw in required if kw not in lowered]
    if missing:
        pytest.fail(
            f"docs/surfacing.md `## LTM Connection` is missing keyword(s) "
            f"that distinguish LTM from a generic proxied upstream: "
            f"{missing!r}. The section must explain (a) LTM bypasses the "
            "compression/cache pipeline, (b) responses feed the surfacing "
            "engine, (c) memtomem crash isolation. See #298 for the "
            "rationale and suggested wording."
        )


def test_cli_md_describes_surfacing_observability_columns() -> None:
    """docs/cli.md's ``stm_surfacing_stats`` row in the observability
    tools table must mention the Phase 1 axes (skip / outcome / cache)
    in addition to the legacy event/feedback summary.

    The MCP tool keeps its name and arguments shape across v0.1.18 →
    v0.1.19, so a quick scan of the table description is the only
    surface that tells an operator "this also reports skip reasons /
    outcomes / cache hit ratio now." A description frozen at the
    pre-#256 wording is silent drift — the tool is still listed, the
    arg is still ``tool?``, but the new axes are invisible to anyone
    not reading CHANGELOG. Scope the assertion to the row that names
    `stm_surfacing_stats` to avoid false-passing on prose elsewhere
    in the file.
    """
    cli_md = _read("docs/cli.md")
    # Markdown table rows are single-line. Match the row that names
    # ``stm_surfacing_stats`` in a backtick to avoid hitting prose
    # references that happen to mention the tool.
    row_match = re.search(r"^\|\s*`stm_surfacing_stats`\s*\|.*$", cli_md, re.MULTILINE)
    if not row_match:
        pytest.fail(
            "docs/cli.md no longer has a `stm_surfacing_stats` row in "
            "the observability tools table — the table was restructured "
            "or the tool was renamed. Update this test alongside the "
            "docs change."
        )
    row = row_match.group(0).lower()
    # All three axes must be reachable from this row. Match
    # case-insensitively and accept either singular or plural forms so
    # a prose tweak ("skip reason" vs "skip reasons") doesn't false-fail.
    required_axes = ("skip", "outcome", "cache")
    missing_axes = [a for a in required_axes if a not in row]
    if missing_axes:
        pytest.fail(
            f"docs/cli.md `stm_surfacing_stats` row is missing Phase 1 "
            f"axis keyword(s): {missing_axes!r}. The row description "
            "must surface skip reasons / outcomes / cache hit ratio "
            "alongside the legacy event/feedback summary so operators "
            "scanning the table see all axes the tool reports "
            "(v0.1.19, #256)."
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
