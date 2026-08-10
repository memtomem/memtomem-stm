"""Advisory stdio smoke for released memtomem core capability contracts."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
from contextlib import chdir
from pathlib import Path
from typing import Any
from unittest.mock import patch

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter


def _run(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        env=env,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture_output,
    )


def _run_json(command: list[str], *, env: dict[str, str], cwd: Path) -> Any:
    return json.loads(_run(command, env=env, cwd=cwd, capture_output=True).stdout)


def _init_legacy_or_schema_two(
    mm: Path,
    *,
    expected: str,
    home: Path,
    project: Path,
    env: dict[str, str],
) -> None:
    memory_dir = home / "memories"
    _run(
        [
            str(mm),
            "init",
            "--non-interactive",
            "--preset",
            "minimal",
            "--provider",
            "none",
            "--memory-dir",
            str(memory_dir),
            "--db-path",
            str(home / "memory.db"),
            "--mcp",
            "skip",
        ],
        env=env,
        cwd=project,
    )
    _run(
        [
            str(mm),
            "add",
            "compatibility sentinel decision",
            "--title",
            "Compatibility sentinel",
            "--file",
            "compatibility.md",
            "--yes",
        ],
        env=env,
        cwd=project,
    )
    if expected != "schema2":
        return

    _run(
        [
            str(mm),
            "pinned",
            "set",
            "compat-policy",
            "--content",
            "always preserve compatibility scope",
        ],
        env=env,
        cwd=project,
    )
    context_file = memory_dir / "context-window.md"
    context_file.write_text(
        "\n\n".join(
            [
                "## Before\n\n" + "before deployment context " * 40,
                "## Match\n\ncompat-window-sentinel " + "matched policy " * 40,
                "## After\n\n" + "after deployment context " * 40,
            ]
        ),
        encoding="utf-8",
    )
    _run(
        [
            str(mm),
            "index",
            str(context_file),
            "--namespace",
            "compat",
            "--force",
        ],
        env=env,
        cwd=project,
    )


def _init_current_guide(mm: Path, *, project: Path, env: dict[str, str]) -> None:
    """Bootstrap a clean temp home, then run the reviewed-resume CLI flow."""
    _run(
        [
            str(mm),
            "init",
            "--non-interactive",
            "--preset",
            "minimal",
            "--namespace",
            "resume-demo",
            "--mcp",
            "skip",
        ],
        env=env,
        cwd=project,
    )
    _run([str(mm), "mem", "init"], env=env, cwd=project)
    _run(
        [
            str(mm),
            "pinned",
            "set",
            "resume-contract",
            "--scope",
            "project_local",
            "--content",
            "Deploy with blue-green; roll back when the error rate exceeds 2%.",
            "--description",
            "Reviewed project resume contract",
            "--priority",
            "10",
        ],
        env=env,
        cwd=project,
    )

    listed = _run_json([str(mm), "pinned", "list", "--json"], env=env, cwd=project)
    block = next(item for item in listed if item["block_id"] == "resume-contract")
    assert block["scope"] == "project_local"
    assert block["description"] == "Reviewed project resume contract"
    assert block["priority"] == 10
    assert "blue-green" in block["content"]

    composed = _run_json(
        [str(mm), "pinned", "compose", "blue-green rollback checklist"],
        env=env,
        cwd=project,
    )
    assert any(item["block_id"] == "resume-contract" for item in composed["pinned"])

    context_file = project / ".memtomem" / "memories.local" / "resume-demo.md"
    context_file.parent.mkdir(parents=True, exist_ok=True)
    sections = []
    for index in range(1, 9):
        marker = "resume-window-sentinel " if index == 4 else ""
        sentence = (
            f"{marker}Phase {index} uses blue-green verification and a 2% rollback threshold. "
        )
        sections.append(f"## Deployment phase {index}\n\n" + sentence * 10)
    context_file.write_text("\n\n".join(sections), encoding="utf-8")
    _run(
        [
            str(mm),
            "index",
            str(context_file.relative_to(project)),
            "--namespace",
            "resume-demo",
            "--force",
        ],
        env=env,
        cwd=project,
    )


async def _assert_current_review_flow(
    adapter: McpClientSearchAdapter,
    mm: Path,
    *,
    project: Path,
    env: dict[str, str],
) -> None:
    candidate = await adapter.candidate_propose(
        "Decision: pause rollout when the error rate exceeds 2%.",
        source="memtomem-stm",
        source_ref="reviewed-memory-resume",
        idempotency_key="reviewed-memory-resume-v1",
    )
    assert candidate is not None and candidate["status"] == "pending"
    candidate_id = candidate["candidate_id"]

    pending = _run_json([str(mm), "review", "list"], env=env, cwd=project)
    assert any(item["id"] == candidate_id for item in pending)
    shown = _run_json([str(mm), "review", "show", candidate_id], env=env, cwd=project)
    assert shown["status"] == "pending"
    rejected = _run_json(
        [
            str(mm),
            "review",
            "reject",
            candidate_id,
            "--reviewer",
            "demo-operator",
            "--reason",
            "reviewed-memory-resume demonstration",
        ],
        env=env,
        cwd=project,
    )
    assert rejected == {"ok": True, "status": "rejected"}
    shown = _run_json([str(mm), "review", "show", candidate_id], env=env, cwd=project)
    assert shown["status"] == "rejected"
    assert shown["reviewer"] == "demo-operator"
    assert shown["decision_reason"] == "reviewed-memory-resume demonstration"

    candidate = await adapter.candidate_propose(
        "Decision: keep the blue-green rollback threshold at 2%.",
        source="memtomem-stm",
        source_ref="reviewed-memory-resume",
        idempotency_key="reviewed-memory-resume-approve-v1",
    )
    assert candidate is not None and candidate["status"] == "pending"
    candidate_id = candidate["candidate_id"]
    approved = _run_json(
        [
            str(mm),
            "review",
            "approve",
            candidate_id,
            "--reviewer",
            "compat-smoke",
        ],
        env=env,
        cwd=project,
    )
    assert approved == {"ok": True, "status": "approved"}
    shown = _run_json([str(mm), "review", "show", candidate_id], env=env, cwd=project)
    assert shown["status"] == "approved"
    assert shown["reviewer"] == "compat-smoke"


def _assert_guide_cleanup(mm: Path, *, project: Path, env: dict[str, str]) -> None:
    """Execute the guide's cleanup step against the released core.

    The guide hands operators destructive commands (``pinned delete``, a source
    unlink, ``gc orphan-sources --apply``). Running them here keeps their
    spelling, ordering, and confirmation contract tied to a real core instead of
    to a docs substring pin.
    """
    _run(
        [str(mm), "pinned", "delete", "resume-contract", "--scope", "project_local"],
        env=env,
        cwd=project,
        capture_output=True,
    )
    assert _run_json([str(mm), "pinned", "list", "--json"], env=env, cwd=project) == []

    source = project / ".memtomem" / "memories.local" / "resume-demo.md"
    source.unlink()
    # The live core watcher may reap the orphan before GC sees it, so assert only
    # that the read-only preview runs and reports on orphans — not a racy hit
    # count, and not the listing-only "Run with --apply" hint.
    preview = _run([str(mm), "gc", "orphan-sources"], env=env, cwd=project, capture_output=True)
    assert "orphan" in preview.stdout.lower()

    # ``--apply`` alone confirms interactively; the guide documents that, and a
    # non-interactive caller must add ``--yes`` (an EOF prompt exits non-zero).
    applied = _run(
        [str(mm), "gc", "orphan-sources", "--apply", "--yes"],
        env=env,
        cwd=project,
        capture_output=True,
    )
    assert "orphan" in applied.stdout.lower()
    after = _run([str(mm), "gc", "orphan-sources"], env=env, cwd=project, capture_output=True)
    assert source.name not in after.stdout


async def _smoke(core_bin_dir: Path, expected: str) -> None:
    with tempfile.TemporaryDirectory(prefix="memtomem-stm-core-smoke-") as temp:
        home = Path(temp)
        env = os.environ.copy()
        env["HOME"] = str(home)
        runtime_dir = home / "run"
        runtime_dir.mkdir(mode=0o700)
        runtime_dir.chmod(0o700)
        env["XDG_RUNTIME_DIR"] = str(runtime_dir)
        mm = core_bin_dir / "mm"
        server = core_bin_dir / "memtomem-server"
        project = home / "project"
        project.mkdir()
        _run(["git", "init", "--quiet"], env=env, cwd=project)

        if expected in {"schema3", "schema4"}:
            _init_current_guide(mm, project=project, env=env)
        else:
            _init_legacy_or_schema_two(
                mm,
                expected=expected,
                home=home,
                project=project,
                env=env,
            )

        with (
            chdir(project),
            patch.dict(
                os.environ,
                {"HOME": str(home), "XDG_RUNTIME_DIR": str(runtime_dir)},
            ),
        ):
            adapter = McpClientSearchAdapter(
                SurfacingConfig(
                    result_format="structured",
                    ltm_mcp_transport="stdio",
                    ltm_mcp_command=str(server),
                    ltm_mcp_args=[],
                )
            )
            try:
                await adapter.warm_up()
                if expected == "legacy":
                    assert adapter.capabilities.context_compose_schema < 2
                    assert await adapter.context_compose("compatibility sentinel") is None
                    results, _, outcome = await adapter.search("compatibility sentinel", top_k=5)
                    assert outcome == "ok"
                    assert any("compatibility sentinel" in item.chunk.content for item in results)
                elif expected == "schema2":
                    assert adapter.capabilities.context_compose_schema == 2
                    bundle = await adapter.context_compose(
                        "compat-window-sentinel",
                        namespace="compat",
                        context_window=1,
                        top_k=5,
                    )
                    assert bundle is not None
                    assert any(
                        "preserve compatibility scope" in item.chunk.content
                        for item in bundle.pinned
                    )
                    hit = next(
                        item
                        for item in bundle.retrieved
                        if "compat-window-sentinel" in item.chunk.content
                    )
                    assert hit.context is None
                else:
                    expected_schema = 4 if expected == "schema4" else 3
                    assert adapter.capabilities.context_compose_schema == expected_schema
                    bundle = await adapter.context_compose(
                        "resume-window-sentinel",
                        max_chars=15_000,
                        namespace="resume-demo",
                        context_window=1,
                        top_k=5,
                    )
                    assert bundle is not None
                    assert any("blue-green" in item.chunk.content for item in bundle.pinned)
                    hit = next(
                        item
                        for item in bundle.retrieved
                        if "resume-window-sentinel" in item.chunk.content
                    )
                    assert hit.chunk.metadata.namespace == "resume-demo"
                    assert hit.context is not None
                    assert "Phase 3" in hit.context.window_before[-1].content
                    assert "Phase 5" in hit.context.window_after[0].content
                    if expected == "schema4":
                        assert bundle.score_scale is not None
                        assert hit.score_scale == bundle.score_scale
                    else:
                        assert bundle.score_scale is None
                        assert hit.score_scale is None
                    await _assert_current_review_flow(
                        adapter,
                        mm,
                        project=project,
                        env=env,
                    )
                    if expected == "schema4":
                        # The guide's cleanup uses ``gc orphan-sources``, which
                        # 0.3.10 does not ship, so it runs only for the rows at
                        # or above the generation that has it. The guide's own
                        # install floor is higher still (Core 0.4.0) and is not
                        # what gates this — these rows replay its CLI flow
                        # against older Cores on purpose.
                        _assert_guide_cleanup(mm, project=project, env=env)
            finally:
                await adapter.stop()


def mcp_pin_is_redundant(pin: str, requires: str, core_python: str) -> tuple[bool, list[str]]:
    """Is ``pin`` already implied by Core's own declared ``mcp`` requirement?

    ``pin`` is the workflow's ``mcp_pin`` (e.g. ``mcp<2``); ``requires`` is the
    newline-joined output of ``importlib.metadata.requires("memtomem")`` read
    from the Core venv, and ``core_python`` its ``X.Y.Z``.

    Returns ``(redundant, active_requirement_strings)``.

    The pin excludes an open-ended range, so no finite set of sample versions
    can prove Core already excludes it — ``mcp<2.1,!=2.0.0`` rejects every
    plausible sample yet still admits ``2.0.1``. packaging exposes no subset
    test, so this proves a *sufficient* condition instead: one active specifier
    whose accepted set provably ends at or below the pin's boundary. Anything
    it cannot prove keeps the pin. That direction matters — a missed notice
    costs a stale hint, a wrong one removes a pin the build still needs.
    """
    from packaging.requirements import Requirement
    from packaging.version import Version

    def series_end(version: Version, components: int | None = None) -> Version:
        """First version above the series ``version`` describes.

        The epoch has to be carried through. ``Version.release`` drops it, and
        every epoch-1 version sorts above every epoch-0 one, so rebuilding from
        the release tuple alone would certify ``==1!0.*`` as capped below ``2``
        while ``1!0.1`` satisfies it.
        """
        parts = list(version.release[:components] if components else version.release) or [0]
        parts[-1] += 1
        tail = ".".join(str(part) for part in parts)
        return Version(f"{version.epoch}!{tail}")

    pin_spec = Requirement(pin).specifier
    boundaries = [s.version for s in pin_spec if s.operator in ("<", "<=")]
    if not boundaries:
        raise ValueError(f"mcp_pin {pin!r} declares no upper bound; this probe cannot judge it")
    boundary = Version(boundaries[0])

    declared = []
    for line in requires.splitlines():
        if not line.strip():
            continue
        try:
            requirement = Requirement(line)
        except Exception:  # a shape packaging cannot parse
            continue
        if requirement.name.lower() != "mcp":
            continue
        # Evaluate in the CORE venv's environment, and with no extra: the
        # install step requests no extras, so an ``extra == "..."`` guarded
        # requirement is not active and must not be judged. Both python fields
        # are supplied — packaging would otherwise fill the omitted one from
        # the *running* interpreter, which is not the one Core runs on.
        if requirement.marker is not None and not requirement.marker.evaluate(
            {
                "python_version": ".".join(core_python.split(".")[:2]),
                "python_full_version": core_python,
                "extra": "",
            }
        ):
            continue
        declared.append(requirement)

    def caps_below(specifier: Any) -> bool:
        operator, raw_version = specifier.operator, specifier.version
        if operator == "<":
            return Version(raw_version) <= boundary
        if operator == "<=":
            return Version(raw_version) < boundary
        if operator == "==" and not raw_version.endswith(".*"):
            return Version(raw_version) < boundary
        if operator in ("==", "~=") and raw_version.endswith(".*"):
            return series_end(Version(raw_version[:-2])) <= boundary
        if operator == "~=":
            # ``~=X.Y`` means ``>=X.Y, ==X.*`` — one component shorter.
            parsed = Version(raw_version)
            return series_end(parsed, len(parsed.release) - 1) <= boundary
        return False

    redundant = any(caps_below(s) for r in declared for s in r.specifier)
    return redundant, [str(r) for r in declared]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-bin-dir", type=Path)
    parser.add_argument("--expect", choices=("legacy", "schema2", "schema3", "schema4"))
    parser.add_argument(
        "--check-mcp-pin",
        metavar="PIN",
        help="Report whether PIN is already implied by Core's own mcp requirement.",
    )
    parser.add_argument(
        "--core-requires",
        type=Path,
        help="File holding the Core venv's python version then its requires() lines.",
    )
    args = parser.parse_args()

    if args.check_mcp_pin:
        if args.core_requires is None:
            parser.error("--check-mcp-pin requires --core-requires")
        core_python, _, requires = args.core_requires.read_text(encoding="utf-8").partition("\n")
        redundant, declared = mcp_pin_is_redundant(
            args.check_mcp_pin, requires, core_python.strip()
        )
        if redundant:
            print(f"::notice::Core now caps mcp itself ({declared}); drop mcp_pin for this row")
        else:
            print(f"{args.check_mcp_pin} still required; Core declares {declared or 'nothing'}")
        return

    if args.core_bin_dir is None or args.expect is None:
        parser.error("--core-bin-dir and --expect are required for the stdio smoke")
    asyncio.run(_smoke(args.core_bin_dir, args.expect))


if __name__ == "__main__":
    main()
