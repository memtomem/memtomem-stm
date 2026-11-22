"""Run the STM server with its graceful teardown held open, for one e2e test.

The twice-signalled test in ``test_signal_shutdown_e2e.py`` has to deliver its
second signal while the server is still tearing down. An idle server can finish
that teardown and exit soon after logging the first signal (121-182 ms in local
macOS runs), so a test that only waits for the log line races the exit (#1434).

This launcher holds the teardown at the start of ``ProxyManager.stop()``, which
the lifespan ``finally`` awaits once, after ``ShutdownSignals.entering_teardown()``.
It leaves files in a directory so the test can see where the process is:

- ``gate-entered``: teardown reached ``ProxyManager.stop()``; an entry marker
  only, written before the hold starts.
- ``gate-left``: the hold ended; the file says ``released``, ``timeout`` or
  ``cancelled``.
- ``stop-done``: the real ``stop()`` returned.

The hold ends when ``gate-release`` appears or after ``_HOLD_SECONDS``. That
bound sits nominally below the 10 s ``await_or_warn`` budget around ``stop()``
and the 10 s teardown watchdog the test configures. The headroom is nominal:
the watchdog starts at the first signal, not at gate entry, and the real
``stop()`` then runs outside the gate's own shield, with only what is left of
its budget. Whether it is shielded then is the lifespan's doing: a signalled
shutdown with the watchdog armed shields the whole close sequence (#1468). The
gate keeps its own shield so the hold does not depend on that.

Usage: ``python _gated_stm_server.py <gate-dir>``
"""

from __future__ import annotations

import runpy
import sys
import time
from pathlib import Path

import anyio

from memtomem_stm.proxy import manager

_HOLD_SECONDS = 8.0
_POLL_SECONDS = 0.01


def _install_gate(gate_dir: Path) -> None:
    real_stop = manager.ProxyManager.stop

    async def gated_stop(self: manager.ProxyManager) -> None:
        (gate_dir / "gate-entered").touch()
        deadline = time.monotonic() + _HOLD_SECONDS
        outcome = "timeout"
        try:
            # Shielded: when the first signal lands just after the handshake,
            # the SDK's cancelled cancel scope re-delivers the cancellation at
            # every await of the teardown, which would end this hold at once
            # (measured). When this shield exits, any enclosing cancellation
            # still pending becomes deliverable again unless an outer shield
            # holds it: the real stop() below is not protected by the gate,
            # only by the lifespan's own shield when that applies (#1468).
            with anyio.CancelScope(shield=True):
                while time.monotonic() < deadline:
                    if (gate_dir / "gate-release").exists():
                        outcome = "released"
                        break
                    await anyio.sleep(_POLL_SECONDS)
        except BaseException:
            # The shield blocks only cancellation from enclosing AnyIO scopes,
            # not a direct Task.cancel(); record that the hold was cut short.
            outcome = "cancelled"
            raise
        finally:
            (gate_dir / "gate-left").write_text(outcome)
        await real_stop(self)
        (gate_dir / "stop-done").touch()

    manager.ProxyManager.stop = gated_stop  # type: ignore[method-assign]


def main() -> None:
    _install_gate(Path(sys.argv[1]))
    # The server reads no arguments; leave it none of ours.
    del sys.argv[1:]
    runpy.run_module("memtomem_stm.server", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
