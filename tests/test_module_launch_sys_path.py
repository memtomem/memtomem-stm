"""An STM-owned ``python -m`` launch must not import from the working directory.

``python -m`` puts the working directory first on ``sys.path``. STM starts the
surfacing daemon, the server a host registration names, and the demo upstream
that way, with the user's project as the working directory, so each must pass
``-P``. Nothing inside STM can make up for a missing flag: Python imports from
that directory (``runpy`` needs ``types``, for one) before any STM code runs.

Every subprocess here runs without ``PYTHONSAFEPATH``, which has the same effect
as ``-P`` and would hide a launch site that lost the flag, and without the
ambient ``PYTHONPATH``, which ``-P`` does not override and which could name the
working directory on its own.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

import memtomem_stm
from memtomem_stm.utils.exec_env import safe_module_argv

_MARKER_ENV = "STM_SHADOW_MARKER"


def _shadow_source(name: str) -> str:
    return (
        "import os\n"
        f'open(os.environ["{_MARKER_ENV}"], "a").write("{name}\\n")\n'
        "raise SystemExit(3)\n"
    )


def _shadow_dir(directory: Path, *names: str) -> Path:
    """Plant shadows that record their name when imported, then exit 3.

    ``"x.py"`` plants a module; ``"x/"`` a package, with a ``__main__`` so that
    ``python -m x`` runs it.
    """
    directory.mkdir()
    for name in names:
        if name.endswith("/"):
            pkg = directory / name[:-1]
            pkg.mkdir()
            (pkg / "__init__.py").write_text(_shadow_source(name[:-1]), encoding="utf-8")
            (pkg / "__main__.py").write_text(_shadow_source(name[:-1]), encoding="utf-8")
        else:
            (directory / name).write_text(_shadow_source(name[:-3]), encoding="utf-8")
    return directory


def _run(
    argv: list[str], cwd: Path, marker: Path, *, pythonpath: str | None = None
) -> tuple[subprocess.CompletedProcess[str], str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONSAFEPATH", "PYTHONPATH")}
    env[_MARKER_ENV] = str(marker)
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    proc = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    imported = marker.read_text(encoding="utf-8") if marker.exists() else ""
    return proc, imported


def _assert_real_cli(proc: subprocess.CompletedProcess[str], imported: str) -> None:
    assert imported == "", f"imported from the working directory: {imported!r}"
    assert proc.returncode == 0, proc.stderr
    assert memtomem_stm.__version__ in proc.stdout


@pytest.fixture
def everything_shadowed(tmp_path: Path) -> Path:
    return _shadow_dir(
        tmp_path / "project", "memtomem_stm/", "types.py", "click.py", "__future__.py", "mcp/"
    )


class TestSafeModuleArgv:
    def test_shape(self) -> None:
        assert safe_module_argv("pkg.mod", "a", "b") == [
            sys.executable,
            "-P",
            "-m",
            "pkg.mod",
            "a",
            "b",
        ]
        assert safe_module_argv("pkg", python="/venv/bin/python")[0] == "/venv/bin/python"

    def test_runs_the_installed_package(self, everything_shadowed: Path, tmp_path: Path) -> None:
        proc, imported = _run(
            safe_module_argv("memtomem_stm", "--version"), everything_shadowed, tmp_path / "m"
        )
        _assert_real_cli(proc, imported)

    def test_positive_control_without_the_flag(
        self, everything_shadowed: Path, tmp_path: Path
    ) -> None:
        argv = [a for a in safe_module_argv("memtomem_stm", "--version") if a != "-P"]
        proc, imported = _run(argv, everything_shadowed, tmp_path / "m")
        # Which shadow wins depends on what the interpreter preloaded (``types``
        # in a plain venv, the package where a ``.pth`` already imported it).
        assert set(imported.split()) & {"memtomem_stm", "types"}
        assert proc.returncode == 3

    def test_pythonpath_naming_the_directory_still_applies(
        self, everything_shadowed: Path, tmp_path: Path
    ) -> None:
        """The documented limit: ``-P`` drops the automatic entry, nothing else."""
        proc, imported = _run(
            safe_module_argv("memtomem_stm", "--version"),
            everything_shadowed,
            tmp_path / "m",
            pythonpath=str(everything_shadowed),
        )
        # A shadow imported while the interpreter is still starting up ends it
        # with its own code, so only "something ran, and it failed" is stable.
        assert imported != ""
        assert proc.returncode != 0


class TestLaunchSites:
    """Each site's own argv, run from a directory that shadows everything."""

    def test_daemon_spawn(
        self, everything_shadowed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memtomem_stm.daemon import spawn

        launched: list[list[str]] = []

        class Child:
            pid = 4242

            def wait(self) -> int:
                return 0

        def record(cmd: list[str], **_kwargs: object) -> Child:
            launched.append(cmd)
            return Child()

        # Scoped: ``spawn.subprocess`` is the real module, and the check below
        # needs the real ``Popen`` back.
        with monkeypatch.context() as patch:
            patch.setattr(spawn.subprocess, "Popen", record)
            patch.setattr(spawn, "spawn_claimed", lambda launch: (launch(), 1)[1])
            patch.setattr(spawn, "release_claim", lambda *_a: None)
            spawn._spawn_detached()

        (cmd,) = launched
        assert cmd[-3:] == ["daemon", "run", "--detached"]
        proc, imported = _run([*cmd[:-3], "--version"], everything_shadowed, tmp_path / "m")
        _assert_real_cli(proc, imported)

    def test_host_registration(self, everything_shadowed: Path, tmp_path: Path) -> None:
        from memtomem_stm.cli.proxy import _registration_command

        command, args, _env = _registration_command(tmp_path / "stm_proxy.json")
        proc, imported = _run([command, *args, "--version"], everything_shadowed, tmp_path / "m")
        _assert_real_cli(proc, imported)

    def test_demo_upstream(self, everything_shadowed: Path, tmp_path: Path) -> None:
        from memtomem_stm.cli.proxy import cli

        config = tmp_path / "stm_proxy.json"
        result = CliRunner().invoke(
            cli,
            ["init", "--demo", "--client", "skip", "--no-validate", "--lang", "en"]
            + ["--config", str(config)],
        )
        assert result.exit_code == 0, result.output
        demo = json.loads(config.read_text(encoding="utf-8"))["upstream_servers"]["demo"]
        assert demo["args"] == ["-P", "-m", "memtomem_stm.demo_server"]

        # stdin is closed, so the real server starts and ends at once.
        proc, imported = _run([demo["command"], *demo["args"]], everything_shadowed, tmp_path / "m")
        assert imported == "", proc.stderr
        assert proc.returncode == 0, proc.stderr


class TestKeptRegistrationHint:
    @staticmethod
    def _mcp_json(directory: Path, entry: object) -> Path:
        path = directory / ".mcp.json"
        path.write_text(json.dumps({"mcpServers": {"memtomem-stm": entry}}), encoding="utf-8")
        return path

    def test_entry_written_before_the_flag(self, tmp_path: Path) -> None:
        from memtomem_stm.cli.proxy import _pre_safe_path_registration_refresh

        path = self._mcp_json(tmp_path, {"command": "/v/python", "args": ["-m", "memtomem_stm"]})
        assert _pre_safe_path_registration_refresh(path) == (
            "mms register --client json --replace-registration"
        )

    def test_refresh_keeps_the_entry_config_path(self, tmp_path: Path) -> None:
        from memtomem_stm.cli.proxy import _pre_safe_path_registration_refresh

        entry = {
            "command": "/v/python",
            "args": ["-m", "memtomem_stm"],
            "env": {"MEMTOMEM_STM_PROXY__CONFIG_PATH": "/etc/stm/custom.json"},
        }
        refresh = _pre_safe_path_registration_refresh(self._mcp_json(tmp_path, entry))
        assert refresh is not None
        assert refresh.endswith("--replace-registration --config /etc/stm/custom.json")

    @pytest.mark.parametrize(
        "entry",
        [
            {"command": "/v/python", "args": ["-P", "-m", "memtomem_stm"]},
            {"command": "/v/python", "args": ["-m", "memtomem_stm", "--verbose"]},
            {"command": "memtomem-stm"},
            "not an object",
        ],
        ids=["current", "edited", "console-script", "malformed"],
    )
    def test_no_hint_for_other_shapes(self, tmp_path: Path, entry: object) -> None:
        from memtomem_stm.cli.proxy import _pre_safe_path_registration_refresh

        assert _pre_safe_path_registration_refresh(self._mcp_json(tmp_path, entry)) is None

    def test_no_hint_for_an_unreadable_file(self, tmp_path: Path) -> None:
        from memtomem_stm.cli.proxy import _pre_safe_path_registration_refresh

        assert _pre_safe_path_registration_refresh(tmp_path / "absent.json") is None
        broken = tmp_path / ".mcp.json"
        broken.write_text("{not json", encoding="utf-8")
        assert _pre_safe_path_registration_refresh(broken) is None

    def test_keep_message(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from memtomem_stm.cli.proxy import _echo_kept_registration

        _echo_kept_registration(
            self._mcp_json(tmp_path, {"command": "/v/python", "args": ["-m", "memtomem_stm"]})
        )
        old = capsys.readouterr().out
        assert "Kept existing registration" in old
        assert "mms register --client json --replace-registration" in old

        _echo_kept_registration(
            self._mcp_json(tmp_path, {"command": "/v/python", "args": ["-P", "-m", "memtomem_stm"]})
        )
        current = capsys.readouterr().out
        assert "Kept existing registration" in current
        assert "Security" not in current
