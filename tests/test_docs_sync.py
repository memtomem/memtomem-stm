"""Cross-file invariants between docs and the source of truth.

Pins drift between ``CONTRIBUTING.md`` / ``docs/`` and the things they
quote (``.github/workflows/ci.yml``, ``src/memtomem_stm/cli/proxy.py``).
A contributor updating one side without the other is otherwise invisible
in local testing and only surfaces through user confusion.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import re
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import Union, get_args, get_origin

import pytest
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _json_fences(relative: str) -> list[dict[str, object]]:
    """Parse every JSON fence in a public Markdown file."""
    blocks = re.findall(r"```json\n(.*?)\n```", _read(relative), re.DOTALL)
    return [json.loads(block) for block in blocks]


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


def _load_core_compat_smoke() -> object:
    """Import the tracked advisory script by path (``scripts/`` is not a package)."""
    path = REPO_ROOT / "scripts" / "core_compat_smoke.py"
    spec = importlib.util.spec_from_file_location("core_compat_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stmconfig_environment_defaults(
    model: type[BaseModel], prefix: tuple[str, ...] = ()
) -> dict[str, object]:
    """Flatten nested models using STM's settings delimiter.

    ``SubModel | None`` is traversed exactly like a bare ``SubModel``, because
    pydantic-settings builds the optional block from its ``__``-suffixed
    variables too. Collapsing such a field into one leaf would let the
    completeness assertion below stay green with a whole block undocumented, so
    ``SubModel | None`` is the *only* union this walker accepts: any other arm
    alongside a model (a second model, or a scalar the walker would silently
    ignore) is a hard failure rather than a silent leaf.
    """
    result: dict[str, object] = {}
    for name, field in model.model_fields.items():
        path = (*prefix, name)
        annotation = field.annotation
        nested: type[BaseModel] | None = None
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            nested = annotation
        elif get_origin(annotation) in (Union, UnionType):
            args = get_args(annotation)
            models = [arg for arg in args if isinstance(arg, type) and issubclass(arg, BaseModel)]
            others = [arg for arg in args if arg not in models and arg is not type(None)]
            if models and (len(models) > 1 or others):
                raise AssertionError(
                    f"{'.'.join(path)} unions a nested model with {len(models) - 1} other "
                    f"model(s) and {len(others)} non-None arm(s); teach this walker which "
                    "shape pydantic-settings builds before the environment reference can "
                    "claim completeness"
                )
            nested = models[0] if models else None
        if nested is not None:
            result.update(_stmconfig_environment_defaults(nested, path))
            continue
        suffix = "__".join(part.upper() for part in path)
        result[f"MEMTOMEM_STM_{suffix}"] = field.get_default(call_default_factory=True)
    return result


def _documented_default(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return value if value else "empty"
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"), default=str)
    return str(value)


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


def test_cli_docs_track_platform_aware_desktop_discovery() -> None:
    """Claude Desktop discovery and CLI docs must cover all native platforms."""
    helper_src = _read("src/memtomem_stm/mms/import_hosts.py")

    func_match = re.search(
        r"def _desktop_config_path\b.*?(?=\ndef |\nclass |\Z)",
        helper_src,
        re.DOTALL,
    )
    assert func_match, "_desktop_config_path helper not found — update this test"
    desktop_func = func_match.group(0)
    assert "Library/Application Support/Claude" in desktop_func
    assert "APPDATA" in desktop_func
    assert ".config/Claude" in desktop_func

    cli_md = _read("docs/cli.md")
    for path in (
        "Library/Application Support/Claude",
        "%APPDATA%\\Claude",
        "~/.config/Claude",
    ):
        assert path in cli_md
    assert "macOS-only" not in cli_md


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

    assert _read("docs/caching.md").splitlines()[0] == "# Response Caching"


def test_public_markdown_json_fences_are_valid() -> None:
    """Every public JSON fence must remain copy/paste parseable."""
    public_docs = [REPO_ROOT / "README.md", *(REPO_ROOT / "docs").rglob("*.md")]
    for path in public_docs:
        if "reports" in path.relative_to(REPO_ROOT).parts:
            continue
        body = path.read_text(encoding="utf-8")
        for number, block in enumerate(
            re.findall(r"```json\n(.*?)\n```", body, re.DOTALL), start=1
        ):
            try:
                json.loads(block)
            except json.JSONDecodeError as exc:
                pytest.fail(f"{path.relative_to(REPO_ROOT)} JSON fence {number}: {exc}")


def test_public_proxy_config_examples_match_runtime_schema() -> None:
    """Representative config examples use real nesting and known fields."""
    from memtomem_stm.proxy.config import ProxyConfig, find_unknown_keys

    proxy_reference = _json_fences("docs/reference/proxy-config.md")[0]
    assert find_unknown_keys(ProxyConfig, proxy_reference) == []
    parsed = ProxyConfig.model_validate(proxy_reference)
    assert parsed.cache.tool_annotation_policy == "strict"
    assert parsed.upstream_servers["filesystem"].selective.max_pending == 100

    for relative in ("docs/configuration.md", "docs/compression.md"):
        examples = _json_fences(relative)
        response_payloads = [
            example
            for example in examples
            if example.get("type") == "toc"
            and {"selection_key", "entries", "hint"} <= example.keys()
        ]
        expected_payloads = 1 if relative == "docs/compression.md" else 0
        assert len(response_payloads) == expected_payloads, relative

        for example in examples:
            if example in response_payloads:
                continue
            assert find_unknown_keys(ProxyConfig, example) == [], relative
            ProxyConfig.model_validate(example)


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

    guide = _read("docs/guides/vibe-coding-getting-started-ko.md")
    native_hooks = _read("docs/guides/native-hooks.md")
    cli = _read("docs/cli.md")
    cli_reference = _read("docs/reference/cli-hooks.md")
    for body in (guide, native_hooks, cli, cli_reference):
        assert "2.1.121" in body, "Claude output replacement lost its minimum version"
        assert "additionalContext" in body, "Codex model-visible hook context disappeared"
    for body in (guide, native_hooks, cli_reference):
        assert "--bare" in body
        assert "--safe-mode" in body
    assert "not explicitly guaranteed" not in native_hooks
    assert "명확히 보장되지" not in guide
    assert "updatedMCPToolOutput" in native_hooks


def test_vibe_coding_guide_pins_client_registration_and_hook_boundaries() -> None:
    """Keep the Korean first-user journey aligned with live host contracts."""
    from click import Choice

    from memtomem_stm.cli.hook_adapter import CodexHookAdapter
    from memtomem_stm.cli.proxy import cli as proxy_cli

    guide = _read("docs/guides/vibe-coding-getting-started-ko.md")
    getting_started = _read("docs/getting-started.md")
    native_hooks = _read("docs/guides/native-hooks.md")

    registrations = ("mms register --client claude", "mms register --client codex")
    for body in (guide, getting_started):
        for command in registrations:
            assert command in body
        assert "mms import --from codex" in body
        assert "~/.mms/registry.toml" in body
        assert "~/.memtomem/stm_proxy.json" in body

    register = proxy_cli.commands["register"]
    mcp_mode = next(param for param in register.params if param.name == "mcp_mode")
    assert isinstance(mcp_mode.type, Choice)
    assert set(mcp_mode.type.choices) == {"claude", "json", "skip"}
    assert "mms register --mcp codex" not in guide

    assert CodexHookAdapter.native_tool_map == {"Bash": "shell", "apply_patch": "edit"}
    assert CodexHookAdapter.can_replace_output is False
    for body in (guide, native_hooks):
        assert "/hooks" in body
        assert "Bash" in body
        for host in ("claude", "codex"):
            preview = f"mms hook install --host {host}"
            assert f"\n{preview}\n" in body
            assert f"\n{preview} --apply\n" in body


def test_public_docs_pin_provider_memory_ownership_and_ingest_boundaries() -> None:
    """Provider-native memory must not be conflated with Core or STM state."""
    surfacing = _read("docs/surfacing.md")
    for token in (
        "~/.claude/projects/<project>/memory/",
        "autoMemoryDirectory",
        "[features] memories = true",
        "/memories",
        "$CODEX_HOME/memories/",
        "mm ingest claude-memory",
        "mm ingest codex-memory",
        "mms import --from codex",
        "AGENTS.md",
    ):
        assert token in surfacing, f"memory-layer guide lost {token!r}"

    readme = _read("README.md")
    getting_started = _read("docs/getting-started.md")
    guide = _read("docs/guides/vibe-coding-getting-started-ko.md")
    assert "docs/surfacing.md#which-memory-layer-does-what" in readme
    assert "surfacing.md#which-memory-layer-does-what" in getting_started
    assert "surfacing.md#which-memory-layer-does-what" in guide
    assert "~/.codex/memories/" in guide
    assert "does not automatically index or synchronize" in readme
    assert "자동 색인되지는 않으며" in guide

    operations = _read("docs/guides/operations.md")
    assert "client-managed memory" in operations


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
    assert "| `MEMTOMEM_STM_PROXY__ENABLED` | boolean | `false` |" in env_ref
    assert "daemon handshakes" in env_ref.lower()
    assert "default state paths" not in env_ref

    from memtomem_stm.proxy.config import ProxyConfig

    assert ProxyConfig().enabled is False

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

    from memtomem_stm.server import (
        stm_compression_feedback,
        stm_memory_propose,
        stm_proxy_read_more,
        stm_surfacing_feedback,
        stm_surfacing_stats,
    )

    read_more = inspect.signature(stm_proxy_read_more)
    assert read_more.parameters["offset"].default == 0
    surfacing = inspect.signature(stm_surfacing_stats)
    assert surfacing.parameters["since"].default is None
    assert surfacing.parameters["limit"].default == 10

    feedback = inspect.signature(stm_surfacing_feedback)
    assert feedback.parameters["rating"].default is None
    assert feedback.parameters["memory_id"].default is None
    assert feedback.parameters["ratings"].default is None

    compression = inspect.signature(stm_compression_feedback)
    assert compression.parameters["kind"].default == "other"
    assert compression.parameters["trace_id"].default is None

    formation = inspect.signature(stm_memory_propose)
    assert formation.parameters["source_ref"].default == ""
    assert formation.parameters["idempotency_key"].default == ""

    mcp_ref = _read("docs/reference/mcp-tools.md")
    for token in (
        "offset?=0",
        "since?",
        "limit=10",
        "rating?",
        "memory_id?",
        "ratings?",
        'kind="other"',
        "trace_id?",
        'source_ref=""',
        'idempotency_key=""',
    ):
        assert token in mcp_ref, f"MCP tool reference lost {token}"


def test_surfacing_docs_pin_current_core_and_library_boundaries() -> None:
    readme = _read("README.md")
    surfacing = _read("docs/surfacing.md")
    for phrase in (
        "context_compose schema 2+",
        "legacy `mem_search`",
        "dependency fault",
        "review-first proposals",
        "first release to carry schema 3",
        "ProxyManager(index_engine=...)",
    ):
        assert phrase in surfacing, f"surfacing guide lost {phrase!r}"
    for body in (readme, surfacing):
        assert "Core 0.3.12" in body
        assert "Core 0.3.13" in body
        assert "schema 4" in body
        assert "planned first release" not in body
    assert "planned for Core 0.3.12" not in surfacing
    assert "Cores newer than v0.3.11" not in surfacing
    assert "expected to carry schema 3" not in surfacing
    assert '"surfacing": {' not in surfacing


def test_first_success_guides_use_real_separate_upstreams() -> None:
    """The copyable happy paths must not promise filesystem tools from demo."""
    for relative in (
        "README.md",
        "docs/getting-started.md",
        "docs/guides/vibe-coding-getting-started-ko.md",
    ):
        body = _read(relative)
        assert "demo__demo_search" in body
        assert "mms add filesystem" in body
        assert "fs__read_file" in body
        assert "mcp__memtomem-stm__" in body


def test_cli_docs_track_live_init_doctor_and_auto_hook_contracts() -> None:
    """Pin safety-sensitive help behavior rather than a stale option snapshot."""
    from click.testing import CliRunner

    from memtomem_stm.cli.hook_cmd import hook_command
    from memtomem_stm.cli.proxy import cli as mms_cli

    init_help = CliRunner().invoke(mms_cli, ["init", "--help"], terminal_width=200)
    assert init_help.exit_code == 0
    assert "--resume" in init_help.output
    assert "aborts when the config already exists" in init_help.output

    doctor = mms_cli.commands["doctor"]
    live_doctor_options = {
        option
        for param in doctor.params
        for option in getattr(param, "opts", ())
        if option.startswith("--")
    }
    doctor_section = re.search(
        r"### `doctor`\n(.*?)(?=\n### |\n## |\Z)",
        _read("docs/cli.md"),
        re.DOTALL,
    )
    assert doctor_section is not None
    for option in live_doctor_options:
        assert option in doctor_section.group(1)
    assert "default doctor run is passive" in doctor_section.group(1)

    hook_help = CliRunner().invoke(hook_command, ["--help"], terminal_width=200)
    assert hook_help.exit_code == 0
    for body in (
        hook_help.output,
        _read("docs/cli.md"),
        _read("docs/guides/native-hooks.md"),
    ):
        assert "turn_id" in body
        assert "Claude" in body


def test_model_aware_ceiling_docs_match_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from memtomem_stm.proxy.config import MODEL_CONTEXT_WINDOWS, ProxyConfig
    from memtomem_stm.surfacing.config import SurfacingConfig

    small_model = "docs-test-small-model"
    monkeypatch.setitem(MODEL_CONTEXT_WINDOWS, small_model, 32_000)
    surfacing = SurfacingConfig(
        consumer_model=small_model,
        max_injection_chars=4_000,
        max_results=5,
    )
    assert surfacing.effective_max_injection_chars() == 1_500
    assert surfacing.effective_max_results() == 2
    assert (
        SurfacingConfig(
            consumer_model="unknown-model",
            max_injection_chars=4_000,
            max_results=5,
        ).effective_max_injection_chars()
        == 4_000
    )

    proxy = ProxyConfig(
        consumer_model=small_model,
        default_max_result_chars=99_999,
    )
    calculated = int(
        MODEL_CONTEXT_WINDOWS[small_model] * proxy.context_budget_ratio * proxy.chars_per_token
    )
    assert proxy.effective_max_result_chars() == min(calculated, 99_999)

    compression = _read("docs/compression.md")
    for phrase in (
        "Model-Aware Ceilings",
        "min(configured value, 1500)",
        "min(configured value, 2)",
        "does not choose a strategy or context window",
    ):
        assert phrase in compression


def test_stmconfig_env_walker_refuses_a_shape_it_would_undercount() -> None:
    """The completeness walker must fail loudly, not silently lose a block."""

    class _Nested(BaseModel):
        alpha: int = 1
        beta: str = "b"

    class _Direct(BaseModel):
        nested: _Nested = _Nested()

    assert _stmconfig_environment_defaults(_Direct) == {
        "MEMTOMEM_STM_NESTED__ALPHA": 1,
        "MEMTOMEM_STM_NESTED__BETA": "b",
    }

    class _Optional(BaseModel):
        nested: _Nested | None = None

    # pydantic-settings builds an optional block from the same suffixed
    # variables, so the walker must not collapse it into one leaf.
    assert _stmconfig_environment_defaults(_Optional) == {
        "MEMTOMEM_STM_NESTED__ALPHA": 1,
        "MEMTOMEM_STM_NESTED__BETA": "b",
    }

    class _Other(BaseModel):
        gamma: int = 2

    class _Ambiguous(BaseModel):
        nested: _Nested | _Other = _Nested()

    with pytest.raises(AssertionError, match=r"1 other model\(s\) and 0 non-None"):
        _stmconfig_environment_defaults(_Ambiguous)

    # A scalar arm is just as ambiguous: traversing would silently drop it.
    class _ModelOrScalar(BaseModel):
        nested: _Nested | str = "s"

    with pytest.raises(AssertionError, match=r"0 other model\(s\) and 1 non-None"):
        _stmconfig_environment_defaults(_ModelOrScalar)

    # A union with no model at all stays an ordinary leaf.
    class _ScalarUnion(BaseModel):
        value: int | None = None

    assert _stmconfig_environment_defaults(_ScalarUnion) == {"MEMTOMEM_STM_VALUE": None}


def test_environment_reference_warns_that_extractor_llm_defaults_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tabulated field default is not the extractor's absent-block behavior.

    ``ExtractionConfig.llm`` defaults to ``None`` and ``effective_llm()`` then
    substitutes a different profile, so every ``EXTRACTION__LLM__*`` row in the
    table describes a config the extractor only uses once someone materializes
    the block. Drive that materialization through the *environment*, the way a
    reader of this table would, and take the prompt failure from the production
    call rather than restating its keyword here.
    """
    import pydantic

    from memtomem_stm.config import STMConfig
    from memtomem_stm.proxy.config import ExtractionConfig, LLMProvider
    from memtomem_stm.proxy.extraction import FactExtractor

    absent = ExtractionConfig()
    assert absent.llm is None
    effective = absent.effective_llm()
    assert effective.provider is LLMProvider.OLLAMA
    assert effective.model == "qwen3:4b"
    assert effective.max_tokens == 1000

    # One leaf, set the way the table invites, materializes the whole block:
    # provider defaults to openai and startup then demands a credential.
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__EXTRACTION__LLM__MODEL", "docs-test-model")
    with pytest.raises(pydantic.ValidationError, match="api_key is required"):
        STMConfig()

    monkeypatch.setenv("MEMTOMEM_STM_PROXY__EXTRACTION__LLM__API_KEY", "docs-test-key")
    materialized = STMConfig().proxy.extraction
    assert materialized.llm is not None
    assert materialized.effective_llm().provider is LLMProvider.OPENAI
    assert materialized.effective_llm().model == "docs-test-model"

    # The production formatting call is what raises; the absent-block profile
    # survives the very same call.
    with pytest.raises(KeyError, match="max_chars"):
        asyncio.run(FactExtractor(materialized)._call_api("payload"))
    absent_extractor = FactExtractor(absent)
    assert absent_extractor._llm_cfg.system_prompt.format(max_facts=absent.max_facts), (
        "the absent-block prompt must format on the same path"
    )

    body = _read("docs/reference/environment-variables.md")
    caveat = body.split("### Caveat: `EXTRACTION__LLM__*` defaults", 1)[1]
    for phrase in (
        "provider `ollama`",
        "`qwen3:4b`",
        "`max_tokens` 1000",
        # The rule is categorical: a sibling left unset takes its field default.
        # An "any single variable flips the provider" claim would be false for
        # `__PROVIDER=ollama`, so the doc must name the per-sibling outcomes.
        "every sibling you did not set then takes the",
        "| `__PROVIDER` | `openai` |",
        "| `__SYSTEM_PROMPT` |",
        "`{max_chars}`",
        "`{max_facts}`",
    ):
        assert phrase in caveat, f"extractor-LLM caveat lost {phrase!r}"
    assert "the provider flips to `openai`" not in caveat, (
        "the unconditional phrasing is false when __PROVIDER is the leaf that is set"
    )


def test_environment_reference_is_complete_and_default_accurate() -> None:
    """Every STMConfig leaf must appear once with its actual model default."""
    from memtomem_stm.config import STMConfig

    body = _read("docs/reference/environment-variables.md")
    table = body.split("<!-- stmconfig-env:start -->", 1)[1].split("<!-- stmconfig-env:end -->", 1)[
        0
    ]
    documented: dict[str, str] = {}
    duplicates: list[str] = []
    for line in table.splitlines():
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if len(cells) != 5 or not cells[0].startswith("`MEMTOMEM_STM_"):
            continue
        name = cells[0].strip("`")
        if name in documented:
            duplicates.append(name)
        default = cells[2]
        documented[name] = default[1:-1] if default.startswith("`") else default

    expected = _stmconfig_environment_defaults(STMConfig)
    assert duplicates == []
    assert documented.keys() == expected.keys()
    assert documented == {name: _documented_default(default) for name, default in expected.items()}

    for direct_name in (
        "MEMTOMEM_STM_HOOK_SURFACE_TOOLS",
        "MMS_CLIENT_SERVER_NAME",
        "MMS_NO_TUI",
        "NO_COLOR",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "KIMI_CODE_HOME",
        "APPDATA",
        "OTEL_SERVICE_NAME",
        "OTEL_RESOURCE_ATTRIBUTES",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    ):
        assert f"`{direct_name}`" in body


def test_public_notebook_inventory_and_state_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lone public notebook must not reference archived numbered lessons."""
    public_notebooks = sorted((REPO_ROOT / "notebooks").glob("*.ipynb"))
    notebook_readme = _read("notebooks/README.md")
    assert public_notebooks
    for notebook in public_notebooks:
        assert notebook.name in notebook_readme

    checked = [
        *public_notebooks,
        REPO_ROOT / "notebooks/_helpers.py",
        REPO_ROOT / "notebooks/_fixtures/doc_mcp.py",
        REPO_ROOT / "notebooks/_fixtures/fake_ltm.py",
    ]
    for path in checked:
        body = path.read_text(encoding="utf-8").lower()
        assert "notebook 02" not in body
        assert "notebook 03" not in body

    helper_path = REPO_ROOT / "notebooks/_helpers.py"
    spec = importlib.util.spec_from_file_location("public_notebook_helpers", helper_path)
    assert spec is not None and spec.loader is not None
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)

    isolated_keys = (
        "MEMTOMEM_STM_PROXY__CONFIG_PATH",
        "MEMTOMEM_STM_PROXY__CACHE__DB_PATH",
        "MEMTOMEM_STM_PROXY__METRICS__DB_PATH",
        "MEMTOMEM_STM_PROXY__COMPRESSION_FEEDBACK__DB_PATH",
        "MEMTOMEM_STM_PROXY__PROGRESSIVE_READS__DB_PATH",
        "MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH",
    )
    for key in isolated_keys:
        monkeypatch.setenv(key, "pre-test")
    monkeypatch.setenv("MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS", "false")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "true")
    kernel_home = os.environ.get("HOME", "")

    config_path = helpers.isolate_stm_state(prefix="docs_sync_")
    root = config_path.parent
    assert all(Path(os.environ[key]).parent == root for key in isolated_keys)
    child_env = helpers.isolated_cli_env(config_path)
    assert child_env["HOME"] == str(root)
    assert os.environ.get("HOME", "") == kernel_home

    notebook = json.loads(public_notebooks[0].read_text(encoding="utf-8"))
    notebook_source = "".join(line for cell in notebook["cells"] for line in cell.get("source", []))
    assert notebook_source.count('"mms"') >= 2
    assert notebook_source.count('"add"') >= 2
    assert "env=isolated_cli_env(config_path)" in notebook_source
    for token in (
        '"--compression"',
        '"selective"',
        '"doc__get_document"',
        '"stm_proxy_select_chunks"',
        "parse_toc_response",
        'assert toc is not None, "Expected a selective-compression TOC"',
    ):
        assert token in notebook_source


def test_readme_and_compatibility_hubs_link_to_split_docs() -> None:
    """The journey/reference split remains discoverable from stable entrypoints."""
    readme = _read("README.md")
    assert "docs/README.md" in readme
    assert "docs/getting-started.md" in readme
    assert "docs/guides/operations.md" in readme
    assert "docs/guides/vibe-coding-getting-started-ko.md" in readme

    getting_started = _read("docs/getting-started.md")
    assert "guides/vibe-coding-getting-started-ko.md" in getting_started

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

    docs_hub = _read("docs/README.md")
    for target in (
        "getting-started.md",
        "guides/vibe-coding-getting-started-ko.md",
        "reference/environment-variables.md",
        "guides/operations.md",
        "compression.md",
        "surfacing.md",
        "adr/README.md",
    ):
        assert target in docs_hub


def test_toolgraph_gateway_guide_pins_observability_contract() -> None:
    """Keep gateway onboarding literals tied to their runtime sources."""
    guide = _read("docs/guides/toolgraph-policy-gateway.md")
    server = _read("src/memtomem_stm/server.py")
    manager = _read("src/memtomem_stm/proxy/manager.py")

    env_flag = "MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS"
    counter = "would_block_calls"
    assert env_flag in guide and env_flag in server
    assert counter in guide and counter in manager


# (pin, Core's declared mcp requirement, redundant?) — the shapes the advisory
# will actually meet. The unsafe direction is a wrong "drop the pin", so every
# ambiguous shape must land on False.
_MCP_PIN_CASES: tuple[tuple[str, str, bool], ...] = (
    ("mcp<2", "mcp[cli]>=1.28.1", False),  # Core 0.3.8-0.3.13 today
    ("mcp<2", "mcp<3", False),  # a cap, but above ours
    ("mcp<2", "mcp!=2", False),  # rejects the boundary, admits 3.x
    ("mcp<2", "mcp!=2.*", False),
    ("mcp<2", "mcp<2.1,!=2.0.0", False),  # rejects samples, admits 2.0.1
    ("mcp<2", "mcp<3\nmcp!=2.*", False),  # jointly sufficient, singly not
    ("mcp<2", "mcp==1!0.*", False),  # epoch 1 sorts above every epoch 0
    ("mcp<2", "mcp~=1!0.5", False),
    ("mcp<2", "httpx>=1.0", False),  # no mcp requirement at all
    ("mcp<2", "mcp<2", True),
    ("mcp<2", "mcp~=1.28", True),  # >=1.28, ==1.*
    ("mcp<2", "mcp~=1.28.1", True),
    ("mcp<2", "mcp==1.*", True),
    ("mcp<2", "mcp<=1.99", True),
    ("mcp<2", "mcp>=1.28.1,<2", True),
)


@pytest.mark.parametrize(("pin", "requires", "redundant"), _MCP_PIN_CASES)
def test_mcp_pin_expiry_probe_only_certifies_a_proven_cap(
    pin: str, requires: str, redundant: bool
) -> None:
    """The advisory's pin probe, exercised as code rather than as a substring."""
    smoke = _load_core_compat_smoke()
    assert smoke.mcp_pin_is_redundant(pin, requires, "3.12.8")[0] is redundant


def test_mcp_pin_expiry_probe_evaluates_markers_against_core_python() -> None:
    """An inactive marker must not be judged, and packaging must not guess.

    packaging fills any environment key it is not given from the *running*
    interpreter, which is STM's, not the Core venv's — so a requirement gated
    on a python version Core does not have could otherwise certify a drop.
    """
    smoke = _load_core_compat_smoke()
    gated = 'mcp<2; python_full_version >= "3.12.6"'
    assert smoke.mcp_pin_is_redundant("mcp<2", gated, "3.12.8")[0] is True
    assert smoke.mcp_pin_is_redundant("mcp<2", gated, "3.12.4") == (False, [])
    # An extra-gated requirement is not active: the workflow installs no extras.
    assert smoke.mcp_pin_is_redundant("mcp<2", 'mcp<2; extra == "cli"', "3.12.8") == (False, [])


def test_mcp_pin_expiry_probe_refuses_a_pin_it_cannot_judge() -> None:
    """A pin with no upper bound has no boundary to prove anything against."""
    smoke = _load_core_compat_smoke()
    with pytest.raises(ValueError, match="declares no upper bound"):
        smoke.mcp_pin_is_redundant("mcp>=1", "mcp<2", "3.12.8")


def test_reviewed_memory_resume_guide_matches_core_contract_smoke() -> None:
    """Keep the copy/paste core CLI flow tied to the released-core advisory."""
    guide = _read("docs/guides/reviewed-memory-resume.md")
    for snippet in (
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
        "mm review reject CANDIDATE_ID",
        "mm pinned delete resume-contract --scope project_local",
        "mm gc orphan-sources --apply",
        "mms remove resume_fs --yes",
    ):
        assert snippet in guide, f"reviewed-memory-resume guide lost {snippet!r}"

    assert "--with 'mcp<2' 'memtomem[all]>=0.3.12,<0.4'" in guide
    assert "memtomem>=0.3.12,<0.4" in guide
    assert "does not approve" in guide and "one implicitly." in guide
    assert "\nmms daemon stop --all\n" not in guide
    assert "schema 4" in guide

    workflow = _read(".github/workflows/core-compat-advisory.yml")
    for version in ('core: "0.3.12"', 'core: "0.3.13"'):
        assert version in workflow
    # The pin is per matrix row, so a newly added Core does not inherit it, and
    # it announces its own expiry rather than outliving the Core gap. Pin every
    # link of that wiring: declaring the field is worthless if the install step
    # stops consuming it or the probe stops being conditional on it.
    assert 'mcp_pin: "mcp<2"' in workflow
    for wiring in (
        '${MCP_PIN:+"$MCP_PIN"}',  # an empty pin adds no install argument
        "if: ${{ matrix.mcp_pin != '' }}",  # probe only runs for pinned rows
        # ...and the judgement runs the tracked, unit-tested helper rather than
        # inline YAML no test can reach.
        "scripts/core_compat_smoke.py",
        "--check-mcp-pin",
        "--core-requires",
    ):
        assert wiring in workflow, f"core-compat advisory lost mcp_pin wiring: {wiring}"
    # Both consumers need the field: the install step to apply the pin and the
    # probe step to judge it. A membership check would survive losing either.
    assert workflow.count("MCP_PIN: ${{ matrix.mcp_pin }}") == 2, (
        "the matrix pin must reach both the install step and the expiry probe"
    )
    # Every matrix row must declare an expectation. Counting matches of a regex
    # that *requires* ``expected`` proves nothing on its own — a malformed row
    # simply would not match — so compare against the number of rows present.
    # Both patterns accept any YAML scalar spelling: counting only `- core: "`
    # would let `- core: '0.3.14'` or an unquoted value slip past both sides.
    scalar = r"[\"']?[^\"'\s]+[\"']?"
    declared_rows = len(re.findall(r"^\s*- core:\s*\S", workflow, re.MULTILINE))
    well_formed = re.findall(
        rf"- core:\s*{scalar}\n\s+expected:\s*(\w+)(?:\n\s+mcp_pin:\s*{scalar})?\n", workflow
    )
    assert declared_rows >= 5, "core-compat matrix lost rows"
    assert len(well_formed) == declared_rows, (
        f"{declared_rows - len(well_formed)} core-compat row(s) do not declare "
        "`expected` immediately after `core`"
    )

    smoke = re.sub(r"\s+", " ", _read("scripts/core_compat_smoke.py"))
    for token in (
        "_init_current_guide",
        '"resume-contract"',
        '"project_local"',
        '"resume-demo"',
        '"pinned", "list", "--json"',
        '"review", "list"',
        '"review", "show"',
        '"review", "approve"',
        '"compat-smoke"',
        # The guide's destructive cleanup commands must be *executed* by the
        # advisory, not merely pinned as guide substrings above.
        "_assert_guide_cleanup",
        '"review", "reject"',
        '"demo-operator"',
        '"pinned", "delete", "resume-contract", "--scope", "project_local"',
        '"gc", "orphan-sources"',
        '"gc", "orphan-sources", "--apply", "--yes"',
    ):
        assert token in smoke, f"released-core advisory lost guide contract token {token!r}"

    # ``--apply`` prompts; the guide has to say so, because the copyable form
    # omits ``--yes`` while any non-interactive caller needs it.
    assert "add `--yes` only in a" in guide

    # The guide's STM-side cleanup commands, checked against the live CLI.
    from click.testing import CliRunner

    from memtomem_stm.cli.proxy import cli as mms_cli

    remove_help = CliRunner().invoke(mms_cli, ["remove", "--help"], terminal_width=200)
    assert remove_help.exit_code == 0
    assert "--yes" in remove_help.output
    daemon_stop_help = CliRunner().invoke(mms_cli, ["daemon", "stop", "--help"], terminal_width=200)
    assert daemon_stop_help.exit_code == 0
    assert "--all" in daemon_stop_help.output


def test_otlp_export_doc_matches_the_shipped_span_vocabulary() -> None:
    """The published attribute table must be exactly what the code emits.

    This drifted once already: the guide promised ``stm.selection_id``,
    ``stm.cache_hit``, an argument digest and an OpenInference marker that no
    call site emitted. A privacy claim that overstates what is *withheld* is
    the benign direction; one that understates what is *sent* is not, so pin
    both directions.
    """
    from memtomem_stm.observability.otlp import _SPAN_ATTRIBUTES

    doc = (REPO_ROOT / "docs" / "otlp-export.md").read_text(encoding="utf-8")
    table = doc.split("### Attribute vocabulary", 1)[1].split("`error.type`", 1)[0]

    documented: dict[str, set[str]] = {}
    for line in table.splitlines():
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if len(cells) != 2 or not cells[0].startswith("`"):
            continue
        documented[cells[0].strip("`")] = {
            token.strip().strip("`") for token in cells[1].split(",") if token.strip()
        }

    emitted = {
        span: {attribute for attribute, _type in mapping.values()}
        for span, mapping in _SPAN_ATTRIBUTES.items()
    }

    assert documented == emitted, (
        "docs/otlp-export.md's attribute table and _SPAN_ATTRIBUTES disagree.\n"
        f"documented-only: { {k: v - emitted.get(k, set()) for k, v in documented.items() if v - emitted.get(k, set())} }\n"
        f"emitted-only: { {k: v - documented.get(k, set()) for k, v in emitted.items() if v - documented.get(k, set())} }"
    )


def test_adr_0001_cited_paths_and_call_site_claim_hold() -> None:
    """ADR 0001's cited repo paths exist and its ``log_feedback`` claim is true.

    The ADR names concrete repo paths and asserts the selection log's
    ``feedback`` event has no production call site. Both statements rot
    silently when code moves; pin them here so the ADR fails CI instead of
    aging in prose. The explicit expected set below makes a regex miss loud:
    coverage cannot silently shrink to an easier subset of citations.
    """
    body = _read("docs/adr/0001-ecosystem-integration-contracts.md")

    cited = {
        token
        for token in re.findall(r"`([^`\n]+)`", body)
        if token == "CLAUDE.md" or token.startswith(("src/", "tests/", "docs/"))
    }
    expected = {
        "CLAUDE.md",
        "src/memtomem_stm/data/policy-bundle.schema.json",
        "src/memtomem_stm/data/toolgraph-contract-v1/",
        "src/memtomem_stm/observability/otlp.py",
        "src/memtomem_stm/proxy/toolgraph_bundle.py",
        "src/memtomem_stm/proxy/toolgraph_cache.py",
        "src/memtomem_stm/proxy/toolgraph_provider.py",
        "src/memtomem_stm/surfacing/mcp_client.py",
        "src/memtomem_stm/utils/sqlite_private.py",
        "tests/test_docs_sync.py",
    }
    assert expected <= cited, f"ADR 0001 lost expected citations: {sorted(expected - cited)}"
    for relative in sorted(cited):
        target = REPO_ROOT / relative.rstrip("/")
        assert target.exists(), f"ADR 0001 cites missing path {relative}"
        if relative.endswith("/"):
            assert target.is_dir(), f"ADR 0001 cites {relative} as a directory"

    from memtomem_stm.proxy.selection_log import SelectionTelemetryLog

    assert callable(SelectionTelemetryLog.log_feedback)
    callers: list[str] = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "log_feedback":
                callers.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert callers == [], (
        f"ADR 0001 claims log_feedback has no production call site, found {callers}; "
        "update the ADR's matrix note alongside this pin"
    )


def test_cli_md_freshness_preset_table_matches_init_mapping() -> None:
    """docs/cli.md's --freshness table pins three numbers: the ``live`` and
    ``reuse`` TTLs written by ``mms init`` and the schema default that
    ``balanced`` leaves in place. All three live in code; a drift in either
    direction must fail here, not in a user's cache behavior."""
    from memtomem_stm.proxy.config import CacheConfig

    cli_md = (REPO_ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    table = re.search(
        r"`--freshness` picks.*?see \[caching\]\(caching\.md\)", cli_md, re.DOTALL
    )
    assert table, "docs/cli.md lost the --freshness preset table"
    text = table.group(0)

    source = (REPO_ROOT / "src" / "memtomem_stm" / "cli" / "proxy.py").read_text(encoding="utf-8")
    mapping = re.search(r'\{"live": (\d+), "reuse": (\d+)\}\[freshness\]', source)
    assert mapping, "init's freshness mapping literal moved; update this pin"
    live_ttl, reuse_ttl = mapping.group(1), mapping.group(2)

    assert f"`{live_ttl}`" in text, f"documented live TTL != code ({live_ttl})"
    assert f"`{reuse_ttl}`" in text, f"documented reuse TTL != code ({reuse_ttl})"
    schema_default = CacheConfig().default_ttl_seconds
    assert schema_default is not None
    # Compare against the table as written. An earlier form of this line
    # searched `text.replace("3600 s", f"{schema_default:g} s")`, which
    # rewrote a stale documented value into the live one and then found it —
    # so a changed schema default passed the guard it was meant to trip.
    assert f"{schema_default:g} s" in text, (
        f"documented balanced default != schema default ({schema_default})"
    )


def test_list_json_keys_are_documented(tmp_path: Path) -> None:
    """Every top-level key ``mms list --json`` emits must appear in the docs'
    ``### list`` section.

    Pinned against the live command rather than the prose alone: #811 added
    ``config_valid`` / ``config_error`` while ``docs/cli.md`` still described
    the output as ``{config_path, servers}``, and nothing failed. A key that
    scripts branch on is exactly what a reader looks up in the docs.
    """
    from click.testing import CliRunner

    from memtomem_stm.cli.proxy import cli as mms_cli

    config = tmp_path / "stm_proxy.json"
    config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}), encoding="utf-8")
    result = CliRunner().invoke(mms_cli, ["list", "--json", "--config", str(config)])
    assert result.exit_code == 0, result.output
    emitted = set(json.loads(result.stdout))

    cli_md = _read("docs/cli.md")
    section_match = re.search(r"### `list`\n(.*?)(?=\n### |\n## |\Z)", cli_md, re.DOTALL)
    if not section_match:
        pytest.fail("docs/cli.md must have a ### `list` section")
    section = section_match.group(1)
    # Either spelling counts: the shape blob quotes keys (`"config_path": ...`)
    # while the surrounding prose backticks them.
    undocumented = sorted(
        key for key in emitted if f'"{key}"' not in section and f"`{key}`" not in section
    )
    assert not undocumented, (
        f"docs/cli.md `list` section does not mention {undocumented} — "
        "`mms list --json` emits them; document the key alongside the change."
    )
