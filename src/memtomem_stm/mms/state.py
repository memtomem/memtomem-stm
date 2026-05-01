"""Pydantic models + atomic TOML I/O for the mms config files.

Three RFC §5.3 files plus one W2 sidecar form mms's persistent state:

* ``~/.mms/registry.toml`` — global MCP definition catalog (secrets in env)
* ``~/.mms/projects.toml`` — auto-managed projects index
* ``<project>/.mms/project.toml`` — per-project enabled MCP names
* ``~/.mms/import_state.toml`` — W2 drift-detection sidecar; per-server
  ``drift_hash`` (foundation in PR1, populated by ``mms import`` in PR2).

The first three share ``SCHEMA_VERSION``; the sidecar carries its own
``IMPORT_STATE_SCHEMA_VERSION`` so a future drift-hash algorithm bump
doesn't invalidate registries (and vice versa). Loading a file whose
recorded version differs from the matching constant raises
:class:`SchemaVersionMismatch` (W1/W2 have no migration logic — RFC
§16's ``mms upgrade-config`` is a separate code path).

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
"""Version constant for the three RFC §5.3 files (registry / projects index /
per-project config). Bump in lockstep when any of those shapes change.

The W2 sidecar has its own :data:`IMPORT_STATE_SCHEMA_VERSION` so a drift-hash
algorithm change can roll forward without forcing a registry migration."""

IMPORT_STATE_SCHEMA_VERSION = 1
"""Version constant for ``~/.mms/import_state.toml`` only. Bump when the
sidecar shape (entry fields, hash format) changes — does **not** force a
registry version bump."""

# Mode bits — registry holds secrets, projects index holds paths only,
# per-project file is committed and intentionally world-readable.
# Import-state sidecar reveals per-server source attribution + timestamps;
# treat as private even though the hash itself is opaque.
_REGISTRY_MODE = 0o600
_PROJECTS_INDEX_MODE = 0o600
_PROJECT_FILE_MODE: int | None = None  # respect user umask
_IMPORT_STATE_MODE = 0o600


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


def import_state_path() -> Path:
    """Drift-detection sidecar (W2). One file per host machine."""
    return mms_home() / "import_state.toml"


PROJECT_MARKER_RELPATH = Path(".mms") / "project.toml"
"""Per-project marker relative path. Detection walks parents looking for this."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MmsConfigError(Exception):
    """Base for mms config load/save errors."""


class SchemaVersionMismatch(MmsConfigError):
    """Persisted ``schema_version`` differs from what this mms supports.

    Each loader carries its own ``expected`` constant (registry/projects
    /project share :data:`SCHEMA_VERSION`; the sidecar uses
    :data:`IMPORT_STATE_SCHEMA_VERSION`) so the error message names the
    *right* number — a sidecar bump doesn't print "registry expects X".

    Higher versions need ``mms upgrade-config`` (planned for W2+); lower
    versions are not yet possible (no W0).
    """

    def __init__(self, path: Path, found: int, expected: int) -> None:
        super().__init__(
            f"{path}: schema_version={found}, this mms supports {expected}. "
            "Run `mms upgrade-config` (planned for W2+) to migrate, or downgrade mms."
        )
        self.path = path
        self.found = found
        self.expected = expected


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


class ImportStateEntry(BaseModel):
    """Drift-detection baseline for one imported MCP server.

    Populated by ``mms import --apply`` (PR2): when an entry is written to
    ``registry.toml``, the corresponding sidecar row records the
    :func:`memtomem_stm.mms.drift.compute_drift_hash` value, the import
    timestamp, and the human-readable host-source label that was shown in
    the ``--plan`` output. PR3 reads this row to classify the next scan's
    candidate as idempotent / drift / removed.

    The ``drift_hash`` regex is locked to the format
    :func:`memtomem_stm.mms.drift.compute_drift_hash` produces, so a typo
    or hand-edit that breaks the format surfaces as ``CorruptedConfig``
    on load instead of a confusing comparison failure later.
    """

    model_config = ConfigDict(extra="forbid")

    drift_hash: str = Field(pattern=r"^sha256:[0-9a-f]{16}$")
    drift_hash_version: int = Field(ge=1)
    last_imported: str  # ISO 8601 UTC, same shape as ProjectIndexEntry.last_seen
    source_label: str  # e.g. "Claude Code (user)", "Codex CLI"


class ImportState(BaseModel):
    """Top-level model for ``~/.mms/import_state.toml`` (W2 sidecar)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = IMPORT_STATE_SCHEMA_VERSION
    entries: dict[str, ImportStateEntry] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Load helpers
# ---------------------------------------------------------------------------


def _load_toml_dict(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise CorruptedConfig(path, f"TOML parse error: {e}") from e


def _check_schema_version(path: Path, raw: dict, expected: int) -> None:
    found = raw.get("schema_version")
    if found is None:
        raise CorruptedConfig(path, "missing top-level `schema_version`")
    if not isinstance(found, int):
        raise CorruptedConfig(path, f"`schema_version` must be int, got {type(found).__name__}")
    if found != expected:
        raise SchemaVersionMismatch(path, found, expected)


def load_registry(path: Path | None = None) -> RegistryConfig:
    """Load ``registry.toml``. Returns empty config if file is missing."""
    p = path or registry_path()
    if not p.is_file():
        return RegistryConfig()
    raw = _load_toml_dict(p)
    _check_schema_version(p, raw, SCHEMA_VERSION)
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
    _check_schema_version(p, raw, SCHEMA_VERSION)
    try:
        return ProjectsIndex.model_validate(raw)
    except ValidationError as e:
        raise CorruptedConfig(p, f"schema validation: {e}") from e


def load_project_config(path: Path) -> ProjectConfig:
    """Load a per-project ``project.toml``. Raises if file is missing."""
    if not path.is_file():
        raise CorruptedConfig(path, "file not found")
    raw = _load_toml_dict(path)
    _check_schema_version(path, raw, SCHEMA_VERSION)
    try:
        return ProjectConfig.model_validate(raw)
    except ValidationError as e:
        raise CorruptedConfig(path, f"schema validation: {e}") from e


def load_import_state(path: Path | None = None) -> ImportState:
    """Load the import-state sidecar. Returns empty state if file is missing.

    Uses :data:`IMPORT_STATE_SCHEMA_VERSION` — independent of registry's
    :data:`SCHEMA_VERSION` so a future drift-hash algorithm bump doesn't
    invalidate registries written under the same mms version.

    Sidecar absence is the normal "no baseline yet" case (e.g. brand-new
    install, or pre-W2 user upgrading) — same idiom as :func:`load_registry`.
    """
    p = path or import_state_path()
    if not p.is_file():
        return ImportState()
    raw = _load_toml_dict(p)
    _check_schema_version(p, raw, IMPORT_STATE_SCHEMA_VERSION)
    try:
        return ImportState.model_validate(raw)
    except ValidationError as e:
        raise CorruptedConfig(p, f"schema validation: {e}") from e


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


def save_import_state(state: ImportState, path: Path | None = None) -> None:
    """Atomically write ``import_state.toml`` with mode 0o600."""
    p = path or import_state_path()
    atomic_write_text(p, tomli_w.dumps(state.model_dump()), mode=_IMPORT_STATE_MODE)


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
