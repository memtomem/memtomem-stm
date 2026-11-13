"""``python -m memtomem_stm`` → the Click CLI.

Primarily so the daemon can re-spawn itself detached with a stable, path-free
invocation (``safe_module_argv("memtomem_stm", "daemon", "run")``) on every
platform, rather than hunting for a console-script path. Because the CLI group
only dispatches the bare-invocation MCP-server path when *no* subcommand is
given, passing ``daemon run`` here invokes that subcommand directly.

Start it with ``-P``, as that helper does. Under a bare ``python -m`` the
working directory is first on the import path, and Python has already imported
from it by the time this module runs.
"""

from __future__ import annotations

from memtomem_stm.cli.proxy import cli

if __name__ == "__main__":
    cli()
