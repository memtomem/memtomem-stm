"""Exercise the shared fixtures under an inherited environment, not a clean CI shell."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


_CONTRACT_TESTS = """\
import os
from pathlib import Path

import pytest

from memtomem_stm.config import STMConfig


def assert_isolated():
    assert not any(name.lower().startswith("memtomem_stm_") for name in os.environ)
    assert os.environ["ISOLATION_SENTINEL"] == "preserved"
    assert os.environ["MEMTOMEM_STMISH_SENTINEL"] == "preserved"
    assert os.environ["HOME"] == os.environ["USERPROFILE"]
    assert Path.home() != Path(os.environ["ISOLATION_AMBIENT_HOME"])
    config = STMConfig()
    assert config.daemon.host == "127.0.0.1"
    assert config.surfacing.enabled is True
    assert config.hook.record_feedback_events is False
    assert config.log_level == "WARNING"
    assert config.proxy.default_max_result_chars == 16000


def test_defaults():
    assert_isolated()


@pytest.fixture
def explicit_override(monkeypatch):
    assert_isolated()
    monkeypatch.setenv("MEMTOMEM_STM_DAEMON__HOST", "127.0.0.3")


def test_fixture_and_test_overrides(explicit_override, monkeypatch):
    assert STMConfig().daemon.host == "127.0.0.3"
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "false")
    assert STMConfig().surfacing.enabled is False
    monkeypatch.undo()
    assert_isolated()


def test_teardown_restores_overridden_ambient_keys(monkeypatch):
    assert_isolated()
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__RECORD_FEEDBACK_EVENTS", "1")
    monkeypatch.setenv("MEMTOMEM_STM_ONLY_IN_TEST", "temporary")
    monkeypatch.setenv("HOME", "/temporary-test-home")
    assert STMConfig().hook.record_feedback_events is True
    if os.environ["ISOLATION_EXPECT_FAILURE"] == "1":
        pytest.fail("intentional failure to exercise teardown")


def test_next_test_starts_clean():
    assert_isolated()
"""

_RUNNER = """\
import os
import pytest


def environment_snapshot():
    return {
        name: value for name, value in os.environ.items()
        if name.lower().startswith("memtomem_stm_")
        or name in ("HOME", "USERPROFILE", "ISOLATION_SENTINEL", "MEMTOMEM_STMISH_SENTINEL")
    }


before = environment_snapshot()
result = pytest.main(["-q", "--tb=short", "--confcutdir=.", "test_contract.py"])
assert result == int(os.environ["ISOLATION_EXPECT_FAILURE"]), result
assert environment_snapshot() == before, "pytest did not restore the inherited environment"
print("INHERITED_ENVIRONMENT_RESTORED")
"""


@pytest.mark.parametrize("fail_test", [False, True], ids=["passing-test", "failing-test"])
def test_shared_fixtures_isolate_and_restore_inherited_environment(
    tmp_path: Path, fail_test: bool
) -> None:
    # Run the REAL conftest in a child: seeding env inside this test would be
    # too late for its own autouse isolation and could pass vacuously in CI.
    suite = tmp_path / "suite"
    suite.mkdir()
    tests_dir = Path(__file__).resolve().parent
    for name in ("conftest.py", "helpers.py"):
        shutil.copyfile(tests_dir / name, suite / name)
    (suite / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (suite / "test_contract.py").write_text(_CONTRACT_TESTS, encoding="utf-8")
    (suite / "run_contract.py").write_text(_RUNNER, encoding="utf-8")

    ambient_home = tmp_path / "ambient-home"
    ambient_home.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(ambient_home),
            "USERPROFILE": str(ambient_home),
            "ISOLATION_AMBIENT_HOME": str(ambient_home),
            "ISOLATION_SENTINEL": "preserved",
            "MEMTOMEM_STMISH_SENTINEL": "preserved",
            "ISOLATION_EXPECT_FAILURE": str(int(fail_test)),
            "MEMTOMEM_STM_DAEMON__HOST": "127.0.0.2",
            "MEMTOMEM_STM_SURFACING__ENABLED": "false",
            "MEMTOMEM_STM_HOOK__RECORD_FEEDBACK_EVENTS": "1",
            "memtomem_stm_log_level": "DEBUG",
            "Memtomem_Stm_Proxy__Default_Max_Result_Chars": "1234",
            "MEMTOMEM_STM_PROXY": '{"enabled": true}',
            "Memtomem_Stm_Proxy__Config_Path": str(ambient_home / "stm_proxy.json"),
            "MEMTOMEM_STM_HOOK_SURFACE_TOOLS": "Read,Custom",
            "MEMTOMEM_STM_FUTURE_FLAG": "unknown setting still belongs to the namespace",
            # This synchronous fixture contract needs no third-party plugins
            # or pytest flags inherited from the outer test invocation.
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_ADDOPTS": "",
            "PYTEST_PLUGINS": "",
        }
    )
    result = subprocess.run(
        [sys.executable, "run_contract.py"],
        cwd=suite,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "INHERITED_ENVIRONMENT_RESTORED" in result.stdout
    assert ("1 failed, 3 passed" if fail_test else "4 passed") in result.stdout
