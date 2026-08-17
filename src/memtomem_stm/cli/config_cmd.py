"""``mms config`` Click subgroup — config-file linting (#611).

``mms config validate`` checks the proxy config file *as written* — no env
overlay — and reports everything the lenient runtime load silently drops or
papers over: unknown keys at every nesting level (the models deliberately
keep pydantic's ``extra="ignore"`` for forward compat, so a typo'd key just
vanishes), schema validation errors (which a running server degrades to
env/defaults on), and the permissive-file-mode warning. Strict by design:
parse errors, validation errors, unknown keys, and a missing file all exit
non-zero so the command can gate CI or a commit hook. The runtime load path
stays lenient — strictness lives only here.

Lives in its own file per the subgroup convention (``mms_project.py`` etc.)
and must not import from ``cli/proxy.py`` — that module imports this group
for registration, so the dependency only goes one way.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
from pydantic import ValidationError

from memtomem_stm.cli._defaults import DEFAULT_PROXY_CONFIG
from memtomem_stm.cli._display import _disp
from memtomem_stm.utils.json_out import dumps as _json_dumps
from memtomem_stm.proxy.config import (
    ProxyConfig,
    _has_annotation_policy,
    _permissive_mode,
    _upstream_inert_state,
    find_unknown_keys,
)

_DEFAULT_CONFIG = DEFAULT_PROXY_CONFIG


def _color_on() -> bool:
    return "NO_COLOR" not in os.environ


def _err(s: str) -> str:
    return click.style(s, fg="red", bold=True) if _color_on() else s


def _warn(s: str) -> str:
    return click.style(s, fg="yellow") if _color_on() else s


def _ok(s: str) -> str:
    return click.style(s, fg="green") if _color_on() else s


@click.group(name="config")
def config_group() -> None:
    """Inspect and validate the proxy config file."""


@config_group.command(name="validate")
@click.option(
    "--config",
    "config_path",
    default=str(_DEFAULT_CONFIG),
    show_default=True,
    help="Path to the proxy config JSON.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON for scripting.")
def validate_command(config_path: str, as_json: bool) -> None:
    """Strictly validate the config file as written (no env overlay).

    Exits non-zero on parse errors, schema validation errors, unknown keys
    (silently ignored at runtime — usually typos), or a missing file.
    Env vars (MEMTOMEM_STM_PROXY__*) are deliberately not merged: this
    lints the artifact you edit and commit, not the runtime composite.
    """
    resolved = Path(config_path).expanduser().resolve()
    errors: list[str] = []
    unknown_keys: list[str] = []
    warnings: list[str] = []
    status = "ok"

    if not resolved.exists():
        status = "missing"
        errors.append(f"config file not found: {resolved}")
    else:
        mode = _permissive_mode(resolved)
        if mode is not None:
            warnings.append(f"permissive mode {mode:o} — consider restricting to 0600")
        data = None
        try:
            data = json.loads(resolved.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                errors.append(f"config root must be a JSON object, got {type(data).__name__}")
                data = None
        except json.JSONDecodeError as exc:
            errors.append(f"invalid JSON: {exc}")
        except OSError as exc:
            errors.append(f"cannot read file: {exc}")
        if data is not None:
            unknown_keys = find_unknown_keys(ProxyConfig, data)
            parsed: ProxyConfig | None = None
            try:
                parsed = ProxyConfig.model_validate(data)
            except ValidationError as exc:
                for err in exc.errors():
                    loc = ".".join(str(part) for part in err["loc"])
                    errors.append(f"{loc}: {err['msg']}" if loc else err["msg"])
            if parsed is not None and (
                inert := _upstream_inert_state(data, enabled=parsed.enabled)
            ):
                # Same advisory (and same shared predicate) as the runtime load
                # path and `mms doctor` (#831). Suppressed when validation
                # failed: those errors dominate and `parsed.enabled` would be
                # unknown anyway.
                warnings.append(
                    f"{len(parsed.upstream_servers)} upstream server(s) configured but the "
                    "proxy is disabled"
                    + (
                        " explicitly"
                        if inert == "explicit"
                        else ' ("enabled" is unset and defaults to false)'
                    )
                    + " — upstream tools will not be advertised to MCP clients. Add "
                    '"enabled": true (or remove upstream_servers) to silence this.'
                )
            cache = data.get("cache")
            cache_enabled = cache.get("enabled", True) if isinstance(cache, dict) else True
            if cache_enabled and not _has_annotation_policy(data):
                # Same advisory (and same shared predicate) as the runtime
                # load path; advisory-only, so it never flips the exit code.
                warnings.append(
                    "cache.tool_annotation_policy not set — using the 'conservative' "
                    "default. New configs are created with 'strict'; add "
                    '"cache": {"tool_annotation_policy": "strict"} (or "conservative" '
                    "to pin current behavior) to silence this."
                )
        if errors or unknown_keys:
            status = "invalid"

    if as_json:
        click.echo(
            _json_dumps(
                {
                    "config_path": str(resolved),
                    "status": status,
                    "errors": errors,
                    "unknown_keys": unknown_keys,
                    "warnings": warnings,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        # Every value below is config- or argv-derived and none is validated:
        # an unknown key is whatever the file contains, a pydantic `loc` is
        # built from the config's own keys (so a hostile *server name* renders
        # inside `upstream_servers.<name>.prefix`), and `resolved` comes from
        # `--config`. Escape the interpolated value only — the `_err`/`_warn`
        # styling wraps around it and its escape codes must stay real (#760).
        click.echo(f"Validating {_disp(str(resolved))}")
        for msg in errors:
            click.echo(f"{_err('Error:')} {_disp(msg)}")
        for path in unknown_keys:
            click.echo(f"{_err('Error:')} unknown key (silently ignored at runtime): {_disp(path)}")
        for msg in warnings:
            click.echo(f"{_warn('Warning:')} {_disp(msg)}")
        if status == "ok":
            click.echo(_ok("OK") + " — config is valid")
        else:
            click.echo(
                f"{_err('Invalid:')} {len(errors)} error(s), {len(unknown_keys)} unknown key(s)"
            )
    if status != "ok":
        raise SystemExit(1)
