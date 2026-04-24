"""Shared helpers for memtomem-stm tutorial notebooks.

All four notebooks import from this module to avoid duplicating the
isolation setup, MCP client boilerplate, and output formatting.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------


def repo_root() -> Path:
    """Return the memtomem-stm repo root regardless of where Jupyter was launched.

    Handles both common layouts: ``jupyter lab`` from repo root (CWD is the
    repo) and from ``notebooks/`` (CWD is notebooks/). Falls back to walking
    upward looking for ``pyproject.toml``.
    """
    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").exists() and (cwd / "notebooks").exists():
        return cwd
    if (cwd.parent / "pyproject.toml").exists() and cwd.name == "notebooks":
        return cwd.parent
    for parent in [cwd, *cwd.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError(
        f"Cannot locate memtomem-stm repo root from CWD={cwd}. "
        "Run jupyter from the repo root or from the notebooks/ directory."
    )


def fixtures_dir() -> Path:
    """Return the absolute path to notebooks/_fixtures/."""
    return (repo_root() / "notebooks" / "_fixtures").resolve()


def fake_ltm_path() -> Path:
    """Return the path to ``notebooks/_fixtures/fake_ltm.py``.

    Used by notebook 03. See that fixture's module docstring for why we
    ship a notebook-local fake instead of reusing the tests/ stand-in.
    """
    path = (fixtures_dir() / "fake_ltm.py").resolve()
    if not path.exists():
        raise RuntimeError(f"Notebook fake LTM not found at {path}")
    return path


# ---------------------------------------------------------------------------
# State isolation
# ---------------------------------------------------------------------------


def isolate_stm_state(prefix: str = "mms_nb_", *, enable_surfacing: bool = False) -> Path:
    """Create an isolated tempdir and point STM's state there via env vars.

    Sets ``MEMTOMEM_STM_PROXY__CONFIG_PATH``, ``...CACHE__DB_PATH``,
    ``...METRICS__DB_PATH``, and ``MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH``
    so that nothing the notebook does touches the user's real
    ``~/.memtomem/`` directory — including the surfacing feedback store
    that holds cross-session dedup state.

    Parameters
    ----------
    prefix
        Tempdir name prefix.
    enable_surfacing
        By default (``False``), disables surfacing so the notebook does not
        depend on a running ``memtomem-server``. Notebook 03 passes
        ``True`` and then calls :func:`configure_fake_ltm` to point surfacing
        at the in-repo fake MCP server.

    Returns
    -------
    Path
        The path to the (not-yet-created) proxy config JSON. Pass this to
        ``mms`` CLI invocations via ``--config <path>``.
    """
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    config_path = tmp / "stm_proxy.json"
    os.environ["MEMTOMEM_STM_PROXY__CONFIG_PATH"] = str(config_path)
    os.environ["MEMTOMEM_STM_PROXY__CACHE__DB_PATH"] = str(tmp / "proxy_cache.db")
    os.environ["MEMTOMEM_STM_PROXY__METRICS__DB_PATH"] = str(tmp / "proxy_metrics.db")
    os.environ["MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH"] = str(tmp / "stm_feedback.db")
    if not enable_surfacing:
        os.environ["MEMTOMEM_STM_SURFACING__ENABLED"] = "false"
    return config_path


def configure_fake_ltm() -> Path:
    """Point STM's surfacing engine at ``notebooks/_fixtures/fake_ltm.py``.

    Used by notebook 03 so that surfacing can be demonstrated without the
    user needing a real ``memtomem-server`` on their PATH. The fixture is
    a notebook-local fake (distinct from ``tests/_fake_memtomem_server.py``)
    that embeds a fresh UUID in each memory chunk — STM's cross-session
    dedup keys on ``sha256(content)`` so a fixed-content fake would appear
    broken on repeated notebook runs. See ``fake_ltm.py``'s module docstring
    for the full rationale. Also lowers the ``min_response_chars``
    threshold so that small tutorial tool responses actually trigger
    surfacing.

    Must be called **after** :func:`isolate_stm_state` (with
    ``enable_surfacing=True``) and **before** :func:`stm_session`.

    Returns
    -------
    Path
        The path to the fake LTM script — displayed in notebook output so
        the user can see what they're pointing at.
    """
    fake = fake_ltm_path()
    os.environ["MEMTOMEM_STM_SURFACING__ENABLED"] = "true"
    os.environ["MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND"] = "uv"
    os.environ["MEMTOMEM_STM_SURFACING__LTM_MCP_ARGS"] = json.dumps(
        ["run", "python", str(fake)]
    )
    # Lower the threshold so notebook-sized responses trigger surfacing.
    # Default is 5000 chars; our fixture tool returns ~200 chars.
    os.environ["MEMTOMEM_STM_SURFACING__MIN_RESPONSE_CHARS"] = "100"
    os.environ["MEMTOMEM_STM_SURFACING__MIN_QUERY_TOKENS"] = "1"
    return fake


# ---------------------------------------------------------------------------
# MCP client: spawn STM and yield an initialized session
# ---------------------------------------------------------------------------


@asynccontextmanager
async def stm_session() -> AsyncIterator[ClientSession]:
    """Spawn ``memtomem-stm`` as a subprocess and yield an initialized ClientSession.

    Usage::

        async with stm_session() as session:
            tools = await session.list_tools()
            result = await session.call_tool("stm_proxy_stats", {})

    Notes
    -----
    ``mcp.client.stdio.stdio_client`` defaults ``errlog`` to ``sys.stderr``,
    which is an ``ipykernel.iostream.OutStream`` inside a Jupyter kernel and
    raises ``UnsupportedOperation: fileno`` when ``subprocess.Popen`` tries
    to attach it to the child. We open ``/dev/null`` explicitly to get a
    real file descriptor that ``Popen`` accepts. The trade-off is that STM's
    lifecycle logs don't appear in notebook cell outputs — if you want to
    see them, run the notebook from the terminal with ``jupyter nbconvert
    --execute`` (which has a usable stderr) or tail STM's own log file.
    """
    params = StdioServerParameters(
        command="memtomem-stm",
        args=[],
        env=dict(os.environ),
    )
    with open(os.devnull, "w") as devnull:
        async with stdio_client(params, errlog=devnull) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def extract_text(result: Any) -> str:
    """Pull the text payload out of a ``CallToolResult``.

    The MCP client returns a ``CallToolResult`` with a ``content`` list of
    ``TextContent`` / ``ImageContent`` items. All STM built-in tools return
    a single text block, so we just grab ``.content[0].text``.
    """
    content = getattr(result, "content", None)
    if not content:
        return ""
    first = content[0]
    text = getattr(first, "text", None)
    if text is not None:
        return str(text)
    return str(first)


def pretty_stats(result: Any) -> str:
    """Format a ``stm_proxy_stats`` result for notebook display.

    ``stm_proxy_stats`` already returns a human-readable multi-line string,
    so this just extracts the text and wraps it in a fenced code block for
    clean rendering in a notebook Markdown cell (use ``IPython.display.Markdown``).
    """
    text = extract_text(result)
    return f"```\n{text}\n```"


def token_estimate(text: str) -> int:
    """Rough token count estimate: ``len(text) // 4``.

    Avoids a ``tiktoken`` dependency. Accurate enough for tutorial
    comparisons — real token counts will vary by tokenizer, but the
    ratio between original and compressed responses is what matters here.
    """
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Tool output parsing
# ---------------------------------------------------------------------------


def parse_toc_response(text: str) -> dict[str, Any] | None:
    """Parse a selective-compression TOC response.

    Returns a dict with keys ``type``, ``selection_key``, ``entries``
    if the text is a valid TOC, else ``None``. STM's selective compressor
    returns the TOC as the first JSON block in the response — strip any
    surrounding footer/header before parsing.
    """
    # Find the first { or [ and try to parse forward
    stripped = text.strip()
    for start_idx, ch in enumerate(stripped):
        if ch in "{[":
            try:
                decoder = json.JSONDecoder()
                obj, _ = decoder.raw_decode(stripped[start_idx:])
                if isinstance(obj, dict) and obj.get("type") == "toc":
                    return obj
                return None
            except json.JSONDecodeError:
                return None
    return None
