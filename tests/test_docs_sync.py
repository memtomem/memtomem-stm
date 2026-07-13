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


def _fwd_slash(obj: object) -> object:
    """Normalize backslashes to forward slashes in every string leaf.

    ``Model().model_dump(mode="json")`` serializes ``Path`` fields with the
    host separator, so a default like ``Path("~/.memtomem/...")`` dumps to
    ``~\\.memtomem\\...`` on Windows. Docs always use forward slashes; compare
    both sides through this normalizer so the config-example pins are
    cross-platform (CI runs the suite on Windows too). No legitimate config
    string value carries an intentional backslash, so this is loss-free.
    """
    if isinstance(obj, str):
        return obj.replace("\\", "/")
    if isinstance(obj, dict):
        return {k: _fwd_slash(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fwd_slash(v) for v in obj]
    return obj


def _canonical_ci_test_filter() -> str:
    """The single CI ``test``-job ``pytest -m`` filter (the one with ``not bench_qa_meta``).

    Shared source of truth for the CLAUDE.md + README + CONTRIBUTING pins
    below: all three quickstarts must quote this verbatim, or a contributor
    runs straight into the ``bench_qa_*`` jobs CI deliberately splits into
    separate workflows.
    """
    ci = _read(".github/workflows/ci.yml")
    # Double-quoted ``pytest -m "…"`` is what the workflow uses today. If the
    # form ever changes (single quotes, ``run: |`` block, matrix variable),
    # this regex falls through to zero matches and we want a loud failure
    # pointing operators back here rather than a cryptic IndexError.
    ci_filters = re.findall(r'pytest -m "([^"]+)"', ci)
    test_job_filters = [f for f in ci_filters if "not bench_qa_meta" in f]
    if not test_job_filters:
        pytest.fail(
            "Could not locate CI's pytest filter — expected a double-quoted "
            '`pytest -m "…not bench_qa_meta…"` in .github/workflows/ci.yml. '
            "The workflow was likely refactored; update this helper and the "
            "docs that quote it (CLAUDE.md, README.md, CONTRIBUTING.md) together."
        )
    if len(test_job_filters) > 1:
        pytest.fail(
            "Multiple CI jobs now use `not bench_qa_meta` — this helper picks "
            "the first match, which may not be the one the docs should mirror. "
            f"Filters found: {test_job_filters!r}. Parse by job name or pin "
            "the canonical one explicitly."
        )
    return test_job_filters[0]


def test_contributing_pytest_command_matches_ci() -> None:
    """CONTRIBUTING.md's ``pytest -m`` filter must match the one in CI.

    The CI ``test`` job filters out ``bench_qa_meta`` (intentional-failure
    self-tests — see ``pyproject.toml`` markers table) and
    ``bench_qa_llm_judge`` (requires ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``)
    among others. A shorter filter in CONTRIBUTING leads new contributors
    straight into those expected failures.
    """
    canonical = _canonical_ci_test_filter()
    contributing_filters = re.findall(r'pytest -m "([^"]+)"', _read("CONTRIBUTING.md"))
    assert canonical in contributing_filters, (
        f"CONTRIBUTING.md must quote the CI pytest filter verbatim.\n"
        f"  CI uses: {canonical!r}\n"
        f"  CONTRIBUTING has: {contributing_filters!r}"
    )


def test_readme_pytest_command_matches_ci() -> None:
    """README.md's Development ``pytest -m`` filter must match CI's verbatim.

    README's quickstart carries the same CI filter as CONTRIBUTING, but was
    previously unpinned — so it silently drifted to a shorter filter (missing
    ``not bench_qa_drift and not bench_qa_perf``) while CONTRIBUTING.md and
    ``ci.yml`` stayed in lockstep. Pin README to the same source of truth so a
    future CI-filter change can't leave the quickstart behind again.
    """
    canonical = _canonical_ci_test_filter()
    readme_filters = re.findall(r'pytest -m "([^"]+)"', _read("README.md"))
    assert canonical in readme_filters, (
        f"README.md must quote the CI pytest filter verbatim.\n"
        f"  CI uses: {canonical!r}\n"
        f"  README has: {readme_filters!r}"
    )


def test_claude_md_pytest_command_matches_ci() -> None:
    """CLAUDE.md's Commands ``pytest -m`` filter must match CI's verbatim.

    CLAUDE.md quoted the short ``-m "not ollama"`` form until #637 — a
    no-op filter (the marker had zero usages) that also under-filtered:
    it didn't exclude ``bench_qa_meta``, whose tests fail by design. Pin
    CLAUDE.md to the same source of truth as README/CONTRIBUTING so the
    command Claude Code runs from context is always the CI command.
    """
    canonical = _canonical_ci_test_filter()
    claude_md_filters = re.findall(r'pytest -m "([^"]+)"', _read("CLAUDE.md"))
    assert canonical in claude_md_filters, (
        f"CLAUDE.md must quote the CI pytest filter verbatim.\n"
        f"  CI uses: {canonical!r}\n"
        f"  CLAUDE.md has: {claude_md_filters!r}"
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
        "selection_log",
    }
)


def test_bundled_server_proxy_manager_omits_index_engine() -> None:
    """The bundled ``mms`` server's ``ProxyManager(...)`` construction in
    ``app_lifespan`` must not pass ``index_engine=``.

    This is the Option B contract (#616): ``auto_index`` and ``extraction``
    remain reserved schema fields for custom library callers, while the
    bundled server intentionally wires no index engine. The pin is grounded
    in ``src/memtomem_stm/server.py``'s single ``ProxyManager(...)`` call,
    which passes only ``config.proxy`` and ``tracker`` positionally plus a
    fixed set of unrelated kwargs. Wiring an ``index_engine=`` here reverses
    that contract and must be coordinated with the configuration docs and
    their paired contract test.

    Paired with
    ``test_configuration_md_reserves_unsupported_indexing_blocks``.
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
            "assuming bundled indexing support was added."
        )
    call = calls[0]

    kwarg_names = {kw.arg for kw in call.keywords if kw.arg is not None}
    if "index_engine" in kwarg_names:
        pytest.fail(
            "server.py wires `index_engine=` into ProxyManager, so the bundled "
            "`mms` server now provides indexing. Update the reserved/unsupported "
            "note in `docs/configuration.md` and the paired contract test."
        )
    if len(call.args) != 2:
        pytest.fail(
            f"`ProxyManager(...)` construction in server.py has "
            f"{len(call.args)} positional args (expected 2: `config.proxy`, "
            "`tracker`). A 3rd positional would smuggle `index_engine` past "
            "the keyword check — `ProxyManager.__init__`'s 3rd parameter is "
            "`index_engine`. Update this test and the reserved/unsupported "
            "note in `docs/configuration.md` alongside that change."
        )
    extras = kwarg_names - _PROXY_MANAGER_EXPECTED_KWARGS
    if extras:
        pytest.fail(
            f"`ProxyManager(...)` construction in server.py has unexpected "
            f"kwarg(s): {sorted(extras)!r}. If you added a new unrelated "
            "kwarg, append it to `_PROXY_MANAGER_EXPECTED_KWARGS` in this "
            "test. If you wired a renamed index engine, update the Option B "
            "configuration contract and docs at the same time."
        )


def test_configuration_md_reserves_unsupported_indexing_blocks() -> None:
    """The representative JSON must not advertise unsupported indexing.

    ``auto_index`` and ``extraction`` remain schema-compatible extension
    fields, but the bundled ``mms`` server has no ``index_engine``. Keep both
    blocks out of the representative config and retain one scoped paragraph
    that points custom ``ProxyManager`` callers at the extension path.
    """
    config_md = _read("docs/configuration.md")
    section_match = re.search(
        r"##\s+Config File[^\n]*\n(.*?)(?=\n##\s|\Z)",
        config_md,
        re.DOTALL,
    )
    if not section_match:
        pytest.fail(
            "docs/configuration.md lost its `## Config File` section; update "
            "this contract alongside any documentation restructure."
        )
    section_body = section_match.group(1)
    block_match = re.search(r"```json\n(.*?)\n```", section_body, re.DOTALL)
    if not block_match:
        pytest.fail(
            "docs/configuration.md's `## Config File` section must retain a "
            "representative JSON block."
        )
    example = block_match.group(1)
    advertised = [key for key in ("auto_index", "extraction") if f'"{key}"' in example]
    if advertised:
        pytest.fail(
            "docs/configuration.md's representative JSON advertises reserved, "
            f"unsupported bundled-server block(s): {advertised!r}."
        )

    required = (
        "reserved",
        "unsupported",
        "bundled mms",
        "proxymanager",
        "index_engine",
    )
    paragraphs = re.split(r"\n\s*\n", config_md)
    matching_notes = []
    for paragraph in paragraphs:
        normalized = paragraph.lower().replace("`", "")
        if all(keyword in normalized for keyword in required):
            matching_notes.append(paragraph)
    if not matching_notes:
        pytest.fail(
            "docs/configuration.md must retain one short note containing "
            "`reserved`, `unsupported`, `bundled mms`, `ProxyManager`, and "
            "`index_engine` so the custom extension path remains discoverable."
        )


def test_surfacing_md_documents_phase_1_observability_sample() -> None:
    """docs/surfacing.md's ``stm_surfacing_stats`` example must include
    the Phase 1 ``Healthy skips`` / ``Fault skips`` / ``Outcomes`` /
    ``Cache`` sections.

    The header strings ship in ``server.py::_format_observability_sections``
    (v0.1.19 / PR #256; the skip-reason healthy/fault split is #362,
    v0.1.24). If a future operator regenerates the sample output and
    only captures the legacy event-counts block, readers of
    `docs/surfacing.md` lose the only operator-facing surface that
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
    required_headers = (
        "Healthy skips",
        "Fault skips",
        "Outcomes",
        "Cache (since process start)",
    )
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


def test_cli_md_commands_index_matches_click_group() -> None:
    """The Commands index in docs/cli.md must match the ``mms`` click group:
    the exact command-name set (both directions) and, per command, a
    description that is a verbatim prefix of the command's actual short
    help (#475 PR4).

    A command added without a docs entry (or documented after removal) is
    invisible drift — the reference reads as complete, so nobody goes
    looking for what's missing — and a stale description misleads even
    when the name survives. Prefix comparison rather than byte equality
    because click's help wrapping (and the ``...`` truncation point)
    shifts with the rendering width, which is not deterministic across
    environments; the words themselves are.
    """
    from memtomem_stm.cli.proxy import cli as mms_cli

    cli_md = _read("docs/cli.md")
    blocks = re.findall(
        r"```\n(Usage: mms \[OPTIONS\] \[COMMAND\] \[ARGS\]\.\.\..*?)```",
        cli_md,
        re.DOTALL,
    )
    if len(blocks) != 1:
        pytest.fail(
            "Expected exactly one fenced `Usage: mms [OPTIONS] [COMMAND] [ARGS]...` block "
            f"in docs/cli.md (the top-level help), found {len(blocks)} — the "
            "reference was restructured; update this test alongside it."
        )
    commands_match = re.search(r"Commands:\n(.*)\Z", blocks[0], re.DOTALL)
    assert commands_match, "The top-level help block lost its `Commands:` listing"
    documented = dict(re.findall(r"^ {2}(\S+) +(.+?)\s*$", commands_match.group(1), re.MULTILINE))
    registered = mms_cli.commands
    assert set(documented) == set(registered), (
        "docs/cli.md Commands index is out of sync with the `mms` click "
        f"group.\n  missing from docs: {sorted(set(registered) - set(documented))}\n"
        f"  documented but not registered: {sorted(set(documented) - set(registered))}\n"
        "Regenerate the block from `mms --help`."
    )
    for name, desc in documented.items():
        prefix = desc[:-3].rstrip() if desc.endswith("...") else desc
        actual = registered[name].get_short_help_str(limit=500)
        assert actual.startswith(prefix), (
            f"docs/cli.md Commands index line for `{name}` no longer matches "
            f"its short help.\n  documented: {desc!r}\n  actual:     {actual!r}\n"
            "Regenerate the block from `mms --help`."
        )


def test_cli_md_eject_usage_block_flags_match_command() -> None:
    """docs/cli.md's ``### `eject` `` usage block must list exactly the
    options ``mms eject`` exposes — both directions — plus the prune
    backup log the section points at (#475 PR4).

    ``mms eject`` is the data-loss-sensitive end of the #475 round-trip:
    its guards (``--accept-schema-loss``, ``--allow-argv-secrets``,
    ``--force``) exist to make destructive variants explicit. A flag added
    to the command but absent from the docs leaves users discovering
    safety semantics from error messages; a documented flag the command
    dropped is worse — copy-pasted commands fail.
    """
    from memtomem_stm.cli.proxy import cli as mms_cli

    eject_cmd = mms_cli.commands.get("eject")
    assert eject_cmd is not None, "`mms eject` is gone — update docs/cli.md and this test"

    cli_md = _read("docs/cli.md")
    section_match = re.search(r"### `eject`\n(.*?)(?=\n### |\n## |\Z)", cli_md, re.DOTALL)
    assert section_match, "docs/cli.md must have a ### `eject` section"
    section = section_match.group(1)
    block_match = re.search(r"```\n(Usage: mms eject .*?)```", section, re.DOTALL)
    assert block_match, "the eject section lost its fenced usage block"

    # Option lines sit at exactly two spaces of indent (`  --to TARGET`,
    # `  -y, --yes`); wrapped continuation lines are indented deeper, so
    # flag mentions inside option help text don't count as documented.
    documented = set(re.findall(r"^ {2}(?:-\w, )?(--[\w-]+)", block_match.group(1), re.MULTILINE))
    registered = {
        opt
        for param in eject_cmd.params
        for opt in getattr(param, "opts", [])
        if opt.startswith("--")
    }
    assert registered, "eject lost all its options? update this test"
    assert documented == registered, (
        "docs/cli.md eject usage block is out of sync with `mms eject`.\n"
        f"  missing from docs: {sorted(registered - documented)}\n"
        f"  documented but not on the command: {sorted(documented - registered)}\n"
        "Regenerate the block from `mms eject --help` (drop the `-h, --help` "
        "line, matching the other sections)."
    )

    # The no-origin fallback story leans on the prune backup log; keep the
    # filename in the section so the suggestion in eject's error output has
    # a documented home. The literal is pinned against the source so a
    # rename fails here instead of silently splitting the two.
    backup_name = "pruned_upstreams.json"
    assert backup_name in _read("src/memtomem_stm/cli/proxy.py")
    assert backup_name in section, (
        "docs/cli.md eject section must mention the prune backup log "
        f"({backup_name}) — eject suggests it for entries without an origin."
    )


def test_user_facing_surfaces_carry_no_private_docs_paths() -> None:
    """README, CHANGELOG, docs/, and src/ must not reference the private
    docs repo by path.

    A path like ``memtomem-docs/<...>.md`` points readers at a file they
    cannot access (the repo is private) — docs/caching.md did exactly
    that for auto-index wiring instructions until 2026-06-11, and an RFC
    path sat in ``mms/__init__``'s module docstring. Bare repo-name
    mentions explicitly marked private (CONTRIBUTING, notebooks/README,
    CLAUDE.md) are deliberate and stay allowed — the pin matches the
    path form only (``memtomem-docs/``, with separator) and only on the
    user-facing surfaces.

    Scope = **git-tracked** files: that is exactly the published repo.
    Untracked local artifacts (the ``docs/reports/`` review-report
    convention) are excluded by construction rather than by a hardcoded
    subtree carve-out, while nested *committed* docs under any future
    ``docs/<subdir>/`` are covered — git pathspec ``*`` crosses ``/``.
    """
    import subprocess

    listed = subprocess.run(
        ["git", "ls-files", "--", "README.md", "CHANGELOG.md", "docs/*.md", "src/*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert len(listed) > 10, f"git ls-files returned suspiciously few files: {listed!r}"
    offenders = []
    for relative in listed:
        path = REPO_ROOT / relative
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "memtomem-docs/" in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        "Private docs-repo path reference(s) in user-facing files: "
        f"{offenders!r}. Replace with public guidance (code paths, "
        "protocol references, public docs links) — readers cannot open "
        "memtomem-docs files."
    )


def test_deprecation_policy_and_upgrade_notes_convention_exist() -> None:
    """The #614 compatibility contract spans three files that reference each
    other by heading: README's policy section (the anchor CHANGELOG and
    CONTRIBUTING link to), CONTRIBUTING's changelog-writing guidance, and
    the CHANGELOG header prose that tells readers where to look. Renaming
    or dropping any one of them silently orphans the other two."""
    readme = _read("README.md")
    contributing = _read("CONTRIBUTING.md")
    changelog = _read("CHANGELOG.md")
    # The convention prose lives ABOVE the first release heading; slicing
    # there means the assertions below cannot be satisfied by the Unreleased
    # block's own `### Upgrade notes` heading surviving a header deletion.
    changelog_header = changelog.split("## [Unreleased]", 1)[0]
    policy_anchor = "README.md#compatibility--deprecation-policy"

    assert "## Compatibility & deprecation policy" in readme, (
        "README lost the '## Compatibility & deprecation policy' section — "
        "CHANGELOG.md and CONTRIBUTING.md link to its anchor."
    )
    assert "## Changelog and upgrade notes" in contributing, (
        "CONTRIBUTING lost the '## Changelog and upgrade notes' section that "
        "defines when a release block needs an '### Upgrade notes' summary."
    )
    # Both referrers must carry the exact anchor of the README section —
    # renaming the heading changes the GitHub anchor and orphans these links.
    for name, text in (("CHANGELOG.md", changelog_header), ("CONTRIBUTING.md", contributing)):
        assert policy_anchor in text, (
            f"{name} no longer links to '{policy_anchor}' — the README policy "
            "heading and its referrers must move together."
        )
    # The inline marker string is the convention's join key: CONTRIBUTING
    # tells writers to use it and the README tells readers to look for it.
    for name, text in (
        ("README.md", readme),
        ("CONTRIBUTING.md", contributing),
        ("CHANGELOG.md header", changelog_header),
    ):
        assert "**Behavior change**:" in text, (
            f"{name} no longer names the '**Behavior change**:' marker the "
            "upgrade-notes convention aggregates."
        )
    assert "Upgrade notes" in changelog_header, (
        "CHANGELOG.md's header prose (before the first release heading) should "
        "tell readers releases with behavior changes open with an 'Upgrade "
        "notes' block."
    )


def test_compression_md_llm_section_documents_privacy_scan() -> None:
    """docs/compression.md's ``## LLM Compression`` section must surface
    ``privacy_scan_enabled``.

    ``LLMCompressorConfig.privacy_scan_enabled``
    (``src/memtomem_stm/proxy/config.py``, default ``True``) is the credential
    scan that keeps API keys / passwords / JWTs out of the outbound LLM call
    (#289); flipping it off sends raw upstream responses to the provider
    unscanned, and ``ProxyManager.start()`` logs a startup WARNING for that
    state on external destinations (#610). Undocumented, the knob and its
    warning are invisible to anyone not reading the source.
    """
    from memtomem_stm.proxy.config import LLMCompressorConfig

    assert "privacy_scan_enabled" in LLMCompressorConfig.model_fields, (
        "LLMCompressorConfig lost `privacy_scan_enabled` — the knob was "
        "renamed or removed; update docs/compression.md and this test together."
    )
    # The docstring/prose claim the scan is default-on; pin that against source
    # so a flipped default can't leave the docs silently wrong. Read the field
    # default directly — instantiating LLMCompressorConfig() validates api_key.
    assert LLMCompressorConfig.model_fields["privacy_scan_enabled"].default is True, (
        "LLMCompressorConfig.privacy_scan_enabled default is no longer True — "
        "docs/compression.md says the credential scan is default-on; update "
        "both together."
    )
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
    if "privacy_scan_enabled" not in section_body:
        pytest.fail(
            "docs/compression.md `## LLM Compression` section must mention "
            "`privacy_scan_enabled` — in the JSON example or the credential "
            "scan prose. Without it, the default-on scan (#289) and the "
            "scan-disabled startup warning (#610) are undiscoverable."
        )
    # The token appearing only in the JSON example would leave the #610
    # scan-disabled behavior undocumented; require the warning prose too so the
    # operator-facing consequence can't be silently dropped.
    lowered = section_body.lower()
    if "warning" not in lowered or "unscanned" not in lowered:
        pytest.fail(
            "docs/compression.md `## LLM Compression` section must describe the "
            "scan-disabled consequence — the startup WARNING and that raw "
            "responses go to the provider UNSCANNED (#610). Found the "
            "`privacy_scan_enabled` token but not the warning prose."
        )


def test_configuration_full_example_documents_supported_config_blocks() -> None:
    """docs/configuration.md's representative proxy example must carry the
    supported ``cache`` and ``toolgraph`` blocks, with keys and default values
    matching ``CacheConfig`` / ``ToolgraphConfig`` exactly, plus the two
    top-level knobs ``default_compression`` / ``max_upstream_chars`` at their
    ProxyConfig defaults.

    Comparing each documented block to ``Model().model_dump(mode="json")``
    (rather than just a key-subset check) pins the example to the real defaults:
    a renamed key, a stray/typo'd key, or a drifted default all fail loudly.
    Scoped to the ``## Config File`` section so moving a block into prose cannot
    satisfy the check. Reserved ``auto_index`` / ``extraction`` blocks are
    intentionally covered by the separate unsupported-indexing contract.

    ``config_path`` is intentionally excluded: it is a runtime-populated field
    (the path the config was loaded from), not a user-authored config key.
    """
    import json

    from memtomem_stm.proxy.config import (
        CacheConfig,
        ProxyConfig,
        ToolgraphConfig,
    )

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
    block_match = re.search(r"```json\n(.*?)\n```", section_match.group(1), re.DOTALL)
    if not block_match:
        pytest.fail(
            "docs/configuration.md `## Config File` section lost its ```json "
            "fenced example — restore the full-example block or update the test."
        )
    example = json.loads(block_match.group(1))

    proxy_defaults = ProxyConfig().model_dump(mode="json")
    for top_level in ("default_compression", "max_upstream_chars"):
        if top_level not in example:
            pytest.fail(
                f"docs/configuration.md full-example omits top-level "
                f"`{top_level}` — it exists on ProxyConfig "
                "(src/memtomem_stm/proxy/config.py). Keep it visible in the "
                "example so operators discover it without reading CHANGELOG."
            )
        if example[top_level] != proxy_defaults[top_level]:
            pytest.fail(
                f"docs/configuration.md full-example shows `{top_level}: "
                f"{example[top_level]!r}` but the ProxyConfig default is "
                f"{proxy_defaults[top_level]!r} — the 'all options' example "
                "should show real defaults."
            )

    expected_blocks = {
        # ``cache`` pins ``tool_annotation_policy`` into the example at its
        # SCHEMA default (conservative) — new configs are written with an
        # explicit "strict", but the full example documents what a key-less
        # file resolves to.
        "cache": CacheConfig().model_dump(mode="json"),
        "toolgraph": ToolgraphConfig().model_dump(mode="json"),
    }
    for name, defaults in expected_blocks.items():
        if name not in example:
            pytest.fail(
                f"docs/configuration.md full-example omits the `{name}` block — "
                f"add it mirroring {name.capitalize()}Config's defaults. The "
                "'all options' claim (and this test) require every ProxyConfig "
                "sub-block to appear."
            )
        if _fwd_slash(example[name]) != _fwd_slash(defaults):
            pytest.fail(
                f"docs/configuration.md `{name}` example does not match "
                f"{name.capitalize()}Config defaults.\n"
                f"  documented: {example[name]!r}\n"
                f"  defaults:   {defaults!r}\n"
                "Keep the example in sync with src/memtomem_stm/proxy/config.py."
            )


def test_cli_md_stats_documents_source_filter() -> None:
    """docs/cli.md's ``### `stats` `` section must document the ``--source``
    provenance filter.

    ``mms stats`` grew a ``--source [mcp|hook]`` option (#512) that filters
    compression rows by provenance — ``mcp`` (proxied upstream tools) vs
    ``hook`` (native built-in tools recorded by ``mms hook``). It shipped
    undocumented; without it, an operator comparing hook vs proxied compression
    has no surface telling them the split exists. Anchored to the live click
    option so a rename fails here, and scoped to the ``### `stats` `` section.
    """
    from memtomem_stm.cli.proxy import cli as mms_cli

    stats_cmd = mms_cli.commands.get("stats")
    assert stats_cmd is not None, "`mms stats` is gone — update docs/cli.md and this test"
    source_param = next(
        (p for p in stats_cmd.params if "--source" in getattr(p, "opts", [])),
        None,
    )
    if source_param is None:
        pytest.fail(
            "`mms stats` no longer exposes a `--source` option — it was "
            "renamed or removed; update docs/cli.md and this test together."
        )
    choices = set(getattr(source_param.type, "choices", ()))
    assert choices == {"mcp", "hook"}, (
        f"`mms stats --source` choices changed to {sorted(choices)!r} — "
        "update the docs and this test."
    )

    cli_md = _read("docs/cli.md")
    section_match = re.search(r"### `stats`\n(.*?)(?=\n### |\n## |\Z)", cli_md, re.DOTALL)
    if not section_match:
        pytest.fail("docs/cli.md must have a ### `stats` section")
    section = section_match.group(1)
    for token in ("--source", "mcp", "hook"):
        if token not in section:
            pytest.fail(
                f"docs/cli.md `stats` section must mention {token!r} — the "
                "`--source` provenance filter (mcp = proxied upstream, hook = "
                "native built-in tools recorded by `mms hook`) is otherwise "
                "undiscoverable (#512)."
            )
    # A bare token list could pass while mapping the two provenances backwards;
    # pin the semantics (mcp → proxied upstream, hook → native built-in) so the
    # prose can't invert without failing.
    lowered = section.lower()
    if "proxied" not in lowered or "native" not in lowered:
        pytest.fail(
            "docs/cli.md `stats` section must map the `--source` values: `mcp` "
            "to proxied upstream tools and `hook` to native built-in tools. "
            "Found the flag tokens but not the provenance mapping prose."
        )


def test_public_docs_pin_current_hook_paths_and_runtime_host_contract() -> None:
    """The hook guide, CLI reference, and live help must agree with host specs."""
    from click.testing import CliRunner

    from memtomem_stm.cli.hook_cmd import hook_command
    from memtomem_stm.cli.hook_hosts import HOOK_HOSTS

    expected = HOOK_HOSTS["kimi"].config_path.as_posix()
    assert expected == "~/.kimi-code/config.toml"
    for relative in (
        "docs/cli.md",
        "docs/guides/native-hooks.md",
        "docs/reference/cli-hooks.md",
    ):
        body = _read(relative)
        assert expected in body, f"{relative} lost the current Kimi config path"
        assert "~/.kimi/config.toml" not in body, f"{relative} restored the legacy Kimi path"
        assert "KIMI_CODE_HOME" in body, f"{relative} must document Kimi's home override"

    result = CliRunner().invoke(hook_command, ["install", "--help"], terminal_width=200)
    assert result.exit_code == 0
    assert expected in result.output
    assert "KIMI_CODE_HOME" in result.output
    assert "~/.kimi/config.toml" not in result.output


def test_new_reference_docs_pin_high_risk_public_fields() -> None:
    """Keep the settings omitted before the v0.1.36 full-doc audit visible."""
    env_ref = _read("docs/reference/environment-variables.md")
    for token in (
        "MEMTOMEM_STM_DATA_DIR",
        "MEMTOMEM_STM_PROXY__ENABLED",
        "MEMTOMEM_STM_PROXY__CONFIG_PATH",
        "MEMTOMEM_STM_HOOK__METRICS_ENABLED",
        "MEMTOMEM_STM_SURFACING__USE_DAEMON",
        "AUTO_TUNE_SCORE_FLOOR",
        "AUTO_TUNE_SCORE_CEILING",
        "CONTEXT_TOOLS",
    ):
        assert token in env_ref, f"environment reference lost {token}"
    assert "MEMTOMEM_STM_ENABLED" not in env_ref
    assert "MEMTOMEM_STM_CONFIG_PATH" not in env_ref

    proxy_ref = _read("docs/reference/proxy-config.md")
    for token in ("json_depth", "min_section_chars", "description_override"):
        assert token in proxy_ref, f"proxy reference lost {token}"

    config_hub = _read("docs/configuration.md")
    for phrase in ("ProxyConfig", "environment/default-only", "Representative configuration"):
        assert phrase in config_hub, f"configuration source boundary lost {phrase!r}"


def test_public_tool_counts_match_runtime_registration_sets() -> None:
    from memtomem_stm.server import _OBSERVABILITY_TOOL_NAMES

    operations = _read("docs/guides/operations.md")
    assert "eight observability and admin tools" in operations
    assert "nine operator tools" not in operations
    assert "ten operator tools" not in operations

    mcp_ref = _read("docs/reference/mcp-tools.md")
    for name in _OBSERVABILITY_TOOL_NAMES:
        assert f"`{name}`" in mcp_ref
    assert len(_OBSERVABILITY_TOOL_NAMES) == 8
    assert "four model-facing tools" in mcp_ref
    assert "`stm_memory_propose`" in mcp_ref


def test_mcp_tool_reference_matches_optional_argument_signatures() -> None:
    """Pin the optional arguments that were previously documented as required/missing."""
    import inspect

    from memtomem_stm.server import stm_proxy_read_more, stm_surfacing_stats

    read_more = inspect.signature(stm_proxy_read_more)
    assert read_more.parameters["offset"].default == 0
    surfacing = inspect.signature(stm_surfacing_stats)
    assert surfacing.parameters["since"].default is None
    assert surfacing.parameters["limit"].default == 10

    mcp_ref = _read("docs/reference/mcp-tools.md")
    for token in ("offset?=0", "since?", "limit=10"):
        assert token in mcp_ref, f"MCP tool reference lost {token}"


def test_readme_and_compatibility_hubs_link_to_split_docs() -> None:
    """The journey/reference split remains discoverable from stable entrypoints."""
    readme = _read("README.md")
    assert "docs/getting-started.md" in readme
    assert "docs/guides/operations.md" in readme

    cli_hub = _read("docs/cli.md")
    for target in (
        "reference/cli-gateway.md",
        "reference/cli-hooks.md",
        "reference/cli-projects.md",
        "reference/mcp-tools.md",
    ):
        assert target in cli_hub

    config_hub = _read("docs/configuration.md")
    for target in (
        "reference/environment-variables.md",
        "reference/proxy-config.md",
    ):
        assert target in config_hub


def test_reviewed_memory_resume_guide_matches_core_contract_smoke() -> None:
    """Keep the copy/paste core CLI flow tied to the released-core advisory."""
    guide = _read("docs/guides/reviewed-memory-resume.md")
    for snippet in (
        "mm init --non-interactive --preset minimal --namespace resume-demo --mcp skip",
        "mm mem init",
        "mm pinned set resume-contract",
        "--scope project_local",
        '--description "Reviewed project resume contract"',
        "--priority 10",
        "mm pinned list --json",
        'mm pinned compose "blue-green rollback checklist"',
        "mm index .memtomem/memories.local/resume-demo.md --namespace resume-demo --force",
        "mm review list",
        "mm review show CANDIDATE_ID",
        'mm review approve CANDIDATE_ID --reviewer "$USER"',
    ):
        assert snippet in guide, f"reviewed-memory-resume guide lost {snippet!r}"

    smoke = _read("scripts/core_compat_smoke.py")
    for token in (
        "_init_schema_three_guide",
        '"resume-contract"',
        '"project_local"',
        '"resume-demo"',
        '"pinned", "list", "--json"',
        '"review", "list"',
        '"review", "show"',
        '"review",\n            "approve"',
        '"compat-smoke"',
    ):
        assert token in smoke, f"released-core advisory lost guide contract token {token!r}"
