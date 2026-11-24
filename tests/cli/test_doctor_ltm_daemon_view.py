"""Doctor's LTM row looks each daemon up under the config that daemon runs with (#1485).

A daemon publishes under ``config_fingerprint`` of its own build. The hook builds
from the environment alone, tolerating a proxy-only break
(``load_env_stm_config``), and never sees doctor's ``--config``; the standalone
server builds strictly from the file its own environment names. These tests
stand a fake ``ping`` in for "a daemon is live under fingerprint X" and check
which fingerprint doctor (and health) look under.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from helpers import set_home
from memtomem_stm.cli.proxy import _surfacing_bootstrap_status, cli
from memtomem_stm.config import STMConfig, load_env_stm_config, stm_config_for_cli
from memtomem_stm.daemon.discovery import config_fingerprint
from memtomem_stm.proxy.staged_status import ProbeStage, StagedProbeResult

CONSUMER_MODEL = "MEMTOMEM_STM_PROXY__CONSUMER_MODEL"
GHOST_OVERRIDE = "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GHOST__TIMEOUT"
CONFIG_PATH = "MEMTOMEM_STM_PROXY__CONFIG_PATH"


class LiveDaemons:
    """Fake discovery: a daemon answers only under the fingerprints it published."""

    def __init__(self, *live: str) -> None:
        self.live = set(live)
        self.pinged: list[str | None] = []
        self.requested: list[str | None] = []

    async def ping(self, config: STMConfig, *, timeout: float = 2.0, **_: Any) -> Any:
        fp = config_fingerprint(config, create_key=False)
        self.pinged.append(fp)
        return {"ltm": "warm", "pid": 1, "port": 2} if fp in self.live else None

    async def ltm_request(
        self, config: STMConfig, op: str, payload: dict[str, Any], **_: Any
    ) -> tuple[str, dict[str, Any] | None]:
        fp = config_fingerprint(config, create_key=False)
        self.requested.append(fp)
        if fp not in self.live:
            return "missing", None  # the client's state when no handshake matches
        return "ok", {"ok": True, "outcome": "ok"}


@pytest.fixture
def ghost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Scratch home and data dir, and a proxy file that declares upstream ``ghost``."""
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path / "data"))
    # Never launch an installed LTM server on the direct route.
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND", "__missing_ltm__")
    path = tmp_path / "ghost.json"
    path.write_text(
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
    return path


def _env_b(monkeypatch: pytest.MonkeyPatch) -> None:
    """The issue's environment: a consumer model plus an override of ``ghost``.

    Only a file that declares ``ghost`` builds strictly; the hook's env-only
    build drops every proxy variable and with them the consumer model.
    """
    monkeypatch.setenv(CONSUMER_MODEL, "some-model")
    monkeypatch.setenv(GHOST_OVERRIDE, "5")


def _hook_fp() -> str:
    """The fingerprint a hook-spawned daemon publishes under (its own build)."""
    return config_fingerprint(load_env_stm_config().config, create_key=True)


def _server_fp(monkeypatch: pytest.MonkeyPatch, config_file: Path) -> str:
    """The fingerprint of a standalone server whose environment names *config_file*."""
    with monkeypatch.context() as patch:
        patch.setenv(CONFIG_PATH, str(config_file))
        return config_fingerprint(STMConfig(), create_key=True)


def _install(monkeypatch: pytest.MonkeyPatch, daemons: LiveDaemons) -> None:
    monkeypatch.setattr("memtomem_stm.daemon.client.ping", daemons.ping)
    monkeypatch.setattr("memtomem_stm.daemon.client.ltm_request", daemons.ltm_request)


def _doctor(config_path: Path | None, **kwargs: Any) -> dict[str, Any]:
    return _surfacing_bootstrap_status(
        1.0,
        prefer_hook_daemon=True,
        config_path=None if config_path is None else str(config_path),
        **kwargs,
    )


# --- the hook route --------------------------------------------------------


def test_doctor_finds_the_hook_daemon_under_the_hook_build(ghost, monkeypatch):
    _env_b(monkeypatch)
    hook_fp = _hook_fp()
    daemons = LiveDaemons(hook_fp)
    _install(monkeypatch, daemons)

    status = _doctor(ghost)

    assert status["ltm_server"]["connected"] is True, status
    assert daemons.pinged == [hook_fp]


def _default_file_without_ghost(tmp_path: Path) -> None:
    """A valid default proxy file that does not declare ``ghost``.

    Doctor short-circuits before the LTM row when the default file is missing
    altogether, so the no-``--config`` cases need one.
    """
    default = tmp_path / "home" / ".memtomem" / "stm_proxy.json"
    default.parent.mkdir(parents=True)
    default.write_text(
        json.dumps(
            {
                "enabled": True,
                "upstream_servers": {
                    "other": {"prefix": "ot", "transport": "stdio", "command": "x"}
                },
                "cache": {"tool_annotation_policy": "strict"},
            }
        ),
        encoding="utf-8",
    )


def _assert_strict_build_rejects_ghost() -> None:
    """Positive control: the strict build fails on the undeclared ``ghost`` override."""
    with pytest.raises(ValidationError) as excinfo:
        stm_config_for_cli(None)
    locs = [err["loc"] for err in excinfo.value.errors()]
    assert ("proxy", "upstream_servers", "ghost", "prefix") in locs, locs


def test_doctor_without_config_probes_despite_a_proxy_only_break(ghost, monkeypatch, tmp_path):
    _default_file_without_ghost(tmp_path)
    _env_b(monkeypatch)
    _assert_strict_build_rejects_ghost()
    hook_fp = _hook_fp()
    daemons = LiveDaemons(hook_fp)
    _install(monkeypatch, daemons)

    status = _doctor(None)

    assert status.get("error") is None, status
    assert status["ltm_server"]["connected"] is True, status
    assert daemons.pinged == [hook_fp]


def test_doctor_probes_the_hook_daemon_under_an_out_of_range_proxy_field(ghost, monkeypatch):
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__MAX_DESCRIPTION_CHARS", "5")
    hook_fp = _hook_fp()
    daemons = LiveDaemons(hook_fp)
    _install(monkeypatch, daemons)

    status = _doctor(ghost)

    assert status.get("error") is None, status
    assert status["ltm_server"]["connected"] is True, status


def test_doctor_follows_the_nested_form_of_the_hook_opt_out(ghost, monkeypatch):
    # The hook routes on the parsed ``hook.use_daemon``, which the nested form
    # turns off (#1495), so doctor leaves the hook's daemon alone too.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK", '{"use_daemon": false}')
    assert load_env_stm_config().config.hook.use_daemon is False  # positive control
    hook_fp = _hook_fp()
    daemons = LiveDaemons(hook_fp)
    _install(monkeypatch, daemons)

    status = _doctor(ghost)

    assert status["ltm_server"]["route"] != "daemon", status
    assert daemons.pinged == []


def test_doctor_under_the_nested_opt_out_follows_the_standalone_daemon(ghost, monkeypatch):
    # The hook's route off leaves the standalone view, which still takes its
    # own daemon route when ``surfacing.use_daemon`` is on.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK", '{"use_daemon": false}')
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__USE_DAEMON", "1")
    _env_b(monkeypatch)
    hook_fp, server_fp = _hook_fp(), _server_fp(monkeypatch, ghost)
    assert hook_fp != server_fp  # positive control: the two daemons differ
    daemons = LiveDaemons(hook_fp, server_fp)
    _install(monkeypatch, daemons)

    status = _doctor(ghost)

    assert status["ltm_server"]["route"] == "daemon", status
    assert daemons.pinged == [server_fp]


@pytest.mark.parametrize("gate", ["0", None], ids=["hook-off", "gate-unset"])
def test_doctor_reports_the_error_of_its_own_view_when_the_hook_build_fails(
    ghost, monkeypatch, gate
):
    # A non-proxy variable breaks every build, so the hook's route cannot be
    # resolved. The error doctor renders is the command's own view's, built
    # with ``--config``, which declares ``ghost``: the environment-only build
    # would also report the ``ghost`` override as incomplete.
    if gate is not None:
        monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", gate)
    monkeypatch.setenv(GHOST_OVERRIDE, "5")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__MAX_RESULTS", "nope")
    with pytest.raises(ValidationError) as ambient:
        STMConfig()
    ambient_locs = [err["loc"] for err in ambient.value.errors()]
    assert ("proxy", "upstream_servers", "ghost", "prefix") in ambient_locs  # positive control

    status = _doctor(ghost)

    error = status.get("error")
    assert error is not None, status
    assert "max_results" in error, error
    assert "ghost" not in error, error


def test_doctor_with_both_routes_on_probes_the_hook_daemon(ghost, monkeypatch):
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__USE_DAEMON", "1")
    _env_b(monkeypatch)
    hook_fp, server_fp = _hook_fp(), _server_fp(monkeypatch, ghost)
    assert hook_fp != server_fp  # positive control: the two daemons differ
    daemons = LiveDaemons(hook_fp, server_fp)
    _install(monkeypatch, daemons)

    _doctor(ghost)

    assert daemons.pinged == [hook_fp]


def test_doctor_measures_the_hook_daemon(ghost, monkeypatch):
    _env_b(monkeypatch)
    hook_fp = _hook_fp()
    daemons = LiveDaemons(hook_fp)
    _install(monkeypatch, daemons)

    status = _doctor(ghost, measure_ltm=True)

    measurement = status["ltm_server"]["measurement"]
    assert measurement is not None, status
    assert measurement["completed_samples"] == measurement["requested_samples"] == 5
    assert daemons.requested == [hook_fp] * 5
    assert daemons.pinged == [hook_fp, hook_fp]  # the initial ping and the refresh


def _doctor_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *args: str) -> dict[str, Any]:
    """``mms doctor --json`` with upstream probes stubbed; the checks by id."""
    from memtomem_stm.cli import proxy as proxy_mod

    async def fake_probe_servers(servers, timeout):
        return {n: StagedProbeResult(stage=ProbeStage.TOOLS_DISCOVERED, tools=2) for n in servers}

    monkeypatch.setattr(proxy_mod, "_probe_servers", fake_probe_servers)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    result = CliRunner().invoke(cli, ["doctor", "--json", *args])
    return {c["id"]: c for c in json.loads(result.output)["checks"]}


def test_doctor_cli_ltm_row_passes_for_a_live_hook_daemon(ghost, monkeypatch, tmp_path):
    _env_b(monkeypatch)
    hook_fp = _hook_fp()
    daemons = LiveDaemons(hook_fp)
    _install(monkeypatch, daemons)

    checks = _doctor_cli(monkeypatch, tmp_path, "--config", str(ghost))

    assert checks["ltm"]["status"] == "PASS", checks["ltm"]
    assert daemons.pinged == [hook_fp]


def test_doctor_cli_without_config_reports_the_break_and_the_live_daemon(
    ghost, monkeypatch, tmp_path
):
    _default_file_without_ghost(tmp_path)
    _env_b(monkeypatch)
    _assert_strict_build_rejects_ghost()
    hook_fp = _hook_fp()
    daemons = LiveDaemons(hook_fp)
    _install(monkeypatch, daemons)

    checks = _doctor_cli(monkeypatch, tmp_path)

    # The proxy checks still report the break; the LTM row reports the daemon.
    assert checks["config_schema"]["status"] == "FAIL", checks["config_schema"]
    assert checks["env_overrides"]["status"] == "WARN", checks["env_overrides"]
    assert checks["ltm"]["status"] == "PASS", checks["ltm"]
    assert daemons.pinged == [hook_fp]


# --- routes and views that must not move ------------------------------------


def test_doctor_standalone_route_keeps_the_config_flag_view(ghost, monkeypatch):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "0")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__USE_DAEMON", "1")
    _env_b(monkeypatch)
    server_fp = _server_fp(monkeypatch, ghost)
    assert server_fp != _hook_fp()  # positive control: the env view would miss it
    daemons = LiveDaemons(server_fp)
    _install(monkeypatch, daemons)

    status = _doctor(ghost)

    assert status["ltm_server"]["connected"] is True, status
    assert daemons.pinged == [server_fp]


def test_health_with_both_routes_on_probes_the_standalone_daemon(ghost, monkeypatch):
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__USE_DAEMON", "1")
    _env_b(monkeypatch)
    hook_fp, server_fp = _hook_fp(), _server_fp(monkeypatch, ghost)
    daemons = LiveDaemons(hook_fp, server_fp)
    _install(monkeypatch, daemons)

    _surfacing_bootstrap_status(1.0, config_path=str(ghost))

    assert daemons.pinged == [server_fp]


def test_doctor_hook_route_with_surfacing_disabled_pings_nothing(ghost, monkeypatch):
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "false")
    daemons = LiveDaemons(_hook_fp())
    _install(monkeypatch, daemons)

    status = _doctor(ghost)

    assert status["ltm_server"]["skipped"] == "surfacing_disabled", status
    assert daemons.pinged == []


def test_without_a_break_the_hook_view_and_the_flag_view_agree(ghost, monkeypatch):
    monkeypatch.setenv(CONSUMER_MODEL, "some-model")
    hook_fp = _hook_fp()
    assert hook_fp == config_fingerprint(stm_config_for_cli(ghost), create_key=False)
    daemons = LiveDaemons(hook_fp)
    _install(monkeypatch, daemons)

    status = _doctor(ghost)

    assert status["ltm_server"]["connected"] is True, status


@pytest.mark.parametrize(
    ("env", "expected", "next_action"),
    [
        (
            {"MEMTOMEM_STM_HOOK__USE_DAEMON": "0"},
            "needs a daemon route",
            "MEMTOMEM_STM_SURFACING__USE_DAEMON=true",
        ),
        # The nested opt-out leaves the flat variable unset, so the advice
        # must name the payload too (#1495).
        (
            {"MEMTOMEM_STM_HOOK": '{"use_daemon": false}'},
            "needs a daemon route",
            "use_daemon in MEMTOMEM_STM_HOOK",
        ),
        ({}, "requires a running daemon", "mms daemon start"),
    ],
    ids=["flat-opt-out", "nested-opt-out", "hook-route"],
)
def test_doctor_measurement_warning_names_the_missing_piece(
    ghost, monkeypatch, tmp_path, env, expected, next_action
):
    # Direct route (hook daemon off, no standalone daemon), or the hook's route
    # with no daemon running under its fingerprint.
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _install(monkeypatch, LiveDaemons())

    checks = _doctor_cli(monkeypatch, tmp_path, "--config", str(ghost), "--measure-ltm")

    measurement = checks["ltm_measurement"]
    assert measurement["status"] == "WARN", measurement
    assert expected in measurement["detail"], measurement
    assert next_action in measurement["next_action"], measurement


@pytest.mark.parametrize("hook_daemon", [None, "0"], ids=["hook-route", "direct-route"])
def test_doctor_measurement_warning_when_surfacing_is_disabled(
    ghost, monkeypatch, tmp_path, hook_daemon
):
    # Either probe skips before any ping, so the row must not claim the daemon
    # is down, nor ask for a daemon route.
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "false")
    if hook_daemon is not None:
        monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", hook_daemon)
    daemons = LiveDaemons(_hook_fp())
    _install(monkeypatch, daemons)

    checks = _doctor_cli(monkeypatch, tmp_path, "--config", str(ghost), "--measure-ltm")

    measurement = checks["ltm_measurement"]
    assert measurement["status"] == "WARN", measurement
    assert "surfacing is disabled" in measurement["detail"], measurement
    assert "mms daemon start" not in (measurement["next_action"] or ""), measurement
    assert daemons.pinged == []


def test_doctor_measurement_warning_when_the_bootstrap_fails(ghost, monkeypatch, tmp_path):
    # A non-proxy break fails the hook's build too, so no route was chosen.
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__MAX_RESULTS", "nope")
    daemons = LiveDaemons()
    _install(monkeypatch, daemons)

    checks = _doctor_cli(monkeypatch, tmp_path, "--config", str(ghost), "--measure-ltm")

    assert "invalid configuration" in checks["ltm"]["detail"], checks["ltm"]
    measurement = checks["ltm_measurement"]
    assert measurement["status"] == "WARN", measurement
    assert "result unavailable" in measurement["detail"], measurement
    assert "daemon route" not in measurement["detail"], measurement
    assert daemons.pinged == []
