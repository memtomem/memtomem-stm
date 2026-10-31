"""Best-effort host discovery survives recursion failures (#1273, #1280).

Two depth limits meet a deep host entry on the import flows. The interpreter's
recursion limit depends on the call stack, so its branches are pinned by
injection (#1275). pydantic-core's serializer cap is a fixed nesting depth on
every interpreter, so the ``origin.original`` abort is pinned with real data.
"""

import copy
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from helpers import set_home
from memtomem_stm.cli import proxy as proxy_mod
from memtomem_stm.cli.proxy import cli

# Past pydantic-core's serializer cap (254 levels), well inside what the
# decoder and ``copy.deepcopy`` survive on the supported interpreters.
_PAST_SERIALIZER_CAP = 300
_DEEP_MARKER = "__deep_probe__"


def _nested(depth: int) -> object:
    value: object = 0
    for _ in range(depth):
        value = [value]
    return value


def _sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: dict) -> Path:
    """Isolated home with one Claude Desktop config holding ``servers``."""
    set_home(monkeypatch, tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("MMS_NO_TUI", "1")
    monkeypatch.setenv("MMS_NO_HOST_PROBE", "1")
    monkeypatch.chdir(tmp_path)
    desktop = tmp_path / "claude_desktop_config.json"
    desktop.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    monkeypatch.setattr(proxy_mod, "_desktop_config_path", lambda: desktop)
    return desktop


def _fail_copying_marked_entries(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Make the module's ``copy.deepcopy`` recurse only for marked host entries."""
    real_deepcopy = copy.deepcopy
    fired: list[dict] = []

    def deepcopy(obj, *args, **kwargs):
        if isinstance(obj, dict) and _DEEP_MARKER in obj:
            fired.append(obj)
            raise RecursionError("private-canary")
        return real_deepcopy(obj, *args, **kwargs)

    monkeypatch.setattr(proxy_mod.copy, "deepcopy", deepcopy)
    return fired


@pytest.mark.parametrize("injected", [False, True], ids=["deep-file", "decoder-error"])
def test_add_from_clients_ignores_unreadable_host_json(tmp_path, monkeypatch, injected):
    set_home(monkeypatch, tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("MMS_NO_TUI", "1")
    monkeypatch.chdir(tmp_path)
    payload = "[" * 100_000 + "]" * 100_000
    host = tmp_path / ".claude.json"
    host.write_text(payload, encoding="utf-8")
    config = tmp_path / "proxy.json"
    config.write_text('{"upstream_servers": {}}', encoding="utf-8")
    calls = []
    real_loads = json.loads

    def loads(text, *args, **kwargs):
        if text == payload:
            calls.append(text)
            if injected:
                raise RecursionError("decoder recursion limit")
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)
    result = CliRunner().invoke(cli, ["add", "--from-clients", "--config", str(config)])

    assert calls, "the real discovery path must read the host config"
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "No MCP servers found" in result.output
    assert host.read_text(encoding="utf-8") == payload
    assert json.loads(config.read_text(encoding="utf-8")) == {"upstream_servers": {}}


class TestDiscoveryCopyRecursion:
    """An entry whose verbatim copy recurses is skipped, not the whole scan."""

    def test_sibling_is_still_imported_and_the_deep_entry_is_named(self, tmp_path, monkeypatch):
        desktop = _sandbox(
            tmp_path,
            monkeypatch,
            {
                "ok": {"command": "/usr/bin/true"},
                "probe": {"command": "/usr/bin/false", _DEEP_MARKER: "secret-value"},
            },
        )
        before = desktop.read_bytes()
        fired = _fail_copying_marked_entries(monkeypatch)
        config = tmp_path / "stm.json"

        result = CliRunner().invoke(cli, ["add", "--import", "--all", "--config", str(config)])

        assert result.exit_code == 0, result.output
        assert result.exception is None
        assert len(fired) == 1
        assert sorted(json.loads(config.read_text(encoding="utf-8"))["upstream_servers"]) == ["ok"]
        assert (
            "skipping 'probe' from Claude Desktop — it holds a value nested too deeply "
            "to copy (value withheld)." in result.stderr
        )
        assert "secret-value" not in result.output
        assert "private-canary" not in result.output
        assert desktop.read_bytes() == before

    def test_skipped_duplicate_is_not_on_the_winners_prune_list(self, tmp_path, monkeypatch):
        """A skipped copy must not join ``duplicates``: every prune surface acts
        on ``registrations``, so it stays in its host file."""
        server = {"command": "/usr/bin/true"}
        _sandbox(tmp_path, monkeypatch, {"twin": {**server, _DEEP_MARKER: True}})
        (tmp_path / ".claude.json").write_text(
            json.dumps({"mcpServers": {"twin": server}}), encoding="utf-8"
        )
        fired = _fail_copying_marked_entries(monkeypatch)

        candidates = proxy_mod._discover_candidates(tmp_path)

        assert len(fired) == 1
        assert [c.name for c in candidates] == ["twin"]
        assert [r.source for r in candidates[0].registrations] == ["Claude Code (user)"]


class TestOriginSerializerCap:
    """An entry past the serializer cap aborts the import before any write."""

    def _deep_desktop(self, tmp_path, monkeypatch) -> Path:
        return _sandbox(
            tmp_path,
            monkeypatch,
            {
                "ok": {"command": "/usr/bin/true"},
                "probe": {"command": "/usr/bin/false", "extra": _nested(_PAST_SERIALIZER_CAP)},
            },
        )

    def test_add_import_aborts_and_prunes_nothing(self, tmp_path, monkeypatch):
        desktop = self._deep_desktop(tmp_path, monkeypatch)
        before = desktop.read_bytes()
        config = tmp_path / "stm.json"

        result = CliRunner().invoke(
            cli,
            ["add", "--import", "--select", "ok,probe", "--prune", "--config", str(config)],
        )

        assert result.exit_code == 1, result.output
        assert not isinstance(result.exception, ValueError), result.exception
        assert (
            "Error: 'probe' in Claude Desktop holds a value nested too deeply to record as "
            "origin.original (value withheld); nothing was written." in result.stderr
        )
        assert not config.exists()
        assert desktop.read_bytes() == before
        assert not (tmp_path / ".memtomem" / "pruned_upstreams.json").exists()

    def test_add_import_json_emits_the_failure_document(self, tmp_path, monkeypatch):
        self._deep_desktop(tmp_path, monkeypatch)
        config = tmp_path / "stm.json"

        result = CliRunner().invoke(
            cli,
            ["add", "--import", "--select", "probe", "--json", "--config", str(config)],
        )

        assert result.exit_code == 1, result.output
        doc = json.loads(result.stdout)
        assert doc["ok"] is False
        assert doc["error"] == "origin_too_deep"
        assert doc["names"] == ["probe"]
        assert not config.exists()

    @pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
    def test_init_aborts_without_writing(self, tmp_path, monkeypatch, as_json):
        self._deep_desktop(tmp_path, monkeypatch)
        config = tmp_path / "stm.json"
        args = ["init", "--no-validate", "--client", "skip", "--config", str(config)]

        result = CliRunner().invoke(
            cli, [*args, *(["--json"] if as_json else [])], input="all\n\n\n"
        )

        assert result.exit_code == 1, result.output
        assert not isinstance(result.exception, ValueError), result.exception
        assert not config.exists()
        message = (
            "Error: 'probe' in Claude Desktop holds a value nested too deeply to record as "
            "origin.original (value withheld); nothing was written."
        )
        if as_json:
            doc = json.loads(result.stdout)
            assert doc["ok"] is False
            assert doc["error"] == "setup_failed"
            assert doc["message"] == message
        else:
            assert message in result.stderr

    def test_standalone_prune_still_handles_the_same_entry(self, tmp_path, monkeypatch):
        """``mms prune`` never builds an origin, so the serializer cap does not
        apply there: the host entry is backed up and removed (#1280 option B,
        rejected)."""
        desktop = self._deep_desktop(tmp_path, monkeypatch)
        config = tmp_path / "stm.json"
        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "probe": {
                            "prefix": "probe",
                            "transport": "stdio",
                            "command": "/usr/bin/false",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        result = CliRunner().invoke(cli, ["prune", "probe", "--yes", "--config", str(config)])

        assert result.exit_code == 0, result.output
        assert sorted(json.loads(desktop.read_text(encoding="utf-8"))["mcpServers"]) == ["ok"]
        backup = json.loads(
            (tmp_path / ".memtomem" / "pruned_upstreams.json").read_text(encoding="utf-8")
        )
        assert [row["name"] for row in backup["entries"]] == ["probe"]
        assert backup["entries"][0]["original"]["extra"] == _nested(_PAST_SERIALIZER_CAP)

    def test_construction_error_is_not_reported_as_depth(self):
        """Only the dump is translated: a ``ValidationError`` is a
        ``ValueError`` too, and is not a nesting failure."""
        from pydantic import ValidationError

        cand = proxy_mod._DiscoveredCandidate(
            name="bad",
            spec=proxy_mod._SOURCE_BY_LABEL["Claude Desktop"],
            entry={"transport": "stdio", "command": "x"},
            raw={"command": "x"},
            identity_unreadable=(),
            source_ref={"kind": "claude-desktop", "path": ["not", "a", "string"]},
        )

        with pytest.raises(ValidationError):
            proxy_mod._build_origin(cand, "2026-10-31T00:00:00Z")


class TestPruneBackupRecursion:
    """A backup failure refuses the append; the caller then skips the delete."""

    def _existing_log(self, tmp_path, monkeypatch) -> Path:
        set_home(monkeypatch, tmp_path)
        log = tmp_path / ".memtomem" / "pruned_upstreams.json"
        log.parent.mkdir(parents=True)
        log.write_text(json.dumps({"schema_version": 1, "entries": []}), encoding="utf-8")
        return log

    def test_log_too_deep_to_parse(self, tmp_path, monkeypatch):
        log = self._existing_log(tmp_path, monkeypatch)
        before = log.read_bytes()
        real_loads = json.loads

        def loads(text, *args, **kwargs):
            if text == before.decode("utf-8"):
                raise RecursionError("private-canary")
            return real_loads(text, *args, **kwargs)

        monkeypatch.setattr(json, "loads", loads)

        result = proxy_mod._append_pruned_backup("probe", {"kind": "claude-desktop"}, {}, "t")

        assert result == (
            False,
            f"backup log {log} is nested too deeply to parse — move it aside and retry",
        )
        assert log.read_bytes() == before

    def test_log_too_deep_to_rewrite(self, tmp_path, monkeypatch):
        log = self._existing_log(tmp_path, monkeypatch)
        before = log.read_bytes()

        def dumps(*_a, **_kw):
            raise RecursionError("private-canary")

        monkeypatch.setattr(proxy_mod, "_json_dumps", dumps)

        result = proxy_mod._append_pruned_backup("probe", {"kind": "claude-desktop"}, {}, "t")

        assert result == (
            False,
            f"backup log {log} cannot be rewritten: nested too deeply to serialize",
        )
        assert log.read_bytes() == before

    def test_prune_keeps_the_host_entry_when_the_backup_cannot_be_written(
        self, tmp_path, monkeypatch
    ):
        desktop = _sandbox(tmp_path, monkeypatch, {"probe": {"command": "/usr/bin/false"}})
        before = desktop.read_bytes()
        config = tmp_path / "stm.json"
        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "probe": {
                            "prefix": "probe",
                            "transport": "stdio",
                            "command": "/usr/bin/false",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        real_dumps = proxy_mod._json_dumps

        def dumps(obj, *args, **kwargs):
            if isinstance(obj, dict) and "entries" in obj:
                raise RecursionError("private-canary")
            return real_dumps(obj, *args, **kwargs)

        monkeypatch.setattr(proxy_mod, "_json_dumps", dumps)

        result = CliRunner().invoke(cli, ["prune", "probe", "--yes", "--config", str(config)])

        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit)
        log = tmp_path / ".memtomem" / "pruned_upstreams.json"
        assert f"backup log {log} cannot be rewritten: nested too deeply to serialize" in (
            result.output
        )
        assert "private-canary" not in result.output
        assert desktop.read_bytes() == before


class TestWriterSerializeRecursion:
    """Writers serialize before writing, so a recursion failure leaves the file."""

    def test_save_reports_a_config_write_error(self, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        config = tmp_path / "stm.json"
        config.write_text('{"upstream_servers": {}}', encoding="utf-8")
        before = config.read_bytes()

        def dumps(*_a, **_kw):
            raise RecursionError("private-canary")

        monkeypatch.setattr(proxy_mod, "_json_dumps", dumps)

        result = CliRunner().invoke(
            cli,
            ["add", "fs", "--command", "/usr/bin/true", "--prefix", "fs", "--config", str(config)],
        )

        assert result.exit_code == 1, result.output
        assert "cannot serialize" in result.stderr
        assert "(RecursionError); the file was not replaced by this write" in result.stderr
        assert "private-canary" not in result.output
        assert config.read_bytes() == before

    def test_mcp_json_writer_aborts_without_writing(self, tmp_path, monkeypatch, capsys):
        mcp_path = tmp_path / ".mcp.json"
        prior = '{"mcpServers": {"existing": {"command": "keep-me"}}}\n'
        mcp_path.write_text(prior, encoding="utf-8")

        def dumps(*_a, **_kw):
            raise RecursionError("private-canary")

        monkeypatch.setattr(proxy_mod.json, "dumps", dumps)

        with pytest.raises(SystemExit) as exc_info:
            proxy_mod._write_mcp_json_for_stm(tmp_path, "memtomem-stm", [])

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "nested too deeply to serialize (RecursionError)" in err
        assert "Registration aborted; the file was not modified." in err
        assert mcp_path.read_text(encoding="utf-8") == prior
