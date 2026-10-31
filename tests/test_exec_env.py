"""Executable lookups must not consult the current directory."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from memtomem_stm.utils.exec_env import harden_exec_search, qualified_path

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PATH semantics")


class TestQualifiedPath:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("/a:/b", "/a:/b"),
            ("/a:.:/b", "/a:/b"),
            (":/a", "/a"),
            ("/a::/b", "/a:/b"),
            ("bin:/a", "/a"),
            ("~/bin:/a", "/a"),  # execvp does not expand ``~`` either
            ('"/a":/b', "/b"),  # a POSIX quote is part of the name
            ("/a:/a:./a", "/a:/a"),  # order and duplicates kept
            (".", ""),
            ("", ""),
        ],
    )
    def test_posix(self, value: str, expected: str) -> None:
        assert qualified_path(value, windows=False) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (r"C:\a;D:\b", r"C:\a;D:\b"),
            (r"C:\a\;C:/c", r"C:\a\;C:/c"),
            (r"\\srv\share\a;\\srv\share", r"\\srv\share\a;\\srv\share"),
            (r"//srv/share/a", r"//srv/share/a"),
            (r"C:\a;bin;\b;/c;C:d;.;;C:/e", r"C:\a;C:/e"),
            (r'"C:\Program Files\x";C:\a', r'"C:\Program Files\x";C:\a'),
            (r'"bin";"C:a";"\b";C:\a', r"C:\a"),
            (".;", ""),
        ],
    )
    def test_windows(self, value: str, expected: str) -> None:
        # The separator comes from ``windows``, so this runs on every OS.
        assert qualified_path(value, windows=True) == expected


class TestHardenExecSearch:
    def test_qualified_path_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        env = {"PATH": "/a:/b", "OTHER": "x"}
        harden_exec_search(env)
        assert env == {"PATH": "/a:/b", "OTHER": "x"}

    def test_relative_entries_are_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        env = {"PATH": ".:/a::bin:/b"}
        harden_exec_search(env)
        assert env == {"PATH": "/a:/b"}

    @pytest.mark.parametrize("value", [".", "", ":", "bin", None])
    def test_never_empty_or_absent(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ) -> None:
        # An empty PATH is the current directory to ``subprocess``; an absent one
        # falls back to ``os.defpath``.
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(os, "defpath", "/bin:/usr/bin")
        env: dict[str, str] = {} if value is None else {"PATH": value}
        harden_exec_search(env)
        assert env == {"PATH": "/bin:/usr/bin"}

    def test_windows_default_drops_its_dot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(os, "defpath", r".;C:\bin")
        env: dict[str, str] = {}
        harden_exec_search(env)
        assert env == {"PATH": r"C:\bin", "NoDefaultCurrentDirectoryInExePath": "1"}

    def test_windows_sets_the_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        env = {"PATH": r"C:\a;.;bin"}
        harden_exec_search(env)
        assert env == {"PATH": r"C:\a", "NoDefaultCurrentDirectoryInExePath": "1"}

    def test_windows_keeps_an_existing_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        env = {"PATH": r"C:\a", "NoDefaultCurrentDirectoryInExePath": "0"}
        harden_exec_search(env)
        # Only its existence counts; its value is the operator's.
        assert env["NoDefaultCurrentDirectoryInExePath"] == "0"

    def test_posix_does_not_set_the_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        env = {"PATH": "/a"}
        harden_exec_search(env)
        assert "NoDefaultCurrentDirectoryInExePath" not in env


def _plant(directory: Path, name: str, marker: Path) -> None:
    """An executable ``name`` in ``directory`` that touches ``marker``."""
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / name
    exe.write_text(f"#!/bin/sh\n: > '{marker}'\n", encoding="utf-8")
    exe.chmod(0o755)


@posix_only
class TestSpawnAfterHardening:
    """Real lookups in a fresh process, from a directory holding a planted executable."""

    NAME = "stm-exec-env-probe"

    def _run(
        self, project: Path, path: str, body: str, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEMTOMEM_STM_")}
        env["PATH"] = path
        env.update(extra)
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(body)],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    @pytest.fixture
    def layout(self, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
        project, safe = tmp_path / "project", tmp_path / "safe"
        planted, legit = tmp_path / "planted.marker", tmp_path / "legit.marker"
        for name in (self.NAME, "pgrep"):
            _plant(project, name, planted)
            _plant(project / "bin", name, planted)
            _plant(safe, name, legit)
        return project, safe, planted, legit

    HARDEN_VIA_CLI = """
        from memtomem_stm.cli.proxy import cli
        try:
            cli(["version"], standalone_mode=False)
        except SystemExit:
            pass
    """

    def test_shell_completion_is_hardened(self, layout: tuple[Path, Path, Path, Path]) -> None:
        # Click resolves completion before any group callback, and its bash
        # completion runs ``bash`` by name to read the version.
        project, _safe, planted, _legit = layout
        _plant(project, "bash", planted)
        body = """
            from memtomem_stm.cli.proxy import cli
            try:
                cli(prog_name="mms")
            except SystemExit:
                pass
        """
        result = self._run(project, ".:/bin:/usr/bin", body, _MMS_COMPLETE="bash_source")
        assert result.returncode == 0, result.stderr
        assert "complete" in result.stdout  # the completion script was produced
        assert not planted.exists()

    def test_stm_child_probe_runs_the_qualified_pgrep(
        self, layout: tuple[Path, Path, Path, Path]
    ) -> None:
        # A real STM spawn site: the child reaper runs ``pgrep`` by bare name.
        project, safe, planted, legit = layout
        body = self.HARDEN_VIA_CLI + textwrap.indent(
            textwrap.dedent("""
                from memtomem_stm.utils.child_reaper import probe_child_pids
                probe_child_pids()
            """),
            " " * 8,
        )
        result = self._run(project, f".:{safe}", body)
        assert result.returncode == 0, result.stderr
        assert legit.exists()
        assert not planted.exists()

    def test_mcp_stdio_spawn_runs_the_qualified_command(
        self, layout: tuple[Path, Path, Path, Path]
    ) -> None:
        # The SDK path STM uses for the LTM server and stdio upstreams, with a
        # bare command like the default ``memtomem-server``.
        project, safe, planted, legit = layout
        body = self.HARDEN_VIA_CLI + textwrap.indent(
            textwrap.dedent(f"""
                import anyio, pathlib
                from mcp import StdioServerParameters
                from mcp.client.stdio import stdio_client
                markers = [pathlib.Path({str(planted)!r}), pathlib.Path({str(legit)!r})]

                async def go():
                    try:
                        params = StdioServerParameters(command={self.NAME!r})
                        async with stdio_client(params):
                            with anyio.move_on_after(10):
                                while not any(m.exists() for m in markers):
                                    await anyio.sleep(0.05)
                    except BaseException:
                        pass

                anyio.run(go)
            """),
            " " * 8,
        )
        result = self._run(project, f".:{safe}", body)
        assert result.returncode == 0, result.stderr
        assert legit.exists()
        assert not planted.exists()

    SPAWN = """
        import shutil, subprocess, sys
        {setup}
        found = shutil.which({name!r})
        try:
            subprocess.run([{name!r}], check=False)
        except FileNotFoundError:
            pass
        print(found)
    """

    @pytest.mark.parametrize("prefix", [".", "", "bin"])
    def test_cli_hardens_before_a_subcommand(
        self, layout: tuple[Path, Path, Path, Path], prefix: str
    ) -> None:
        project, safe, planted, legit = layout
        setup = (
            "from memtomem_stm.cli.proxy import cli\n"
            "try:\n"
            "    cli(['version'], standalone_mode=False)\n"
            "except SystemExit:\n"
            "    pass"
        )
        body = self.SPAWN.format(setup=textwrap.indent(setup, " " * 8).strip(), name=self.NAME)
        result = self._run(project, f"{prefix}:{safe}", body)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines()[-1] == str(safe / self.NAME)
        assert legit.exists()
        assert not planted.exists()

    def test_server_main_hardens_without_the_cli(
        self, layout: tuple[Path, Path, Path, Path]
    ) -> None:
        project, safe, planted, legit = layout
        # Stop ``main`` right after its first line of real work: the hardening
        # must already have run by then.
        setup = (
            "import memtomem_stm.server as srv\n"
            "class _Stop(Exception):\n"
            "    pass\n"
            "def _boom(*a, **k):\n"
            "    raise _Stop\n"
            "srv.STMConfig = _boom\n"
            "srv.configure_stderr_logging = lambda **k: None\n"
            "try:\n"
            "    srv.main()\n"
            "except (_Stop, SystemExit):\n"
            "    pass"
        )
        body = self.SPAWN.format(setup=textwrap.indent(setup, " " * 8).strip(), name=self.NAME)
        result = self._run(project, f".:{safe}", body)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines()[-1] == str(safe / self.NAME)
        assert legit.exists()
        assert not planted.exists()

    def test_no_qualified_entry_left(self, layout: tuple[Path, Path, Path, Path]) -> None:
        project, _safe, planted, _legit = layout
        setup = "from memtomem_stm.utils.exec_env import harden_exec_search\nharden_exec_search()"
        body = self.SPAWN.format(setup=textwrap.indent(setup, " " * 8).strip(), name=self.NAME)
        result = self._run(project, ".", body)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines()[-1] == "None"
        assert not planted.exists()

    def test_positive_control_without_hardening(
        self, layout: tuple[Path, Path, Path, Path]
    ) -> None:
        # The same spawn without the hardening runs the planted file, so the
        # tests above are not passing for some unrelated reason.
        project, safe, planted, _legit = layout
        body = self.SPAWN.format(setup="pass", name=self.NAME)
        result = self._run(project, f".:{safe}", body)
        assert result.returncode == 0, result.stderr
        assert planted.exists()


def _copy_python(directory: Path, name: str) -> None:
    """A runnable copy of this interpreter as ``<directory>/<name>.exe``."""
    import shutil

    directory.mkdir(parents=True, exist_ok=True)
    # A virtualenv's ``python.exe`` is a redirector that needs its
    # ``pyvenv.cfg``; copy the base interpreter it launches instead.
    exe = Path(getattr(sys, "_base_executable", sys.executable))
    shutil.copy(exe, directory / f"{name}.exe")
    # The interpreter DLLs must sit next to the copy; the stdlib comes from
    # PYTHONHOME (``sys.base_prefix``), set by the caller.
    for dll in exe.parent.glob("*.dll"):
        shutil.copy(dll, directory / dll.name)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows current-directory search")
class TestWindowsCurrentDirectory:
    NAME = "stm-exec-env-probe"

    @pytest.fixture
    def layout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
        project, safe = tmp_path / "project", tmp_path / "safe"
        _copy_python(project, self.NAME)
        _copy_python(safe, self.NAME)
        monkeypatch.chdir(project)
        monkeypatch.setenv("PATH", str(safe))
        monkeypatch.setenv("PYTHONHOME", sys.base_prefix)
        monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
        return project, safe

    def _ran_from(self) -> Path:
        out = subprocess.run(
            [self.NAME, "-c", "import sys; print(sys.executable)"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout.strip()
        return Path(out).resolve().parent

    def test_which(self, layout: tuple[Path, Path]) -> None:
        import shutil

        project, safe = layout
        # Positive control: without the variable the current directory wins.
        before = shutil.which(self.NAME)
        assert before is not None
        assert Path(before).resolve().parent == project.resolve()
        harden_exec_search()
        after = shutil.which(self.NAME)
        assert after is not None
        assert Path(after).resolve().parent == safe.resolve()

    def test_create_process(self, layout: tuple[Path, Path]) -> None:
        # A bare name handed to CreateProcess, as ``subprocess`` does for the
        # host CLIs and the MCP SDK does when its own lookup finds nothing.
        project, safe = layout
        assert self._ran_from() == project.resolve()  # positive control
        harden_exec_search()
        assert self._ran_from() == safe.resolve()
