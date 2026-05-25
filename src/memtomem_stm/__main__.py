"""``python -m memtomem_stm`` → the Click CLI.

Primarily so the daemon can re-spawn itself detached with a stable, path-free
invocation (``[sys.executable, "-m", "memtomem_stm", "daemon", "run"]``) on
every platform, rather than hunting for a console-script path. Because the CLI
group only dispatches the bare-invocation MCP-server path when *no* subcommand
is given, passing ``daemon run`` here invokes that subcommand directly.
"""

from __future__ import annotations

from memtomem_stm.cli.proxy import cli

if __name__ == "__main__":
    cli()
