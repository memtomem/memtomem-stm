"""Shared fixtures for memtomem-stm tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.feedback_store import FeedbackStore
from helpers import set_home


@pytest.fixture(autouse=True)
def isolate_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Keep the developer's home and STM environment out of every test.

    ``STMConfig()`` reads the proxy config file when the environment overrides
    a field of an upstream server (#835), and its path defaults under ``~``. A
    contributor whose real ``stm_proxy.json`` happens to declare a server named
    like a fixture's would otherwise see that file complete the fixture's
    config — a failure that reproduces on one machine only. Redirecting the
    home directory isolates that default (and every other ``expanduser()``)
    without adding a ``MEMTOMEM_STM_*`` variable that tests would then see.

    ``STMConfig()`` also reads the whole ``MEMTOMEM_STM_`` namespace (#1028).
    Clear it case-insensitively, including flat hook knobs read directly from
    ``os.environ``. Tests and their fixtures set intentional overrides with
    ``monkeypatch.setenv`` AFTER isolation; unrelated namespaces are untouched.

    This owns its patch so a test's ``monkeypatch.undo()`` cannot restore the
    ambient environment mid-test. The monkeypatch fixture below depends on
    this one: its changes must unwind BEFORE we restore the developer's values.
    """
    with pytest.MonkeyPatch.context() as patch:
        set_home(patch, tmp_path_factory.mktemp("home"))
        # MonkeyPatch restores deletions in reverse. Delete backwards so STM
        # keys regain their original order: case-equivalent names are resolved
        # last-one-wins by settings on POSIX.
        for name in reversed(list(os.environ)):
            if name.lower().startswith("memtomem_stm_"):
                patch.delenv(name, raising=False)
        yield


@pytest.fixture
def monkeypatch(isolate_home: None) -> Iterator[pytest.MonkeyPatch]:
    """Standard per-test patching, nested inside home/environment isolation."""
    with pytest.MonkeyPatch.context() as patch:
        yield patch


@pytest.fixture
def surfacing_config() -> SurfacingConfig:
    """SurfacingConfig with short timeouts, no webhooks."""
    return SurfacingConfig(
        enabled=True,
        timeout_seconds=1.0,
        fire_webhook=False,
        feedback_enabled=True,
        auto_tune_enabled=True,
        cache_ttl_seconds=5.0,
        cooldown_seconds=1.0,
    )


@pytest.fixture
def feedback_store(tmp_path: Path) -> FeedbackStore:
    db = tmp_path / "test_feedback.db"
    store = FeedbackStore(db)
    store.initialize()
    yield store
    store.close()


@pytest.fixture
def proxy_cache(tmp_path: Path) -> ProxyCache:
    db = tmp_path / "test_cache.db"
    cache = ProxyCache(db, max_entries=100)
    cache.initialize()
    yield cache
    cache.close()


@pytest.fixture
def token_tracker() -> TokenTracker:
    return TokenTracker(metrics_store=None)


@pytest.fixture(autouse=True)
def _no_real_child_sweep(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test's teardown sweep the test runner's own children.

    ``app_lifespan``'s teardown terminates this process's direct children as
    the #906 backstop. Under pytest "this process" is the test runner, whose
    children are xdist workers and whatever subprocess a fixture spawned — so
    any test driving the lifespan to teardown would kill them, machine- and
    ordering-dependently. Report none by default; a test that needs the real
    probe marks itself ``real_child_sweep`` and is then responsible for its own
    children.
    """
    if request.node.get_closest_marker("real_child_sweep"):
        return
    monkeypatch.setattr("memtomem_stm.utils.child_reaper.probe_child_pids", lambda: set())


@pytest.fixture(autouse=True)
def _no_real_teardown_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test arm the real hard-exit backstop.

    ``app_lifespan`` arms a watchdog that calls ``os._exit`` if teardown does
    not finish in time (#906) — in a test that is the pytest process, killed
    mid-run with no report. Lifespan tests also build their config from a
    MagicMock, so the timeout would not even be a number. The watchdog's own
    behaviour is pinned in tests/test_teardown_watchdog.py, and its wiring by
    the tests that replace this double with a recording one.
    """

    class _InertWatchdog:
        def __init__(self, timeout_seconds: object, *, before_exit: object = None) -> None:
            pass

        def arm(self) -> None:
            pass

        def disarm(self) -> None:
            pass

    monkeypatch.setattr("memtomem_stm.server.TeardownWatchdog", _InertWatchdog)


@pytest.fixture(autouse=True)
def _no_real_shutdown_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test take over the test runner's SIGTERM/SIGINT.

    ``app_lifespan`` installs handlers that close fd 0 and, on a second signal,
    ``os._exit`` (#906). pytest-asyncio runs its loop on the main thread, so
    those install for real: a Ctrl-C during the run would close pytest's own
    stdin instead of interrupting it, and a lifespan that raised before its
    teardown would leave them installed for every later test. The behaviour is
    pinned in tests/test_signal_shutdown.py, the wiring by the tests that
    replace this double with a recording one.
    """

    class _InertSignals:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def install(self) -> None:
            pass

        def entering_teardown(self) -> None:
            pass

        def remove(self) -> None:
            pass

        def trigger(self, reason: str) -> None:
            pass

    monkeypatch.setattr("memtomem_stm.server.ShutdownSignals", _InertSignals)


@pytest.fixture(autouse=True)
def _no_real_parent_liveness(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never let a test watch the test runner's own parent.

    ``app_lifespan`` can start a poller that shuts the process down when it is
    reparented (#914). Under pytest the parent is whatever launched the run, and
    the shutdown it asks for cancels the loop's tasks. The feature gate is not
    enough on its own: lifespan tests build their config from a MagicMock, which
    has to spell the interval out (comparing one to ``0`` raises), so a test that
    forgets would otherwise decide this by accident. Instrumentation is stubbed
    too, since the server object those tests pass is the shared module-level one
    and the wrapping would outlive the test. Behaviour is pinned in tests/test_parent_liveness.py, the
    wiring by the tests that replace these doubles with recording ones. A test
    marked ``real_client_activity`` gets the real instrumentation, which is how
    the SDK surface it reaches into stays pinned.
    """

    class _InertWatcher:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def run(self) -> None:
            pass

        def note_activity(self) -> None:
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr("memtomem_stm.server.ParentLivenessWatcher", _InertWatcher)
    if not request.node.get_closest_marker("real_client_activity"):
        monkeypatch.setattr(
            "memtomem_stm.server._instrument_client_activity", lambda _server, _watcher: True
        )
