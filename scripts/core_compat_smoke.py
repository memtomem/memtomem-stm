"""Advisory stdio smoke for released memtomem core capability contracts."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter


def _run(command: list[str], *, env: dict[str, str]) -> None:
    subprocess.run(command, env=env, check=True, text=True)


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
        )
        if expected == "enhanced":
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
            )

        with patch.dict(
            os.environ,
            {"HOME": str(home), "XDG_RUNTIME_DIR": str(runtime_dir)},
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
                else:
                    assert adapter.capabilities.context_compose_schema >= 2
                    bundle = await adapter.context_compose(
                        "compatibility sentinel",
                        namespace=None,
                        context_window=0,
                        top_k=5,
                    )
                    assert bundle is not None
                    assert any(
                        "preserve compatibility scope" in item.chunk.content
                        for item in bundle.pinned
                    )
                    assert any(
                        "compatibility sentinel" in item.chunk.content for item in bundle.retrieved
                    )
            finally:
                await adapter.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-bin-dir", type=Path, required=True)
    parser.add_argument("--expect", choices=("legacy", "enhanced"), required=True)
    args = parser.parse_args()
    asyncio.run(_smoke(args.core_bin_dir, args.expect))


if __name__ == "__main__":
    main()
