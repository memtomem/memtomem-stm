"""``load_env_stm_config`` — bare env construction that survives a proxy-only break (#847).

The hook and the daemon read ``surfacing`` / ``hook`` / ``daemon`` and never
``proxy``, yet a broken ``MEMTOMEM_STM_PROXY*`` variable used to fail their
whole ``STMConfig()``. The loader rebuilds once with every proxy variable
removed: success means the failure was the proxy subtree's alone; a rebuild
that also fails re-raises the ORIGINAL error.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsError

from memtomem_stm.config import STMConfig, load_env_stm_config
from memtomem_stm.proxy.config import ProxyConfig
from memtomem_stm.surfacing.config import SurfacingConfig
from helpers import set_home


@pytest.fixture(autouse=True)
def _clean_stm_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.upper().startswith("MEMTOMEM_STM_"):
            monkeypatch.delenv(key)
    set_home(monkeypatch, tmp_path / "home")


def _assert_proxy_at_defaults(config: STMConfig) -> None:
    assert config.proxy == ProxyConfig()


def test_healthy_env_is_not_degraded(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__MAX_DESCRIPTION_CHARS", "300")

    load = load_env_stm_config()

    assert load.proxy_error is None
    assert type(load.config) is STMConfig
    assert load.config.proxy.max_description_chars == 300


@pytest.mark.parametrize(
    ("env", "error"),
    [
        pytest.param(
            {
                "MEMTOMEM_STM_PROXY__CONFIG_PATH": "/nonexistent/stm_proxy.json",
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GHOST__TIMEOUT": "5",
            },
            ValidationError,
            id="server_the_file_does_not_declare",
        ),
        pytest.param({"MEMTOMEM_STM_PROXY": "{"}, SettingsError, id="undecodable_payload"),
        pytest.param(
            {"MEMTOMEM_STM_PROXY__MAX_DESCRIPTION_CHARS": "5"},
            ValidationError,
            id="out_of_range_scalar",
        ),
        pytest.param(
            {"memtomem_stm_proxy__max_description_chars": "5"},
            ValidationError,
            id="lower_case_name",
        ),
    ],
)
def test_proxy_only_break_degrades_to_proxy_defaults(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], error: type[Exception]
):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(error):
        STMConfig()  # the precondition: the bare construction does fail

    load = load_env_stm_config()

    assert isinstance(load.proxy_error, error)
    _assert_proxy_at_defaults(load.config)


def test_degraded_load_keeps_the_other_subtrees(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__MAX_DESCRIPTION_CHARS", "5")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__MIN_SCORE", "0.3")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS", "7")

    load = load_env_stm_config()

    assert load.proxy_error is not None
    assert load.config.surfacing.min_score == 0.3
    assert load.config.hook.daemon_timeout_seconds == 7


def test_degraded_load_drops_valid_proxy_vars_too(monkeypatch: pytest.MonkeyPatch):
    """The whole subtree is at defaults, including a valid neighbor of the
    broken var and the consumer model ``model_post_init`` would propagate."""
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__MAX_DESCRIPTION_CHARS", "5")
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__CONSUMER_MODEL", "some-model")

    load = load_env_stm_config()

    assert load.proxy_error is not None
    assert load.config.proxy.consumer_model == ProxyConfig().consumer_model
    assert load.config.surfacing.consumer_model == SurfacingConfig().consumer_model
    assert load.config.surfacing.consumer_model != "some-model"


@pytest.mark.parametrize(
    "proxy_env",
    [
        pytest.param({"MEMTOMEM_STM_PROXY__MAX_DESCRIPTION_CHARS": "5"}, id="validation"),
        pytest.param({"MEMTOMEM_STM_PROXY": "{"}, id="settings"),
    ],
)
def test_mixed_break_reraises_the_original_error(
    monkeypatch: pytest.MonkeyPatch, proxy_env: dict[str, str]
):
    from memtomem_stm import config as config_mod

    for key, value in proxy_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__MIN_SCORE", "nope")
    real = config_mod.STMConfig
    first_errors: list[Exception] = []

    def _recording_build() -> STMConfig:
        try:
            return real()
        except (ValidationError, SettingsError) as exc:
            first_errors.append(exc)
            raise

    monkeypatch.setattr(config_mod, "STMConfig", _recording_build)

    with pytest.raises((ValidationError, SettingsError)) as caught:
        load_env_stm_config()

    # The first build's own error object, not the retry's (whose locations
    # would no longer name the proxy variable), and without the retry's
    # error chained onto its traceback.
    assert len(first_errors) == 1
    assert caught.value is first_errors[0]
    assert caught.value.__suppress_context__ is True
    assert caught.value.__cause__ is None


def test_non_proxy_break_alone_still_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__MIN_SCORE", "nope")

    with pytest.raises(ValidationError):
        load_env_stm_config()


@pytest.mark.parametrize(
    "extra_env",
    [
        pytest.param({}, id="no_proxy_var"),
        # Not the proxy field: only ``…_PROXY`` and ``…_PROXY__*`` count.
        pytest.param({"MEMTOMEM_STM_PROXYX": "1"}, id="prefix_lookalike"),
    ],
)
def test_no_proxy_var_means_no_retry(monkeypatch: pytest.MonkeyPatch, extra_env: dict[str, str]):
    """With no proxy variable to remove, a retry that succeeds could only mean
    the environment changed between the two builds — never proxy attribution.
    The first build here fixes the env behind itself, so a retry would pass."""
    from memtomem_stm import config as config_mod

    for key, value in extra_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__MIN_SCORE", "nope")
    real = config_mod.STMConfig
    builds: list[str] = []

    def _first_build_then_fix_env() -> STMConfig:
        builds.append("full")
        try:
            return real()
        finally:
            monkeypatch.delenv("MEMTOMEM_STM_SURFACING__MIN_SCORE")

    monkeypatch.setattr(config_mod, "STMConfig", _first_build_then_fix_env)

    with pytest.raises(ValidationError):
        load_env_stm_config()
    assert builds == ["full"]


def test_unrelated_exceptions_propagate_untouched(monkeypatch: pytest.MonkeyPatch):
    from memtomem_stm import config as config_mod

    monkeypatch.setenv("MEMTOMEM_STM_PROXY__MAX_DESCRIPTION_CHARS", "5")

    def _boom() -> STMConfig:
        raise RuntimeError("not a settings error")

    monkeypatch.setattr(config_mod, "STMConfig", _boom)
    with pytest.raises(RuntimeError, match="not a settings error"):
        load_env_stm_config()
