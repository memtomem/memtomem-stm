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


def _init_schema_three_guide(mm: Path, *, project: Path, env: dict[str, str]) -> None:
    """Run the core CLI portion of the reviewed-memory-resume guide."""
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
        sections.append(f"## Deployment phase {index}\n\n" + sentence * 35)
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


async def _assert_schema_three_review_flow(
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

        if expected == "schema3":
            _init_schema_three_guide(mm, project=project, env=env)
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
                    assert adapter.capabilities.context_compose_schema == 3
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
                    assert hit.context.window_before
                    assert hit.context.window_after
                    await _assert_schema_three_review_flow(
                        adapter,
                        mm,
                        project=project,
                        env=env,
                    )
            finally:
                await adapter.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-bin-dir", type=Path, required=True)
    parser.add_argument("--expect", choices=("legacy", "schema2", "schema3"), required=True)
    args = parser.parse_args()
    asyncio.run(_smoke(args.core_bin_dir, args.expect))


if __name__ == "__main__":
    main()
