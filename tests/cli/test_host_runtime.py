"""Managed host-runtime policy: resolution, migration, and deadline ordering."""

from __future__ import annotations

import pytest

from memtomem_stm.cli.host_runtime import (
    HostRuntimePolicy,
    parse_managed_hook_runtime,
    resolve_host_runtime_policy,
    runtime_env_overrides,
)


@pytest.fixture(autouse=True)
def _isolated_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "MEMTOMEM_STM_HOOK__USE_DAEMON",
        "MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS",
        "MEMTOMEM_STM_SURFACING__USE_DAEMON",
        "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS",
        "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT",
    ):
        monkeypatch.delenv(key, raising=False)


def test_fresh_policy_uses_effective_timeout_and_orders_hook_deadline() -> None:
    policy = resolve_host_runtime_policy()
    assert policy == HostRuntimePolicy(
        use_daemon=True,
        surfacing_timeout_seconds=3.0,
        daemon_timeout_seconds=4.0,
        persist_query_text=False,
    )


def test_cli_timeout_wins_over_existing_and_existing_wins_over_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS", "5")
    existing = (
        "memtomem-stm hook --host claude --no-daemon "
        "--surfacing-timeout-seconds 12 --daemon-timeout-seconds 20 "
        "--persist-query-text"
    )
    preserved = resolve_host_runtime_policy(existing_command=existing)
    assert preserved == HostRuntimePolicy(False, 12.0, 20.0, True)

    explicit = resolve_host_runtime_policy(
        existing_command=existing,
        use_daemon=True,
        surfacing_timeout_seconds=30.0,
    )
    assert explicit == HostRuntimePolicy(True, 30.0, 31.0, True)


def test_legacy_inline_env_policy_is_recovered() -> None:
    parsed = parse_managed_hook_runtime(
        "env MEMTOMEM_STM_HOOK__USE_DAEMON=false "
        "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS=12 "
        "MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS=14 "
        "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT=false "
        "memtomem-stm hook --host claude"
    )
    assert parsed.use_daemon is False
    assert parsed.surfacing_timeout_seconds == 12.0
    assert parsed.daemon_timeout_seconds == 14.0
    assert parsed.persist_query_text is False


def test_runtime_overlay_ignores_invalid_values_and_normalizes_deadline() -> None:
    overrides = runtime_env_overrides(
        use_daemon="true",
        surfacing_timeout_seconds="12",
        daemon_timeout_seconds="2",
        persist_query_text="false",
    )
    assert overrides == {
        "MEMTOMEM_STM_HOOK__USE_DAEMON": "true",
        "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS": "12",
        "MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS": "13",
        "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT": "false",
    }
    assert (
        runtime_env_overrides(
            use_daemon="garbage",
            surfacing_timeout_seconds="nan",
            daemon_timeout_seconds="-1",
            persist_query_text="garbage",
        )
        == {}
    )


def test_mcp_policy_serializes_shared_daemon_contract() -> None:
    policy = HostRuntimePolicy(True, 12.0, 13.0, False)
    assert policy.mcp_env() == {
        "MEMTOMEM_STM_SURFACING__USE_DAEMON": "true",
        "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS": "12",
        "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT": "false",
    }
