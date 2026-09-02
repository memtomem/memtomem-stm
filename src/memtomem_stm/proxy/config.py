"""Proxy gateway configuration."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import types
from collections.abc import Mapping
from urllib.parse import urlparse
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self, Union, get_args, get_origin

from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic.fields import FieldInfo

from memtomem_stm.proxy.token_estimate import tokens_to_chars
from pydantic_core import ErrorDetails
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsError,
)

from memtomem_stm.proxy import prefixes

logger = logging.getLogger(__name__)


_PROXY_ENV_PREFIX = "MEMTOMEM_STM_PROXY__"
_PROXY_ENV_BARE = _PROXY_ENV_PREFIX[:-2]  # the block's own name, one JSON payload


_MISSING = object()


#: Floor for ``max_description_chars`` at both the global and the per-server
#: level. ``max_description_chars`` caps the client-visible description, out of
#: which the ``[proxied] `` prefix (10) and a truncation ellipsis (3) are fixed
#: costs; the floor leaves ~19 characters for upstream text (#893).
#:
#: 32 is a usability choice, not the arithmetic minimum. That minimum is 10:
#: registration prepends the prefix unconditionally, so a cap below its length
#: cannot be met at all, while every cap of 10 or more is met exactly. The
#: floor is set where a surviving description can still say something, and it
#: does NOT guarantee one — an upstream that supplies no description
#: contributes no text at any cap, leaving only the prefix and, where it fits,
#: the convention suffix (#896). Not imported from ``tool_metadata``, which
#: imports this module.
MIN_DESCRIPTION_CHARS = 32


@dataclass(frozen=True)
class EnvOverlayResult:
    """The resolved proxy env overlay plus the raw variables that produced it.

    ``fragment`` is exactly what settings resolved (plain dicts, no marks) and
    is the only part most callers consume. The raw sides exist for
    ``_env_override_hint``, which attributes validation errors to variables by
    *re-measuring* the environment (leave-one-out trials, see the hint), not
    by inferring which variable supplied which subtree — the reconstruction
    #843 removed after four review rounds each found a fresh counterexample
    in it.

    ``names`` maps each lowered name to the ORIGINAL spelling that supplied
    the surviving value (last case-equivalent spelling wins, matching the
    value collapse in ``_MappingEnvSource._load_env_vars``), so warnings
    render a variable the operator can actually find on a case-sensitive
    system. ``malformed`` records the names settings refuses to decode — a
    per-variable fact, computed once so per-trial rebuilds need not re-probe.

    ``rejected`` records the variables whose payload the overlay dropped
    ENTIRELY rather than carrying into the fragment: a bare
    ``MEMTOMEM_STM_PROXY`` that is not valid JSON, or that decodes to
    something other than an object. Those resolve to an empty fragment, so
    without this field they are indistinguishable from "the operator set
    nothing" — while the server refuses to start on exactly that environment.
    A caller that must not act on a config the operator did not choose reads
    this; ``__bool__`` deliberately stays fragment-only, since the fragment is
    still what merges.
    """

    fragment: dict[str, Any]
    scoped: dict[str, str]
    names: dict[str, str]
    malformed: frozenset[str]
    rejected: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.fragment)


def _as_overlay(value: EnvOverlayResult | dict[str, Any] | None) -> EnvOverlayResult | None:
    """Normalize the ``env_overrides`` boundary.

    Callers that build a plain dict by hand (tests, mostly) keep working: the
    fragment is honored everywhere, but with no raw variables to measure the
    hint stays silent — attribution requires the overlay to come from
    ``collect_proxy_env_overrides``.
    """
    if value is None:
        return None
    if isinstance(value, EnvOverlayResult):
        return value
    return EnvOverlayResult(fragment=value, scoped={}, names={}, malformed=frozenset())


class _MappingEnvSource(EnvSettingsSource):
    """``EnvSettingsSource`` reading a supplied mapping instead of the process.

    The one hook needed to ask settings what an arbitrary environment resolves
    to. Names are lower-cased here for the same reason settings lower-cases
    them: two spellings of one variable collapse into a single entry, last
    value at the first position, and that surviving position is what decides
    whether a mapping parent or a deeper child wins.
    """

    def __init__(self, settings_cls: type[BaseSettings], mapping: dict[str, str]) -> None:
        self._mapping = mapping
        super().__init__(settings_cls)

    def _load_env_vars(self) -> Mapping[str, str | None]:
        return {key.lower(): value for key, value in self._mapping.items()}


def _settings_proxy_fragment(scoped: dict[str, str]) -> dict[str, Any]:
    """What settings resolves ``scoped`` to under ``proxy``, before validation.

    A settings source explodes, decodes and canonicalizes but does not
    validate, which is exactly the leniency the overlay needs: it carries
    fragments the config file completes.
    """
    # Imported here because `memtomem_stm.config` imports this module.
    from memtomem_stm.config import STMConfig

    fragment = _MappingEnvSource(STMConfig, scoped)().get("proxy")
    return fragment if isinstance(fragment, dict) else {}


def _var_path(name: str) -> tuple[str, ...]:
    """The lower-cased variable name as a path under ``proxy``.

    Empty components are kept, not dropped: settings turns a doubled delimiter
    into an empty *key*, which is a real mapping entry
    (``…__UPSTREAM_SERVERS____PREFIX`` configures the server named "").

    The bare block name (``MEMTOMEM_STM_PROXY``, one JSON payload for the
    whole block, #840) is the empty path: it addresses the entire ``proxy``
    subtree, and the ordinary tuple-prefix comparisons place it at-or-above
    every location. It never COVERS a deeper variable, in either order —
    settings reads it as the base value that exploded variables deep-update —
    which ``_live_var_paths`` encodes explicitly.
    """
    if len(name) == len(_PROXY_ENV_BARE):
        return ()
    return tuple(name[len(_PROXY_ENV_PREFIX) :].split("__"))


def _live_var_paths(scoped: dict[str, str]) -> list[tuple[str, tuple[str, ...]]]:
    """The variables that still contributed something, in environment order.

    The proxy-scoped reading of ``live_env_paths``: settings resolves a mapping
    parent and a deeper child last-one-wins, so a variable whose path a LATER
    variable covers wrote nothing into the result. The bare block payload
    (empty path) is the exception — it is the base deeper variables
    deep-update, never a coverer.
    """
    entries = [(name, _var_path(name)) for name in scoped]
    non_covering = frozenset(name for name, path in entries if not path)
    return live_env_paths(entries, non_covering=non_covering)


def _fragment_for(scoped: dict[str, str], malformed: frozenset[str]) -> dict[str, Any]:
    """The overlay fragment for *scoped*, with malformed values kept raw.

    The one place the overlay pipeline lives: resolve the decodable variables
    with settings, then re-insert each malformed value as its raw string —
    but only where a later variable did not cover it, since re-inserting a
    value settings had already replaced would overwrite the payload that won
    with the string that lost. The collector calls this once for the full
    environment; ``_env_override_hint`` calls it per leave-one-out trial so
    every trial reproduces the pipeline exactly, malformed handling included.

    ``malformed`` is supplied by the caller (a per-variable fact — decoding
    is per-variable — computed once by ``collect_proxy_env_overrides``), so a
    trial does not re-probe every variable.
    """
    survivors = {k: v for k, v in scoped.items() if k not in malformed}
    try:
        fragment = _settings_proxy_fragment(survivors)
    except SettingsError:  # pragma: no cover - every undecodable value was removed
        logger.debug("Env overlay rebuild failed after dropping malformed values")
        fragment = {}
    if malformed:
        live = {name for name, _ in _live_var_paths(scoped)}
        for name in scoped:  # environment order, matching settings
            if name in malformed and name in live:
                _insert_raw(fragment, _var_path(name), scoped[name])
    return fragment


def _insert_raw(fragment: dict[str, Any], path: tuple[str, ...], raw: str) -> None:
    """Put an undecodable value back at its path, as the raw string.

    The empty path (a malformed bare ``MEMTOMEM_STM_PROXY`` payload) has no
    slot in a dict fragment — the raw string would BE the whole subtree — so
    it is skipped here; ``collect_proxy_env_overrides`` warns about it once.
    The server itself refuses to start on that value (``STMConfig()`` raises
    ``SettingsError``), so the overlay never diverges from a running config.
    """
    if not path:
        return
    cursor: dict[str, Any] = fragment
    for part in path[:-1]:
        existing = cursor.get(part, _MISSING)
        if existing is not _MISSING and not isinstance(existing, dict):
            # A parent variable supplied a non-mapping; settings does not let a
            # deeper one resurrect a mapping over it, so neither does this.
            return
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    cursor[path[-1]] = raw


def collect_proxy_env_overrides(environ: dict[str, str] | None = None) -> EnvOverlayResult:
    """Resolve ``MEMTOMEM_STM_PROXY__*`` env vars into an overlay.

    The ``fragment`` is layered on top of the JSON config file so the
    documented precedence (env > file > defaults) holds end-to-end. Without
    this, the file-load path in ``server.py`` would clobber every env-set
    field (``MEMTOMEM_STM_PROXY__ENABLED`` included — the file load is
    unconditional; env wins purely through this overlay).

    The resolution itself is pydantic-settings' own: prefix matching, case
    folding, ``__`` explosion, complex-field JSON decoding, field-key
    canonicalization and its container boundary, and parent/child ordering all
    come from an ``EnvSettingsSource``, not from a reimplementation of it. Five
    review rounds of #836 each found a fresh divergence between the two, all
    from the same root cause; asking settings is how that class closes (#837).

    One thing stays this module's own, because a settings source does not do
    it: a **malformed** complex value makes the source raise, while here it
    has to survive as the raw string and reach ``model_validate``, which names
    the field (see ``_fragment_for``). Substituting a default would be the
    silent degrade this module exists to prevent. Decoding is per-variable, so
    a variable that fails ON ITS OWN is a culprit and one that parses alone
    cannot be: settings stays the oracle even for attributing its own error.

    The raw sides of the result exist for ``_env_override_hint``, which
    attributes a later validation failure by re-measuring the environment
    rather than by provenance inference (#843).
    """
    env = environ if environ is not None else dict(os.environ)
    # Lowercase both sides rather than upper-casing the name: settings
    # lowercases, and the two are not inverses. `MEMTOMEM_ſTM_PROXY__…`
    # upper-cases onto the prefix (U+017F → "S") while settings, which never
    # upper-cases, ignores the variable entirely.
    prefix = _PROXY_ENV_PREFIX.lower()
    bare = _PROXY_ENV_BARE.lower()
    scoped: dict[str, str] = {}
    names: dict[str, str] = {}
    for key, val in env.items():
        lowered_key = key.lower()
        # The bare block name is honored by settings as one JSON payload for
        # the whole proxy block (#840); requiring the delimiter dropped it,
        # so with a file present the file silently won over a variable the
        # server honors. Exact match only: `MEMTOMEM_STM_PROXYX` is neither.
        if lowered_key == bare or lowered_key.startswith(prefix):
            # Case-equivalent spellings collapse the same way the values do:
            # last one wins, so the rendered name is the spelling that
            # supplied the surviving value.
            scoped[lowered_key] = val
            names[lowered_key] = key
    if not scoped:
        return EnvOverlayResult(fragment={}, scoped={}, names={}, malformed=frozenset())

    rejected: frozenset[str] = frozenset()
    try:
        fragment = _settings_proxy_fragment(scoped)
        malformed: frozenset[str] = frozenset()
        if bare in scoped:
            decoded_bare = json.loads(scoped[bare])  # decodable, or settings had raised
            if decoded_bare is not None and not isinstance(decoded_bare, dict):
                # `[]`, a string, a number: settings decodes it, validation
                # rejects the server outright — while the overlay would
                # silently resolve to nothing and let diagnostics describe a
                # config that cannot start. (`null` IS consistent: settings
                # falls back to the field default, which is exactly what an
                # empty overlay expresses.)
                logger.warning(
                    "Ignoring non-object %s payload (%s) — the server itself "
                    "rejects this environment at startup",
                    names[bare],
                    type(decoded_bare).__name__,
                )
                rejected = frozenset({names[bare]})
    except SettingsError:
        malformed = frozenset(name for name in scoped if _fails_to_decode(name, scoped[name]))
        if bare in malformed and any(name == bare for name, _ in _live_var_paths(scoped)):
            # No dict slot can carry the raw string for the WHOLE block, so
            # the overlay cannot keep it the way it keeps deeper malformed
            # values. The server refuses to start on it (`STMConfig()`
            # raises), so this warning is the CLI-side diagnostic.
            logger.warning(
                "Ignoring malformed %s payload (not valid JSON) — the server "
                "itself rejects this environment at startup",
                names[bare],
            )
            rejected = frozenset({names[bare]})
        fragment = _fragment_for(scoped, malformed)
    return EnvOverlayResult(
        fragment=fragment, scoped=scoped, names=names, malformed=malformed, rejected=rejected
    )


def _rejected_env_error(overlay: EnvOverlayResult | None) -> str | None:
    """Why a caller must not act on this overlay, or ``None``.

    An overlay that dropped the operator's whole proxy block resolves to the
    same empty fragment as an unset environment, and every load then returns a
    config built from the file and the defaults — one the operator did not
    choose. The server refuses to start on that environment, so a command that
    is about to WRITE somewhere the config names has to see it too.

    Names only, never values: this string reaches an operator's terminal and a
    ``--json`` document, and the payload it describes is the kind of place a
    credential gets pasted.
    """
    if overlay is None or not overlay.rejected:
        return None
    return (
        f"{', '.join(sorted(overlay.rejected))} could not be decoded as a proxy "
        "config object and was ignored entirely"
    )


def _fails_to_decode(name: str, value: str) -> bool:
    """Whether this variable alone is one settings refuses to decode."""
    try:
        _settings_proxy_fragment({name: value})
    except SettingsError:
        return True
    return False


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *overrides* on top of *base*; returns a new dict."""
    out = dict(base)
    for k, v in overrides.items():
        existing = out.get(k)
        if isinstance(v, dict) and isinstance(existing, dict):
            out[k] = _deep_merge(existing, v)
        else:
            out[k] = v
    return out


def _permissive_mode(resolved: Path) -> int | None:
    """Mode bits of *resolved* when it is group/world-accessible, else ``None``.

    Shared by the load path and ``mms config validate`` so the two warnings
    can't drift apart. ``None`` also covers a failed ``stat`` (best-effort).
    """
    try:
        mode = resolved.stat().st_mode & 0o777
    except OSError:
        return None
    return mode if mode & 0o077 else None


def _has_annotation_policy(data: dict[str, Any]) -> bool:
    """Whether a raw config dict explicitly sets ``cache.tool_annotation_policy``.

    Shared by the load path and ``mms config validate`` so the two
    missing-policy advisories can't drift apart. A non-dict ``cache`` value
    counts as unset — validation will reject it separately.
    """
    cache = data.get("cache")
    return isinstance(cache, dict) and "tool_annotation_policy" in cache


def _upstream_inert_state(
    data: dict[str, Any], *, enabled: bool
) -> Literal["default", "explicit"] | None:
    """Whether configured upstreams are inert because the proxy is disabled.

    Upstream tools are only registered when ``proxy.enabled`` is true, so a
    disabled proxy with a populated ``upstream_servers`` advertises nothing to
    MCP clients while every direct probe of those servers still succeeds
    (#831). Shared by the load path, ``mms config validate`` and ``mms
    doctor`` so the three advisories can't drift apart.

    Returns ``None`` when there is nothing to say (proxy enabled, or no
    upstreams), ``"explicit"`` when the config states ``enabled`` itself — an
    operator choosing control-only mode — and ``"default"`` when the key is
    absent and the silent ``False`` default is what disabled the proxy.

    ``enabled`` is passed in from the *validated* model rather than read off
    ``data`` so env-string coercion ("0", "false") can't make the callers
    disagree with the runtime.
    """
    if enabled:
        return None
    servers = data.get("upstream_servers")
    if not isinstance(servers, dict) or not servers:
        return None
    return "explicit" if "enabled" in data else "default"


def model_upstream_inert_state(config: ProxyConfig) -> Literal["default", "explicit"] | None:
    """:func:`_upstream_inert_state` for a config with no raw dict behind it.

    The server's env-only startup keeps the ``STMConfig`` pydantic-settings
    parse instead of rebuilding from the overlay (see
    ``_apply_proxy_file_config``), so the only record of whether ``enabled``
    was *stated* is ``model_fields_set`` — the same signal the #288 surfacing
    advisory reads.
    """
    if config.enabled or not config.upstream_servers:
        return None
    return "explicit" if "enabled" in config.model_fields_set else "default"


def warn_if_upstreams_inert(
    state: Literal["default", "explicit"] | None,
    count: int,
    resolved: Path,
    *,
    logger_: logging.Logger,
) -> None:
    """Log the "#288 inert config" advisory for the upstream half (#831).

    One emitter for every path that can reach this state — the file load, the
    env-only load, and the server's missing-file no-swap — so the wording
    can't drift between the shapes that have a config file to inspect and the
    one that doesn't.

    The message says the gate is *applied at startup* because this also runs
    under ``ProxyConfigLoader``'s hot reload, where tool registration is
    already done: flipping a running proxy to ``enabled: false`` does not
    unadvertise anything until the next start, so an unqualified "will not be
    advertised" would be a false operational signal there.
    """
    if not state:
        return
    logger_.warning(
        "Proxy config %s configures %d upstream server(s) but the proxy is "
        "disabled%s — the upstream configuration is present but inert; the gate is "
        "applied at startup, so upstream tools are not advertised to MCP clients until "
        'it is true. Add "enabled": true (or remove upstream_servers) to silence.',
        resolved,
        count,
        " explicitly" if state == "explicit" else ' ("enabled" is unset and defaults to false)',
    )


def _sanitized_load_error(exc: Exception) -> str:
    """Error summary safe to surface beyond the local process log.

    ``ConfigLoadResult.error`` flows to the MCP client via
    ``stm_proxy_health``, so it must not echo config *values*. Pydantic
    smuggles them in two ways: ``input_value=...`` (dropped via
    ``include_input=False``) and the rendered ``msg`` of a custom
    model-validator — e.g. the duplicate-prefix check embeds the prefix
    string, which is the secret itself if someone typos a token into a
    ``prefix`` field. So the summary uses ``loc`` + the machine-readable
    ``type`` code (``dict_type`` / ``value_error`` / ``missing`` …) only,
    never ``msg``. Full messages stay in the local stderr log and in
    ``mms config validate``, which reads the raw errors directly.

    Non-pydantic errors (``json.JSONDecodeError``, the non-object-root
    ``ValueError``) describe positions/types, not config values.
    """
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors(include_url=False, include_input=False):
            loc = ".".join(str(part) for part in err["loc"])
            parts.append(f"{loc} ({err['type']})" if loc else err["type"])
        summary = "; ".join(parts)
        return f"{exc.error_count()} validation error(s): {summary}"
    return str(exc)


_AMBIENT_VALIDATION_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
"""Process-env inputs ``ProxyConfig`` validation itself reads.

``LLMCompressorConfig._require_api_key_for_hosted_providers`` consults these
(stripped), so the same config dict can validate differently as they come and
go. The hint memo key includes their presence so a cached attribution never
outlives the validation behavior it measured; a drift-guard test pins that
each listed var actually affects validation.
"""

_ErrorKey = tuple[tuple[Any, ...], str, str]

_hint_memo: dict[str, str] = {}


def _error_key(e: ErrorDetails) -> _ErrorKey:
    return (tuple(e.get("loc", ())), str(e.get("type", "")), str(e.get("msg", "")))


def _error_template(key: _ErrorKey) -> _ErrorKey:
    """The error key with value-shaped tokens masked out of the message.

    Validator messages are format strings embedding the offending values
    (``reconnect_delay_seconds (40.0) must be <= …``), so two firings of the
    SAME check differ only in those tokens, while two DISTINCT checks that
    share ``(loc, type)`` — model validators raising ``ValueError`` at the
    same model — keep different wording. Masking numbers and quoted strings
    recovers the check's identity without knowing the schema; two distinct
    checks whose wording differs only in values would be conflated, which is
    accepted (the masking is syntactic, never a re-derivation of validator
    semantics).
    """
    loc, typ, msg = key
    msg = re.sub(r"'[^']*'", "'#'", msg)
    msg = re.sub(r"-?\d+(?:\.\d+)?", "#", msg)
    return (loc, typ, msg)


def _validation_error_keys(data: dict[str, Any] | None) -> frozenset[_ErrorKey]:
    """Error keys *data* reproduces ON ITS OWN under the lenient validation.

    Used for both directions of the differential probe: the file alone
    (empty when there is no file — every failure is env-caused) and the env
    overlay alone. A non-pydantic failure attributes nothing.
    """
    if data is None:
        return frozenset()
    try:
        ProxyConfig.model_validate(data)
    except ValidationError as exc:
        return frozenset(_error_key(e) for e in exc.errors())
    except Exception:
        return frozenset()
    return frozenset()


def _overlay_value_at(fragment: dict[str, Any], loc: tuple[Any, ...]) -> Any:
    """The overlay's value at *loc*, walking the resolved fragment.

    Returns the replacing non-dict when the walk dead-ends on one (an env
    string clobbered a container the model expected — the env still supplied
    whatever sits under *loc*), and ``_MISSING`` when the overlay never
    touched the path. Integer loc parts (list indices) match nothing in a
    dict fragment except through a non-dict dead-end above them.
    """
    node: Any = fragment
    for part in loc:
        if not isinstance(node, dict):
            return node
        if part in node:
            node = node[part]
        elif str(part) in node:
            node = node[str(part)]
        else:
            return _MISSING
    return node


def _hint_memo_key(
    overlay: EnvOverlayResult,
    file_data: dict[str, Any] | None,
    merged_errors: list[ErrorDetails],
) -> str:
    """Digest of everything attribution depends on.

    ``scoped`` items in INSERTION order — environment order affects
    parent/child resolution, so sorting would collapse environments that
    resolve differently. ``names`` is included so a respelled variable
    re-renders. Hashed so raw (possibly secret-bearing) values are not
    retained in the cache key.
    """
    ambient = tuple(
        (name, bool(os.environ.get(name, "").strip())) for name in _AMBIENT_VALIDATION_VARS
    )
    payload = repr(
        (
            tuple(overlay.scoped.items()),
            tuple(overlay.names.items()),
            tuple(sorted(overlay.malformed)),
            overlay.fragment,  # normally f(scoped), but the carrier is only
            # shallowly frozen and constructible by hand; attribution reads
            # the fragment directly, so it is part of the identity
            file_data,
            tuple(_error_key(e) for e in merged_errors),
            ambient,
        )
    )
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


def _env_override_hint(
    exc: Exception,
    env_overrides: EnvOverlayResult | dict[str, Any] | None,
    file_data: dict[str, Any] | None = None,
) -> str:
    """Name the env var(s) implicated in a config ValidationError.

    A broken ``MEMTOMEM_STM_PROXY__*`` value fails validation of the MERGED
    config, and the load falls back to defaults with a single warning —
    without this hint the operator sees "Failed to parse proxy config <file>"
    and debugs the FILE while the file is fine. (Fallback semantics are
    unchanged: this only improves the warning.)

    Attribution is by MEASUREMENT, not provenance inference (#843): a
    variable is implicated in an error exactly when removing that one
    variable and re-resolving the WHOLE remaining environment — settings
    itself, then the same merge and validation the caller ran — makes that
    error disappear without confounds. Only variable names the operator
    actually set are ever rendered (in their original spelling), so the hint
    cannot synthesize a name or echo a payload-internal mapping key.

    Per error, in order:

    1. **File pre-filter**: an error whose ``(loc, type, msg)`` the file
       reproduces on its own is file-caused and skipped — unless the overlay
       supplies the value at ``loc``, in which case the file's reproduction
       is about a value the merged config does not even hold (file ``-5`` vs
       env ``-6`` yield the identical gt-violation key) and measurement
       proceeds.
    2. **Clean implication**: removing the variable clears the error — where
       "clears" excludes the same validator re-firing at the same non-root
       loc with different embedded values (a mutation, not a clearing; root
       errors keep full-key identity because distinct validators share
       ``value_error`` there and only the message tells them apart). A trial
       that clears the error while genuinely REVEALING other errors proves
       nothing by itself — pydantic masks model validators behind new field
       errors, and raise-and-stop root validators surface the next error in
       line, so a repairing variable looks exactly like a causer. Such a
       confounded trial implicates only when the variable's own fragment,
       validated alone WITHOUT the file, reproduces the error (file-free on
       purpose: a single-variable overlay merged with the file is the merged
       config, which reproduces everything vacuously).
    3. **Fallbacks** when no variable is cleanly implicated:
       an error the file does NOT reproduce is certainly env-caused —
       variables below the loc (below the parent entry for ``missing``; a
       root error relates to all) are named, and a variable ABOVE it only
       with supply evidence: its own fragment holds the loc, and for a value
       error holds the very value the merged config complains about, so a
       shadowed different value or a sibling-only aggregate stays out;
       swap-only clearers are excluded, and a no-op removal (two variables
       supplying the same failing value) additionally needs its fragment to
       reproduce the error alone. An error the file DOES reproduce names
       only variables supplying the merged value at the loc as an observable
       non-dict overwrite.

    Documented no-hint corners (adjudicated in #843's plan review): a root
    error the file reproduces identically stays file-attributed even when an
    env payload shadows the file with the same broken content — at
    ``loc=()`` the shadower is indistinguishable from an innocent variable,
    and diagnosis converges sequentially (fix the file; a still-broken
    overlay then attributes cleanly). Likewise an env value that only
    changes the file's own aggregated root message may go unnamed rather
    than risk naming a repairer. And a shadowed value that is the coerced
    TWIN of the survivor (a payload's numeric ``-5`` under a deeper
    ``"-5"``) is not recognized as the same supply — key-level measurement
    cannot see pydantic coercion without re-deriving the schema, the class
    #842 closed — so the survivor is named and the twin surfaces on the
    next load.

    Trials are memoized (size-1, keyed on everything attribution reads)
    because a failed hot reload re-runs this warning every poll. Settings
    RESOLUTIONS stay linear in the live-variable count (the pinned bound);
    per-trial bookkeeping (`_live_var_paths` inside malformed rebuilds) is
    quadratic in variables but constant-factor cheap.
    """
    overlay = _as_overlay(env_overrides)
    if overlay is None or not overlay.fragment or not isinstance(exc, ValidationError):
        return ""
    if not overlay.scoped:
        return ""  # hand-built overlay: fragment honored, nothing to measure
    merged_errors = exc.errors()
    memo_key = _hint_memo_key(overlay, file_data, merged_errors)
    cached = _hint_memo.get(memo_key)
    if cached is not None:
        return cached
    hint = _attribute_env_overrides(overlay, merged_errors, file_data)
    _hint_memo.clear()  # size-1: the loader retries one failing state
    _hint_memo[memo_key] = hint
    return hint


def _attribute_env_overrides(
    overlay: EnvOverlayResult,
    merged_errors: list[ErrorDetails],
    file_data: dict[str, Any] | None,
) -> str:
    """Leave-one-out attribution over the live variables. See the hint."""
    scoped = overlay.scoped
    fragment = overlay.fragment
    merged_keys = frozenset(_error_key(e) for e in merged_errors)
    file_alone_keys = _validation_error_keys(file_data)
    merged_data = _deep_merge(file_data, fragment) if file_data is not None else fragment
    live = _live_var_paths(scoped)

    # One trial per live variable: the config without it. A variable a later
    # one covered is not live — it contributed nothing, and its value
    # resurfaces naturally inside the trial that removes the coverer.
    trials: dict[str, tuple[frozenset[_ErrorKey], dict[str, Any]] | None] = {}
    for name, _path in live:
        remaining = {k: v for k, v in scoped.items() if k != name}
        trial_fragment = _fragment_for(remaining, overlay.malformed - {name}) if remaining else {}
        trial_data = (
            _deep_merge(file_data, trial_fragment) if file_data is not None else trial_fragment
        )
        try:
            ProxyConfig.model_validate(trial_data)
        except ValidationError as trial_exc:
            trials[name] = (frozenset(_error_key(e) for e in trial_exc.errors()), trial_data)
        except Exception:  # non-pydantic: no attribution from this trial
            trials[name] = None
        else:
            trials[name] = (frozenset(), trial_data)

    # Lazy per-variable sufficiency probes: what one variable ALONE resolves
    # to, and what that fragment reproduces WITHOUT the file. Every place a
    # removal trial is ambiguous — it revealed other errors, or it changed
    # nothing because a sibling supplies the same value — asks these instead
    # of any whole-overlay, ancestry, or file-merged fact, which two
    # diff-review rounds showed over-attribute (a repair-only sibling, an
    # empty ancestor payload, a file error inherited into the probe: for a
    # single-variable overlay a file-merged probe IS the merged config, so it
    # reproduces every error vacuously).
    solo_fragments: dict[str, dict[str, Any]] = {}
    solo_keys: dict[str, frozenset[_ErrorKey]] = {}

    def _solo_fragment(name: str) -> dict[str, Any]:
        if name not in solo_fragments:
            solo_fragments[name] = _fragment_for({name: scoped[name]}, overlay.malformed & {name})
        return solo_fragments[name]

    def _solo_keys(name: str) -> frozenset[_ErrorKey]:
        if name not in solo_keys:
            solo_keys[name] = _validation_error_keys(_solo_fragment(name))
        return solo_keys[name]

    solo_with_file_keys: dict[str, frozenset[_ErrorKey]] = {}

    def _solo_with_file_keys(name: str) -> frozenset[_ErrorKey]:
        # The candidate's fragment completed by the file — the probe for an
        # env-internal swap, where the file's half of an entry (its prefix)
        # is needed before the failing validator even runs. Never consulted
        # for a repair-shaped reveal, where it would be vacuous: reveals of
        # file-alone keys with a single live variable make this probe the
        # merged config itself.
        if name not in solo_with_file_keys:
            frag = _solo_fragment(name)
            data = _deep_merge(file_data, frag) if file_data is not None else frag
            solo_with_file_keys[name] = _validation_error_keys(data)
        return solo_with_file_keys[name]

    implicated: set[str] = set()
    for err in merged_errors:
        key = _error_key(err)
        key_template = _error_template(key)
        loc = tuple(err.get("loc", ()))
        loc_strs = tuple(str(part) for part in loc)
        overlay_at_loc = _overlay_value_at(fragment, loc) if loc else _MISSING
        reaches = overlay_at_loc is not _MISSING
        if key in file_alone_keys and not reaches:
            continue  # exclusively file-caused

        def _solo_causes(name: str, revealed: frozenset[_ErrorKey]) -> bool:
            # Candidate-specific evidence for a confounded trial: the
            # variable's own fragment reproduces this error. Which probe
            # depends on the reveal's shape — a reveal containing a
            # file-alone key is repair-shaped, and the evidence must be
            # FILE-FREE (a repairing variable's file-merged probe is the
            # merged config itself and reproduces everything vacuously);
            # a purely env-internal reveal means at least two live variables
            # are interacting, and the probe may borrow the file's half of
            # the entry (the prefix that lets the failing validator run).
            if any(r in file_alone_keys for r in revealed):
                solo = _solo_keys(name)
                if key in solo:
                    return True
                # One escalation (diff review R6): when the file-free probe
                # could not even RUN the failing check — it fails with
                # ``missing`` errors on this error's own branch, the entry
                # fields the file supplies — AND the candidate supplies a
                # field this error's message names, complete with the file
                # and ask again. The field-name gate is what separates a
                # causer (its ``reconnect_delay_seconds`` is in the message
                # comparing reconnect to its maximum) from a repairer whose
                # contribution the failing check never reads (R5's
                # ``max_reconnect_delay_seconds`` under a call-timeout
                # message); validator messages conventionally name the
                # fields they compare, and a message that does not keeps the
                # variable unnamed — the fail-safe direction. Root errors
                # never escalate: for them the file-completed probe of a
                # lone variable is the merged config itself.
                blocked = bool(loc) and any(
                    r[1] == "missing" and (r[0][: len(loc)] == loc or loc[: len(r[0])] == r[0])
                    for r in solo
                )
                if not blocked:
                    return False
                supplied = _overlay_value_at(_solo_fragment(name), loc)
                names_under: set[str] = set()
                stack = [supplied]
                while stack:
                    node = stack.pop()
                    if isinstance(node, dict):
                        for k, v in node.items():
                            names_under.add(str(k))
                            stack.append(v)
                if not any(re.search(rf"\b{re.escape(field)}\b", key[2]) for field in names_under):
                    return False
                return key in _solo_with_file_keys(name)
            return key in _solo_with_file_keys(name)

        clean: set[str] = set()
        swapped: set[str] = set()
        noops: set[str] = set()
        for name, path in live:
            trial = trials[name]
            if trial is None:
                continue
            trial_keys, trial_data = trial
            if trial_data == merged_data:
                noops.add(name)  # removal changed nothing observable
                continue
            if key in trial_keys:
                continue  # removal did not clear this error
            if loc and any(_error_template(r) == key_template for r in trial_keys):
                # The same CHECK re-fired at the same non-root loc with
                # different embedded values: the error MUTATED, it did not
                # clear — a contextual pair like head_chars/min_head_chars
                # would otherwise read as cleared-plus-revealed in both
                # directions and nobody would be named (diff review R3).
                # Check identity is the masked message template, not
                # (loc, type): one model validator can hold several distinct
                # checks that all raise ``value_error`` at the same loc, and
                # a repair that exposes the NEXT check must not read as a
                # mutation of the first (diff review R5). Root errors keep
                # full-key identity throughout.
                continue
            revealed = {
                r
                for r in trial_keys - merged_keys
                if not (r[0] and any(_error_template(m) == _error_template(r) for m in merged_keys))
            }
            if revealed and not _solo_causes(name, frozenset(revealed)):
                # The removal did not just clear the error, it swapped it for
                # others: pydantic masks model validators behind new field
                # errors, and raise-and-stop root validators surface the next
                # error at the same loc — the disappearance proves nothing by
                # itself (a repairing variable looks exactly like this). Only
                # a variable that reproduces the error on its own is a
                # causer here.
                swapped.add(name)
                continue
            clean.add(name)
        if clean:
            implicated.update(clean)
            continue

        def _ancestor_supplies(name: str) -> bool:
            # Candidate-specific supply evidence for a variable ABOVE the
            # error's path (diff review R3): its own fragment must hold the
            # loc — and for a value error, hold the very value the merged
            # config complains about, so a payload whose different broken
            # value merely shares the generic error key stays out (fixing
            # the merged value first is the sequential diagnosis).
            if err.get("type") == "missing":
                target = loc[:-1]
                return bool(target) and (
                    _overlay_value_at(_solo_fragment(name), target) is not _MISSING
                )
            solo_at = _overlay_value_at(_solo_fragment(name), loc)
            return solo_at is not _MISSING and solo_at == _overlay_value_at(merged_data, loc)

        if key not in file_alone_keys:
            # Certainly env-caused, but no single removal was cleanly
            # implicating (overdetermined among env vars, mutated in every
            # trial, or masked in every trial): coarse path attribution,
            # excluding swap-only clearers; a variable BELOW the loc supplied
            # part of the failing subtree, while one ABOVE it needs the
            # supply evidence (an aggregate holding only a sibling entry is
            # an ancestor but not a cause); a no-op removal additionally
            # needs its fragment to reproduce the error alone (two variables
            # supplying the same failing value make every single removal a
            # no-op; an empty ancestor payload reproduces nothing).
            rel = loc_strs[:-1] if err.get("type") == "missing" else loc_strs
            for name, path in live:
                if name in swapped or (name in noops and key not in _solo_keys(name)):
                    continue
                trial = trials[name]
                if trial is not None and key in trial[0] and key not in _solo_keys(name):
                    # The EXACT error survived this variable's removal and the
                    # variable alone reproduces nothing: a bystander that
                    # merely shares the failing subtree (an unrelated timeout
                    # beside the reconnect pair, diff review R4). Variables
                    # whose removal mutated the error stay, as do exact
                    # survivors with solo evidence (same-value
                    # overdetermination).
                    continue
                below = path[: len(rel)] == rel
                above = rel[: len(path)] == path
                if not (below or above):
                    continue
                if not below and loc and not _ancestor_supplies(name):
                    continue
                implicated.add(name)
        elif reaches and not isinstance(overlay_at_loc, dict):
            # File/env overdetermination that passed the pre-filter: name
            # only variables whose OWN fragment supplies the merged value at
            # the loc (file `-5` beside env `-6` — or the identical `-5`),
            # never one that merely sits above it; only observable non-dict
            # overwrites at the loc qualify at all.
            for name, path in live:
                if loc_strs[: len(path)] != path:
                    continue
                if not _ancestor_supplies(name):
                    continue
                implicated.add(name)
    if not implicated:
        return ""
    rendered = sorted(overlay.names.get(name, name.upper()) for name in implicated)
    return " (env override(s) implicated: " + ", ".join(rendered) + ")"


def live_env_paths(
    entries: list[tuple[str, tuple[str, ...]]],
    *,
    non_covering: frozenset[str] = frozenset(),
) -> list[tuple[str, tuple[str, ...]]]:
    """The entries a later one did not cover, keeping order.

    Settings resolves a mapping parent and a deeper child last-one-wins, so a
    variable whose path a LATER variable covers contributed nothing to the
    resolved config. Naming it points the operator at a value that is not in
    the config being complained about, and hides the one that is. A later
    variable that goes DEEPER does not cover it: settings merges that on top
    and both are really present.

    ``non_covering`` names entries that never cover a deeper variable:
    exact-FIELD-NAME object payloads, which settings reads as the BASE value
    and deep-updates the delimiter-exploded variables on top of, so such a
    payload loses to a deeper variable in EITHER order (oracle-pinned,
    #840). Callers decide membership — the rule is about *object payloads on
    a field's own name*, not about path length: a scalar root variable
    (``…_LOG_LEVEL=INVALID``) genuinely discards a descendant settings
    ignores, and must keep covering it.
    """
    return [
        (name, path)
        for index, (name, path) in enumerate(entries)
        if not any(
            later_name not in non_covering and path[: len(later)] == later
            for later_name, later in entries[index + 1 :]
        )
    ]


def env_var_hint_for_validation_error(
    exc: Exception, environ: Mapping[str, str] | None = None
) -> str:
    """Name the ``MEMTOMEM_STM_*`` var(s) implicated in an ``STMConfig`` error.

    The sibling of ``_env_override_hint`` for the OTHER failure point: that one
    explains a merged-config failure inside the load path, which owns an env
    overlay to attribute against; this one explains ``STMConfig()`` itself
    failing, where the only evidence is the error location and the environment.

    Hints are derived from the var names that EXIST, never synthesized from the
    error location, so the operator is only ever pointed at something they set:

    - a var at or above the location is named (``…__CACHE__ENABLED=nope``, and
      an aggregate ``…__UPSTREAM_SERVERS`` payload whose inner entry faulted);
    - a var BELOW the location is named too, because a model-level validator
      reports at the model's own path while the offending field sits under it;
    - a ``missing`` error is the exception that needs its SIBLINGS: the field
      the error names is precisely the one nobody set, so the vars that created
      the incomplete entry are the ones to fix (``…__GH__COMMAND`` for a
      ``upstream_servers.gh.prefix`` that is missing). A var ABOVE that entry
      qualifies only if its JSON payload actually reaches it — otherwise a
      ``MEMTOMEM_STM_PROXY`` block carrying nothing but ``cache`` settings gets
      blamed for an upstream server it never mentions.

    A model-level error names every var under the model, the way
    ``_env_override_hint`` names every leaf for a root error: the validator
    that failed spans them, so narrowing would mean guessing.

    A variable a later one covered is never named — it did not contribute the
    value being complained about. See ``live_env_paths``.

    Matching is case-insensitive because settings resolves names that way; only
    names, never values, are rendered (cf. ``hide_input_in_errors``).
    """
    if not isinstance(exc, ValidationError):
        return ""
    env = os.environ if environ is None else environ
    prefix = "memtomem_stm_"
    var_paths: dict[tuple[str, ...], str] = {}
    for key in env:
        lowered = key.lower()
        if lowered.startswith(prefix) and len(lowered) > len(prefix):
            var_paths[tuple(lowered[len(prefix) :].split("__"))] = key

    def _starts_with(path: tuple[str, ...], head: tuple[str, ...]) -> bool:
        return path[: len(head)] == head

    def _payload_reaches(name: str, path: tuple[str, ...], target: tuple[str, ...]) -> bool:
        """Whether the var's JSON payload declares anything at *target*.

        Keys are matched case-insensitively as a fallback, because an error
        location reports a model field by its DECLARED name while the payload
        may spell it any way settings accepts (``'{"UPSTREAM_SERVERS": …}'``).
        Exact first, so a mapping whose keys are operator data — a server named
        ``GH`` is not the one named ``gh`` — is read literally where it can be.
        """
        try:
            node = json.loads(env[name])
        except (TypeError, ValueError):
            return False  # a scalar var above the entry declared no entry
        for key in target[len(path) :]:
            if not isinstance(node, dict):
                return False
            if key in node:
                node = node[key]
                continue
            folded = [k for k in node if str(k).lower() == key.lower()]
            if not folded:
                return False
            node = node[folded[0]]
        return True

    # First position, last value — the collapse settings applies to
    # case-equivalent names — then two settings-oracle rules before the
    # covering filter (#840, both codex-reviewed against counterexamples):
    #
    # - what a variable IS is what settings RESOLVES it to, alone — never
    #   ``json.loads`` (a scalar field given ``'{}'`` resolves to the string,
    #   not a mapping) and never the schema re-derived by hand;
    # - a variable under an ancestor whose resolved value is NOT a mapping is
    #   dead in either order — settings ignores descendants of a non-mapping
    #   parent — and must not be named (this also subsumes scalar roots
    #   covering their ignored descendants);
    # - a length-1 entry whose resolved value IS a mapping is the field's own
    #   base payload, which deeper variables deep-update: it never covers.
    entries = [(name, path) for path, name in var_paths.items()]

    from memtomem_stm.config import STMConfig  # circular at module level

    resolved_alone: dict[str, Any] = {}

    def _resolved_value(name: str, path: tuple[str, ...]) -> Any:
        """What settings resolves this one variable to, at its own path."""
        if name not in resolved_alone:
            try:
                node: Any = _MappingEnvSource(STMConfig, {name.lower(): env[name]})()
            except SettingsError:
                node = _MISSING
            else:
                for part in path:
                    if not isinstance(node, dict) or part not in node:
                        node = _MISSING
                        break
                    node = node[part]
            resolved_alone[name] = node
        return resolved_alone[name]

    def _dead_under_non_mapping_parent(path: tuple[str, ...]) -> bool:
        for other, other_path in entries:
            if len(other_path) < len(path) and path[: len(other_path)] == other_path:
                value = _resolved_value(other, other_path)
                if value is not _MISSING and not isinstance(value, dict):
                    return True
        return False

    entries = [(n, p) for n, p in entries if not _dead_under_non_mapping_parent(p)]
    live = live_env_paths(
        entries,
        non_covering=frozenset(
            n for n, p in entries if len(p) == 1 and isinstance(_resolved_value(n, p), dict)
        ),
    )

    implicated: set[str] = set()
    for err in exc.errors():
        loc = tuple(str(part) for part in err.get("loc", ()))
        if not loc:
            continue  # no path to attribute; naming every var would be noise
        for name, path in live:
            if err.get("type") == "missing":
                # Siblings of the missing field: a var that set one of them, or
                # a payload var above the entry that does declare it.
                parent = loc[:-1]
                if not parent:
                    continue
                if _starts_with(path, parent) or (
                    _starts_with(parent, path) and _payload_reaches(name, path, parent)
                ):
                    implicated.add(name)
            elif _starts_with(loc, path) or _starts_with(path, loc):
                implicated.add(name)
    if not implicated:
        return ""
    return " (env var(s) implicated: " + ", ".join(sorted(implicated)) + ")"


class CompressionStrategy(StrEnum):
    NONE = "none"
    AUTO = "auto"
    TRUNCATE = "truncate"
    EXTRACT_FIELDS = "extract_fields"
    SCHEMA_PRUNING = "schema_pruning"
    SKELETON = "skeleton"
    LLM_SUMMARY = "llm_summary"
    SELECTIVE = "selective"
    HYBRID = "hybrid"
    PROGRESSIVE = "progressive"


class TokenEstimationMode(StrEnum):
    """How token-equivalent response budgets are evaluated at gate time."""

    STATIC = "static"
    UNICODE = "unicode"


class TailMode(StrEnum):
    TOC = "toc"
    TRUNCATE = "truncate"


class TransportType(StrEnum):
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable_http"


class ExposureProfile(StrEnum):
    """Operating mode for the tool-exposure hard filter (#465).

    Controls how *signal-based* eligibility rules (runtime health,
    sensitive-metadata scan) are enforced at tool-advertisement time.
    Structural rules (composed-name overflow, duplicate names) and explicit
    config rules (``hidden``, ``expose_in_profiles``) apply in every profile
    — a profile never overrides what the operator wrote or what would break
    the client.

    - ``strict`` (default): signal rules hard-reject — flagged tools are not
      advertised, with the reason recorded in selection telemetry.
    - ``review``: signal rules demote instead of reject — flagged tools stay
      advertised but carry a ``risk_penalty`` in tool-relevance telemetry
      (#466), so an operator can observe what *would* be hidden.
    - ``explore``: signal rules are off.
    """

    STRICT = "strict"
    REVIEW = "review"
    EXPLORE = "explore"


class LLMProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"


# Hosts whose traffic never leaves the machine — a scan-off LLM path pointed
# here does not cross a trust boundary, so the #610 startup warning stays
# silent for them.
_LOCAL_LLM_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


class LLMCompressorConfig(BaseModel):
    provider: LLMProvider = LLMProvider.OPENAI
    model: str = "gpt-4.1-mini"
    api_key: str = ""
    base_url: str = ""
    system_prompt: str = (
        "Summarize the following content concisely, preserving all key information. "
        "Keep the summary under {max_chars} characters."
    )
    max_tokens: int = Field(default=500, gt=0)
    # Timeout for a single LLM compression call. A slow or hung LLM endpoint
    # would otherwise freeze the pipeline AFTER the upstream has already
    # responded — outside the upstream ``call_timeout_seconds`` (#206).
    # On timeout the compressor falls back to TruncateCompressor (matching
    # other LLM failure modes: privacy, circuit_breaker, llm_error).
    llm_timeout_seconds: float = Field(default=60.0, gt=0.0)
    # When true, scan the upstream response for API keys / passwords / JWT /
    # private keys before sending it to the LLM provider; on a hit, skip the
    # outbound call and fall back to TruncateCompressor (last_fallback="privacy").
    # Default-on: an operator who flips ``compression: llm_summary`` should not
    # have to remember a second knob to avoid leaking credentials to OpenAI /
    # Anthropic / a custom ``base_url``. Set to false only when the response
    # body is known to be sensitive-free or you are using a self-hosted
    # provider you trust (e.g. local Ollama). See #289.
    privacy_scan_enabled: bool = True

    def is_external_destination(self) -> bool:
        """True when this LLM path sends text off the machine.

        OpenAI / Anthropic are always external. Ollama is treated as local only
        when its ``base_url`` host is loopback (the common self-hosted case);
        an Ollama endpoint on a remote host still crosses the boundary. Used by
        the #610 startup warning to avoid false alarms on local Ollama.
        """
        if self.provider in (LLMProvider.OPENAI, LLMProvider.ANTHROPIC):
            return True
        host = urlparse(self.base_url).hostname or ""
        return host not in _LOCAL_LLM_HOSTS

    @model_validator(mode="after")
    def _require_api_key_for_hosted_providers(self) -> LLMCompressorConfig:
        # Deliberately EAGER: every llm block in the config is validated at
        # load, even when the strategy that would use it is not selected
        # (compression: truncate with an attached llm block, or
        # extraction.enabled: false). The trade was reviewed 2026-06-11 and
        # kept: deferring the key check to first use would also defer the
        # failure of a genuinely-enabled compressor from startup to the first
        # tool call. Operators pasting an example llm block they don't use
        # yet should remove it (or use provider: ollama) — documented in
        # docs/compression.md.
        if self.provider not in (LLMProvider.OPENAI, LLMProvider.ANTHROPIC):
            return self
        if self.api_key:
            return self
        env_var = "OPENAI_API_KEY" if self.provider == LLMProvider.OPENAI else "ANTHROPIC_API_KEY"
        env_val = os.environ.get(env_var, "").strip()
        if env_val:
            self.api_key = env_val
            return self
        raise ValueError(
            f"api_key is required for provider='{self.provider.value}' "
            f"(set api_key in config or the {env_var} environment variable)"
        )


class CleaningConfig(BaseModel):
    enabled: bool = True
    strip_html: bool = True
    deduplicate: bool = True
    collapse_links: bool = True


class HybridConfig(BaseModel):
    head_chars: int = Field(default=5000, gt=0)
    tail_mode: TailMode = TailMode.TOC
    min_toc_budget: int = Field(default=200, gt=0)
    min_head_chars: int = Field(default=100, gt=0)
    head_ratio: float = Field(default=0.6, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_ordering(self) -> Self:
        # min_head_chars > head_chars makes HybridCompressor's head-budget
        # guard fire on every call, silently degrading the operator's chosen
        # hybrid strategy to plain truncation. Reject the combination at load
        # like UpstreamServerConfig does for its dependent numeric fields,
        # instead of accepting a config that is structurally a no-op.
        if self.min_head_chars > self.head_chars:
            raise ValueError(
                f"min_head_chars ({self.min_head_chars}) must be <= head_chars ({self.head_chars})"
            )
        return self


class SelectiveConfig(BaseModel):
    max_pending: int = Field(default=100, gt=0)
    pending_ttl_seconds: float = Field(default=300.0, ge=0.0)
    json_depth: int = Field(default=1, gt=0)
    min_section_chars: int = Field(default=50, ge=0)
    pending_store: Literal["memory", "sqlite"] = "memory"
    pending_store_path: Path = Path("~/.memtomem/pending_selections.db")


class AutoIndexConfig(BaseModel):
    enabled: bool = False
    background: bool = False
    min_chars: int = Field(default=2000, ge=0)
    memory_dir: Path = Path("~/.memtomem/proxy_index")
    namespace: str = "proxy-{server}"


class ExtractionStrategy(StrEnum):
    """Strategy for automatic fact extraction from tool responses."""

    NONE = "none"
    LLM = "llm"
    HEURISTIC = "heuristic"
    HYBRID = "hybrid"


def _default_extraction_llm() -> LLMCompressorConfig:
    """Default LLM config for fact extraction: Ollama qwen3:4b (no-think mode)."""
    return LLMCompressorConfig(
        provider=LLMProvider.OLLAMA,
        model="qwen3:4b",
        base_url="http://localhost:11434",
        system_prompt=(
            "/no_think\n"
            "You are a knowledge extraction system. Extract discrete, atomic facts "
            "from the following tool response.\n\n"
            "Rules:\n"
            "- Each fact must be a single, self-contained statement\n"
            "- Categorize: decision, preference, technical, process, relationship, "
            "definition, reference\n"
            "- Rate confidence 0.0-1.0\n"
            "- Extract up to {max_facts} most important facts\n"
            "- Skip boilerplate, navigation, and UI text\n"
            "- Include relevant tags\n\n"
            "Respond ONLY with a JSON array:\n"
            '[{{"content": "...", "category": "...", "confidence": 0.8, '
            '"tags": ["tag1"]}}]'
        ),
        max_tokens=1000,
    )


class ExtractionConfig(BaseModel):
    """Configuration for automatic fact extraction from tool responses."""

    enabled: bool = False
    strategy: ExtractionStrategy = ExtractionStrategy.LLM
    llm: LLMCompressorConfig | None = None
    max_facts: int = Field(default=10, gt=0)
    min_response_chars: int = Field(default=500, ge=0)
    dedup_threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    memory_dir: Path = Path("~/.memtomem/extracted_facts")
    namespace: str = "facts-{server}"
    background: bool = True
    max_input_chars: int = Field(default=20000, gt=0)

    def effective_llm(self) -> LLMCompressorConfig:
        """Return user-provided LLM config or the default (Ollama qwen3:4b)."""
        return self.llm or _default_extraction_llm()


class ProgressiveConfig(BaseModel):
    """Configuration for progressive (cursor-based) delivery."""

    chunk_size: int = Field(default=4000, gt=0)
    """Characters per chunk delivered to the agent."""
    max_stored: int = Field(default=200, gt=0)
    """Maximum concurrent stored progressive responses."""
    ttl_seconds: float = Field(default=1800.0, ge=0.0)
    """Time-to-live for stored responses (seconds)."""
    include_structure_hint: bool = True
    """Include remaining headings/structure hint in first chunk footer."""


class ToolOverrideConfig(BaseModel):
    compression: CompressionStrategy | None = None
    max_result_chars: int | None = Field(default=None, gt=0)
    max_result_tokens: int | None = Field(default=None, gt=0)
    """Token-equivalent budget for this tool. When set, takes precedence over
    ``max_result_chars`` and is converted to a char budget via the resolved
    ``chars_per_token`` ratio. Useful for non-Latin-script content where a
    fixed char budget under-triggers compression. See ``token_estimate.py``."""
    chars_per_token: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    """Per-tool override for the chars-per-token ratio used to convert a token
    budget to a char budget. Falls back to the upstream server's ratio, then
    ``ProxyConfig.chars_per_token``. The budget it converts may be this tool's
    ``max_result_tokens`` or the one inherited from the server — the ratio
    describes what this tool returns, not where its budget is written (#929).
    Inert when the tool sets ``max_result_chars`` and no token budget of its
    own: a char budget is absolute and nothing converts into it. ``gt=0``
    admits ``+inf`` on its own (#722), which the conversion cannot represent —
    the three ratio fields reject non-finite values together because #929 made
    this one reachable from a budget written at either of the other two."""
    token_estimation_mode: TokenEstimationMode | None = None
    """Per-tool gate mode. ``unicode`` measures the actual response; ``None``
    inherits the upstream or proxy setting."""
    retention_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    """Override the dynamic retention floor for this tool.

    When set, the ratio guard uses this value instead of the global
    size-based scaling (<1KB→0.9, <3KB→0.75, etc.).  Useful for tools
    whose responses tolerate more aggressive compression or, conversely,
    for tools where even small losses are costly.
    """
    llm: LLMCompressorConfig | None = None
    selective: SelectiveConfig | None = None
    hybrid: HybridConfig | None = None
    progressive: ProgressiveConfig | None = None
    cleaning: CleaningConfig | None = None
    auto_index: bool | None = None
    extraction: bool | None = None
    cache: bool | None = None
    """Per-tool response-cache opt-in/out. ``None`` (default) defers to the
    server-level ``cache``, then to the global
    ``CacheConfig.tool_annotation_policy``. ``True`` force-caches this tool
    (overriding the annotation policy — e.g. to re-enable caching for a tool an
    upstream mis-annotates as a writer); ``False`` never caches it (e.g. a
    volatile read tool, or a writer on an upstream that omits annotations).
    Under the ``strict`` policy — which new configs set explicitly — ``True``
    is the supported allowlist for a known-read-only tool whose upstream omits
    annotations. The privacy / transient-key store guards still apply when
    ``True``."""
    cache_ttl_seconds: float | None = Field(default=None, ge=0.0)
    """Per-tool override for the response-cache TTL (seconds). ``None`` (default)
    defers to the server-level ``cache_ttl_seconds``, then to the global
    ``CacheConfig.default_ttl_seconds``. A positive value caches this tool's
    responses for that many seconds; ``0`` disables caching for this tool: while
    the resolved TTL is ``<= 0`` the lookup is bypassed, so a stale row is never
    served (mirroring the global ``default_ttl_seconds <= 0`` behavior); any
    existing on-disk row is cleaned up opportunistically (invalidated when an
    identical call next stores a text response) and otherwise expires under its
    original frozen TTL. Independent of the ``cache`` on/off
    gate: ``cache: false`` always wins (never cached); ``cache: true`` with
    ``cache_ttl_seconds: 0`` is eligible but TTL-disabled, i.e. effectively off.
    Unlike the global field, ``None`` here means *inherit*, not *never expires*."""
    hidden: bool = False
    description_override: str | None = None
    expose_in_profiles: list[ExposureProfile] | None = None
    """Exposure profiles in which this tool is advertised (#465).

    ``None`` (default) means every profile. A set list restricts the tool to
    those profiles — e.g. ``["explore"]`` keeps a destructive admin tool out
    of production exposure. Overrides the upstream-level
    ``expose_in_profiles`` when both are set. An empty list is equivalent to
    ``hidden: true``. This is a visibility constraint only; it does not
    exempt the tool from signal-based rules in the profiles where it is
    visible."""


class OriginSource(BaseModel):
    """One host-config location an imported upstream entry came from (#475)."""

    kind: str
    """Machine-readable source kind: ``claude-user`` / ``claude-project`` /
    ``mcp-json`` / ``claude-desktop``. Kept in lockstep with the CLI's shared
    source table (``cli/proxy.py``); a plain ``str`` rather than an enum so a
    config written by a newer CLI with a new kind still validates here."""
    path: str | None = None
    """Filesystem anchor for path-scoped kinds: the resolved project dir for
    ``claude-project``, the ``.mcp.json`` path for ``mcp-json``."""
    pruned: bool = False
    """``True`` once this source's host entry was removed by a prune writer.
    Per-source rather than per-entry because prune permits partial failure
    across the primary source and its duplicates."""
    pruned_at: str | None = None


class UpstreamOrigin(BaseModel):
    """Import provenance for an upstream entry (#475).

    Written by the CLI import paths (``mms init`` / ``mms add --import``) so
    the entry can later be restored to its host config verbatim (``mms
    eject``). The proxy runtime never reads it — the field exists here to
    document the schema and give the CLI a validated constructor.

    ``original`` is the verbatim host entry and may contain secrets
    (``env`` / ``headers``); CLI ``--json`` outputs must strip it (see the
    redacted serializer in ``cli/proxy.py``) rather than dumping it.
    """

    schema_version: int = 1
    source: OriginSource
    duplicates: list[OriginSource] = []
    imported_at: str | None = None
    original: dict[str, Any] | None = None


class UpstreamServerConfig(BaseModel):
    command: str = ""
    args: list[str] = []
    env: dict[str, str] | None = None
    cwd: Path | None = None
    """Working directory for stdio servers. Useful for project-scoped MCP
    servers and avoids shell-specific ``cd`` wrappers on Windows."""
    prefix: str
    transport: TransportType = TransportType.STDIO
    url: str = ""
    headers: dict[str, str] | None = None
    compression: CompressionStrategy = CompressionStrategy.AUTO
    max_result_chars: int = Field(default=8000, gt=0)
    max_result_tokens: int | None = Field(default=None, gt=0)
    """Token-equivalent budget for this upstream. When set, takes precedence
    over ``max_result_chars`` and is converted to a char budget via the
    resolved ``chars_per_token`` ratio. See ``token_estimate.py`` for the
    estimator used at gate time."""
    chars_per_token: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    """Per-server override for the chars-per-token ratio. Falls back to
    ``ProxyConfig.chars_per_token`` (default 3.5, English-biased). Set to
    ~2.0 for Korean-dominant content, ~1.3 for Chinese-dominant."""
    token_estimation_mode: TokenEstimationMode | None = None
    """Per-server token gate mode. ``None`` inherits the proxy default."""
    retention_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    """Per-server retention floor override (see ToolOverrideConfig)."""
    llm: LLMCompressorConfig | None = None
    selective: SelectiveConfig | None = None
    hybrid: HybridConfig | None = None
    progressive: ProgressiveConfig | None = None
    cleaning: CleaningConfig | None = None
    tool_overrides: dict[str, ToolOverrideConfig] = {}
    auto_index: bool | None = None
    extraction: bool | None = None
    cache: bool | None = None
    """Per-server response-cache opt-in/out (see ``ToolOverrideConfig.cache``).
    ``None`` (default) defers to the global ``CacheConfig.tool_annotation_policy``;
    ``True``/``False`` force every tool on this upstream in/out of the cache —
    ``True`` is the server-wide strict-mode allowlist for a trusted read-only
    upstream that omits annotations. A per-tool ``cache`` override wins over
    this."""
    cache_ttl_seconds: float | None = Field(default=None, ge=0.0)
    """Per-server response-cache TTL override (see
    ``ToolOverrideConfig.cache_ttl_seconds``). ``None`` (default) defers to the
    global ``CacheConfig.default_ttl_seconds``; a positive value sets the TTL for
    every tool on this upstream; ``0`` disables caching for the whole upstream. A
    per-tool ``cache_ttl_seconds`` wins over this."""
    expose_in_profiles: list[ExposureProfile] | None = None
    """Exposure profiles in which this upstream's tools are advertised
    (#465). ``None`` (default) means every profile. Per-tool
    ``expose_in_profiles`` overrides this when set. See
    ``ToolOverrideConfig.expose_in_profiles``."""
    surfacing_enabled: bool = True
    """Opt this upstream's proxied tool responses in/out of the SURFACE stage
    (proactive memory surfacing). Default ``True`` preserves existing behavior;
    ``False`` suppresses surfacing for every tool on this server.

    Useful for third-party upstreams whose calls never match the user's LTM
    (so the per-call LTM search is pure wasted latency), or to keep a sensitive
    upstream's request context out of LTM queries entirely.

    Enforced in ``ProxyManager``, which reads this from ``stm_proxy.json`` via
    the hot-reloaded config — *not* in the ``SurfacingEngine`` relevance gate,
    which is built once at startup from the top-level ``SurfacingConfig`` and
    never sees per-upstream config. For tool-grained or glob scope instead, see
    ``SurfacingConfig.exclude_tools`` (matches ``server__tool``)."""
    max_retries: int = Field(default=3, ge=0)
    reconnect_delay_seconds: float = Field(default=1.0, ge=0.0)
    max_reconnect_delay_seconds: float = Field(default=30.0, ge=0.0)
    connect_timeout_seconds: float = Field(default=30.0, gt=0.0)
    """End-to-end budget for establishing a session with this upstream.

    One shared monotonic deadline covers transport entry (process spawn or
    HTTP/SSE connect), MCP ``initialize()``, and the ``tools/list`` discovery
    call — each phase gets whatever budget remains, so a slow phase cannot
    grant later phases a fresh window. Applied identically at first connect
    and at every reconnect.

    For network transports the same value is also passed as the SDK client
    factory's ``timeout=`` (the httpx connect budget); ``sse_read_timeout``
    stays at the SDK default so long-lived streams don't inherit the connect
    budget. Contrast with ``call_timeout_seconds`` (per tool-call attempt)
    and ``overall_deadline_seconds`` (per tool call across retries).
    """
    call_timeout_seconds: float = Field(default=90.0, gt=0.0)
    """Per-attempt timeout for ``session.call_tool()`` against this upstream.

    Without this bound, a silently-hung upstream blocks the proxy indefinitely
    and every downstream client blocks on the proxy. On ``TimeoutError`` the
    session is force-reset (so the orphaned ``request_id`` cannot pollute a
    future call) and the retry loop proceeds to the next attempt, capped by
    ``max_retries`` and ``overall_deadline_seconds``.

    Default 90s: most tool calls complete in <30s, LLM-backed tools can take
    30-60s, 90s leaves headroom without permitting an infinite hang. Lower for
    known-fast upstreams; raise for upstreams that invoke long-running LLMs.
    """
    overall_deadline_seconds: float = Field(default=180.0, gt=0.0)
    """Total wall-clock budget for a single tool call across all retry attempts.

    Each attempt's effective timeout is ``min(call_timeout_seconds,
    remaining_deadline)``. When the deadline is exhausted the retry loop aborts
    and ``TimeoutError`` propagates. This prevents ``call_timeout_seconds ×
    (max_retries+1)`` worst-case blowout while still allowing multiple attempts
    within a bounded window. Default 180s = 2× ``call_timeout_seconds``.
    """
    circuit_max_failures: int = Field(default=3, ge=0)
    """Consecutive failed calls before this upstream's circuit breaker opens.

    Counts one failure per *call* that exhausts its retry/deadline budget on a
    transport fault or timeout — not one per attempt, and not tool-level
    ``isError`` results (an erroring tool proves the upstream is alive). While
    open, calls fast-fail with a ``circuit_open`` error instead of paying the
    full retry/deadline cost; cached responses keep serving. ``0`` disables
    the breaker for this upstream. Connect-time snapshot like ``max_retries``:
    edits apply on the next restart, not via config hot-reload.
    """
    circuit_reset_seconds: float = Field(default=60.0, gt=0.0)
    """Seconds an open circuit breaker waits before allowing a probe call."""
    max_description_chars: int = Field(default=200, ge=MIN_DESCRIPTION_CHARS)
    """Cap on the CLIENT-VISIBLE description, ``[proxied] `` prefix included.

    Composes with the global setting as ``min(server, global)``, not as an
    override. Read from the connect-time snapshot like the other per-server
    fields here, so an edit takes effect when this upstream next connects — a
    restart or a reconnect — not on a config hot-reload, and not on a
    ``tools/list_changed`` refresh, which replaces the catalogue but not the
    config it is advertised under (#893).
    """
    strip_schema_descriptions: bool = False
    origin: UpstreamOrigin | None = None
    """Import provenance (#475) — see :class:`UpstreamOrigin`. CLI-owned
    metadata: the server validates the shape but never reads it at runtime.
    Older binaries that predate the field ignore it via pydantic's default
    ``extra="ignore"``; the CLI's raw-dict load/save preserves it through
    every config mutation."""

    @model_validator(mode="after")
    def _check_ordering(self) -> Self:
        if self.reconnect_delay_seconds > self.max_reconnect_delay_seconds:
            raise ValueError(
                f"reconnect_delay_seconds ({self.reconnect_delay_seconds}) "
                f"must be <= max_reconnect_delay_seconds ({self.max_reconnect_delay_seconds})"
            )
        if self.call_timeout_seconds > self.overall_deadline_seconds:
            raise ValueError(
                f"call_timeout_seconds ({self.call_timeout_seconds}) "
                f"must be <= overall_deadline_seconds ({self.overall_deadline_seconds})"
            )
        return self


class CacheConfig(BaseModel):
    enabled: bool = True
    db_path: Path = Path("~/.memtomem/proxy_cache.db")
    default_ttl_seconds: float | None = Field(default=3600.0, ge=0.0)
    max_entries: int = Field(default=10000, gt=0)
    tool_annotation_policy: Literal["conservative", "strict", "ignore"] = "conservative"
    """Which proxied tool responses are eligible for the response cache, based on
    the upstream tool's MCP annotations (``readOnlyHint`` / ``destructiveHint``).

    The proxy sits transparently in front of every upstream tool, so without a
    gate a mutating tool (``create_*`` / ``send_*`` / ``write_*`` / ``delete_*``)
    called twice with identical args within the TTL is served the first call's
    cached success WITHOUT re-executing the side effect — the agent is told it
    mutated when it did not.

    - ``conservative`` (default): cache every tool EXCEPT those that explicitly
      self-declare as writers (``readOnlyHint is False`` or
      ``destructiveHint is True``). Keeps caching for the un-annotated majority
      and for declared read-only tools, while refusing to memoize a side effect
      the upstream itself flags as mutating. A tool declaring BOTH
      ``readOnlyHint=True`` and ``destructiveHint=True`` is contradictory and
      is deliberately treated as a writer (not the spec-literal reading, which
      scopes ``destructiveHint`` to ``readOnlyHint=false`` tools) — distrusting
      a self-contradiction costs one cache slot; trusting it could replay a
      side effect. Re-enable such a tool via a per-tool ``cache: true``.
    - ``strict``: cache ONLY tools that explicitly declare ``readOnlyHint=True``.
      Safest (the MCP spec treats a missing ``readOnlyHint`` as may-mutate), but
      drops caching for every upstream that omits annotations.
    - ``ignore``: pre-gate behavior — cache every tool regardless of annotations.

    The ``conservative`` default is a compatibility choice for files that
    predate the knob: NEW config files (``mms init`` / ``mms add`` /
    ``mms add --from-clients``) are written with an explicit ``"strict"``, and
    loading a file without the key logs a migration advisory.

    A per-tool / per-server ``cache`` override (``ToolOverrideConfig.cache`` /
    ``UpstreamServerConfig.cache``) takes precedence over this policy — under
    ``strict`` that override is the allowlist for un-annotated read-only tools.
    The privacy and transient-key store guards always apply on top, regardless
    of this knob."""


class MetricsConfig(BaseModel):
    enabled: bool = True
    db_path: Path = Path("~/.memtomem/proxy_metrics.db")
    max_history: int = Field(default=10000, gt=0)


class CompressionFeedbackConfig(BaseModel):
    """Configuration for the stm_compression_feedback learning loop.

    Collection-only in this release: reports are persisted for later
    inspection via ``stm_compression_stats`` and for future auto-tuning.
    Shares the user-wide ``~/.memtomem/stm_feedback.db`` file with
    surfacing feedback (different tables; WAL mode makes concurrent
    access safe).
    """

    enabled: bool = True
    db_path: Path = Path("~/.memtomem/stm_feedback.db")
    retention_days: int = Field(default=90, ge=0)
    """#584 — days to keep ``compression_feedback`` rows before a startup
    purge deletes them. The table is otherwise append-only and unbounded.
    ``0`` disables the purge (rows kept indefinitely — the pre-#584
    behavior)."""


class ProgressiveReadsConfig(BaseModel):
    """Configuration for progressive-delivery read telemetry.

    Records one row per initial progressive response plus one row per
    ``stm_proxy_read_more`` follow-up into ``progressive_reads``.
    Aggregates surface via ``stm_progressive_stats`` and enable
    stratified analysis of follow-up rate by tool / compression
    strategy / response size. Shares the user-wide
    ``~/.memtomem/stm_feedback.db`` file with surfacing and
    compression feedback (disjoint tables; WAL mode makes concurrent
    access safe).
    """

    enabled: bool = True
    db_path: Path = Path("~/.memtomem/stm_feedback.db")
    retention_days: int = Field(default=90, ge=0)
    """#584 — days to keep ``progressive_reads`` rows before a startup purge
    deletes them. The table is append-only by design and otherwise
    unbounded. ``0`` disables the purge (rows kept indefinitely — the
    pre-#584 behavior)."""


class SelectionTelemetryConfig(BaseModel):
    """Configuration for tool-selection telemetry (#467).

    When enabled, the proxy appends one ``selection`` + one ``execution``
    JSONL record per proxied call to ``path`` (schema and redaction policy
    in ``proxy/selection_log.py``). Off by default: it is a new disk write
    path, so the operator opts in explicitly. The flag is read at startup
    (lifespan wiring, like ``metrics.enabled``) — toggling it requires a
    restart, not a hot-reload.
    """

    enabled: bool = False
    path: Path = Path("~/.memtomem/stm_selection_log.jsonl")
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    """Fraction of calls recorded; applies to the selection+execution pair
    atomically so the log never contains an execution without its selection."""
    max_bytes: int = Field(default=50_000_000, gt=0)
    """Rotate the log when it reaches this size."""
    max_backups: int = Field(default=3, ge=0)
    """Rotated files kept (``.1`` … ``.N``); ``0`` truncates instead."""


class ToolRelevanceConfig(BaseModel):
    """Configuration for per-call tool-relevance ranking (#466 v0).

    Deterministic BM25 ranking of the advertised tool set against the
    call's query signal, recorded ONLY into selection telemetry
    (``candidate_features``) — exposure never changes. Inert unless
    ``selection_telemetry.enabled`` is also on (there is nowhere else for
    the ranking to go in v0), so the default-on here adds no write path
    by itself. Read per call via the hot-reloaded proxy config.
    """

    enabled: bool = True
    top_n: int = Field(default=20, gt=0)
    """Ranked candidates recorded per selection event (full advertised
    set is already in ``candidate_tools``; this bounds the scored list)."""


class ExposureConfig(BaseModel):
    """Configuration for the STM-native tool-exposure hard filter (#465).

    The filter runs at advertisement time (``ProxyManager.get_proxy_tools``)
    — the proxy's tool-exposure choke point — and decides which upstream
    tools the client model gets to see. Rejected tools are not registered;
    their reject reasons land in selection telemetry (#467,
    ``reject_reasons``) when it is enabled. Relevance ranking (#466) runs
    over the filter's *output*, so a hard-rejected tool can never be
    resurrected by ranking.

    Health signals are evaluated once at proxy startup from the persisted
    metrics store (``proxy_metrics.db``), so a tool does not slide in and out
    of the advertisement as calls fail. A tool hidden for health is
    re-evaluated at the next startup: once its failures age out of
    ``health_window_hours`` it is advertised again (startup-grained half-open
    probing). What the upstream declares is re-read when it changes: a
    reconnect or a ``tools/list_changed`` re-runs the filter, reconciles what
    is registered, and — when that actually changed the registry — asks the
    clients it can reach to re-list (#917).
    """

    profile: ExposureProfile = ExposureProfile.STRICT
    health_window_hours: float = Field(default=24.0, gt=0.0)
    """Look-back window over ``proxy_metrics.db`` for per-tool health."""
    health_min_calls: int = Field(default=5, gt=0)
    """Minimum calls inside the window before health is judged at all —
    below this the tool is presumed healthy (insufficient evidence)."""
    health_error_rate_threshold: float = Field(default=0.95, gt=0.0, le=1.0)
    """Upstream-attributable error rate (transport / timeout / protocol /
    upstream_error — proxy-internal pipeline errors do not count against
    the tool) at or above which a tool is flagged unhealthy. The default
    is deliberately conservative: only consistently failing tools
    (≥95% of recent calls) are flagged."""
    review_risk_penalty: float = Field(default=0.5, ge=0.0, le=1.0)
    """Multiplicative demotion recorded for signal-flagged tools under the
    ``review`` profile: ``final_score = relevance_score * (1 - penalty)``
    in tool-relevance telemetry (#466). Exposure itself never changes in
    ``review``."""


class ToolgraphConfig(BaseModel):
    """Optional external tool-graph eligibility provider (#465).

    Consults a separate, non-proxied tool-graph MCP server for cross-server
    authorization / data-flow eligibility facts and feeds them into the
    STM-native exposure filter as an additional rule source (alongside the
    config / structural / native-signal rules already in
    ``tool_eligibility.filter_tools``). The graph is *consulted*, never
    proxied: the client never sees its tools, and STM holds **no
    Python-level dependency** on the external package — all traffic goes
    over the MCP protocol via :class:`~memtomem_stm.proxy.toolgraph_provider.ToolgraphConsultAdapter`
    (stdio transport), mirroring the surfacing LTM-consult pattern.

    Default-off: when ``enabled`` is ``True`` the stdio consult runs once at
    proxy startup, exactly like the health-flag precompute. If an upstream
    later replaces its catalogue the advertisement is rebuilt (#917) but the
    consult is not re-run, so a tool the graph never saw gets a
    ``toolgraph_unconsulted`` reason instead of defaulting to allowed —
    profile-gated like the rest of the family, so ``strict`` withholds it,
    ``review`` demotes it and ``explore`` ignores it (#918). The verdict feeds
    ``tool_eligibility.filter_tools`` via per-candidate ``toolgraph_*`` reject
    codes (profile-gated, like the native signal rules) or a whole-call
    fail-closed withhold, and pins ``graph_generation`` into selection
    telemetry. Failures map onto the four ``on_*`` knobs below. Neo4j (behind
    the graph server) is an operational prerequisite of enabling this block:
    a compatible server reports a backend outage with the typed
    ``backend_unavailable`` MCP envelope, which is classified through
    ``on_unreachable``. Untyped, unknown, or malformed error results remain
    contract failures (``on_protocol_error``).
    """

    enabled: bool = False
    source: Literal["stdio", "bundle"] = "stdio"
    """Policy source. ``stdio`` preserves the one-shot MCP consult; ``bundle``
    consumes a portable, atomically published Toolgraph policy artifact and
    never launches a Toolgraph subprocess."""
    bundle_path: Path = Path("~/.memtomem/toolgraph/policy-bundle.json")
    """Portable policy artifact used when ``source`` is ``bundle``."""
    command: str = "toolgraph"
    """Launch command for the stdio tool-graph MCP server. Defaults to the
    graph server's registered console script (mirroring the surfacing
    ``memtomem-server`` default); ``serve`` with no flag runs stdio
    (``serve --http`` is out of scope for v1 — stdio transport only)."""
    args: list[str] = ["serve"]
    env: dict[str, str] | None = None
    """Extra environment for the launched server (e.g. ``NEO4J_URI`` /
    ``NEO4J_USER`` / ``NEO4J_PASSWORD``). ``None`` inherits only mcp's safe
    default-environment allowlist (PATH / HOME / SHELL / TERM / USER /
    LOGNAME); set ``NEO4J_*`` etc. explicitly here — they are merged *over*
    that default and are not picked up from the proxy's own environment."""
    agent_id: str = "stm-proxy"
    """Identity the graph authorizes eligibility against; must be registered
    in the graph. A typo returns ``agent_found=false`` — see
    ``on_agent_not_found``."""
    server_name_map: dict[str, str] = {}
    """Maps an STM upstream connection key (the operator-chosen key in
    ``upstream_servers``) to the tool-graph server's *crawled* name. The two
    are independent strings, so they coincide only by luck; an empty map
    assumes identity and relies on a heuristic mismatch warning."""
    query_profile: str = "strict"
    """Profile passed to the upstream ``eligible_tools`` consult. Kept a free
    string (not coupled to STM's own ``ExposureProfile``) because the graph's
    profile ladder is the external package's concern; STM applies its own
    profile semantics on top."""
    on_unreachable: Literal["open", "closed"] = "open"
    """Transport down / timeout. ``open`` (default) skips the external rule
    family and advertises per STM-native rules (the graph is an enhancement,
    not a hard dependency); ``closed`` withholds every tool the graph did not
    bless (high-assurance)."""
    on_agent_not_found: Literal["fail_start", "open", "closed"] = "fail_start"
    """Graph reachable but ``agent_id`` unknown — almost always a config
    typo. ``fail_start`` (default) fails startup loudly so a typo cannot
    silently disable enforcement; ``open`` / ``closed`` are explicit opt-ins."""
    on_protocol_error: Literal["fail_start", "open", "closed"] = "fail_start"
    """Graph reachable but incompatible (missing ``eligible_tools``, malformed
    ``structuredContent``, non-int ``graph_generation``, unknown-profile
    error). ``fail_start`` (default) treats a contract break as a loud
    startup failure rather than a silent passthrough."""
    on_tool_not_found: Literal["open", "closed"] = "open"
    """A specific candidate was never crawled (the graph's blind spot).
    ``open`` (default) does not hide a working tool; ``closed`` rejects
    uncrawled candidates (high-assurance)."""
    risk_penalty_scale: float = Field(default=1.0, ge=0.0, allow_inf_nan=False)
    """Multiplier mapping the graph's per-candidate ``risk_score`` (the
    rule-based data-flow/DENY risk, ``[0,1]``) to a relevance ``risk_penalty``
    for eligible-but-risky tools (#493): ``penalty = min(risk_score * scale,
    1.0)``, demoting them in tool-relevance ranking telemetry (#466) in EVERY
    profile — never exposure (ranking can neither resurrect nor hard-reject).
    When ``> 0`` the consult runs a second, best-effort ``rank_features`` batch
    query in the same startup session; a failure there degrades to no penalties
    (logged, never a startup gate). The penalty composes with the native
    ``review_risk_penalty`` via a complement-product when both apply. ``0``
    zeroes every penalty, but it no longer skips the enrichment on its own:
    that same query also produces the per-candidate facts selection telemetry
    records (#469), so it still runs when ``selection_telemetry.enabled`` is
    set. With both off — or a disabled ``toolgraph`` block — nothing consumes
    the enrichment and it is skipped entirely."""
    timeout_seconds: float = Field(default=5.0, gt=0.0)
    """Per-consult timeout for the startup batch query."""
    consult_cache_enabled: bool = True
    """Disk-cache a successful consult's verdict keyed by ``graph_generation``
    (#494). On restart, a cheap generation probe is still made (so a degraded
    graph is never masked); only the expensive per-candidate ``eligible_tools`` /
    ``rank_features`` evaluation is skipped when the generation, candidate set,
    agent, profile, and backend all match a cached row."""
    consult_cache_path: Path = Path("~/.memtomem/toolgraph_consult.db")
    """SQLite path for the consult cache (#494). One DB serves all graph
    backends, disambiguated by a provider fingerprint over ``command`` / ``args``
    / env *keys*; point distinct backends sharing identical command/args/env-keys
    but different env *values* at distinct paths."""
    consult_cache_max_scopes: int = Field(default=64, gt=0)
    """Maximum number of cached consult rows kept in the #494 disk cache before
    the oldest (by ``created_at``) are trimmed on each write. Bounds growth of
    the user-wide ``toolgraph_consult.db``; one row per ``(provider, agent,
    profile, candidate-set, generation)`` scope, so this caps total rows across
    all scopes, not per scope. Must be ``>= 1`` (``0`` would trim every row on
    every write, defeating the cache)."""


# Static context window sizes (tokens) for known model families.
# Used by ProxyConfig.effective_max_result_chars() to scale compression budget.
# Prefix-matched: "claude-sonnet-4-20250514" matches "claude-sonnet-4".
# Ordered longest-prefix-first where ambiguity exists.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic — Claude 4.x / 4.5 / 4.6
    "claude-opus-4": 200000,
    "claude-sonnet-4": 200000,
    "claude-haiku-4": 200000,
    # OpenAI — GPT-4.1 / o-series / GPT-4o
    "gpt-4.1-mini": 1048576,
    "gpt-4.1-nano": 1048576,
    "gpt-4.1": 1048576,
    "gpt-4o-mini": 128000,
    "gpt-4o": 128000,
    "o4-mini": 200000,
    "o3-pro": 200000,
    "o3-mini": 200000,
    "o3": 200000,
    "o1-pro": 200000,
    "o1-mini": 128000,
    "o1": 200000,
    # Google — Gemini 2.x
    "gemini-2.5-pro": 1048576,
    "gemini-2.5-flash": 1048576,
    "gemini-2.0-flash": 1048576,
    "gemini-2": 1048576,
    # Meta — Llama 4
    "llama-4-maverick": 1048576,
    "llama-4-scout": 512000,
    "llama-4": 512000,
    # Open-weight
    "qwen-3": 131072,
    "qwen3": 131072,
    "deepseek-r1": 131072,
    "deepseek-v3": 131072,
    "mistral-large": 131072,
    "codestral": 262144,
    "command-a": 262144,
}


_EMBEDDING_PROVIDER_DEFAULTS: dict[str, str] = {
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com",
}


class RelevanceScorerConfig(BaseModel):
    """Configuration for query-aware relevance scoring.

    When ``embedding_provider`` is ``"openai"``, the ``OPENAI_API_KEY``
    environment variable must be set for authentication.
    """

    scorer: str = "bm25"
    """Scorer type: "bm25" (default, zero-latency) or "embedding" (semantic)."""
    embedding_provider: str = "ollama"
    """Embedding provider: "ollama" or "openai". Only used when scorer="embedding"."""
    embedding_model: str = "nomic-embed-text"
    """Embedding model name. Only used when scorer="embedding"."""
    embedding_base_url: str | None = None
    """Embedding API base URL. Defaults to the provider's standard endpoint
    (Ollama → http://localhost:11434, OpenAI → https://api.openai.com).
    Only used when scorer="embedding"."""
    embedding_timeout: float = Field(default=10.0, gt=0.0)
    """Embedding API timeout in seconds."""

    @model_validator(mode="after")
    def _apply_provider_default_url(self) -> "RelevanceScorerConfig":
        if self.embedding_base_url is None:
            self.embedding_base_url = _EMBEDDING_PROVIDER_DEFAULTS.get(
                self.embedding_provider, "http://localhost:11434"
            )
        return self


def _model_arms(annotation: Any) -> list[type[BaseModel]] | None:
    """BaseModel arms of *annotation* after unwrapping ``Annotated``/unions.

    Returns ``None`` when descending would risk false positives: a non-model
    leaf, a mixed union (model | free-form), or anything else where key
    existence isn't defined by a model schema. The classification is read off
    the annotation itself — no name allowlist — so it stays correct as the
    config models evolve.
    """
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        arms: list[type[BaseModel]] = []
        for arm in get_args(annotation):
            if arm is type(None):
                continue
            sub = _model_arms(arm)
            if sub is None:  # mixed union — don't guess, don't descend
                return None
            arms.extend(sub)
        return arms or None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return None


def _container_value_arms(annotation: Any) -> tuple[str, list[type[BaseModel]]] | None:
    """Classify a container annotation whose *values* are models.

    Returns ``("dict", arms)`` for ``dict[str, Model]`` (user-defined keys —
    descend into values only) or ``("list", arms)`` for ``list[Model]``;
    ``None`` for free-form containers (``dict[str, str]``, ``dict[str, Any]``)
    and everything else.
    """
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    origin = get_origin(annotation)
    if origin is dict:
        args = get_args(annotation)
        if len(args) == 2 and (arms := _model_arms(args[1])) is not None:
            return ("dict", arms)
    elif origin in (list, tuple, set):
        args = get_args(annotation)
        if args and (arms := _model_arms(args[0])) is not None:
            return ("list", arms)
    return None


def _unknown_keys_via_arms(
    arms: list[type[BaseModel]], data: Mapping[str, Any], prefix: str
) -> list[str]:
    """Unknown keys under a (possibly multi-arm) model annotation.

    With several model arms (none in today's tree), only keys unknown to
    *every* arm are flagged — conservative, no false positives.
    """
    per_arm = [find_unknown_keys(arm, data, prefix) for arm in arms]
    common = set(per_arm[0])
    for other in per_arm[1:]:
        common &= set(other)
    return sorted(common)


def find_unknown_keys(
    model_cls: type[BaseModel], data: Mapping[str, Any], prefix: str = ""
) -> list[str]:
    """Dotted paths in *data* that no field of *model_cls* (recursively) accepts.

    The proxy config models deliberately keep pydantic's default
    ``extra="ignore"`` for forward compatibility (older binaries must ignore
    fields written by newer CLIs — see ``UpstreamServerConfig.origin``), which
    means a typo'd key is silently dropped at load time. This walker gives the
    load path and ``mms config validate`` a way to *name* those dropped keys
    without giving up the lenient validation.

    Key-existence only: a value of the wrong runtime type (a string where a
    model object belongs) is skipped silently — ``model_validate`` owns type
    errors. ``dict[str, Model]`` fields (``upstream_servers``,
    ``tool_overrides``) have user-defined keys, so only their values are
    descended; free-form leaves (``env``, ``headers``, ``origin.original``,
    ``server_name_map``) are never descended.
    """
    unknown: list[str] = []
    known: dict[str, Any] = {}
    for name, field in model_cls.model_fields.items():
        known[name] = field.annotation
        if field.alias:
            known[field.alias] = field.annotation
    for key, value in data.items():
        path = f"{prefix}{key}"
        if key not in known:
            unknown.append(path)
            continue
        annotation = known[key]
        if (arms := _model_arms(annotation)) is not None:
            if isinstance(value, Mapping):
                unknown.extend(_unknown_keys_via_arms(arms, value, f"{path}."))
        elif (container := _container_value_arms(annotation)) is not None:
            kind, arms = container
            if kind == "dict" and isinstance(value, Mapping):
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, Mapping):
                        unknown.extend(
                            _unknown_keys_via_arms(arms, sub_value, f"{path}.{sub_key}.")
                        )
            elif kind == "list" and isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, Mapping):
                        unknown.extend(_unknown_keys_via_arms(arms, item, f"{path}[{i}]."))
    return sorted(unknown)


@dataclass(frozen=True)
class ConfigLoadResult:
    """Outcome of ``ProxyConfig.load_from_file_with_status``.

    ``error`` is set iff the file exists but failed to parse or validate —
    the case a running server silently papers over by falling back to
    env/default config. A missing file is not an error (``config`` may still
    carry the env-only/defaults rebuild under ``missing_ok=True``).
    """

    config: ProxyConfig | None
    error: str | None
    unknown_keys: tuple[str, ...] = ()
    env_error: str | None = None
    """Set when the ``MEMTOMEM_STM_PROXY`` environment is not the one
    ``config`` reflects. Two shapes, and the second is independent of whether a
    file exists:

    * there is no config FILE and the env-only overlay failed to validate, so
      ``config`` is a defaults rebuild matching neither the environment nor
      any file;
    * the overlay dropped a bare payload entirely — malformed JSON, or a
      decoded non-object (see ``EnvOverlayResult.rejected``) — which resolves
      to an empty fragment indistinguishable from an unset environment. That
      one is reported with a file present too: the file then decides a config
      the operator's environment was meant to override.

    Separate from ``error`` because the two call for different handling: a
    running server tolerates this and starts on defaults (the historical
    behavior ``error`` drives), while a command that WRITES somewhere the
    config names must not act on a path the operator did not choose.
    """


class ProxyConfig(BaseModel):
    enabled: bool = False
    config_path: Path = Path("~/.memtomem/stm_proxy.json")
    upstream_servers: dict[str, UpstreamServerConfig] = {}
    default_compression: CompressionStrategy = CompressionStrategy.AUTO
    default_max_result_chars: int = Field(default=16000, gt=0)
    max_upstream_chars: int = Field(default=10_000_000, gt=0)
    """Hard cap on the size of the upstream response loaded into memory before
    compression. A misbehaving (or malicious) upstream returning a 100 MB
    payload would otherwise OOM the proxy. When the cap is exceeded the
    response is truncated with a notice and the call is recorded as
    ``upstream_error`` / ``oversize`` in ``proxy_metrics.db``.

    Default 10 M chars (~10 MB UTF-8). Per-server / per-tool overrides are a
    follow-up if needed.
    """
    max_upstream_bytes: int = Field(default=41_943_040, gt=0)
    """UTF-8 JSON byte cap on each inbound MCP message envelope (40 MiB).

    Unlike ``max_upstream_chars``, this includes non-text content,
    ``structuredContent``, ``_meta`` and error envelopes. Oversized messages
    are rejected instead of truncated because arbitrary structured or binary
    payloads cannot be truncated without corrupting their schema.

    The cap is enforced on each decoded message's compact JSON size, measured
    where the transport hands the message to the client session. Insignificant
    whitespace an upstream may have sent is therefore not counted, and neither
    are defaults that validation fills in further along: a validated result
    model can serialize larger than the envelope that carried it — a content
    block's omitted ``type`` discriminator is added per block — so a result
    measured after parsing is a different number and is not what this bounds.
    """
    min_result_retention: float = Field(default=0.65, ge=0.0, le=1.0)
    relevance_scorer: RelevanceScorerConfig = Field(default_factory=RelevanceScorerConfig)
    """Minimum fraction of response to preserve after compression (0-1).

    If ``default_max_result_chars`` or per-tool ``max_result_chars`` would
    retain less than this fraction of the cleaned response, the effective
    budget is raised to ``len(response) * min_result_retention``.

    Default 0.65 ensures at least 65% of every response survives compression.
    Set to 0 to disable and use fixed budgets only.
    """
    max_description_chars: int = Field(default=200, ge=MIN_DESCRIPTION_CHARS)
    """Cap on the CLIENT-VISIBLE description, ``[proxied] `` prefix included.

    The effective budget for an upstream is ``min(server, global)``, so raising
    only this one does not widen a stricter per-server value. Read live, but
    applied only where a tool is advertised, so a description already
    registered keeps the length it was given until the next registration — a
    restart, or an upstream catalogue change (#893).
    """
    strip_schema_descriptions: bool = False
    advertise_context_query: bool = False
    """Advertise the proxy-only ``_context_query`` string in every upstream
    tool schema. Opt-in preserves existing catalogs; the argument is stripped
    before forwarding and only guides query-aware compression and surfacing."""
    # Bounded lock acquisition timeout (#208). Applies to internal state
    # locks in ``ProxyManager`` (selective compressor, LLM compressor,
    # extractor). A timeout here raises ``LockTimeoutError`` → recorded as
    # ``ErrorCategory.LOCK_TIMEOUT`` — distinct from upstream TIMEOUT (#206)
    # since lock hangs indicate an internal bug (deadlock / stuck holder),
    # not a slow dependency. Default 30s: anything longer is almost
    # certainly a bug in the lock-holding code.
    lock_timeout_seconds: float = Field(default=30.0, gt=0.0)
    consumer_model: str = ""
    context_budget_ratio: float = Field(default=0.05, ge=0.0, le=1.0)
    chars_per_token: float = Field(default=3.5, gt=0.0, allow_inf_nan=False)
    """Default chars-per-token ratio used to convert token budgets into char
    budgets. The default ``3.5`` is English-biased (ASCII text averages
    ~4.0 chars/token for cl100k_base). Set to ~2.0 for Korean-dominant
    workloads, ~1.3 for Chinese-dominant. Per-server / per-tool overrides
    are available on ``UpstreamServerConfig`` and ``ToolOverrideConfig``.
    Also used inside ``effective_max_result_chars()`` to convert the
    consumer model's context window from tokens to chars."""
    token_estimation_mode: TokenEstimationMode = TokenEstimationMode.STATIC
    """``static`` preserves chars-per-token conversion. ``unicode`` uses the
    runtime codepoint estimator and remains opt-in in 0.1.x."""
    cache: CacheConfig = Field(default_factory=CacheConfig)
    auto_index: AutoIndexConfig = Field(default_factory=AutoIndexConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    compression_feedback: CompressionFeedbackConfig = Field(
        default_factory=CompressionFeedbackConfig
    )
    progressive_reads: ProgressiveReadsConfig = Field(default_factory=ProgressiveReadsConfig)
    selection_telemetry: SelectionTelemetryConfig = Field(default_factory=SelectionTelemetryConfig)
    tool_relevance: ToolRelevanceConfig = Field(default_factory=ToolRelevanceConfig)
    exposure: ExposureConfig = Field(default_factory=ExposureConfig)
    toolgraph: ToolgraphConfig = Field(default_factory=ToolgraphConfig)

    @model_validator(mode="after")
    def _check_nonempty_upstream_prefixes(self) -> Self:
        # Empty / whitespace-only prefix produces composed names like
        # ``__list_items`` and skews ``tool_name_budget.composed_length``
        # (the prefix portion is zero), so the 64-char overflow guard
        # underestimates the real surface name a client sees. A single
        # empty prefix also slips past the uniqueness check below. Fail
        # at config load and name the upstream key so the user sees
        # which entry has the typo.
        empty = prefixes.empty_prefix_keys(
            {server_key: cfg.prefix for server_key, cfg in self.upstream_servers.items()}
        )
        if empty:
            raise ValueError(f"Empty upstream prefix in upstreams: {empty}")
        return self

    @model_validator(mode="after")
    def _check_unique_upstream_prefixes(self) -> Self:
        # Two upstreams sharing a prefix make composed names <prefix>__<tool>
        # collide. ProxyManager keeps a shared `seen_prefixed` set as
        # defense-in-depth and silently drops the second-loaded duplicate
        # with a logger.warning, so the user sees mysterious missing tools
        # instead of a config error. Surface the collision at load time.
        # The detection + wording live in ``proxy/prefixes.py``, shared with
        # the CLI's pre-save check so both sides can't diverge.
        collisions = prefixes.prefix_collisions(
            {server_key: cfg.prefix for server_key, cfg in self.upstream_servers.items()}
        )
        if collisions:
            raise ValueError(prefixes.format_collision_error(collisions))
        return self

    def effective_max_result_chars(self) -> int:
        """Compute max_result_chars scaled by consumer model's context window.

        If ``consumer_model`` is set and matches a known model prefix,
        the budget is ``context_window * context_budget_ratio * chars_per_token``
        (tokens → chars), capped at ``default_max_result_chars``. The
        ``chars_per_token`` field defaults to ``3.5`` (English-biased) and
        is configurable for non-Latin-script workloads.
        """
        if not self.consumer_model:
            return self.default_max_result_chars
        # Prefix match: "claude-sonnet-4-20250514" matches "claude-sonnet-4"
        ctx_tokens = None
        for prefix, tokens in MODEL_CONTEXT_WINDOWS.items():
            if self.consumer_model.startswith(prefix):
                ctx_tokens = tokens
                break
        if ctx_tokens is None:
            return self.default_max_result_chars
        model_budget = int(ctx_tokens * self.context_budget_ratio * self.chars_per_token)
        if model_budget <= 0:
            # context_budget_ratio is validated ge=0.0, so 0 is a legal value —
            # but a 0-char budget would flow into every per-server max_chars
            # and compress responses to nothing whenever min_result_retention
            # (itself disable-able with 0) doesn't rescue it. A degenerate
            # model budget means "model scaling effectively off", not "no
            # output": fall back to the static default, which is gt=0.
            return self.default_max_result_chars
        return min(model_budget, self.default_max_result_chars)

    @staticmethod
    def load_from_file(
        path: Path,
        env_overrides: EnvOverlayResult | dict[str, Any] | None = None,
        *,
        missing_ok: bool = True,
        log_warnings: bool = True,
    ) -> ProxyConfig | None:
        """Load config from *path*. Returns ``None`` on parse/validation error
        (with ``missing_ok=True``, distinct from file-not-found which returns
        a default ``ProxyConfig``).

        When *env_overrides* is supplied it is deep-merged on top of the file
        contents so env-set fields win over file-set fields, matching the
        ``env > file > defaults`` precedence documented in
        ``docs/configuration.md``.

        With ``missing_ok=False`` a missing file returns ``None`` instead of
        the env-only/defaults rebuild. Callers that already hold a better
        env-aware config than a rebuild from the overlay — ``STMConfig()``'s
        pydantic-settings parse, which is the authoritative reading of the
        environment — use this to decline the swap in a single atomic call,
        rather than a separate ``exists()``
        pre-check that races with file deletion. A file deleted between the
        existence check and the read also lands on ``None`` here, so every
        disappearance mode converges on "do not swap".

        Callers that need to distinguish "missing" from "present but broken"
        use ``load_from_file_with_status`` instead.
        """
        return ProxyConfig.load_from_file_with_status(
            path, env_overrides, missing_ok=missing_ok, log_warnings=log_warnings
        ).config

    @staticmethod
    def load_from_file_with_status(
        path: Path,
        env_overrides: EnvOverlayResult | dict[str, Any] | None = None,
        *,
        missing_ok: bool = True,
        log_warnings: bool = True,
    ) -> ConfigLoadResult:
        """``load_from_file`` with the failure mode preserved in the result.

        Same loading semantics and logging; additionally reports (a) an
        ``error`` string when the file exists but fails to parse/validate —
        so the server can surface "running defaults because the file is
        broken" in health output instead of only a stderr line — and (b) the
        ``unknown_keys`` the lenient validation silently dropped, walked over
        the raw file dict *before* the env merge so an env-injected key can
        never be misattributed as a file typo.

        ``error`` is sanitized (location + message, never ``input_value``):
        it flows to the MCP client via ``stm_proxy_health``, and a mistyped
        secret-bearing field would otherwise embed the secret itself.

        ``log_warnings=False`` suppresses the advisory warnings (permissive
        mode, unknown keys, missing ``cache.tool_annotation_policy``) for
        re-loads of a file some earlier load already warned about — e.g. ``ProxyManager.start()``'s empty-upstreams
        fallback, which would otherwise duplicate them at startup. Parse
        *failures* are always logged: a silent ``None`` is the dark-failure
        mode this module exists to prevent.
        """
        resolved = path.expanduser().resolve()
        overlay = _as_overlay(env_overrides)
        env_data = overlay.fragment if overlay is not None else None
        # Independent of whether the surviving fragment is empty: a dropped
        # bare block leaves nothing behind to validate, which is exactly why
        # the fragment cannot be the thing that decides this.
        env_rejected = _rejected_env_error(overlay)
        if not resolved.exists():
            logger.debug("Proxy config file not found: %s", resolved)
            if not missing_ok:
                return ConfigLoadResult(config=None, error=None)
            if env_data:
                try:
                    env_config = ProxyConfig.model_validate(env_data)
                    if log_warnings:
                        # An env-only setup is a supported shape and needs the
                        # same inert-upstream advisory as a file. This branch
                        # is the `missing_ok=True` callers (CLI, loader); the
                        # server takes `missing_ok=False` and warns from
                        # `_apply_proxy_file_config` instead.
                        warn_if_upstreams_inert(
                            _upstream_inert_state(env_data, enabled=env_config.enabled),
                            len(env_config.upstream_servers),
                            resolved,
                            logger_=logger,
                        )
                    return ConfigLoadResult(config=env_config, error=None, env_error=env_rejected)
                except Exception as exc:
                    logger.warning(
                        "Env-only proxy config failed validation: %s%s — using defaults",
                        exc,
                        _env_override_hint(exc, overlay),
                    )
                    # Reported, not raised: the defaults rebuild stays the
                    # result so every existing caller behaves as before, while
                    # a caller that cannot safely accept "some other config"
                    # can see that this one is not the operator's.
                    return ConfigLoadResult(
                        config=ProxyConfig(), error=None, env_error=_sanitized_load_error(exc)
                    )
            return ConfigLoadResult(config=ProxyConfig(), error=None, env_error=env_rejected)
        # Warn if config is group/world-readable (may contain API keys)
        mode = _permissive_mode(resolved)
        if mode is not None and log_warnings:
            logger.warning(
                "Proxy config %s has permissive mode %o — consider restricting to 0600",
                resolved,
                mode,
            )
        file_data: dict[str, Any] | None = None
        unknown_keys: tuple[str, ...] = ()
        try:
            loaded = json.loads(resolved.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                # Reject non-object roots BEFORE the env merge: ``[]`` would
                # otherwise slip through ``_deep_merge`` (``dict([])`` is
                # ``{}``) and an env override on top would validate cleanly,
                # silently accepting an invalid config file.
                raise ValueError(f"config root must be a JSON object, got {type(loaded).__name__}")
            file_data = loaded
            unknown_keys = tuple(find_unknown_keys(ProxyConfig, file_data))
            data = _deep_merge(file_data, env_data) if env_data else file_data
            config = ProxyConfig.model_validate(data)
            if unknown_keys and log_warnings:
                # One aggregated line, not one per key: the hot-reload loader
                # re-runs this on every mtime change.
                logger.warning(
                    "Proxy config %s has %d unknown key(s) (ignored — possible typo): %s",
                    resolved,
                    len(unknown_keys),
                    ", ".join(unknown_keys),
                )
            if log_warnings and config.cache.enabled and not _has_annotation_policy(data):
                # Migration advisory: new configs written by `mms init`/`mms add`
                # carry an explicit "strict", but a key-less legacy file keeps
                # the conservative Pydantic default. Checked against the MERGED
                # data (not the raw file) so an env-supplied policy — an
                # explicit operator choice — suppresses it. Skipped with the
                # cache disabled: the only other policy reader, the timeout-
                # retry gate, treats strict and conservative identically.
                logger.warning(
                    "Proxy config %s does not set cache.tool_annotation_policy — using the "
                    "'conservative' default. New configs are created with 'strict'; add "
                    '"cache": {"tool_annotation_policy": "strict"} (or "conservative" to '
                    "pin current behavior) to silence this.",
                    resolved,
                )
            if log_warnings:
                # Checked against the MERGED data so an env-enabled proxy
                # stays quiet.
                warn_if_upstreams_inert(
                    _upstream_inert_state(data, enabled=config.enabled),
                    len(config.upstream_servers),
                    resolved,
                    logger_=logger,
                )
            return ConfigLoadResult(
                config=config, error=None, unknown_keys=unknown_keys, env_error=env_rejected
            )
        except (json.JSONDecodeError, Exception) as exc:
            # The parse-failure warning dominates; the unknown-keys warning is
            # suppressed here but the paths stay in the result for `mms
            # config validate` to report alongside the errors.
            logger.warning(
                "Failed to parse proxy config %s: %s%s",
                resolved,
                exc,
                _env_override_hint(exc, overlay, file_data),
            )
            return ConfigLoadResult(
                config=None, error=_sanitized_load_error(exc), unknown_keys=unknown_keys
            )


def effective_compression(cfg: UpstreamServerConfig, proxy_cfg: ProxyConfig) -> CompressionStrategy:
    """Resolve an upstream's compression strategy against the global default.

    #292: ``ProxyConfig.default_compression`` was previously unread, so an
    operator setting it in ``stm_proxy.json`` saw no effect on any upstream —
    every server fell back to its own default of AUTO. ``model_fields_set``
    distinguishes "operator omitted compression" (→ honour the global default)
    from "operator explicitly typed ``compression: auto``" (→ honour their
    explicit choice).

    Lives here, beside the models it reads, so every reader shares it (#926):
    the proxy manager, the tuner and the CLI each held their own copy, and
    #924 was two of those copies disagreeing.
    """
    if "compression" in cfg.model_fields_set:
        return cfg.compression
    return proxy_cfg.default_compression


def effective_compression_pair(
    cfg: UpstreamServerConfig,
    override: ToolOverrideConfig | None,
    proxy_cfg: ProxyConfig,
) -> tuple[CompressionStrategy, HybridConfig | None]:
    """Full per-tool precedence: tool override → per-server → global default.

    Both fields resolve together because the convention suffix reads them
    together: ``hybrid`` decides whether the HYBRID strategy needs a retrieval
    hint at all. Every reader that needs a tool's effective strategy calls this
    (#926) — a partial copy is how the advertisement and the call path drifted
    apart in #924, and how the tuner came to recommend pinning a strategy the
    global default had already selected.

    Callers must not pair a server config from one reload generation with a
    global from another: resolving a connect-time omission against a newer
    global can name a follow-up tool the call will not use. Deliberately
    passing a retired connect-time config for a server the file no longer
    defines is fine, as long as every reader resolves that same object against
    the same global.
    """
    compression = effective_compression(cfg, proxy_cfg)
    hybrid = cfg.hybrid
    if override is not None:
        if override.compression is not None:
            compression = override.compression
        if override.hybrid is not None:
            hybrid = override.hybrid
    return compression, hybrid


def effective_max_result_chars(
    cfg: UpstreamServerConfig,
    override: ToolOverrideConfig | None,
    proxy_cfg: ProxyConfig,
) -> tuple[int, int | None]:
    """The char budget a call runs under, and the token budget behind it.

    Mirrors the strategy precedence above, for the other field a reader is
    likely to want: a token budget takes precedence over a char budget at the
    same level, and a server that leaves ``max_result_chars`` at its default
    defers to the model-aware global (``effective_max_result_chars()``) rather
    than to that literal default. The second element is the token budget when
    one is in force, so a caller can tell "1000 chars because 400 tokens" from
    "1000 chars" — the two differ for a caller reasoning about what an edit to
    a char field would do, and at which level it would have to be written.

    Shared for the same reason as the strategy pair (#926): the tuner reading
    the nominal ``max_result_chars`` saw 8000 where a call ran under a global
    16000, and recommended an "increase" that was a cut.

    ``chars_per_token`` resolves on its own axis — tool override → server →
    proxy default — independently of the level the token budget comes from
    (#929). The ratio describes the *content a tool returns* (a tool serving
    CJK text needs a different one than its English neighbour), so a tool that
    states its ratio and inherits the server's token budget gets both. Reading
    the tool ratio only beside a tool token budget silently dropped it, against
    what the field docstrings and the configuration guide both promise. A
    per-tool ``max_result_chars`` with no per-tool token budget beside it still
    ends the question before any ratio applies: a char budget is absolute, and
    nothing converts into it. Set both on one tool and tokens win, as they do
    at every other level, so the ratio stays live.

    What this returns is the *nominal* budget. ``token_estimation_mode`` set to
    ``unicode`` measures the response instead and derives its own char budget,
    and ``progressive`` has no result-size gate at all — neither reads the
    ratio at call time.
    """
    default_server_max = UpstreamServerConfig.model_fields["max_result_chars"].default
    token_budget = cfg.max_result_tokens
    cpt = cfg.chars_per_token if cfg.chars_per_token is not None else proxy_cfg.chars_per_token

    if override is not None:
        if override.max_result_chars is not None and override.max_result_tokens is None:
            return override.max_result_chars, None
        if override.max_result_tokens is not None:
            token_budget = override.max_result_tokens
        if override.chars_per_token is not None:
            cpt = override.chars_per_token

    if token_budget is not None:
        return tokens_to_chars(token_budget, cpt), token_budget
    if cfg.max_result_chars == default_server_max:
        return proxy_cfg.effective_max_result_chars(), None
    return cfg.max_result_chars, None


def _resolve_config_path_for_completion(
    proxy_init: dict[str, Any] | None, proxy_env: dict[str, Any]
) -> Path:
    """Where the config file lives, per the precedence that names it.

    ``config_path`` is itself configurable, so the completion source cannot ask
    a validated ``ProxyConfig`` for it — that config is what is being built.
    Init kwargs outrank the environment, matching the source order, and the
    field default is the last word. A value that is PRESENT but empty is
    honored, not skipped: ``config_path=""`` resolves to ``Path(".")`` at
    runtime, and reading the default file instead would complete a server out
    of a file the running config never names.

    A present value of some other type needs no handling of its own: the field
    is a ``Path``, which pydantic refuses to build from anything but a string
    or a path (``bytes`` included), so such a config fails validation whatever
    this returns.
    """
    for source in (proxy_init, proxy_env):
        if isinstance(source, dict) and "config_path" in source:
            raw = source["config_path"]
            if isinstance(raw, str | Path):
                return Path(raw).expanduser()
    return Path(str(ProxyConfig.model_fields["config_path"].default)).expanduser()


def _completed_entry_validates(file_entry: dict[str, Any], env_entry: dict[str, Any]) -> bool:
    """Whether the file's entry, with the environment's fields on top, stands up.

    The gate that keeps the completion incapable of breaking a config that
    worked. Emitting the file's entry can only help when the result VALIDATES;
    when it does not — a file field that is invalid on its own, an entry a
    higher-precedence source was already completing — contributing nothing
    leaves exactly the outcome the operator had before this source existed,
    including the identity of the error they were already getting.

    Asking whether the *environment's* fragment validates alone would be the
    wrong question in both directions: it says "skip" for a per-field override
    of an optional field, where the file has everything else to give
    (``…__GH__PREFIX`` over a file-declared server), and it says "complete" for
    a fragment that a broken file entry then fails.

    The merge is the shallow approximation of what ``deep_update`` does one
    level down; it decides eligibility only, and the real layering is settings'.
    """
    try:
        UpstreamServerConfig.model_validate({**file_entry, **env_entry})
    except Exception:
        return False
    return True


def _file_upstream_servers(path: Path) -> dict[str, Any]:
    """``upstream_servers`` as the file literally spells it, or ``{}``.

    Deliberately raw and forgiving: this runs during ``STMConfig()``, where a
    broken file must not become a construction failure. Every diagnostic about
    the file — unknown keys, permissions, parse errors — belongs to
    ``load_from_file_with_status``, which reads it again for real.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    servers = data.get("upstream_servers")
    return servers if isinstance(servers, dict) else {}


class UpstreamServerCompletionSource(PydanticBaseSettingsSource):
    """Completes a file-declared upstream server that env vars override per field.

    ``MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__<NAME>__<FIELD>`` is a documented
    override shape, but settings validates the fragment it built from the
    environment on its own, where a server carrying one field has no ``prefix``
    — so overriding one field of a server the FILE declares made ``STMConfig()``
    refuse to construct, naming a field the operator did set in a file the
    process never opened (#835). That killed the server at startup and every
    ``STMConfig()`` in the CLI with it.

    Ordered BELOW the environment source, this supplies the file's entry for
    exactly those server names the environment mentions, so ``deep_update``
    lands the env fields on top and validation sees the completed server —
    the same config the load path would later merge. Servers the environment
    does not mention are NOT supplied: the config-file boundary
    (``docs/configuration.md``) keeps the file's own upstreams arriving through
    ``load_from_file_with_status``, whose warnings have nowhere to go from
    inside a settings source.

    Server names match exactly, without case folding, because settings does not
    fold mapping keys either: the environment always yields a lower-cased name,
    so a file server spelled ``GitHub`` is not completed by ``…__GITHUB__…`` —
    and is not merged with it downstream either, which is the point. Anything
    that goes wrong here yields ``{}``, leaving the pre-#835 behavior.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
    ) -> None:
        super().__init__(settings_cls)
        self._init_settings = init_settings
        self._env_settings = env_settings

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # This source contributes a subtree through __call__; the per-field hook
        # is part of the ABC and is never consulted for it.
        raise NotImplementedError

    def __call__(self) -> dict[str, Any]:
        try:
            return self._completion()
        except Exception:
            # A completion is an optimization on the operator's behalf; it must
            # never be the reason a config fails to build.
            logger.debug("Upstream-server completion skipped", exc_info=True)
            return {}

    def _completion(self) -> dict[str, Any]:
        proxy_init = self._init_settings().get("proxy")
        if proxy_init is not None and not isinstance(proxy_init, dict):
            # An explicit `proxy=` object replaces the field wholesale, so there
            # is no env fragment left for the file to complete.
            return {}
        try:
            env_data = self._env_settings()
        except Exception:
            # A malformed complex value; the main build raises it unchanged.
            return {}
        proxy_env = env_data.get("proxy")
        if not isinstance(proxy_env, dict):
            return {}
        env_servers = proxy_env.get("upstream_servers")
        if not isinstance(env_servers, dict):
            return {}
        # A non-mapping env entry replaces the whole server rather than
        # overriding fields of it, so the file has nothing to contribute.
        env_entries = {
            name: entry for name, entry in env_servers.items() if isinstance(entry, dict)
        }
        if not env_entries:
            return {}
        file_servers = _file_upstream_servers(
            _resolve_config_path_for_completion(proxy_init, proxy_env)
        )
        completion = {}
        for name, env_entry in env_entries.items():
            file_entry = file_servers.get(name)
            if isinstance(file_entry, dict) and _completed_entry_validates(file_entry, env_entry):
                completion[name] = file_entry
        if not completion:
            return {}
        return {"proxy": {"upstream_servers": completion}}


class ProxyConfigLoader:
    """mtime-based hot-reload for proxy config file.

    Env overrides captured at construction time are re-applied on every
    reload so ``MEMTOMEM_STM_PROXY__*`` settings continue to win over file
    contents after the agent edits ``stm_proxy.json`` at runtime.
    """

    def __init__(
        self, path: Path, env_overrides: EnvOverlayResult | dict[str, Any] | None = None
    ) -> None:
        self._path = path.expanduser().resolve()
        self._cached: ProxyConfig | None = None
        self._mtime: float = 0.0
        self._env_overrides = env_overrides if env_overrides else None

    def seed(self, config: ProxyConfig) -> None:
        self._cached = config
        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:
            self._mtime = -1.0

    @property
    def current(self) -> ProxyConfig | None:
        """The last config this loader SEEDED or successfully LOADED, no stat().

        Identity ("is the snapshot I pinned still the newest generation?") is
        the only question this answers, and answering it must not itself be a
        filesystem read — the callers are the ones avoiding those (#871).

        Deliberately NOT "the last object ``get()`` returned": the unseeded
        fallbacks (missing file, unparseable file) build a config per call and
        do not record it, because recording one would have to advance state
        the parse-failure retry contract depends on staying unadvanced. Those
        loaders answer ``None`` here even after handing a config out, which
        reads as "no generation to compare against" — the safe answer for an
        identity check, since a caller that cannot establish staleness must
        not act as if it had. ``ProxyManager`` seeds in ``__init__``, so its
        loader always has one.
        """
        return self._cached

    def get(self) -> ProxyConfig:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            if self._cached is not None:
                return self._cached
            return (
                ProxyConfig.load_from_file(self._path, env_overrides=self._env_overrides)
                or ProxyConfig()
            )
        if mtime != self._mtime or self._cached is None:
            loaded = ProxyConfig.load_from_file(self._path, env_overrides=self._env_overrides)
            if loaded is not None:
                self._cached = loaded
                self._mtime = mtime
            else:
                # Don't advance _mtime on parse failure: the next get() must
                # retry instead of treating the broken file as up-to-date,
                # otherwise a fix that lands within filesystem mtime
                # granularity (or before any other write) would be ignored.
                logger.warning("Proxy config parse failed; keeping previous config")
        # An unseeded loader whose first load failed has nothing cached, and
        # every caller does attribute access on the result — hand back a config
        # rather than None. _mtime stays unadvanced above, so the next get()
        # still retries the broken file.
        return self._cached if self._cached is not None else self._env_only_config()

    def _env_only_config(self) -> ProxyConfig:
        """Defaults for an unseeded loader with nothing loadable, env applied.

        A broken file must not be the one condition that drops
        ``MEMTOMEM_STM_PROXY__*``: this class promises they win over file
        contents, and the sibling OSError branch above honors them through
        ``load_from_file``. Bare defaults here would make the SAME environment
        mean different things depending on whether the file is missing (env
        applied) or unparseable (env silently gone).
        """
        overlay = _as_overlay(self._env_overrides)
        fragment = overlay.fragment if overlay is not None else None
        if fragment:
            try:
                return ProxyConfig.model_validate(fragment)
            except ValidationError:
                # The overlay itself is unusable; defaults are all that is
                # left. Logged because a silently dropped override is the
                # dark-failure mode this branch exists to close.
                logger.warning("Environment config overrides are invalid; using defaults")
        return ProxyConfig()
