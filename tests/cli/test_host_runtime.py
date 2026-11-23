"""Managed host-runtime policy: resolution, migration, and deadline ordering."""

from __future__ import annotations

import pytest

from memtomem_stm.cli.host_runtime import (
    HostRuntimePolicy,
    PartialHostRuntimePolicy,
    _parse_bool,
    _parse_positive_float,
    format_seconds,
    parse_managed_hook_runtime,
    resolve_host_runtime_policy,
    runtime_env_overrides,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("garbage", None),
        (" TRUE ", True),
        ("On", True),
        ("Y", True),
        ("t", True),
        ("f", False),
        ("0", False),
        ("no", False),
    ],
)
def test_parse_bool_direct(raw: str | None, expected: bool | None) -> None:
    assert _parse_bool(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("0", None),
        ("-0", None),
        ("-1", None),
        ("nan", None),
        ("inf", None),
        ("1e400", None),
        ("12", 12.0),
        (" 3.5 ", 3.5),
    ],
)
def test_parse_positive_float_direct(raw: str | None, expected: float | None) -> None:
    assert _parse_positive_float(raw) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(12.0, "12"), (2.5, "2.5"), (0.1 + 0.2, "0.3"), (1e16, "1e+16")],
)
def test_format_seconds_direct(seconds: float, expected: str) -> None:
    assert format_seconds(seconds) == expected


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


@pytest.mark.parametrize(
    ("assignments", "expected"),
    [
        # The proxy's variable alone: the hook does not read it (#1484).
        ({"MEMTOMEM_STM_SURFACING__USE_DAEMON": "false"}, None),
        ({"MEMTOMEM_STM_SURFACING__USE_DAEMON": "true"}, None),
        # An empty hook value no longer falls through to the proxy's variable.
        (
            {"MEMTOMEM_STM_HOOK__USE_DAEMON": "", "MEMTOMEM_STM_SURFACING__USE_DAEMON": "false"},
            None,
        ),
        # Positive controls: the hook's own variable is still recovered.
        (
            {
                "MEMTOMEM_STM_HOOK__USE_DAEMON": "true",
                "MEMTOMEM_STM_SURFACING__USE_DAEMON": "false",
            },
            True,
        ),
        (
            {
                "MEMTOMEM_STM_HOOK__USE_DAEMON": "false",
                "MEMTOMEM_STM_SURFACING__USE_DAEMON": "true",
            },
            False,
        ),
    ],
)
def test_daemon_mode_is_recovered_only_from_what_the_hook_reads(
    assignments: dict[str, str], expected: bool | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem_stm.cli.hook_cmd import _daemon_enabled

    prefix = " ".join(f"{key}={value}" for key, value in assignments.items())
    parsed = parse_managed_hook_runtime(f"env {prefix} mms hook --host claude")
    assert parsed.use_daemon is expected

    # The hook's routing predicate under the same environment: a recovered
    # value must agree with it. An unrecovered one makes install fall back on
    # its own effective ``hook.use_daemon``. (An empty hook value also fails
    # the hook's ``STMConfig`` load before routing; this pins only that
    # install no longer reads the proxy's variable in its place.)
    for key, value in assignments.items():
        monkeypatch.setenv(key, value)
    assert _daemon_enabled() is (True if expected is None else expected)


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


def test_explicit_config_path_reaches_the_policy_construction(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#839 (review round 1): ``init``/``register`` resolve the policy via
    ``_registration_command`` while holding the ``--config`` path, but the
    bare construction read the default file — so a per-field env override of
    a server only the flag's file declares refused to build and crashed the
    registration.

    The root conftest isolates HOME and ambient STM variables so a developer
    shell cannot make the positive control's expected failure disappear."""
    import json

    from pydantic import ValidationError

    config = tmp_path / "stm_proxy.json"
    config.write_text(
        json.dumps(
            {
                "enabled": True,
                "upstream_servers": {"fake": {"prefix": "fk", "command": "file-server"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-server")

    # Positive control: the override really is live and really does break the
    # bare construction (the default path declares no such server).
    with pytest.raises(ValidationError):
        resolve_host_runtime_policy()

    policy = resolve_host_runtime_policy(config_path=config)
    assert policy.use_daemon is True


# ── recovery reads what the hook reads (#1476) ───────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        # The shell drops a comment, so the hook never receives these flags.
        "mms hook --host # --persist-query-text",
        "mms hook --host claude #c --persist-query-text",
        # A here-string: the shell sends the flag to stdin, not to the hook.
        "mms hook --host <<< --persist-query-text",
        # Click reads this word as the ``--host`` value, not as a setting.
        "mms hook --host MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT=true",
    ],
)
def test_recovery_reads_no_setting_the_hook_does_not_receive(command: str) -> None:
    assert parse_managed_hook_runtime(command) == PartialHostRuntimePolicy()


def test_recovery_takes_the_last_of_repeated_flags_like_the_hook() -> None:
    parsed = parse_managed_hook_runtime(
        "mms hook --persist-query-text --no-persist-query-text --use-daemon --no-daemon "
        "--surfacing-timeout-seconds 3 --surfacing-timeout-seconds=7 --host claude"
    )
    # The surfacing deadline also sets the daemon deadline (7 + 1), as in the hook.
    assert parsed == PartialHostRuntimePolicy(
        use_daemon=False,
        surfacing_timeout_seconds=7.0,
        daemon_timeout_seconds=8.0,
        persist_query_text=False,
    )


def test_recovery_prefers_a_flag_over_the_legacy_env_prefix() -> None:
    parsed = parse_managed_hook_runtime(
        "env MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT=true "
        "MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS=9 "
        "mms hook --host claude --no-persist-query-text"
    )
    assert parsed == PartialHostRuntimePolicy(daemon_timeout_seconds=9.0, persist_query_text=False)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # An invalid flag value is dropped by the hook, so the inline setting stays.
        (
            "env MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS=9 "
            "mms hook --daemon-timeout-seconds invalid --host claude",
            PartialHostRuntimePolicy(daemon_timeout_seconds=9.0),
        ),
        # A surfacing deadline flag sets the daemon deadline over the inline one.
        (
            "env MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS=9 "
            "mms hook --surfacing-timeout-seconds 3 --host claude",
            PartialHostRuntimePolicy(surfacing_timeout_seconds=3.0, daemon_timeout_seconds=4.0),
        ),
    ],
)
def test_recovery_lays_flags_over_the_env_prefix_like_the_hook(
    command: str, expected: PartialHostRuntimePolicy
) -> None:
    assert parse_managed_hook_runtime(command) == expected


def test_the_split_grammar_keeps_no_backtracking_state_on_a_long_line() -> None:
    """A long unrelated handler line is checked in memory independent of its
    length (the per-character form peaked near 117 MiB on a 1M-character line)."""
    import tracemalloc

    from memtomem_stm.cli.host_runtime import _shell_tokens_accepts

    line = "echo " + "x" * 1_000_000 + "$"
    tracemalloc.start()
    try:
        assert _shell_tokens_accepts(line) is False
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 1 << 20
