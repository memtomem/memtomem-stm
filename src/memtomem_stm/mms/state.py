"""Pydantic models + atomic TOML I/O for the three mms config files.

Spec: RFC §5.3 — three TOML files form W1's persistent state:

* ``~/.mms/registry.toml`` — global MCP definition catalog (secrets in env)
* ``~/.mms/projects.toml`` — auto-managed projects index
* ``<project>/.mms/project.toml`` — per-project enabled MCP names

All three start with ``schema_version = 1``. Loading a higher version
raises :class:`SchemaVersionMismatch` (W1 has no migration logic — RFC
§16's ``mms upgrade-config`` is a W2+ separate code path).

Path resolution goes through :func:`mms_home` etc. so tests can
``monkeypatch.setenv("HOME", tmp_path)`` and have everything land in a
sandbox.
"""

from __future__ import annotations

import tomllib
from datetime import datetime, timezone
from pathlib import Path

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from memtomem_stm.utils.fileio import atomic_write_text

SCHEMA_VERSION = 1

# Mode bits — registry holds secrets, projects index holds paths only,
# per-project file is committed and intentionally world-readable.
_REGISTRY_MODE = 0o600
_PROJECTS_INDEX_MODE = 0o600
_PROJECT_FILE_MODE: int | None = None  # respect user umask


# ---------------------------------------------------------------------------
# Path helpers — resolved at call time so monkeypatched HOME works in tests.
# ---------------------------------------------------------------------------


def mms_home() -> Path:
    """Global mms state directory (``~/.mms``)."""
    return Path.home() / ".mms"


def registry_path() -> Path:
    return mms_home() / "registry.toml"


def projects_index_path() -> Path:
    return mms_home() / "projects.toml"


PROJECT_MARKER_RELPATH = Path(".mms") / "project.toml"
"""Per-project marker relative path. Detection walks parents looking for this."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MmsConfigError(Exception):
    """Base for mms config load/save errors."""


class SchemaVersionMismatch(MmsConfigError):
    """Persisted ``schema_version`` differs from what this mms supports.

    W1 only handles ``schema_version = 1``. Higher versions need ``mms
    upgrade-config`` (W2+); lower versions are not yet possible (no W0).
    Raised by every loader so the CLI can present a single message.
    """

    def __init__(self, path: Path, found: int) -> None:
        super().__init__(
            f"{path}: schema_version={found}, this mms supports {SCHEMA_VERSION}. "
            "Run `mms upgrade-config` (planned for W2+) to migrate, or downgrade mms."
        )
        self.path = path
        self.found = found


class CorruptedConfig(MmsConfigError):
    """TOML parse failure or pydantic validation failure on load."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RegistryServer(BaseModel):
    """A single MCP server definition stored in ``registry.toml``."""

    model_config = ConfigDict(extra="forbid")

    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    prefix: str


class RegistryConfig(BaseModel):
    """Top-level model for ``~/.mms/registry.toml``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    servers: dict[str, RegistryServer] = Field(default_factory=dict)


class ProjectIndexEntry(BaseModel):
    """One row in ``projects.toml`` — auto-managed, do not hand-edit."""

    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    last_seen: str  # ISO 8601 UTC, e.g. "2026-05-01T13:24:03Z"


class ProjectsIndex(BaseModel):
    """Top-level model for ``~/.mms/projects.toml``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    projects: list[ProjectIndexEntry] = Field(default_factory=list)


class ProjectMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class ProjectMcp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: list[str] = Field(default_factory=list)


class ProjectConfig(BaseModel):
    """Top-level model for ``<project>/.mms/project.toml``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    project: ProjectMeta
    mcp: ProjectMcp = Field(default_factory=ProjectMcp)


# ---------------------------------------------------------------------------
# Load helpers
# ---------------------------------------------------------------------------


def _load_toml_dict(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise CorruptedConfig(path, f"TOML parse error: {e}") from e


def _check_schema_version(path: Path, raw: dict) -> None:
    found = raw.get("schema_version")
    if found is None:
        raise CorruptedConfig(path, "missing top-level `schema_version`")
    if not isinstance(found, int):
        raise CorruptedConfig(path, f"`schema_version` must be int, got {type(found).__name__}")
    if found != SCHEMA_VERSION:
        raise SchemaVersionMismatch(path, found)


def load_registry(path: Path | None = None) -> RegistryConfig:
    """Load ``registry.toml``. Returns empty config if file is missing."""
    p = path or registry_path()
    if not p.is_file():
        return RegistryConfig()
    raw = _load_toml_dict(p)
    _check_schema_version(p, raw)
    try:
        return RegistryConfig.model_validate(raw)
    except ValidationError as e:
        raise CorruptedConfig(p, f"schema validation: {e}") from e


def load_projects_index(path: Path | None = None) -> ProjectsIndex:
    """Load ``projects.toml``. Returns empty index if file is missing."""
    p = path or projects_index_path()
    if not p.is_file():
        return ProjectsIndex()
    raw = _load_toml_dict(p)
    _check_schema_version(p, raw)
    try:
        return ProjectsIndex.model_validate(raw)
    except ValidationError as e:
        raise CorruptedConfig(p, f"schema validation: {e}") from e


def load_project_config(path: Path) -> ProjectConfig:
    """Load a per-project ``project.toml``. Raises if file is missing."""
    if not path.is_file():
        raise CorruptedConfig(path, "file not found")
    raw = _load_toml_dict(path)
    _check_schema_version(path, raw)
    try:
        return ProjectConfig.model_validate(raw)
    except ValidationError as e:
        raise CorruptedConfig(path, f"schema validation: {e}") from e


# ---------------------------------------------------------------------------
# Save helpers
# ---------------------------------------------------------------------------


def save_registry(config: RegistryConfig, path: Path | None = None) -> None:
    """Atomically write ``registry.toml`` with mode 0o600 (contains secrets)."""
    p = path or registry_path()
    atomic_write_text(p, tomli_w.dumps(config.model_dump()), mode=_REGISTRY_MODE)


def save_projects_index(index: ProjectsIndex, path: Path | None = None) -> None:
    """Atomically write ``projects.toml`` with mode 0o600 (paths are private)."""
    p = path or projects_index_path()
    atomic_write_text(p, tomli_w.dumps(index.model_dump()), mode=_PROJECTS_INDEX_MODE)


def save_project_config(config: ProjectConfig, path: Path) -> None:
    """Atomically write a per-project ``project.toml`` (committed file, default mode)."""
    atomic_write_text(path, tomli_w.dumps(config.model_dump()), mode=_PROJECT_FILE_MODE)


# ---------------------------------------------------------------------------
# Index mutation helpers (used by `mms project init` to auto-add)
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    """ISO 8601 UTC string with seconds precision and trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_project_in_index(
    index: ProjectsIndex, *, name: str, path: str, now: str | None = None
) -> ProjectsIndex:
    """Return a new index with the (name, path) row upserted.

    Match key is ``path`` (canonical), not name — same path with a renamed
    project is the same entry. ``last_seen`` is refreshed on every upsert.
    """
    ts = now or utc_now_iso()
    new_entry = ProjectIndexEntry(name=name, path=path, last_seen=ts)
    others = [p for p in index.projects if p.path != path]
    return ProjectsIndex(
        schema_version=index.schema_version,
        projects=[*others, new_entry],
    )
