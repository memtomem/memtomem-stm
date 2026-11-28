"""Nothing the environment gives a field outranks ``mms hook``'s flag for it.

POSIX lets ``MEMTOMEM_STM_X`` and ``memtomem_stm_x`` both be set, and
pydantic-settings folds names to lower case, so the last spelling in the
environment wins (#1503). The block's JSON can also repeat the field under
another letter case, or the block can be ``null`` (#1537). Each reader that
applies the command's flags — the hook's scope (which ``mms daemon
--hook-command`` and a daemon the hook spawns also go through) and doctor's
``load_env_stm_config(env_overlay)`` build — must read the command's value
whatever the environment holds.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.hook_cmd import _runtime_registration_env
from memtomem_stm.cli.proxy import cli
from memtomem_stm.config import load_env_stm_config

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows folds environment names itself"
)

PERSIST = "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT"
SURFACING_TIMEOUT = "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS"
DAEMON_TIMEOUT = "MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS"
USE_DAEMON = "MEMTOMEM_STM_HOOK__USE_DAEMON"

# (variable, the command's value, the read value, a later ambient value, its read value)
_FLAGS: list[tuple[str, str, Any, str, Any]] = [
    (PERSIST, "false", False, "true", True),
    (SURFACING_TIMEOUT, "7.5", 7.5, "1.5", 1.5),
    (DAEMON_TIMEOUT, "9", 9.0, "8", 8.0),
    (USE_DAEMON, "false", False, "true", True),
]

_READ: dict[str, Callable[[Any], Any]] = {
    PERSIST: lambda c: c.surfacing.persist_query_text,
    SURFACING_TIMEOUT: lambda c: c.surfacing.timeout_seconds,
    DAEMON_TIMEOUT: lambda c: c.hook.daemon_timeout_seconds,
    USE_DAEMON: lambda c: c.hook.use_daemon,
}


def _lower(name: str) -> str:
    return name.lower()


def _mixed(name: str) -> str:
    head, _, field = name.rpartition("__")
    return f"{head}__{field.lower()}"


_ALIASES = pytest.mark.parametrize("alias", [_lower, _mixed], ids=["lower", "mixed"])
_EACH_FLAG = pytest.mark.parametrize(
    ("name", "value", "expected", "ambient", "ambient_read"),
    _FLAGS,
    ids=["persist", "surfacing-timeout", "daemon-timeout", "use-daemon"],
)


_CANONICAL = {PERSIST: "false", SURFACING_TIMEOUT: "4.5", DAEMON_TIMEOUT: "6", USE_DAEMON: "false"}
"""A value for the canonical spelling unlike the later one's, so a positive
control shows which spelling the bare build read."""


def _collide(monkeypatch: pytest.MonkeyPatch, name: str, ambient: str, alias: Any) -> None:
    """The canonical spelling first, then a case-variant one after it."""
    monkeypatch.setenv(name, _CANONICAL[name])
    monkeypatch.setenv(alias(name), ambient)


@_POSIX_ONLY
@_ALIASES
@_EACH_FLAG
def test_the_hooks_overlay_outranks_a_later_spelling(
    monkeypatch, name, value, expected, ambient, ambient_read, alias
):
    _collide(monkeypatch, name, ambient, alias)
    # Positive control: without the overlay, the later spelling is read.
    assert _READ[name](load_env_stm_config().config) == ambient_read

    with _runtime_registration_env({name: value}):
        built = load_env_stm_config().config

    assert _READ[name](built) == expected


@_POSIX_ONLY
@_ALIASES
@_EACH_FLAG
def test_doctors_overlay_build_outranks_a_later_spelling(
    monkeypatch, name, value, expected, ambient, ambient_read, alias
):
    _collide(monkeypatch, name, ambient, alias)
    assert _READ[name](load_env_stm_config().config) == ambient_read

    assert _READ[name](load_env_stm_config({name: value}).config) == expected


@_POSIX_ONLY
@_ALIASES
def test_only_a_case_variant_spelling_is_outranked_too(monkeypatch, alias):
    # No canonical spelling at all: the overlay adds it, and the alias still
    # must not win.
    monkeypatch.setenv(_mixed(PERSIST) if alias is _lower else _lower(PERSIST), "true")

    with _runtime_registration_env({PERSIST: "false"}):
        built = load_env_stm_config().config

    assert built.surfacing.persist_query_text is False
    assert load_env_stm_config({PERSIST: "false"}).config.surfacing.persist_query_text is False


@_POSIX_ONLY
@pytest.mark.parametrize("alias_first", [True, False], ids=["alias-first", "alias-last"])
@pytest.mark.parametrize("raises", [False, True], ids=["normal-exit", "raising-exit"])
def test_the_overlay_restores_every_spelling_in_place(monkeypatch, alias_first, raises):
    if alias_first:
        monkeypatch.setenv(_lower(SURFACING_TIMEOUT), "1.5")
        monkeypatch.setenv(SURFACING_TIMEOUT, "2.5")
    else:
        monkeypatch.setenv(SURFACING_TIMEOUT, "2.5")
        monkeypatch.setenv(_lower(SURFACING_TIMEOUT), "1.5")
    before = list(os.environ.items())
    bare_before = load_env_stm_config().config.surfacing.timeout_seconds

    try:
        with _runtime_registration_env({SURFACING_TIMEOUT: "7.5", PERSIST: "false"}):
            assert load_env_stm_config().config.surfacing.timeout_seconds == 7.5
            if raises:
                raise RuntimeError("inside the overlay")
    except RuntimeError:
        assert raises

    assert list(os.environ.items()) == before
    assert load_env_stm_config().config.surfacing.timeout_seconds == bare_before


@_POSIX_ONLY
def test_a_collision_beside_an_invalid_proxy_variable_still_degrades(monkeypatch):
    # The proxy retry rebuilds without MEMTOMEM_STM_PROXY*; the command's
    # value must still be the one read there.
    _collide(monkeypatch, PERSIST, "true", _lower)
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__CACHE__ENABLED", "nope")

    with _runtime_registration_env({PERSIST: "false"}):
        hook = load_env_stm_config()
    doctor = load_env_stm_config({PERSIST: "false"})

    for load in (hook, doctor):
        assert load.proxy_error is not None
        assert load.config.surfacing.persist_query_text is False


@pytest.mark.parametrize(
    "overlay",
    [{}, {name: value for name, value, *_ in _FLAGS}],
    ids=["no-flags", "installed-flags"],
)
def test_the_overlay_imports_no_settings(overlay):
    """The hook enters the overlay before it reads its payload, and an empty
    or malformed one needs no settings, so the overlay must not pay for the
    settings stack (#1503 review: about 2 ms against 60 ms). ``mms hook
    install`` writes all four flags, so both shapes are the common path."""
    code = (
        "import sys\n"
        "from memtomem_stm.cli.hook_cmd import _runtime_registration_env\n"
        "assert 'memtomem_stm.config' not in sys.modules\n"
        f"with _runtime_registration_env({overlay!r}):\n"
        "    pass\n"
        "print('memtomem_stm.config' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


_CHILD = (
    "import json\n"
    "from memtomem_stm.config import load_env_stm_config\n"
    "c = load_env_stm_config().config\n"
    "print(json.dumps([c.surfacing.persist_query_text, c.surfacing.timeout_seconds,"
    " c.hook.daemon_timeout_seconds, c.hook.use_daemon]))\n"
)


def _child_reads(env: dict[str, str] | None) -> list[Any]:
    out = subprocess.run(
        [sys.executable, "-c", _CHILD], env=env, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def _reads(config: Any) -> list[Any]:
    return [
        config.surfacing.persist_query_text,
        config.surfacing.timeout_seconds,
        config.hook.daemon_timeout_seconds,
        config.hook.use_daemon,
    ]


@_POSIX_ONLY
def test_a_daemon_spawned_inside_the_scope_reads_the_commands_values(monkeypatch, tmp_path):
    """What a daemon the hook spawns builds: the flags reach it through the
    environment ``daemon.spawn`` hands it, read by ``mms daemon run
    --detached``, over later spellings and the block's JSON alike (#1537)."""
    from helpers import detached_daemon_config

    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    overlay = {name: value for name, value, *_ in _FLAGS}
    for name, _value, _expected, ambient, _read in _FLAGS:
        _collide(monkeypatch, name, ambient, _lower)
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING", '{"TIMEOUT_SECONDS": 1.5}')

    with _runtime_registration_env(overlay):
        # Control: any other process started here sees the environment alone.
        assert _child_reads(None) == [True, 1.5, 8.0, True]
        spawned = detached_daemon_config()
        doctor = load_env_stm_config(overlay).config

    assert _reads(spawned) == [False, 7.5, 9.0, False]
    from memtomem_stm.daemon.discovery import config_fingerprint

    assert config_fingerprint(spawned, create_key=True) == config_fingerprint(
        doctor, create_key=True
    )


@_POSIX_ONLY
def test_the_hook_builds_hint_names_the_variable_it_read(monkeypatch, tmp_path: Path):
    """A model-level error names every variable under the model; the flag's
    is named as the overlay set it, never as a spelling the build did not
    read."""
    from memtomem_stm.cli.proxy import _env_overrides_found_row, _HookCommandView

    _collide(monkeypatch, PERSIST, "true", _lower)
    # ``surfacing`` fails as a whole: an auto-tune floor above min_score.
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__AUTO_TUNE_SCORE_FLOOR", "0.9")
    view = _HookCommandView(
        state="found",
        path=tmp_path / "settings.json",
        locations=("hooks.PostToolUse[0].hooks[0]",),
        env={PERSIST: "false"},
        use_daemon=True,
    )
    try:
        load_env_stm_config(view.env)
    except Exception as exc:  # noqa: BLE001 — the build's own error is the input
        hook_outcome: Any = exc
    else:
        pytest.fail("the hook build was expected to fail")

    _status, detail, _next = _env_overrides_found_row(view, hook_outcome, hook_outcome)

    hook_clause = detail.split("; doctor's environment alone")[0]
    assert PERSIST in hook_clause
    assert _lower(PERSIST) not in hook_clause


# --- the parent block's JSON and a ``null`` block (#1537) ---------------------

_BLOCK = {
    PERSIST: ("MEMTOMEM_STM_SURFACING", "persist_query_text"),
    SURFACING_TIMEOUT: ("MEMTOMEM_STM_SURFACING", "timeout_seconds"),
    DAEMON_TIMEOUT: ("MEMTOMEM_STM_HOOK", "daemon_timeout_seconds"),
    USE_DAEMON: ("MEMTOMEM_STM_HOOK", "use_daemon"),
}

_JSON_VALUE = {PERSIST: "true", SURFACING_TIMEOUT: "1.5", DAEMON_TIMEOUT: "8", USE_DAEMON: "true"}
"""The upper-case JSON key's value: unlike the command's, so a control shows it is read."""

_DEFAULT_READ = {PERSIST: True, SURFACING_TIMEOUT: 3.0, DAEMON_TIMEOUT: 2.5, USE_DAEMON: True}
_NULL_FLAG = {PERSIST: "false", SURFACING_TIMEOUT: "7.5", DAEMON_TIMEOUT: "9", USE_DAEMON: "false"}
"""For a ``null`` block: values unlike the defaults the ``null`` falls back to."""

_ANY_FLAG = pytest.mark.parametrize(
    "name", list(_BLOCK), ids=["persist", "surfacing-timeout", "daemon-timeout", "use-daemon"]
)


def _no_stm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.lower().startswith("memtomem_stm"):
            monkeypatch.delenv(key)


def _json_case_variant(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    block, field = _BLOCK[name]
    lower = _NULL_FLAG[name]
    monkeypatch.setenv(
        block, json.dumps({field: json.loads(lower), field.upper(): json.loads(_JSON_VALUE[name])})
    )
    monkeypatch.setenv(name, _CANONICAL[name])


def _hook_build(overlay: dict[str, str]) -> Any:
    with _runtime_registration_env(overlay):
        return load_env_stm_config().config


@_ANY_FLAG
def test_a_json_case_variant_of_the_field_does_not_outrank_the_flag(monkeypatch, name):
    _no_stm_env(monkeypatch)
    _json_case_variant(monkeypatch, name)
    command = {SURFACING_TIMEOUT: "7.25", PERSIST: "false", DAEMON_TIMEOUT: "9.5"}.get(
        name, "false"
    )
    expected = _READ[name](load_env_stm_config({name: command}).config)
    # Positive control: without the flag, the upper-case JSON key is read.
    assert _READ[name](load_env_stm_config().config) == json.loads(_JSON_VALUE[name])

    assert _READ[name](_hook_build({name: command})) == json.loads(command)
    assert expected == json.loads(command)


@_ANY_FLAG
def test_a_null_block_does_not_drop_the_flag(monkeypatch, name):
    _no_stm_env(monkeypatch)
    block, _field = _BLOCK[name]
    monkeypatch.setenv(block, "null")
    monkeypatch.setenv(name, _NULL_FLAG[name])
    # Positive control: the ``null`` drops the flat variable, defaults are read.
    assert _READ[name](load_env_stm_config().config) == _DEFAULT_READ[name]

    assert _READ[name](_hook_build({name: _NULL_FLAG[name]})) == json.loads(_NULL_FLAG[name])
    built = load_env_stm_config({name: _NULL_FLAG[name]}).config
    assert _READ[name](built) == json.loads(_NULL_FLAG[name])


def test_a_null_block_keeps_hiding_its_other_children(monkeypatch):
    # Turning the ``null`` into a block must not activate a child variable the
    # ``null`` was hiding (the defect that dropped the JSON rewrite in #1503).
    _no_stm_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING", "null")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__MAX_RESULTS", "invalid")
    assert load_env_stm_config().config.surfacing.max_results == 3  # control: hidden

    for built in (_hook_build({PERSIST: "false"}), load_env_stm_config({PERSIST: "false"}).config):
        assert built.surfacing.persist_query_text is False
        assert built.surfacing.max_results == 3


@pytest.mark.parametrize("raw", ["[]", '"x"', "3"], ids=["list", "string", "number"])
def test_a_block_that_is_not_an_object_still_fails_with_a_flag(monkeypatch, raw):
    from pydantic import ValidationError

    _no_stm_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING", raw)
    with pytest.raises(ValidationError):
        load_env_stm_config()  # control
    with pytest.raises(ValidationError):
        _hook_build({PERSIST: "false"})
    with pytest.raises(ValidationError):
        load_env_stm_config({PERSIST: "false"})


def test_the_json_keeps_its_other_fields_under_a_flag(monkeypatch):
    _no_stm_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING", '{"max_results": 2, "TIMEOUT_SECONDS": 9}')

    built = _hook_build({SURFACING_TIMEOUT: "7"})

    assert built.surfacing.timeout_seconds == 7.0
    assert built.surfacing.max_results == 2


# --- the scope, the spawn edge and the detached run (#1537) --------------------

CHANNEL = "MEMTOMEM_STM_HOOK_FLAGS"


def test_no_build_reads_the_channel(monkeypatch):
    # An inherited channel is not a settings field and no build reads it: a
    # flagless hook call, doctor and a plain build all see the environment.
    _no_stm_env(monkeypatch)
    monkeypatch.setenv(CHANNEL, json.dumps({PERSIST: "false", SURFACING_TIMEOUT: "7"}))

    assert load_env_stm_config().config.surfacing.persist_query_text is True
    assert _hook_build({}).surfacing.persist_query_text is True
    assert load_env_stm_config({}).config.surfacing.timeout_seconds == 3.0


@pytest.mark.parametrize("scope", [None, {}], ids=["no-scope", "empty-scope"])
def test_a_spawn_without_flags_drops_an_inherited_channel(monkeypatch, scope):
    from memtomem_stm.cli.host_runtime import hook_flags_scope
    from memtomem_stm.daemon.spawn import _child_env

    monkeypatch.setenv(CHANNEL, json.dumps({PERSIST: "false"}))
    monkeypatch.setenv("MEMTOMEM_STM_KEEP_ME", "1")
    if scope is None:
        env = _child_env()
    else:
        with hook_flags_scope(scope):
            env = _child_env()

    assert CHANNEL not in env
    assert env["MEMTOMEM_STM_KEEP_ME"] == "1"


def test_a_spawn_with_flags_hands_them_over(monkeypatch):
    from memtomem_stm.cli.host_runtime import decode_hook_flags_channel
    from memtomem_stm.daemon.spawn import _child_env

    monkeypatch.setenv(CHANNEL, json.dumps({USE_DAEMON: "true"}))  # replaced, not merged
    flags = {PERSIST: "false", SURFACING_TIMEOUT: "7.5"}
    with _runtime_registration_env(flags):
        env = _child_env()

    assert decode_hook_flags_channel(env[CHANNEL]) == flags
    assert CHANNEL in os.environ and PERSIST not in os.environ  # this environment is unchanged


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "[]",
        json.dumps({"MEMTOMEM_STM_PROXY__CACHE__ENABLED": "false"}),
        json.dumps({PERSIST: False}),
    ],
    ids=["not-json", "not-an-object", "unknown-name", "not-a-string"],
)
def test_the_detached_run_refuses_a_channel_it_did_not_write(monkeypatch, raw, tmp_path):
    from click.testing import CliRunner

    from memtomem_stm.cli.proxy import cli

    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(CHANNEL, raw)
    started: list[Any] = []
    monkeypatch.setattr("memtomem_stm.daemon.server.run", lambda config: started.append(config))

    result = CliRunner().invoke(cli, ["daemon", "run", "--detached"])

    assert result.exit_code == 1, result.output
    assert started == []
    assert CHANNEL in result.output
    if raw not in ("{", "[]"):
        assert raw not in result.output


def test_a_foreground_run_ignores_the_channel(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from memtomem_stm.cli.proxy import cli

    _no_stm_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(CHANNEL, json.dumps({PERSIST: "false"}))
    started: list[Any] = []
    monkeypatch.setattr(
        "memtomem_stm.daemon.server.run", lambda config: started.append(config) or 0
    )

    result = CliRunner().invoke(cli, ["daemon", "run"])

    assert result.exit_code == 0, result.output
    assert started[0].surfacing.persist_query_text is True


def test_overlapping_calls_do_not_change_each_others_builds(monkeypatch):
    import threading

    _no_stm_env(monkeypatch)
    inside = threading.Event()
    release = threading.Event()
    seen: list[bool] = []

    def flagged_call() -> None:
        with _runtime_registration_env({PERSIST: "false"}):
            inside.set()
            release.wait(timeout=10)
            seen.append(load_env_stm_config().config.surfacing.persist_query_text)

    worker = threading.Thread(target=flagged_call)
    worker.start()
    assert inside.wait(timeout=10)
    # A flagless call here, while the other is inside its scope, then ends first.
    assert _hook_build({}).surfacing.persist_query_text is True
    release.set()
    worker.join(timeout=10)

    assert seen == [False]
    assert load_env_stm_config().config.surfacing.persist_query_text is True


@pytest.mark.parametrize(
    ("env", "args", "timeout", "budget"),
    [
        ({SURFACING_TIMEOUT: "20"}, [], 20.0, 28.0),
        ({SURFACING_TIMEOUT: "1"}, ["--surfacing-timeout-seconds", "30"], 30.0, 38.0),
        # A null block drops the flat variable from the build (#1573).
        ({"MEMTOMEM_STM_SURFACING": "null", SURFACING_TIMEOUT: "inf"}, [], 3.0, 12.0),
        ({"MEMTOMEM_STM_SURFACING": "null", SURFACING_TIMEOUT: "nan"}, [], 3.0, 12.0),
        # Spellings the build reads and the flat variable's own text does not.
        ({"MEMTOMEM_STM_SURFACING": '{"timeout_seconds": 60}'}, [], 60.0, 68.0),
        ({_lower(SURFACING_TIMEOUT): "60"}, [], 60.0, 68.0),
    ],
    ids=["flat", "command-flag", "null-inf", "null-nan", "block-json", "lower-case"],
)
def test_the_backstop_follows_the_builds_surfacing_timeout(monkeypatch, env, args, timeout, budget):
    _no_stm_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__METRICS_ENABLED", "0")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    armed: list[float] = []
    seen: list[tuple[float, float]] = []
    reschedule = asyncio.Timeout.reschedule

    def record(self: asyncio.Timeout, when: float | None) -> None:
        loop = asyncio.get_running_loop()
        armed.append(float("inf") if when is None else when - loop.time())
        reschedule(self, when)

    async def surfacing(_call: Any, *, config: Any, **_kwargs: Any) -> dict[str, Any]:
        seen.append((config.surfacing.timeout_seconds, armed[-1]))
        return {}

    monkeypatch.setattr(asyncio.Timeout, "reschedule", record)
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._run_hook", surfacing)
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "notes.md"},
        "tool_response": "text",
    }

    CliRunner().invoke(cli, ["hook", "--host", "claude", *args], input=json.dumps(payload))

    # Asserted out here: the hook swallows what is raised inside it.
    assert seen == [(timeout, pytest.approx(budget, abs=1.0))]


def test_doctor_builds_its_environment_without_an_enclosing_scope(monkeypatch):
    from memtomem_stm.cli.host_runtime import active_hook_flags
    from memtomem_stm.cli.proxy import _without_hook_flags

    _no_stm_env(monkeypatch)
    seen: list[Any] = []

    @_without_hook_flags
    def doctor_body() -> None:
        seen.append(
            (active_hook_flags(), load_env_stm_config().config.surfacing.persist_query_text)
        )
        seen.append(load_env_stm_config({PERSIST: "false"}).config.surfacing.persist_query_text)

    with _runtime_registration_env({PERSIST: "false"}):
        doctor_body()
        assert active_hook_flags() == {PERSIST: "false"}  # the caller's scope is back

    assert seen == [({}, True), False]


def test_a_rejected_flag_is_named_alone(monkeypatch):
    # The command's deadline is rejected at its own field; the ``null`` block
    # above it supplied nothing there and is not named.
    from pydantic import ValidationError

    from memtomem_stm.config import hook_flags_hint

    _no_stm_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STM_HOOK", "null")
    flags = {DAEMON_TIMEOUT: "1.79769313486232e+308"}
    with pytest.raises(ValidationError) as caught:
        load_env_stm_config(flags)

    hint = hook_flags_hint(caught.value, flags)

    assert hint == f" (env var(s) implicated: {DAEMON_TIMEOUT})"
    assert "1.797" not in hint


@pytest.mark.parametrize("form", ["json-case-variant", "null-block"])
@_ANY_FLAG
def test_a_spawned_daemon_reads_the_flag_over_the_block(monkeypatch, tmp_path, name, form):
    from helpers import detached_daemon_config

    _no_stm_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    if form == "null-block":
        monkeypatch.setenv(_BLOCK[name][0], "null")
        command = _NULL_FLAG[name]
    else:
        _json_case_variant(monkeypatch, name)
        command = {SURFACING_TIMEOUT: "7.25", PERSIST: "false", DAEMON_TIMEOUT: "9.5"}.get(
            name, "false"
        )
    assert _READ[name](load_env_stm_config().config) != json.loads(command)  # control

    with _runtime_registration_env({name: command}):
        spawned = detached_daemon_config()

    assert _READ[name](spawned) == json.loads(command)


def test_a_deeply_nested_channel_is_refused_by_name():
    # Passed to the decoder directly: a value this long is over the Windows
    # environment-variable limit. The detached run's handling of the error is
    # covered by the malformed-channel cases above.
    from memtomem_stm.cli.host_runtime import HookFlagsChannelError, decode_hook_flags_channel

    with pytest.raises(HookFlagsChannelError) as caught:
        decode_hook_flags_channel("[" * 100_000 + "]" * 100_000)

    assert str(caught.value).startswith(CHANNEL)


def test_the_doctor_command_runs_without_an_enclosing_scope(monkeypatch):
    from click.testing import CliRunner

    from memtomem_stm.cli.host_runtime import active_hook_flags
    from memtomem_stm.cli.proxy import cli

    seen: list[dict[str, str]] = []

    class Stop(Exception):
        pass

    def first_step(*_args: Any, **_kwargs: Any) -> Any:
        seen.append(active_hook_flags())
        raise Stop

    monkeypatch.setattr("memtomem_stm.cli.proxy.resolve_cli_config_path", first_step)

    with _runtime_registration_env({PERSIST: "false"}):
        result = CliRunner().invoke(cli, ["doctor"])
        assert active_hook_flags() == {PERSIST: "false"}  # the caller's scope is back

    assert isinstance(result.exception, Stop), result.output
    assert seen == [{}]


def test_each_error_is_attributed_on_its_own(monkeypatch):
    # A rejected command deadline beside an unrelated bad variable: the flag
    # is named for its own error, the bad variable for its own, and the valid
    # ``MEMTOMEM_STM_HOOK`` block above the flag for neither.
    from pydantic import ValidationError

    from memtomem_stm.config import hook_flags_hint

    _no_stm_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STM_HOOK", '{"compression": {"enabled": true}}')
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__MAX_RESULTS", "invalid")
    flags = {DAEMON_TIMEOUT: "1.79769313486232e+308"}
    with pytest.raises(ValidationError) as caught:
        load_env_stm_config(flags)

    hint = hook_flags_hint(caught.value, flags)

    assert hint == (
        f" (env var(s) implicated: {DAEMON_TIMEOUT}, MEMTOMEM_STM_SURFACING__MAX_RESULTS)"
    )
