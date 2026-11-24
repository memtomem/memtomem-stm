"""Doctor looks the hook's daemon up with the registered hook command's options (#1494).

``mms hook install`` writes runtime flags into the hook command, and the hook
applies them over its environment before it builds its config
(``runtime_env_overrides``). Two of them (``--surfacing-timeout-seconds``,
``--[no-]persist-query-text``) enter the daemon fingerprint, and ``--no-daemon``
decides the route. Doctor reads only a handler install owns
(``hook_hosts.owned_handler``, #1499). These tests write a
Claude Code user settings file, stand a fake ``ping`` in for "a daemon is live
under fingerprint X" (as ``test_doctor_ltm_daemon_view`` does), and check which
fingerprint doctor looks under and what the row says about it.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from helpers import set_home
from memtomem_stm.cli.host_runtime import HostRuntimePolicy, hook_command_words, hook_launcher
from memtomem_stm.cli.proxy import _surfacing_bootstrap_status, cli
from memtomem_stm.config import STMConfig, load_env_stm_config
from memtomem_stm.daemon.discovery import config_fingerprint
from memtomem_stm.proxy.staged_status import ProbeStage, StagedProbeResult

PERSIST = "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT"
SURFACING_TIMEOUT = "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS"
HOOK_GATE = "MEMTOMEM_STM_HOOK__USE_DAEMON"
STANDALONE_GATE = "MEMTOMEM_STM_SURFACING__USE_DAEMON"
CONFIG_PATH = "MEMTOMEM_STM_PROXY__CONFIG_PATH"
FIRST = "hooks.PostToolUse[0].hooks[0]"
SECOND = "hooks.PostToolUse[0].hooks[1]"


class LiveDaemons:
    """Fake discovery: a daemon answers only under the fingerprints it published."""

    def __init__(self, *live: str) -> None:
        self.live = set(live)
        self.pinged: list[str | None] = []

    async def ping(self, config: STMConfig, *, timeout: float = 2.0, **_: Any) -> Any:
        fp = config_fingerprint(config, create_key=False)
        self.pinged.append(fp)
        return {"ltm": "warm", "pid": 1, "port": 2} if fp in self.live else None


@pytest.fixture(autouse=True)
def posix_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read commands with the POSIX rules on every runner; the Windows rules
    have their own test (``test_windows_commands_are_not_read``)."""
    monkeypatch.setattr("memtomem_stm.cli.hook_registration.running_on_windows", lambda: False)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path / "data"))
    # Never launch an installed LTM server on the direct route.
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND", "__missing_ltm__")
    return home


@pytest.fixture
def proxy_file(tmp_path: Path) -> Path:
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "upstream_servers": {"up": {"prefix": "up", "transport": "stdio", "command": "x"}},
                "cache": {"tool_annotation_policy": "strict"},
            }
        ),
        encoding="utf-8",
    )
    return path


def _settings(home: Path, *handlers: dict[str, Any]) -> Path:
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": list(handlers)}]}}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _cmd(text: str) -> dict[str, Any]:
    return {"type": "command", "command": text}


def _installed(
    *,
    use_daemon: bool = True,
    surfacing: float | None = None,
    daemon: float | None = None,
) -> str:
    """The command ``mms hook install --host claude`` writes, joined the way the
    writer joins it: by default a fresh install's (the settings defaults for the
    deadlines, query text not persisted)."""
    defaults = STMConfig()
    policy = HostRuntimePolicy(
        use_daemon=use_daemon,
        surfacing_timeout_seconds=(
            defaults.surfacing.timeout_seconds if surfacing is None else surfacing
        ),
        daemon_timeout_seconds=defaults.hook.daemon_timeout_seconds if daemon is None else daemon,
    )
    return shlex.join(hook_command_words(hook_launcher(), "claude", policy.hook_args()))


def _fp(monkeypatch: pytest.MonkeyPatch, **env: str) -> str:
    """The fingerprint of the env-only build under doctor's environment plus *env*
    (how the hook and a daemon it spawns build)."""
    with monkeypatch.context() as patch:
        for key, value in env.items():
            patch.setenv(key, value)
        return config_fingerprint(load_env_stm_config().config, create_key=True)


def _bare_fp(monkeypatch: pytest.MonkeyPatch) -> str:
    return _fp(monkeypatch)


def _installed_fp(monkeypatch: pytest.MonkeyPatch) -> str:
    return _fp(monkeypatch, **{PERSIST: "false"})


def _install(monkeypatch: pytest.MonkeyPatch, daemons: LiveDaemons) -> None:
    monkeypatch.setattr("memtomem_stm.daemon.client.ping", daemons.ping)


def _doctor(config_path: Path | None = None, **kwargs: Any) -> dict[str, Any]:
    return _surfacing_bootstrap_status(
        1.0,
        prefer_hook_daemon=True,
        config_path=None if config_path is None else str(config_path),
        **kwargs,
    )


def _doctor_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *args: str
) -> tuple[dict[str, Any], str]:
    """``mms doctor --json`` with upstream probes stubbed: checks by id, raw output."""
    from memtomem_stm.cli import proxy as proxy_mod

    async def fake_probe_servers(servers, timeout):
        return {n: StagedProbeResult(stage=ProbeStage.TOOLS_DISCOVERED, tools=2) for n in servers}

    monkeypatch.setattr(proxy_mod, "_probe_servers", fake_probe_servers)
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    monkeypatch.chdir(project)
    result = CliRunner().invoke(cli, ["doctor", "--json", *args])
    return {c["id"]: c for c in json.loads(result.output)["checks"]}, result.output


# --- the issue's cases -------------------------------------------------------


def test_doctor_finds_the_daemon_of_an_ordinarily_installed_hook(home, monkeypatch):
    _settings(home, _cmd(_installed()))
    bare_fp, hook_fp = _bare_fp(monkeypatch), _installed_fp(monkeypatch)
    assert bare_fp != hook_fp  # positive control: the issue's two fingerprints
    daemons = LiveDaemons(hook_fp)
    _install(monkeypatch, daemons)

    status = _doctor()

    assert status["ltm_server"]["connected"] is True, status
    assert daemons.pinged == [hook_fp]
    view = status["hook_view"]
    assert (view["state"], view["locations"], view["hook_route"]) == ("found", [FIRST], True)


def test_a_no_daemon_hook_sends_doctor_to_the_standalone_route(home, tmp_path, monkeypatch):
    # #1491's proxy-only break: the file declares ``ghost``, the environment
    # overrides it and sets a consumer model. The strict ``--config`` build
    # keeps the consumer model; the hook's env-only build drops it.
    ghost = tmp_path / "ghost.json"
    ghost.write_text(
        json.dumps(
            {
                "enabled": True,
                "upstream_servers": {
                    "ghost": {"prefix": "gh", "transport": "stdio", "command": "x"}
                },
                "cache": {"tool_annotation_policy": "strict"},
            }
        ),
        encoding="utf-8",
    )
    _settings(home, _cmd(_installed(use_daemon=False)))  # before the break: reads defaults
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__CONSUMER_MODEL", "some-model")
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GHOST__TIMEOUT", "5")
    monkeypatch.setenv(STANDALONE_GATE, "1")
    with monkeypatch.context() as patch:
        patch.setenv(CONFIG_PATH, str(ghost))
        server_fp = config_fingerprint(STMConfig(), create_key=True)
    bare_fp, hook_fp = _bare_fp(monkeypatch), _installed_fp(monkeypatch)
    assert len({server_fp, bare_fp, hook_fp}) == 3  # positive control
    daemons = LiveDaemons(bare_fp, hook_fp, server_fp)
    _install(monkeypatch, daemons)

    status = _doctor(ghost)

    assert daemons.pinged == [server_fp]
    view = status["hook_view"]
    assert (view["state"], view["hook_route"], view["gate_from_command"]) == (
        "found",
        False,
        True,
    )


def test_the_standalone_route_gets_no_hook_option(home, proxy_file, monkeypatch):
    # ``--surfacing-timeout-seconds`` with ``--no-daemon``: the standalone build
    # must not see the hook's timeout, which would change its fingerprint.
    monkeypatch.setenv(STANDALONE_GATE, "1")
    _settings(home, _cmd(_installed(use_daemon=False, surfacing=7.0)))
    with monkeypatch.context() as patch:
        patch.setenv(CONFIG_PATH, str(proxy_file))
        server_fp = config_fingerprint(STMConfig(), create_key=True)
        patch.setenv(SURFACING_TIMEOUT, "7")
        leaked_fp = config_fingerprint(STMConfig(), create_key=True)
    assert server_fp != leaked_fp  # positive control
    daemons = LiveDaemons(server_fp)
    _install(monkeypatch, daemons)

    _doctor(proxy_file)

    assert daemons.pinged == [server_fp]


def test_cli_row_names_the_command_it_read(home, proxy_file, monkeypatch, tmp_path):
    path = _settings(home, _cmd(_installed()))
    daemons = LiveDaemons(_installed_fp(monkeypatch))
    _install(monkeypatch, daemons)

    checks, _ = _doctor_cli(monkeypatch, tmp_path, "--config", str(proxy_file))

    row = checks["ltm"]
    assert row["status"] == "PASS", row
    expected = (
        f"route: the hook's daemon, with the options of the STM hook command at {FIRST} in {path}"
    )
    assert expected in row["detail"], row
    assert row["detail"].endswith("(Claude Code user settings only)"), row


def test_cli_row_names_the_command_that_turned_the_hook_daemon_off(
    home, proxy_file, monkeypatch, tmp_path
):
    _settings(home, _cmd(_installed(use_daemon=False)))
    _install(monkeypatch, LiveDaemons())

    checks, _ = _doctor_cli(monkeypatch, tmp_path, "--config", str(proxy_file))

    detail = checks["ltm"]["detail"]
    assert (
        f"route: the direct LTM probe, hook's daemon off by the STM hook command at {FIRST}"
        in detail
    ), detail


def test_cli_row_names_doctors_environment_when_it_turned_the_hook_daemon_off(
    home, proxy_file, monkeypatch, tmp_path
):
    monkeypatch.setenv(HOOK_GATE, "0")
    monkeypatch.setenv(STANDALONE_GATE, "1")
    _install(monkeypatch, LiveDaemons())

    checks, _ = _doctor_cli(monkeypatch, tmp_path, "--config", str(proxy_file))

    detail = checks["ltm"]["detail"]
    assert (
        "route: the standalone daemon route, hook's daemon off in doctor's environment" in detail
    ), detail
    assert "no STM hook command in" in detail, detail


def test_a_use_daemon_flag_turns_the_route_on_over_an_ambient_opt_out(home, monkeypatch):
    monkeypatch.setenv(HOOK_GATE, "0")
    _settings(home, _cmd(_installed()))
    hook_fp = _installed_fp(monkeypatch)
    daemons = LiveDaemons(hook_fp)
    _install(monkeypatch, daemons)

    status = _doctor()

    assert status["ltm_server"]["route"] == "daemon", status
    assert daemons.pinged == [hook_fp]
    assert status["hook_view"]["hook_route"] is True


def test_the_hook_deadline_comes_from_the_command(home, monkeypatch):
    # ``hook.daemon_timeout_seconds`` is not in the fingerprint, so only the
    # timeout checks' input shows whether the flag was applied.
    default = STMConfig().hook.daemon_timeout_seconds
    _settings(home, _cmd(_installed(surfacing=3.0, daemon=9.5)))
    assert default != 9.5  # positive control
    _install(monkeypatch, LiveDaemons(_bare_fp(monkeypatch)))

    status = _doctor()

    assert status["timeouts"]["hook_daemon_seconds"] == 9.5, status


# --- what the hook builds from its command ------------------------------------


def test_an_owned_command_without_settings_adds_nothing(home, monkeypatch):
    # The form releases 0.1.29-0.1.38 wrote: the hook then runs with its
    # environment, so doctor's view is the bare one, read from a found command.
    _settings(home, _cmd(shlex.join(hook_command_words(hook_launcher(), "claude", ()))))
    bare_fp = _bare_fp(monkeypatch)
    daemons = LiveDaemons(bare_fp)
    _install(monkeypatch, daemons)

    status = _doctor()

    assert daemons.pinged == [bare_fp]
    assert (status["hook_view"]["state"], status["hook_view"]["gate_from_command"]) == (
        "found",
        False,
    )


def test_a_uv_launcher_install_wrote_is_read(home, monkeypatch, tmp_path):
    policy = HostRuntimePolicy(
        use_daemon=True, surfacing_timeout_seconds=3.0, daemon_timeout_seconds=4.0
    )
    launcher = hook_launcher(str(tmp_path / "a checkout"))
    _settings(home, _cmd(shlex.join(hook_command_words(launcher, "claude", policy.hook_args()))))
    hook_fp = _fp(monkeypatch, **{PERSIST: "false", SURFACING_TIMEOUT: "3"})
    assert hook_fp != _bare_fp(monkeypatch)  # positive control
    daemons = LiveDaemons(hook_fp)
    _install(monkeypatch, daemons)

    _doctor()

    assert daemons.pinged == [hook_fp]


@pytest.mark.parametrize(
    "command",
    [
        f"env {PERSIST}=false memtomem-stm hook --host claude",
        "mms hook --host claude --no-persist-query-text",
        "memtomem-stm hook --host claude --no-persist-query-text",
        "memtomem-stm hook --host codex --use-daemon --surfacing-timeout-seconds 2 "
        "--daemon-timeout-seconds 3 --no-persist-query-text",
    ],
    ids=["env-prefix", "hand-written-mms", "flags-left-out", "another-host"],
)
def test_a_command_install_did_not_write_is_not_read(home, monkeypatch, command):
    # Each may run the hook with query text off, but install did not write it,
    # so what it runs with is not read from it (#1499).
    _settings(home, _cmd(command))
    bare_fp = _bare_fp(monkeypatch)
    assert bare_fp != _installed_fp(monkeypatch)  # positive control
    daemons = LiveDaemons(bare_fp)
    _install(monkeypatch, daemons)

    status = _doctor()

    assert daemons.pinged == [bare_fp]
    assert (status["hook_view"]["state"], status["hook_view"]["locations"]) == (
        "unreadable",
        [FIRST],
    )


# --- which commands count -----------------------------------------------------


@pytest.mark.parametrize(
    ("other", "state"),
    [
        (_cmd("__INSTALLED__"), "several"),
        ({"type": "command", "command": "mms", "args": ["hook", "--no-daemon"]}, "unreadable"),
        ({"type": "command", "command": "__INSTALLED__", "args": []}, "unreadable"),
        ({"type": "command", "command": "__INSTALLED__", "shell": "powershell"}, "unreadable"),
        (_cmd(f"{HOOK_GATE}=false mms hook --host claude"), "unreadable"),
        (_cmd("mms hook --host claude --surfacing-timeout-seconds 3 # pinned"), "unreadable"),
    ],
    ids=["two-recognized", "exec-form", "args-field", "shell-field", "bare-assignment", "comment"],
)
def test_a_second_handler_that_may_run_the_hook_keeps_the_bare_view(
    home, monkeypatch, other, state
):
    installed = _installed()
    if other.get("command") == "__INSTALLED__":
        other = {**other, "command": installed}
    _settings(home, _cmd(installed), other)
    bare_fp = _bare_fp(monkeypatch)
    assert bare_fp != _installed_fp(monkeypatch)  # positive control
    daemons = LiveDaemons(bare_fp)
    _install(monkeypatch, daemons)

    status = _doctor()

    assert daemons.pinged == [bare_fp]
    assert status["hook_view"]["state"] == state
    # ``several`` names both; ``unreadable`` names only the one it could not read.
    expected = [FIRST, SECOND] if state == "several" else [SECOND]
    assert status["hook_view"]["locations"] == expected


@pytest.mark.parametrize(
    "other",
    [
        {"type": "prompt", "command": "mms hook --host claude --no-daemon"},
        _cmd("mms hook --help"),
        {"type": "command", "command": "mms", "args": ["hook", "--help"]},
        _cmd("mms hook install --host claude"),
        _cmd("echo hello"),
        "not a handler",
    ],
    ids=[
        "not-a-command-handler",
        "help",
        "exec-form-help",
        "subcommand",
        "unrelated",
        "not-a-dict",
    ],
)
def test_a_handler_that_cannot_run_the_bridge_is_ignored(home, monkeypatch, other):
    _settings(home, _cmd(_installed()), other)
    hook_fp = _installed_fp(monkeypatch)
    daemons = LiveDaemons(hook_fp)
    _install(monkeypatch, daemons)

    status = _doctor()

    assert daemons.pinged == [hook_fp]
    assert (status["hook_view"]["state"], status["hook_view"]["locations"]) == ("found", [FIRST])


def test_unreadable_settings_keep_the_bare_view(home, monkeypatch):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("[1]", encoding="utf-8")
    bare_fp = _bare_fp(monkeypatch)
    daemons = LiveDaemons(bare_fp)
    _install(monkeypatch, daemons)

    status = _doctor()

    assert daemons.pinged == [bare_fp]
    assert status["hook_view"]["state"] == "settings_error"
    assert "not an object" in status["hook_view"]["settings_error"]


def test_no_settings_file_keeps_the_bare_view(home, monkeypatch):
    bare_fp = _bare_fp(monkeypatch)
    daemons = LiveDaemons(bare_fp)
    _install(monkeypatch, daemons)

    status = _doctor()

    assert daemons.pinged == [bare_fp]
    assert status["hook_view"]["state"] == "none"


def test_windows_commands_are_not_read(home, monkeypatch):
    monkeypatch.setattr("memtomem_stm.cli.hook_registration.running_on_windows", lambda: True)
    _settings(home, _cmd(_installed()))
    bare_fp = _bare_fp(monkeypatch)
    daemons = LiveDaemons(bare_fp)
    _install(monkeypatch, daemons)

    status = _doctor()

    assert daemons.pinged == [bare_fp]
    assert (status["hook_view"]["state"], status["hook_view"]["locations"]) == (
        "windows",
        [FIRST],
    )


def test_health_reads_no_hook_command(home, proxy_file, monkeypatch):
    _settings(home, _cmd(_installed()))
    _install(monkeypatch, LiveDaemons())

    reads: list[None] = []

    def spy() -> Any:
        reads.append(None)
        raise AssertionError("health must not read the hook registration")

    monkeypatch.setattr("memtomem_stm.cli.hook_hosts.claude_post_tool_handlers", spy)

    status = _surfacing_bootstrap_status(1.0, config_path=str(proxy_file))

    assert reads == []
    assert "hook_view" not in status


@pytest.mark.parametrize("doctor", [False, True], ids=["health", "doctor-hook-route"])
def test_a_feedback_db_inspection_failure_ends_the_section_before_any_probe(
    home, proxy_file, monkeypatch, doctor
):
    # A probe can launch an LTM server, so it must not run when the section is
    # going to report an error anyway.
    _settings(home, _cmd(_installed()))
    probes: list[None] = []

    def failing_inspection(path: Any) -> Any:
        raise ValueError("embedded null byte")

    def probe(*args: Any, **kwargs: Any) -> Any:
        probes.append(None)
        return {}

    monkeypatch.setattr(
        "memtomem_stm.surfacing.feedback_store.inspect_feedback_db", failing_inspection
    )
    monkeypatch.setattr("memtomem_stm.cli.proxy._ltm_status", probe)

    status = _surfacing_bootstrap_status(
        1.0, prefer_hook_daemon=doctor, config_path=str(proxy_file)
    )

    assert status["error"] == "ValueError", status
    assert probes == []


# --- the overlay leaves nothing behind ----------------------------------------


def test_the_overlay_is_restored_after_a_probe(home, monkeypatch):
    _settings(home, _cmd(_installed()))
    _install(monkeypatch, LiveDaemons(_installed_fp(monkeypatch)))
    before = dict(os.environ)

    status = _doctor()

    assert status["ltm_server"]["connected"] is True, status
    assert dict(os.environ) == before


def test_the_overlay_is_restored_when_the_probe_raises(home, monkeypatch):
    _settings(home, _cmd(_installed()))
    seen: list[str | None] = []

    def failing_status(*args: Any, **kwargs: Any) -> Any:
        seen.append(os.environ.get(PERSIST))
        raise RuntimeError("boom")

    monkeypatch.setattr("memtomem_stm.cli.proxy._ltm_status", failing_status)
    before = dict(os.environ)

    status = _doctor()

    assert seen == ["false"]  # positive control: the overlay was in effect
    assert status["error"] == "RuntimeError", status
    assert dict(os.environ) == before


def test_applied_env_restores_what_it_set_when_a_later_value_fails(monkeypatch):
    monkeypatch.delenv("MEMTOMEM_STM_TEST_FIRST", raising=False)
    monkeypatch.setenv("MEMTOMEM_STM_TEST_SECOND", "kept")
    from memtomem_stm.cli.proxy import _applied_env

    values: dict[str, str] = {"MEMTOMEM_STM_TEST_FIRST": "set", "MEMTOMEM_STM_TEST_SECOND": "x"}
    real_setitem = type(os.environ).__setitem__

    def setitem(self: Any, key: str, value: str) -> None:
        if key == "MEMTOMEM_STM_TEST_SECOND" and value == "x":
            raise UnicodeEncodeError("utf-8", value, 0, 1, "refused")
        real_setitem(self, key, value)

    monkeypatch.setattr(type(os.environ), "__setitem__", setitem)
    with pytest.raises(UnicodeEncodeError):
        with _applied_env(values):
            pass  # pragma: no cover - the apply fails first
    monkeypatch.setattr(type(os.environ), "__setitem__", real_setitem)

    assert "MEMTOMEM_STM_TEST_FIRST" not in os.environ
    assert os.environ["MEMTOMEM_STM_TEST_SECOND"] == "kept"


# --- nothing from the command reaches output ----------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "env MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND=SENTINEL-7f3 mms hook --host claude",
        "mms hook --host claude --surfacing-timeout-seconds SENTINEL-7f3",
    ],
    ids=["env-prefix", "flag-value"],
)
def test_command_values_stay_out_of_doctor_output(
    home, proxy_file, monkeypatch, tmp_path, caplog, command
):
    _settings(home, _cmd(command))
    _install(monkeypatch, LiveDaemons())
    caplog.set_level(logging.DEBUG)

    checks, output = _doctor_cli(monkeypatch, tmp_path, "--config", str(proxy_file))

    assert "ltm" in checks
    assert "SENTINEL" not in output
    assert "SENTINEL" not in caplog.text
