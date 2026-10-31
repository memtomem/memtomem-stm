"""``mms eject`` over a deeply nested ``origin.original`` (#1282).

The depth at which the config decoder still parses an entry but a later walk
recurses depends on the interpreter and the call stack (#1275), so every
branch is pinned by injection: a marker key in the stored original selects
the one payload a patched ``copy.deepcopy`` / ``json.dumps`` refuses, and
everything else passes through. Each stage has one catch: resolution turns a
recursion into a plan error, execution into a failed row, and the manual hint
into a withheld-value line.
"""

import copy
import json
import subprocess

import pytest
from click.testing import CliRunner

from helpers import set_home
from memtomem_stm.cli import proxy as proxy_mod
from memtomem_stm.cli.proxy import cli

_MARKER = "__deep_probe__"
_PLAN_ERROR = (
    "the entry or its target configuration is nested too deeply to prepare a restore "
    "(value withheld). This STM entry was not changed"
)
_RUN_ERROR = (
    "the entry or its target configuration is nested too deeply to restore — the STM entry is kept"
)
_HINT_FALLBACK = (
    "# the restore payload for probe is nested too deeply to print; the entry is still "
    "in the STM config under upstream_servers.probe"
)


def _original(**extra) -> dict:
    return {"command": "/usr/bin/false", _MARKER: True, **extra}


def _marked(obj) -> bool:
    return isinstance(obj, dict) and _MARKER in obj


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolated home, a Desktop path inside it, and a writer for the STM config."""
    set_home(monkeypatch, tmp_path)
    monkeypatch.setenv("MMS_NO_HOST_PROBE", "1")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    desktop = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(proxy_mod, "_desktop_config_path", lambda: desktop)
    config = tmp_path / "stm.json"

    def write(kind: str = "claude-desktop", original: dict | None = None) -> bytes:
        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "probe": {
                            "prefix": "probe",
                            "transport": "stdio",
                            "command": "/usr/bin/false",
                            "origin": {
                                "source": {"kind": kind},
                                "imported_at": "2026-10-31T00:00:00Z",
                                "original": original if original is not None else _original(),
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return config.read_bytes()

    return {"home": tmp_path, "desktop": desktop, "config": config, "write": write}


@pytest.fixture
def fake_claude(sandbox, monkeypatch):
    """A ``claude mcp`` that really edits ``~/.claude.json`` user-scope servers.

    ``remove`` deletes and ``add-json`` sets, so a ``--force`` test can tell an
    overwritten host registration from one that survived.
    """
    host = sandbox["home"] / ".claude.json"
    calls: list[list[str]] = []

    def run(cmd, timeout=5, cwd=None):
        calls.append(list(cmd))
        data = json.loads(host.read_text(encoding="utf-8")) if host.exists() else {}
        servers = data.setdefault("mcpServers", {})
        if cmd[:3] == ["claude", "mcp", "remove"]:
            servers.pop(cmd[3], None)
        elif cmd[:3] == ["claude", "mcp", "add-json"]:
            servers[cmd[3]] = json.loads(cmd[4])
        host.write_text(json.dumps(data), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(proxy_mod, "_run_claude_mcp", run)
    return {"host": host, "calls": calls}


def _refuse_marked_deepcopy(monkeypatch) -> list:
    real = copy.deepcopy
    fired: list = []

    def deepcopy(obj, *args, **kwargs):
        if _marked(obj):
            fired.append(obj)
            raise RecursionError("private-canary")
        return real(obj, *args, **kwargs)

    monkeypatch.setattr(proxy_mod.copy, "deepcopy", deepcopy)
    return fired


def _refuse_marked_dumps(monkeypatch, *, after_resolution: bool) -> list:
    """Make ``json.dumps`` recurse on the marked payload.

    With ``after_resolution`` the failure is armed only once every plan has
    been resolved, so the resolution preflight passes and the execution and
    reporting catches are what get exercised.
    """
    real_dumps = json.dumps
    state = {"armed": not after_resolution}
    fired: list = []

    def dumps(obj, *args, **kwargs):
        if state["armed"] and _marked(obj):
            fired.append(obj)
            raise RecursionError("private-canary")
        return real_dumps(obj, *args, **kwargs)

    monkeypatch.setattr(proxy_mod.json, "dumps", dumps)
    if after_resolution:
        real_resolve = proxy_mod._resolve_eject_plan

        def resolve(*args, **kwargs):
            plan = real_resolve(*args, **kwargs)
            state["armed"] = True
            return plan

        monkeypatch.setattr(proxy_mod, "_resolve_eject_plan", resolve)
    return fired


def _eject(sandbox, *args: str):
    return CliRunner().invoke(cli, ["eject", "probe", *args, "--config", str(sandbox["config"])])


class TestResolutionStage:
    """A recursion while preparing the restore is a plan error; nothing runs."""

    @pytest.mark.parametrize(
        "args", [("--yes",), ("--dry-run",), ("--yes", "--keep")], ids=["yes", "dry-run", "keep"]
    )
    def test_copy_recursion_is_a_plan_error(self, sandbox, monkeypatch, args):
        before = sandbox["write"]()
        fired = _refuse_marked_deepcopy(monkeypatch)

        result = _eject(sandbox, *args)

        assert fired
        assert result.exit_code == 1, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert f"probe: cannot eject — {_PLAN_ERROR}" in result.output
        assert "private-canary" not in result.output
        assert sandbox["config"].read_bytes() == before
        assert not sandbox["desktop"].exists()

    def test_trial_encode_recursion_is_a_plan_error(self, sandbox, monkeypatch):
        """The copy succeeds, the encode the writers would need does not."""
        before = sandbox["write"]()
        fired = _refuse_marked_dumps(monkeypatch, after_resolution=False)

        result = _eject(sandbox, "--yes")

        assert fired
        assert result.exit_code == 1, result.output
        assert f"probe: cannot eject — {_PLAN_ERROR}" in result.output
        assert sandbox["config"].read_bytes() == before
        assert not sandbox["desktop"].exists()

    def test_json_mode_emits_one_document(self, sandbox, monkeypatch):
        before = sandbox["write"]()
        _refuse_marked_deepcopy(monkeypatch)

        result = _eject(sandbox, "--yes", "--json")

        assert result.exit_code == 1, result.output
        doc = json.loads(result.stdout)
        assert doc["ok"] is False
        assert [(row["name"], row["error"]) for row in doc["plan"]] == [("probe", _PLAN_ERROR)]
        assert sandbox["config"].read_bytes() == before


class TestExecutionStage:
    """A recursion after resolution is a failed row and never removes the STM entry."""

    def _verify_recurses(self, monkeypatch) -> list:
        fired: list = []

        def verify(plan):
            fired.append(plan.name)
            raise RecursionError("private-canary")

        monkeypatch.setattr(proxy_mod, "_eject_verify", verify)
        return fired

    @pytest.mark.parametrize("schema_loss", [False, True], ids=["plain", "accept-schema-loss"])
    def test_verify_recursion_after_a_write_keeps_the_stm_entry(
        self, sandbox, monkeypatch, schema_loss
    ):
        before = sandbox["write"]()
        fired = self._verify_recurses(monkeypatch)
        args = ["--yes", *(["--accept-schema-loss"] if schema_loss else [])]

        result = _eject(sandbox, *args)

        assert fired == ["probe"]
        assert result.exit_code == 1, result.output
        assert f"probe: {_RUN_ERROR}" in result.stderr
        assert "private-canary" not in result.output
        assert sandbox["config"].read_bytes() == before
        # The write ran before the verify: dual registration, not disappearance.
        restored = json.loads(sandbox["desktop"].read_text(encoding="utf-8"))
        assert restored["mcpServers"]["probe"] == _original()

    def test_verify_recursion_on_an_already_present_entry(self, sandbox, monkeypatch):
        before = sandbox["write"]()
        sandbox["desktop"].write_text(
            json.dumps({"mcpServers": {"probe": _original()}}), encoding="utf-8"
        )
        host_before = sandbox["desktop"].read_bytes()
        fired = self._verify_recurses(monkeypatch)

        result = _eject(sandbox, "--yes")

        assert fired == ["probe"]
        assert result.exit_code == 1, result.output
        assert f"probe: {_RUN_ERROR}" in result.stderr
        assert sandbox["config"].read_bytes() == before
        assert sandbox["desktop"].read_bytes() == host_before

    def test_json_mode_reports_the_failed_row_in_one_document(self, sandbox, monkeypatch):
        before = sandbox["write"]()
        self._verify_recurses(monkeypatch)

        result = _eject(sandbox, "--yes", "--json")

        assert result.exit_code == 1, result.output
        doc = json.loads(result.stdout)
        assert [(row["name"], row["error"]) for row in doc["failed"]] == [("probe", _RUN_ERROR)]
        assert sandbox["config"].read_bytes() == before

    @pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
    def test_verify_decode_recursion_is_not_a_schema_loss(self, sandbox, monkeypatch, as_json):
        """The real verifier re-reads the host file after the write. A decoder
        recursion there used to read as "entry not found", which
        ``--accept-schema-loss`` accepted, removing the STM entry unverified."""
        before = sandbox["write"]()
        desktop = sandbox["desktop"]
        real_write = proxy_mod._eject_host_write
        real_loads = json.loads
        state = {"armed": False}

        def write(plan):
            outcome = real_write(plan)
            state["armed"] = True
            return outcome

        def loads(text, *args, **kwargs):
            if state["armed"] and isinstance(text, str) and _MARKER in text:
                raise RecursionError("private-canary")
            return real_loads(text, *args, **kwargs)

        monkeypatch.setattr(proxy_mod, "_eject_host_write", write)
        monkeypatch.setattr(proxy_mod.json, "loads", loads)
        args = ["--yes", "--accept-schema-loss", *(["--json"] if as_json else [])]

        result = _eject(sandbox, *args)

        assert state["armed"]
        assert result.exit_code == 1, result.output
        row = (
            f"cannot verify the host entry: cannot parse target config {desktop} "
            "(RecursionError); fix it before ejecting — the STM entry is kept"
        )
        if as_json:
            doc = real_loads(result.stdout)
            assert doc["ok"] is False
            assert [(r["name"], r["error"]) for r in doc["failed"]] == [("probe", row)]
        else:
            assert f"probe: {row}" in result.stderr
        assert "private-canary" not in result.output
        assert sandbox["config"].read_bytes() == before

    @pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
    def test_absent_host_entry_is_not_a_schema_loss(self, sandbox, monkeypatch, as_json):
        """A writer that reports success without creating the registration
        leaves nothing to verify; ``--accept-schema-loss`` used to accept that
        as a mismatch and remove the only copy from STM."""
        before = sandbox["write"]()
        monkeypatch.setattr(proxy_mod, "_eject_host_write", lambda plan: (True, None))
        args = ["--yes", "--accept-schema-loss", *(["--json"] if as_json else [])]

        result = _eject(sandbox, *args)

        assert result.exit_code == 1, result.output
        row = (
            "cannot verify the host entry: 'probe' is not at the expected host location "
            "after the restore — the STM entry is kept"
        )
        if as_json:
            doc = json.loads(result.stdout)
            assert doc["restored"] == []
            assert [(r["name"], r["error"]) for r in doc["failed"]] == [("probe", row)]
        else:
            assert f"probe: {row}" in result.stderr
        assert sandbox["config"].read_bytes() == before
        assert not sandbox["desktop"].exists()

    @pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
    def test_desktop_writer_encode_recursion_is_a_failed_row(self, sandbox, monkeypatch, as_json):
        """The JSON-file writers encode the whole host document after
        resolution; their own recursion catch (#1273) yields a failed row."""
        before = sandbox["write"]()
        desktop = sandbox["desktop"]
        desktop.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        host_before = desktop.read_bytes()
        real_dumps = proxy_mod._json_dumps
        fired: list = []

        def dumps(obj, *args, **kwargs):
            servers = obj.get("mcpServers") if isinstance(obj, dict) else None
            if isinstance(servers, dict) and _marked(servers.get("probe")):
                fired.append(obj)
                raise RecursionError("private-canary")
            return real_dumps(obj, *args, **kwargs)

        monkeypatch.setattr(proxy_mod, "_json_dumps", dumps)

        result = _eject(sandbox, "--yes", *(["--json"] if as_json else []))

        assert fired
        assert result.exit_code == 1, result.output
        row = f"{desktop} is nested too deeply to rewrite — fix or move it aside and retry"
        if as_json:
            doc = json.loads(result.stdout)
            assert [(r["name"], r["error"]) for r in doc["failed"]] == [("probe", row)]
        else:
            assert f"probe: {row}" in result.stderr
        assert "private-canary" not in result.output
        assert sandbox["config"].read_bytes() == before
        assert desktop.read_bytes() == host_before

    def test_force_does_not_pre_remove_when_the_argv_cannot_be_built(
        self, sandbox, monkeypatch, fake_claude
    ):
        """The argv check runs before the destructive pre-remove, and a depth
        failure is one of its answers: the different host registration that
        ``--force`` would replace survives, and so does the STM entry."""
        before = sandbox["write"]("claude-user")
        other = {"command": "/usr/bin/other-server"}
        fake_claude["host"].write_text(json.dumps({"mcpServers": {"probe": other}}))
        fired = _refuse_marked_dumps(monkeypatch, after_resolution=True)

        result = _eject(sandbox, "--yes", "--force")

        assert fired
        assert result.exit_code == 1, result.output
        assert (
            "probe: payload is nested too deeply to pass to `claude mcp add-json`" in result.stderr
        )
        assert fake_claude["calls"] == []
        host = json.loads(fake_claude["host"].read_text(encoding="utf-8"))
        assert host["mcpServers"]["probe"] == other
        assert sandbox["config"].read_bytes() == before


class TestManualHint:
    """The hint printed for a failed row withholds a payload it cannot encode."""

    def _host_write_fails(self, monkeypatch) -> None:
        monkeypatch.setattr(proxy_mod, "_eject_host_write", lambda plan: (False, "disk full"))

    def test_text_hint_falls_back(self, sandbox, monkeypatch):
        sandbox["write"]()
        self._host_write_fails(monkeypatch)
        _refuse_marked_dumps(monkeypatch, after_resolution=True)

        result = _eject(sandbox, "--yes")

        assert result.exit_code == 1, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "probe: disk full" in result.stderr
        assert f"  {_HINT_FALLBACK}" in result.stderr
        assert "private-canary" not in result.output

    def test_json_hint_falls_back(self, sandbox, monkeypatch):
        sandbox["write"]()
        self._host_write_fails(monkeypatch)
        _refuse_marked_dumps(monkeypatch, after_resolution=True)

        result = _eject(sandbox, "--yes", "--json")

        assert result.exit_code == 1, result.output
        doc = json.loads(result.stdout)
        assert [(row["name"], row["hint"]) for row in doc["failed"]] == [("probe", _HINT_FALLBACK)]

    def test_hint_still_renders_a_shallow_payload(self):
        hint = proxy_mod._eject_manual_hint("probe", "claude-user", None, {"command": "x"})

        assert hint == 'claude mcp add-json probe \'{"command": "x"}\' -s user'


def test_argv_spawn_error_names_each_cause(monkeypatch):
    assert proxy_mod._argv_spawn_error("probe", {"command": "x"}) is None
    assert (
        proxy_mod._argv_spawn_error("\udcff", {"command": "x"})
        == "server name or payload is not valid UTF-8"
    )
    _refuse_marked_dumps(monkeypatch, after_resolution=False)
    assert (
        proxy_mod._argv_spawn_error("probe", _original())
        == "payload is nested too deeply to pass to `claude mcp add-json`"
    )


def test_a_shallow_original_still_restores(sandbox):
    sandbox["write"](original={"command": "/usr/bin/false"})

    result = _eject(sandbox, "--yes")

    assert result.exit_code == 0, result.output
    assert "probe" not in json.loads(sandbox["config"].read_text())["upstream_servers"]
    restored = json.loads(sandbox["desktop"].read_text(encoding="utf-8"))
    assert restored["mcpServers"]["probe"] == {"command": "/usr/bin/false"}
