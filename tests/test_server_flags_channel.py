"""The MCP server applies its registration's runtime policy as a settings layer (#1544).

``mms register`` / ``mms init`` write the policy into the registration's
environment. As flat variables alone it lost to two forms of the parent block:
the field repeated under another letter case in the block's JSON, and a
``null`` block (which pydantic-settings drops with its flat children). The
registration also carries ``MEMTOMEM_STM_SERVER_FLAGS``, which ``server.main``
applies through ``host_runtime.hook_flags_scope`` — the layer the hook uses
(#1537) — so the server and the hook build the same configuration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from memtomem_stm.cli.host_runtime import HostRuntimePolicy, hook_flags_scope
from memtomem_stm.config import STMConfig, load_env_stm_config

CHANNEL = "MEMTOMEM_STM_SERVER_FLAGS"
PERSIST = "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT"
TIMEOUT = "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS"
USE_DAEMON = "MEMTOMEM_STM_SURFACING__USE_DAEMON"

POLICY = HostRuntimePolicy(
    use_daemon=True, surfacing_timeout_seconds=7.25, daemon_timeout_seconds=9.0
)
"""A non-default timeout, so a build that read the default fails the check."""

_FORMS = {
    "null-block": "null",
    # Each field twice, the upper-case key last with values unlike the policy's:
    # the later key wins pydantic-settings' fold.
    "json-case-variant": json.dumps(
        {
            "persist_query_text": True,
            "PERSIST_QUERY_TEXT": True,
            "timeout_seconds": 1.5,
            "TIMEOUT_SECONDS": 1.5,
            "use_daemon": False,
            "USE_DAEMON": False,
        }
    ),
}
_EACH_FORM = pytest.mark.parametrize("form", list(_FORMS))


def _managed(config: STMConfig) -> tuple[bool, float, bool]:
    s = config.surfacing
    return (s.persist_query_text, s.timeout_seconds, s.use_daemon)


_POLICY_READ = (False, 7.25, True)


@pytest.fixture
def registration_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """The environment a host starts a new registration's server with, proxy
    off so the lifespan starts no upstream."""
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__ENABLED", "false")
    for name, value in POLICY.mcp_env().items():
        monkeypatch.setenv(name, value)
    return monkeypatch


@pytest.fixture
def _restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        yield
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


def _run_main(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """``server.main()`` with ``mcp.run`` standing in by entering the real
    lifespan under ``anyio.run``. Returns the startup build and the
    lifespan's."""
    from memtomem_stm import server

    seen: dict[str, Any] = {}

    def capture_logging(config: STMConfig) -> None:
        seen["startup"] = config

    async def drive() -> None:
        async with server.app_lifespan(server.mcp) as ctx:
            seen["lifespan"] = ctx.config

    monkeypatch.setattr(server, "configure_server_logging", capture_logging)
    monkeypatch.setattr(server.mcp, "run", lambda: anyio.run(drive))
    server.main()
    return seen


@_EACH_FORM
def test_the_server_builds_the_registrations_policy(registration_env, form, _restore_root_logging):
    registration_env.setenv("MEMTOMEM_STM_SURFACING", _FORMS[form])

    seen = _run_main(registration_env)

    assert _managed(seen["startup"]) == _POLICY_READ
    assert _managed(seen["lifespan"]) == _POLICY_READ


@_EACH_FORM
def test_without_the_channel_the_block_still_outranks_the_flat_variables(
    registration_env, form, _restore_root_logging
):
    """Control: the flat variables alone lose to each form, so the check above
    can fail; and ``main()`` opens no scope of its own."""
    registration_env.setenv("MEMTOMEM_STM_SURFACING", _FORMS[form])
    registration_env.delenv(CHANNEL, raising=False)

    seen = _run_main(registration_env)

    assert _managed(seen["startup"]) != _POLICY_READ
    assert _managed(seen["lifespan"]) == _managed(seen["startup"])


def test_main_without_the_channel_keeps_an_enclosing_scope(registration_env, _restore_root_logging):
    """The flat variable says ``true`` and the enclosing scope ``false``: a
    ``main()`` that cleared the scope would read ``true``."""
    registration_env.delenv(CHANNEL, raising=False)
    registration_env.setenv(PERSIST, "true")
    with hook_flags_scope({PERSIST: "false"}):
        seen = _run_main(registration_env)
    assert seen["startup"].surfacing.persist_query_text is False
    assert seen["lifespan"].surfacing.persist_query_text is False


def test_the_channel_stays_in_the_environment(registration_env, _restore_root_logging):
    before = os.environ[CHANNEL]
    _run_main(registration_env)
    assert os.environ[CHANNEL] == before


@pytest.mark.parametrize("form", list(_FORMS))
def test_a_daemon_the_server_spawns_builds_the_hooks_configuration(registration_env, form):
    """The server spawns its daemon from a worker thread
    (``daemon_adapter``'s ``asyncio.to_thread``); the flags reach the child in
    ``MEMTOMEM_STM_HOOK_FLAGS``, and its fingerprint is the hook's daemon's."""
    from helpers import detached_daemon_config

    from memtomem_stm.cli.hook_cmd import _runtime_registration_env
    from memtomem_stm.cli.host_runtime import (
        SERVER_FLAGS_CHANNEL_ENV,
        _hook_overlay,
        decode_server_flags_channel,
    )
    from memtomem_stm.daemon.discovery import config_fingerprint

    registration_env.setenv("MEMTOMEM_STM_SURFACING", _FORMS[form])
    flags = decode_server_flags_channel(os.environ[SERVER_FLAGS_CHANNEL_ENV])

    async def from_a_worker() -> Any:
        return await asyncio.to_thread(detached_daemon_config)

    with hook_flags_scope(flags):
        server_config = STMConfig()
        server_daemon = asyncio.run(from_a_worker())
    hook_overlay = _hook_overlay(POLICY.hook_args())
    with _runtime_registration_env(hook_overlay):
        hook_daemon = detached_daemon_config()
    hook_config = load_env_stm_config(hook_overlay).config

    assert _managed(server_config) == _POLICY_READ
    assert _managed(server_daemon) == _POLICY_READ
    prints = [
        config_fingerprint(c, create_key=True)
        for c in (server_config, server_daemon, hook_daemon, hook_config)
    ]
    assert None not in prints
    assert len(set(prints)) == 1


def test_a_spawned_daemon_is_not_handed_the_server_channel(monkeypatch):
    from memtomem_stm.daemon.spawn import _child_env

    monkeypatch.setenv(CHANNEL, json.dumps({PERSIST: "false", TIMEOUT: "3", USE_DAEMON: "true"}))
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", "/nonexistent")  # control: others pass

    env = _child_env()

    assert CHANNEL not in env
    assert env["MEMTOMEM_STM_DATA_DIR"] == "/nonexistent"


_BAD = {
    "not-json": "{",
    "not-an-object": "[]",
    "empty": "{}",
    "missing-a-key": json.dumps({PERSIST: "false", TIMEOUT: "3"}),
    "unknown-name": json.dumps(
        {PERSIST: "false", TIMEOUT: "3", USE_DAEMON: "true", "MEMTOMEM_STM_HOOK__USE_DAEMON": "x"}
    ),
    "not-a-string": json.dumps({PERSIST: False, TIMEOUT: "3", USE_DAEMON: "true"}),
    "not-json-with-a-secret": '{"sk-SENTINEL-1544": ',
}


def _duplicate_persist() -> str:
    body = json.dumps({TIMEOUT: "3", USE_DAEMON: "true"})[:-1]
    return body + f', "{PERSIST}": "false", "{PERSIST}": "true"}}'


@pytest.mark.parametrize(
    "raw", [*_BAD.values(), _duplicate_persist()], ids=[*_BAD, "duplicate-key"]
)
def test_a_channel_the_registration_did_not_write_stops_the_server(
    registration_env, raw, capsys, _restore_root_logging
):
    from memtomem_stm import server

    registration_env.setenv(CHANNEL, raw)
    ran: list[bool] = []
    registration_env.setattr(server.mcp, "run", lambda: ran.append(True))

    with pytest.raises(SystemExit) as exit_:
        server.main()
    out, err = capsys.readouterr()

    assert exit_.value.code == 1
    assert ran == []
    assert out == ""
    [line] = err.splitlines()
    assert CHANNEL in line
    assert "SENTINEL" not in err
    if raw not in ("{", "[]", "{}"):
        assert raw not in err
        assert '"true"' not in err


def test_a_rejected_channel_value_names_the_channel_and_its_key(
    registration_env, capsys, _restore_root_logging
):
    from memtomem_stm import server

    flags = {PERSIST: "false", TIMEOUT: "-4", USE_DAEMON: "true"}
    registration_env.setenv(CHANNEL, json.dumps(flags))

    with pytest.raises(SystemExit):
        server.main()
    err = capsys.readouterr().err

    [line] = [ln for ln in err.splitlines() if "environment configuration" in ln]
    assert f"{CHANNEL}[{TIMEOUT}]" in line
    assert f" {TIMEOUT}" not in line.replace(f"{CHANNEL}[{TIMEOUT}]", "")
    assert "-4" not in line


def test_another_error_keeps_the_environments_attribution(
    registration_env, capsys, _restore_root_logging
):
    from memtomem_stm import server

    registration_env.setenv("MEMTOMEM_STM_PROXY__CACHE__ENABLED", "nope")

    with pytest.raises(SystemExit):
        server.main()
    err = capsys.readouterr().err

    [line] = [ln for ln in err.splitlines() if "environment configuration" in ln]
    assert "MEMTOMEM_STM_PROXY__CACHE__ENABLED" in line
    assert CHANNEL not in line


def test_a_model_level_error_names_the_channels_keys_by_the_channel(
    registration_env, capsys, _restore_root_logging
):
    """``surfacing`` fails as a whole (an auto-tune floor above min_score): the
    variable that caused it is named as set, the channel's keys under the
    model by the channel, and no flat spelling the build did not read."""
    from memtomem_stm import server

    registration_env.setenv("MEMTOMEM_STM_SURFACING__AUTO_TUNE_SCORE_FLOOR", "0.9")

    with pytest.raises(SystemExit):
        server.main()
    err = capsys.readouterr().err

    [line] = [ln for ln in err.splitlines() if "environment configuration" in ln]
    assert "MEMTOMEM_STM_SURFACING__AUTO_TUNE_SCORE_FLOOR" in line
    for name in (PERSIST, TIMEOUT, USE_DAEMON):
        assert f"{CHANNEL}[{name}]" in line
        assert line.count(name) == 1  # only inside the channel's label


def test_the_registration_writes_the_channel_with_the_flat_values():
    from memtomem_stm.cli.host_runtime import decode_server_flags_channel

    env = POLICY.mcp_env()
    flat = {name: value for name, value in env.items() if name != CHANNEL}

    assert set(flat) == {PERSIST, TIMEOUT, USE_DAEMON}
    assert decode_server_flags_channel(env[CHANNEL]) == flat
