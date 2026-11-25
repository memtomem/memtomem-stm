"""``mms daemon … --hook-command claude`` builds as the registered hook command does (#1515).

The hook sets the variables its command's flags name in ``os.environ`` before
it builds its config, and a daemon it spawns inherits them
(``hook_cmd._runtime_registration_env``). Two of them enter the daemon
fingerprint, so a bare ``mms daemon`` command run from a shell addresses
another daemon. ``--hook-command claude`` reads the handler doctor reads
(``proxy._hook_command_view``) and applies the same variables the same way.
These tests stand fake ``ping`` / ``shutdown`` calls in for "a daemon answers
under fingerprint X" and check which fingerprint each subcommand acts on.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from helpers import set_home
from memtomem_stm.cli.host_runtime import HostRuntimePolicy, hook_command_words, hook_launcher
from memtomem_stm.cli.proxy import cli
from memtomem_stm.config import STMConfig, load_env_stm_config
from memtomem_stm.daemon.discovery import config_fingerprint
from memtomem_stm.daemon.locking import LockProbe, lock_path

PERSIST = "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT"
SURFACING_TIMEOUT = "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS"
HOOK_GATE = "MEMTOMEM_STM_HOOK__USE_DAEMON"
FIRST = "hooks.PostToolUse[0].hooks[0]"
SUFFIX = "--hook-command claude"
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows folds environment names itself"
)


@pytest.fixture(autouse=True)
def posix_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memtomem_stm.cli.hook_registration.running_on_windows", lambda: False)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def home(tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(data_dir))
    for name in (PERSIST, SURFACING_TIMEOUT, HOOK_GATE):
        monkeypatch.delenv(name, raising=False)
    return home


def _settings(home: Path, *handlers: dict[str, Any]) -> Path:
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": list(handlers)}]}}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _cmd(text: str) -> dict[str, Any]:
    return {"type": "command", "command": text}


def _installed(*, use_daemon: bool = True, surfacing: float | None = None) -> str:
    """The command ``mms hook install --host claude`` writes (query text not persisted)."""
    defaults = STMConfig()
    policy = HostRuntimePolicy(
        use_daemon=use_daemon,
        surfacing_timeout_seconds=(
            defaults.surfacing.timeout_seconds if surfacing is None else surfacing
        ),
        daemon_timeout_seconds=defaults.hook.daemon_timeout_seconds,
    )
    return shlex.join(hook_command_words(hook_launcher(), "claude", policy.hook_args()))


def _fp(monkeypatch: pytest.MonkeyPatch, **env: str) -> str:
    """The env-only build's fingerprint under this environment plus *env*."""
    with monkeypatch.context() as patch:
        for key, value in env.items():
            patch.setenv(key, value)
        return config_fingerprint(load_env_stm_config().config, create_key=True)


def _doctors_fp() -> str:
    """The fingerprint doctor probes for the registered command (#1494)."""
    from memtomem_stm.cli.proxy import _hook_command_view

    view = _hook_command_view()
    assert view.state == "found"
    return config_fingerprint(load_env_stm_config(view.env).config, create_key=True)


class Daemons:
    """Fake client: a daemon answers only under the fingerprints in ``live``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *live: str) -> None:
        self.live = set(live)
        self.pinged: list[str | None] = []
        self.shut: list[str | None] = []
        monkeypatch.setattr("memtomem_stm.daemon.client.ping", self.ping)
        monkeypatch.setattr("memtomem_stm.daemon.client.shutdown", self.shutdown)

    @staticmethod
    def _fp(config: STMConfig, fingerprint: str | None) -> str | None:
        return fingerprint or config_fingerprint(config, create_key=False)

    async def ping(self, config: STMConfig, *, timeout: float = 2.0, fingerprint=None) -> Any:
        fp = self._fp(config, fingerprint)
        self.pinged.append(fp)
        if fp in self.live:
            return {"pid": 1, "host": "127.0.0.1", "port": 2, "ltm": "warm"}
        return None

    async def shutdown(self, config: STMConfig, *, fingerprint=None) -> bool:
        fp = self._fp(config, fingerprint)
        self.shut.append(fp)
        if fp in self.live:
            self.live.discard(fp)
            return True
        return False


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli, ["daemon", *args])


# --- the fingerprint each subcommand acts on ---------------------------------


def test_status_looks_under_the_hook_commands_fingerprint(home, monkeypatch):
    _settings(home, _cmd(_installed()))
    hook_fp, bare_fp = _fp(monkeypatch, **{PERSIST: "false"}), _fp(monkeypatch)
    assert hook_fp != bare_fp  # positive control: the default install's flags move it
    assert _doctors_fp() == hook_fp
    daemons = Daemons(monkeypatch)

    hooked = _run("status", "--json", "--hook-command", "claude")
    bare = _run("status", "--json")

    assert hooked.exit_code == 0, hooked.output
    assert bare.exit_code == 0, bare.output
    assert daemons.pinged == [hook_fp, bare_fp]
    assert PERSIST not in os.environ


def test_start_spawns_a_child_that_builds_the_hook_fingerprint(home, monkeypatch):
    _settings(home, _cmd(_installed(surfacing=7.25)))
    expected = _doctors_fp()
    assert expected != _fp(monkeypatch)
    daemons = Daemons(monkeypatch)
    children: list[str] = []

    def spawn_child() -> None:
        # What the detached ``mms daemon run`` rebuilds: the inherited environment.
        fp = config_fingerprint(load_env_stm_config().config, create_key=False)
        children.append(fp)
        daemons.live.add(fp)

    monkeypatch.setattr("memtomem_stm.daemon.spawn._spawn_detached", spawn_child)

    result = _run("start", "--hook-command", "claude")

    assert result.exit_code == 0, result.output
    assert children == [expected]
    assert "daemon started" in result.output
    assert PERSIST not in os.environ and SURFACING_TIMEOUT not in os.environ


@_POSIX_ONLY
def test_a_later_spelling_folds_as_it_does_for_the_hook(home, monkeypatch):
    # The canonical name is set first, so the command's value replaces it in
    # place and the later lower-case spelling still wins the fold (#1503);
    # the query-text flag still moves the fingerprint off the bare build.
    monkeypatch.setenv(SURFACING_TIMEOUT, "2")
    monkeypatch.setenv(SURFACING_TIMEOUT.lower(), "4")
    _settings(home, _cmd(_installed(surfacing=7.25)))
    expected = _doctors_fp()
    assert expected != _fp(monkeypatch)
    with monkeypatch.context() as patch:
        # Positive control: had the command's value won the fold, the
        # fingerprint would differ.
        patch.delenv(SURFACING_TIMEOUT.lower())
        assert expected != _fp(monkeypatch, **{PERSIST: "false", SURFACING_TIMEOUT: "7.25"})
    daemons = Daemons(monkeypatch)

    result = _run("status", "--json", "--hook-command", "claude")

    assert result.exit_code == 0, result.output
    assert daemons.pinged == [expected]


def test_stop_shuts_down_only_the_hook_daemon(home, monkeypatch):
    _settings(home, _cmd(_installed()))
    hook_fp, bare_fp = _fp(monkeypatch, **{PERSIST: "false"}), _fp(monkeypatch)
    daemons = Daemons(monkeypatch, hook_fp, bare_fp)

    result = _run("stop", "--hook-command", "claude")

    assert result.exit_code == 0, result.output
    assert daemons.shut == [hook_fp]
    assert daemons.live == {bare_fp}


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 0.0}

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    monkeypatch.setattr(
        "memtomem_stm.cli.daemon_cmd.time",
        SimpleNamespace(time=lambda: clock["t"], sleep=sleep, monotonic=lambda: clock["t"]),
    )


def test_restart_acts_on_the_hook_fingerprint_throughout(home, data_dir, monkeypatch):
    from memtomem_stm.cli import daemon_cmd
    from memtomem_stm.cli import proxy as proxy_mod

    _settings(home, _cmd(_installed()))
    hook_fp = _fp(monkeypatch, **{PERSIST: "false"})
    daemons = Daemons(monkeypatch)
    reads: list[str] = []
    real_view = proxy_mod._hook_command_view

    def counted_view() -> Any:
        view = real_view()
        reads.append(view.state)
        return view

    probed: list[Path] = []
    real_probe = daemon_cmd._probe_lock

    def recording_probe(path: Path) -> LockProbe:
        probed.append(path)
        return real_probe(path)

    children: list[str] = []

    def spawn_child() -> None:
        fp = config_fingerprint(load_env_stm_config().config, create_key=False)
        children.append(fp)
        daemons.live.add(fp)

    monkeypatch.setattr(proxy_mod, "_hook_command_view", counted_view)
    monkeypatch.setattr(daemon_cmd, "_probe_lock", recording_probe)
    monkeypatch.setattr("memtomem_stm.daemon.spawn._spawn_detached", spawn_child)

    result = _run("restart", "--hook-command", "claude")

    assert result.exit_code == 0, result.output
    assert reads == ["found"]
    assert daemons.shut == [hook_fp]
    assert probed and set(probed) == {lock_path(data_dir, hook_fp)}
    assert children == [hook_fp]


def test_a_failed_restart_names_the_option_and_leaks_nothing(home, monkeypatch):
    from memtomem_stm.cli import daemon_cmd

    _settings(home, _cmd(_installed()))
    _fp(monkeypatch)  # a discovery key, so restart waits on a lock
    Daemons(monkeypatch)
    _fake_clock(monkeypatch)
    monkeypatch.setattr(daemon_cmd, "_probe_lock", lambda path: LockProbe.HELD)

    hooked = _run("restart", "--hook-command", "claude")
    bare = _run("restart")

    assert hooked.exit_code == 1
    assert f"run `mms daemon start {SUFFIX}` again shortly" in hooked.output
    assert bare.exit_code == 1
    assert "run `mms daemon start` again shortly" in bare.output
    assert SUFFIX not in bare.output
    assert PERSIST not in os.environ


# --- what the hook does with the daemon --------------------------------------


@pytest.mark.parametrize(
    ("flag", "ambient", "uses", "reason"),
    [
        (True, "false", True, "yes"),
        (False, "true", False, f"no (the STM hook command at {FIRST} sets --no-daemon)"),
    ],
    ids=["use-daemon-over-a-later-false", "no-daemon-over-a-later-true"],
)
@_POSIX_ONLY
def test_status_follows_the_commands_daemon_flag(home, monkeypatch, flag, ambient, uses, reason):
    # The command's flag decides the hook's route (#1501), even where a later
    # case-equivalent spelling makes the build read the opposite.
    from memtomem_stm.cli.proxy import _hook_command_view

    monkeypatch.setenv(HOOK_GATE, "true" if ambient == "false" else "false")
    monkeypatch.setenv(HOOK_GATE.lower(), ambient)
    _settings(home, _cmd(_installed(use_daemon=flag)))
    # Positive control: the build itself reads the opposite of the flag.
    view = _hook_command_view()
    assert view.use_daemon is flag
    assert load_env_stm_config(view.env).config.hook.use_daemon is not flag
    Daemons(monkeypatch)

    as_json = _run("status", "--json", "--hook-command", "claude")
    text = _run("status", "--hook-command", "claude")

    assert json.loads(as_json.output)["hook_will_use_daemon"] is uses
    assert f"hook will use daemon: {reason}" in text.output


def test_the_hooks_flag_repairs_an_ambient_value_the_bare_build_rejects(home, monkeypatch):
    from memtomem_stm.cli import daemon_cmd

    _settings(home, _cmd(_installed()))
    monkeypatch.setenv(PERSIST, "not-a-bool")
    Daemons(monkeypatch)
    foreign = {
        "fingerprint": "f" * 16,
        "pid": 7,
        "listening": True,
        "exits_when_idle": False,
        "idle_shutdown_seconds": 0,
    }
    monkeypatch.setattr(daemon_cmd, "_live_foreign_daemons", lambda config, fp: [foreign])
    monkeypatch.setattr(daemon_cmd, "_can_force_terminate", lambda: True)

    hooked = _run("status", "--hook-command", "claude")
    bare = _run("status")

    assert bare.exit_code == 1  # positive control: the bare form cannot build
    assert hooked.exit_code == 0, hooked.output
    assert "1 daemon(s) running under a different config" in hooked.output
    # ``stop --all`` would stop the hook's own daemon first, so "if nothing
    # uses them" is not its condition here: no cleanup hint is offered.
    assert "stop --all" not in hooked.output


def test_bare_status_keeps_the_cleanup_hint(home, monkeypatch):
    from memtomem_stm.cli import daemon_cmd

    Daemons(monkeypatch)
    foreign = {
        "fingerprint": "f" * 16,
        "pid": 7,
        "listening": True,
        "exits_when_idle": False,
        "idle_shutdown_seconds": 0,
    }
    monkeypatch.setattr(daemon_cmd, "_live_foreign_daemons", lambda config, fp: [foreign])
    monkeypatch.setattr(daemon_cmd, "_can_force_terminate", lambda: True)

    result = _run("status")

    assert "stop them with `mms daemon stop --all` (it stops every daemon listed above)" in (
        result.output
    )


@pytest.mark.parametrize("hooked", [True, False], ids=["hook-command", "bare"])
def test_a_key_error_suggests_stop_all_only_without_the_option(home, data_dir, monkeypatch, hooked):
    # ``stop --all`` reaches every daemon under data_dir, the hook's own
    # included, so a --hook-command run does not suggest it; the bare run
    # keeps its way out of a damaged key.
    from memtomem_stm.cli import daemon_cmd
    from memtomem_stm.daemon import discovery

    _settings(home, _cmd(_installed()))
    data_dir.mkdir(parents=True, exist_ok=True)
    discovery.discovery_key_path(data_dir).write_bytes(b"not a key\n")
    monkeypatch.setattr(daemon_cmd, "_can_force_terminate", lambda: True)

    result = _run("stop", *(["--hook-command", "claude"] if hooked else []))

    assert result.exit_code == 1
    assert "the daemon discovery key cannot be used" in result.output
    assert ("stop --all" in result.output) is not hooked, result.output


# --- when there is no single command to build from ---------------------------


def _unreadable(home: Path) -> None:
    _settings(
        home, _cmd(_installed()), _cmd("mms hook --host claude --surfacing-timeout-seconds 3")
    )


@pytest.mark.parametrize(
    ("arrange", "words"),
    [
        (lambda home, mp: _settings(home), "no STM hook command"),
        (lambda home, mp: _settings(home, _cmd(_installed()), _cmd(_installed())), "2 STM hook"),
        (lambda home, mp: _unreadable(home), "could not be read"),
        (
            lambda home, mp: (
                _settings(home, _cmd(_installed())),
                mp.setattr("memtomem_stm.cli.hook_registration.running_on_windows", lambda: True),
            ),
            "not read on Windows",
        ),
        (
            lambda home, mp: (
                (home / ".claude").mkdir()
                or (home / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
            ),
            "settings",
        ),
    ],
    ids=["none", "several", "unreadable", "windows", "settings-error"],
)
@pytest.mark.parametrize("subcommand", ["start", "stop", "status", "restart"])
def test_no_single_command_refuses_without_acting(home, monkeypatch, arrange, words, subcommand):
    arrange(home, monkeypatch)
    daemons = Daemons(monkeypatch)
    monkeypatch.setattr("memtomem_stm.daemon.spawn._spawn_detached", lambda: pytest.fail("spawned"))

    result = _run(subcommand, "--hook-command", "claude")

    assert result.exit_code == 1
    assert words in result.output, result.output
    assert "--surfacing-timeout-seconds" not in result.output  # never the command's text
    assert daemons.pinged == [] and daemons.shut == []


def test_the_settings_path_is_escaped(tmp_path, monkeypatch, home):
    # A bidi control: valid in a file name on every platform, and not
    # something Click strips the way it strips an ANSI sequence.
    config_dir = tmp_path / "cfg\u202egpj"
    config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    result = _run("status", "--hook-command", "claude")

    assert result.exit_code == 1
    assert "\u202e" not in result.output
    assert "cfg\\u202Egpj" in result.output
    assert "no STM hook command" in result.output
