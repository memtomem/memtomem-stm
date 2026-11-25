"""A case-variant spelling never outranks a variable ``mms hook`` sets from
its command's flags (#1503).

POSIX lets ``MEMTOMEM_STM_X`` and ``memtomem_stm_x`` both be set, and
pydantic-settings folds names to lower case, so the last spelling in the
environment wins. Each reader that applies the command's flags — the hook's
own overlay (which ``mms daemon --hook-command`` and a daemon the hook spawns
also go through) and doctor's ``load_env_stm_config(env_overlay)`` build —
must read the command's value whatever spelling comes later.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from memtomem_stm.cli.hook_cmd import _runtime_registration_env
from memtomem_stm.config import load_env_stm_config

pytestmark = pytest.mark.skipif(
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


@_ALIASES
@_EACH_FLAG
def test_doctors_overlay_build_outranks_a_later_spelling(
    monkeypatch, name, value, expected, ambient, ambient_read, alias
):
    _collide(monkeypatch, name, ambient, alias)
    assert _READ[name](load_env_stm_config().config) == ambient_read

    assert _READ[name](load_env_stm_config({name: value}).config) == expected


@_ALIASES
def test_only_a_case_variant_spelling_is_outranked_too(monkeypatch, alias):
    # No canonical spelling at all: the overlay adds it, and the alias still
    # must not win.
    monkeypatch.setenv(_mixed(PERSIST) if alias is _lower else _lower(PERSIST), "true")

    with _runtime_registration_env({PERSIST: "false"}):
        built = load_env_stm_config().config

    assert built.surfacing.persist_query_text is False
    assert load_env_stm_config({PERSIST: "false"}).config.surfacing.persist_query_text is False


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


def test_an_empty_overlay_imports_no_settings():
    """A hook command without flags must not pay for the settings stack before
    it reads its payload (#1503 review: about 2 ms against 60 ms)."""
    code = (
        "import sys\n"
        "from memtomem_stm.cli.hook_cmd import _runtime_registration_env\n"
        "assert 'memtomem_stm.config' not in sys.modules\n"
        "with _runtime_registration_env({}):\n"
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


def test_a_process_started_inside_the_overlay_reads_the_commands_values(monkeypatch):
    """What a daemon the hook spawns inherits: the environment itself, not
    just the in-process build."""
    overlay = {name: value for name, value, *_ in _FLAGS}
    for name, _value, _expected, ambient, _read in _FLAGS:
        _collide(monkeypatch, name, ambient, _lower)
    clean = {k: v for k, v in os.environ.items() if not k.lower().startswith("memtomem_stm_")}

    with _runtime_registration_env(overlay):
        inherited = _child_reads(None)

    assert inherited == _child_reads({**clean, **overlay})
    assert inherited == [False, 7.5, 9.0, False]


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
