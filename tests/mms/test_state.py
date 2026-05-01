"""Tests for ``mms.state`` — pydantic models + atomic TOML round-trip."""

from __future__ import annotations

import stat
import tomllib
from pathlib import Path

import pytest

from memtomem_stm.mms import state


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    """Repoint ``~/.mms`` at a sandbox dir; yield it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_mms_home_resolves_under_home(sandbox_home):
    assert state.mms_home() == sandbox_home / ".mms"
    assert state.registry_path() == sandbox_home / ".mms" / "registry.toml"
    assert state.projects_index_path() == sandbox_home / ".mms" / "projects.toml"
    assert state.import_state_path() == sandbox_home / ".mms" / "import_state.toml"


def test_project_marker_relpath_is_dot_mms_project_toml():
    assert state.PROJECT_MARKER_RELPATH == Path(".mms") / "project.toml"


# ---------------------------------------------------------------------------
# Registry round-trip
# ---------------------------------------------------------------------------


def test_load_registry_returns_empty_when_missing(sandbox_home):
    cfg = state.load_registry()
    assert cfg.servers == {}
    assert cfg.schema_version == state.SCHEMA_VERSION


def test_registry_round_trip(sandbox_home):
    cfg = state.RegistryConfig(
        servers={
            "filesystem": state.RegistryServer(
                command="npx",
                args=["-y", "@modelcontextprotocol/server-filesystem", "/home/user"],
                prefix="fs",
            ),
            "github": state.RegistryServer(
                command="npx",
                args=["-y", "@mcp/github"],
                env={"GITHUB_TOKEN": "ghp_secret"},
                prefix="gh",
            ),
        }
    )
    state.save_registry(cfg)
    loaded = state.load_registry()
    assert loaded == cfg


def test_save_registry_uses_0o600(sandbox_home):
    cfg = state.RegistryConfig(servers={"foo": state.RegistryServer(command="echo", prefix="f")})
    state.save_registry(cfg)
    mode = stat.S_IMODE(state.registry_path().stat().st_mode)
    assert mode == 0o600


# ---------------------------------------------------------------------------
# Projects index round-trip + upsert
# ---------------------------------------------------------------------------


def test_load_projects_index_returns_empty_when_missing(sandbox_home):
    idx = state.load_projects_index()
    assert idx.projects == []


def test_projects_index_round_trip(sandbox_home):
    idx = state.ProjectsIndex(
        projects=[
            state.ProjectIndexEntry(
                name="proj-a", path="/tmp/proj-a", last_seen="2026-05-01T13:24:03Z"
            ),
            state.ProjectIndexEntry(
                name="proj-b", path="/tmp/proj-b", last_seen="2026-04-29T09:11:00Z"
            ),
        ]
    )
    state.save_projects_index(idx)
    loaded = state.load_projects_index()
    assert loaded == idx


def test_upsert_project_replaces_by_path(sandbox_home):
    idx = state.ProjectsIndex(
        projects=[
            state.ProjectIndexEntry(
                name="old-name", path="/tmp/proj", last_seen="2026-01-01T00:00:00Z"
            ),
        ]
    )
    new_idx = state.upsert_project_in_index(
        idx, name="new-name", path="/tmp/proj", now="2026-05-01T13:24:03Z"
    )
    assert len(new_idx.projects) == 1
    assert new_idx.projects[0].name == "new-name"
    assert new_idx.projects[0].last_seen == "2026-05-01T13:24:03Z"


def test_upsert_project_appends_new_path(sandbox_home):
    idx = state.ProjectsIndex(
        projects=[
            state.ProjectIndexEntry(
                name="proj-a", path="/tmp/proj-a", last_seen="2026-01-01T00:00:00Z"
            ),
        ]
    )
    new_idx = state.upsert_project_in_index(
        idx, name="proj-b", path="/tmp/proj-b", now="2026-05-01T13:24:03Z"
    )
    assert len(new_idx.projects) == 2
    assert {p.name for p in new_idx.projects} == {"proj-a", "proj-b"}


def test_utc_now_iso_format(sandbox_home):
    s = state.utc_now_iso()
    # 2026-05-01T13:24:03Z
    assert len(s) == 20
    assert s.endswith("Z")
    assert s[10] == "T"


# ---------------------------------------------------------------------------
# Import-state sidecar (W2 drift-detection foundation)
# ---------------------------------------------------------------------------


def test_load_import_state_returns_empty_when_missing(sandbox_home):
    s = state.load_import_state()
    assert s.entries == {}
    assert s.schema_version == state.SCHEMA_VERSION


def test_import_state_round_trip(sandbox_home):
    s = state.ImportState(
        entries={
            "filesystem": state.ImportStateEntry(
                drift_hash="sha256:40c96f4ab4762ceb",
                drift_hash_version=1,
                last_imported="2026-05-01T13:24:03Z",
                source_label="Claude Code (user)",
            ),
            "github": state.ImportStateEntry(
                drift_hash="sha256:1234567890abcdef",
                drift_hash_version=1,
                last_imported="2026-05-01T13:24:03Z",
                source_label="Codex CLI",
            ),
        }
    )
    state.save_import_state(s)
    loaded = state.load_import_state()
    assert loaded == s


def test_save_import_state_uses_0o600(sandbox_home):
    s = state.ImportState(
        entries={
            "x": state.ImportStateEntry(
                drift_hash="sha256:0123456789abcdef",
                drift_hash_version=1,
                last_imported="2026-05-01T13:24:03Z",
                source_label="Cursor (user)",
            )
        }
    )
    state.save_import_state(s)
    mode = stat.S_IMODE(state.import_state_path().stat().st_mode)
    assert mode == 0o600


def test_import_state_schema_version_mismatch(tmp_path):
    target = tmp_path / "import_state.toml"
    target.write_text("schema_version = 2\n[entries]\n", encoding="utf-8")
    with pytest.raises(state.SchemaVersionMismatch) as exc_info:
        state.load_import_state(target)
    assert exc_info.value.found == 2
    assert exc_info.value.path == target


def test_import_state_extra_field_rejected(tmp_path):
    """Sidecar shape is locked by ``extra="forbid"`` — an unknown column on
    an entry must be a CorruptedConfig, not a silent accept."""
    target = tmp_path / "import_state.toml"
    target.write_text(
        "schema_version = 1\n[entries.foo]\n"
        'drift_hash = "sha256:0000000000000000"\n'
        "drift_hash_version = 1\n"
        'last_imported = "2026-05-01T13:24:03Z"\n'
        'source_label = "test"\n'
        'unknown = "boom"\n',
        encoding="utf-8",
    )
    with pytest.raises(state.CorruptedConfig):
        state.load_import_state(target)


def test_import_state_on_disk_shape(sandbox_home):
    s = state.ImportState(
        entries={
            "filesystem": state.ImportStateEntry(
                drift_hash="sha256:40c96f4ab4762ceb",
                drift_hash_version=1,
                last_imported="2026-05-01T13:24:03Z",
                source_label="Claude Code (user)",
            )
        }
    )
    state.save_import_state(s)
    raw = tomllib.loads(state.import_state_path().read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["entries"]["filesystem"]["drift_hash"] == "sha256:40c96f4ab4762ceb"
    assert raw["entries"]["filesystem"]["drift_hash_version"] == 1
    assert raw["entries"]["filesystem"]["source_label"] == "Claude Code (user)"


# ---------------------------------------------------------------------------
# ProjectConfig round-trip
# ---------------------------------------------------------------------------


def test_project_config_round_trip(tmp_path):
    target = tmp_path / ".mms" / "project.toml"
    cfg = state.ProjectConfig(
        project=state.ProjectMeta(name="memtomem-stm"),
        mcp=state.ProjectMcp(enabled=["filesystem", "github"]),
    )
    state.save_project_config(cfg, target)
    loaded = state.load_project_config(target)
    assert loaded == cfg


def test_project_config_load_missing_raises(tmp_path):
    target = tmp_path / "nope.toml"
    with pytest.raises(state.CorruptedConfig) as exc_info:
        state.load_project_config(target)
    assert "file not found" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Schema version + corruption errors
# ---------------------------------------------------------------------------


def test_schema_version_mismatch_distinct_from_corruption(tmp_path):
    target = tmp_path / "project.toml"
    target.write_text(
        'schema_version = 2\n[project]\nname = "x"\n[mcp]\nenabled = []\n', encoding="utf-8"
    )
    with pytest.raises(state.SchemaVersionMismatch) as exc_info:
        state.load_project_config(target)
    assert exc_info.value.found == 2
    assert exc_info.value.path == target
    # And: message points at upgrade-config (W2+)
    assert "upgrade-config" in str(exc_info.value)


def test_missing_schema_version_is_corruption(tmp_path):
    target = tmp_path / "project.toml"
    target.write_text('[project]\nname = "x"\n[mcp]\nenabled = []\n', encoding="utf-8")
    with pytest.raises(state.CorruptedConfig) as exc_info:
        state.load_project_config(target)
    assert "schema_version" in exc_info.value.reason
    # And: not a SchemaVersionMismatch — a different code path
    assert not isinstance(exc_info.value, state.SchemaVersionMismatch)


def test_toml_parse_error_is_corruption(tmp_path):
    target = tmp_path / "project.toml"
    target.write_text("this is = not [valid toml\n", encoding="utf-8")
    with pytest.raises(state.CorruptedConfig) as exc_info:
        state.load_project_config(target)
    assert "TOML parse error" in exc_info.value.reason


def test_extra_field_rejected(tmp_path):
    target = tmp_path / "project.toml"
    target.write_text(
        'schema_version = 1\n[project]\nname = "x"\nextra_unknown = "boom"\n[mcp]\nenabled = []\n',
        encoding="utf-8",
    )
    with pytest.raises(state.CorruptedConfig):
        state.load_project_config(target)


def test_schema_version_wrong_type(tmp_path):
    target = tmp_path / "project.toml"
    target.write_text(
        'schema_version = "1"\n[project]\nname = "x"\n[mcp]\nenabled = []\n',
        encoding="utf-8",
    )
    with pytest.raises(state.CorruptedConfig) as exc_info:
        state.load_project_config(target)
    assert "must be int" in exc_info.value.reason


# ---------------------------------------------------------------------------
# TOML on-disk shape sanity (matches RFC §5.3 examples)
# ---------------------------------------------------------------------------


def test_registry_on_disk_shape_matches_rfc(sandbox_home):
    cfg = state.RegistryConfig(
        servers={
            "filesystem": state.RegistryServer(command="npx", args=["-y", "@mcp/fs"], prefix="fs")
        }
    )
    state.save_registry(cfg)
    raw = tomllib.loads(state.registry_path().read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["servers"]["filesystem"]["command"] == "npx"
    assert raw["servers"]["filesystem"]["prefix"] == "fs"


def test_projects_index_on_disk_shape_matches_rfc(sandbox_home):
    idx = state.ProjectsIndex(
        projects=[
            state.ProjectIndexEntry(name="x", path="/tmp/x", last_seen="2026-05-01T13:24:03Z")
        ]
    )
    state.save_projects_index(idx)
    raw = tomllib.loads(state.projects_index_path().read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    # RFC §5.3 uses [[projects]] array-of-tables
    assert isinstance(raw["projects"], list)
    assert raw["projects"][0]["name"] == "x"


def test_project_config_on_disk_shape_matches_rfc(tmp_path):
    target = tmp_path / "project.toml"
    cfg = state.ProjectConfig(
        project=state.ProjectMeta(name="proj"),
        mcp=state.ProjectMcp(enabled=["a", "b"]),
    )
    state.save_project_config(cfg, target)
    raw = tomllib.loads(target.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["project"]["name"] == "proj"
    assert raw["mcp"]["enabled"] == ["a", "b"]
