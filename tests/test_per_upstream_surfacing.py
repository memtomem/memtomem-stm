"""Per-upstream surfacing toggle (``UpstreamServerConfig.surfacing_enabled``).

The flag lives in ``stm_proxy.json`` (ProxyConfig) and is enforced in
``ProxyManager`` — *not* in the ``SurfacingEngine`` relevance gate, which is
built once at startup from the top-level ``SurfacingConfig`` and never sees
per-upstream config. These tests pin both the config default/round-trip and the
manager short-circuit on every surfacing entry point (normal + progressive),
including the fail-open behavior for an unknown server.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from memtomem_stm.proxy.config import ProxyConfig, UpstreamServerConfig
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics import TokenTracker

_LONG = "x" * 50  # comfortably over any min_response_chars the engine would gate on


def _manager(tmp_path: Path, *, surfacing_enabled: bool) -> ProxyManager:
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "stm_proxy.json",
        upstream_servers={
            "svc": UpstreamServerConfig(prefix="svc", surfacing_enabled=surfacing_enabled),
        },
    )
    engine = MagicMock()
    engine.surface = AsyncMock(return_value="SURFACED")
    engine.observability = MagicMock()
    engine.injection_mode = "append"
    return ProxyManager(proxy_cfg, TokenTracker(), surfacing_engine=engine)


# ── config field ─────────────────────────────────────────────────────────


class TestConfigField:
    def test_default_is_enabled(self) -> None:
        assert UpstreamServerConfig(prefix="c7").surfacing_enabled is True

    def test_round_trips_through_model_dump(self) -> None:
        cfg = UpstreamServerConfig(prefix="c7", surfacing_enabled=False)
        dumped = cfg.model_dump()
        assert dumped["surfacing_enabled"] is False
        assert UpstreamServerConfig(**dumped).surfacing_enabled is False


# ── _surfacing_enabled_for ───────────────────────────────────────────────


class TestEnabledLookup:
    def test_reads_per_upstream_flag(self, tmp_path: Path) -> None:
        assert _manager(tmp_path, surfacing_enabled=False)._surfacing_enabled_for("svc") is False
        assert _manager(tmp_path, surfacing_enabled=True)._surfacing_enabled_for("svc") is True

    def test_unknown_server_fails_open(self, tmp_path: Path) -> None:
        # An unconfigured server must not silently lose surfacing — best-effort.
        assert _manager(tmp_path, surfacing_enabled=False)._surfacing_enabled_for("ghost") is True


# ── ProxyManager enforcement ─────────────────────────────────────────────


class TestApplySurfacing:
    async def test_disabled_upstream_short_circuits(self, tmp_path: Path) -> None:
        mgr = _manager(tmp_path, surfacing_enabled=False)
        out = await mgr._apply_surfacing("svc", "lookup", {}, _LONG)
        assert out == _LONG
        mgr._surfacing_engine.surface.assert_not_called()
        mgr._surfacing_engine.observability.record_skip.assert_called_once_with(
            "lookup", "upstream_disabled"
        )

    async def test_enabled_upstream_surfaces(self, tmp_path: Path) -> None:
        mgr = _manager(tmp_path, surfacing_enabled=True)
        out = await mgr._apply_surfacing("svc", "lookup", {}, _LONG)
        assert out == "SURFACED"
        mgr._surfacing_engine.surface.assert_awaited_once()
        mgr._surfacing_engine.observability.record_skip.assert_not_called()


class TestApplySurfacingProgressive:
    async def test_disabled_upstream_short_circuits(self, tmp_path: Path) -> None:
        mgr = _manager(tmp_path, surfacing_enabled=False)
        text, ok, err = await mgr._apply_surfacing_on_progressive("svc", "lookup", {}, _LONG)
        assert (text, ok, err) == (_LONG, None, None)
        mgr._surfacing_engine.surface.assert_not_called()
        mgr._surfacing_engine.observability.record_skip.assert_called_once_with(
            "lookup", "upstream_disabled"
        )

    async def test_enabled_upstream_surfaces(self, tmp_path: Path) -> None:
        mgr = _manager(tmp_path, surfacing_enabled=True)
        text, ok, err = await mgr._apply_surfacing_on_progressive("svc", "lookup", {}, _LONG)
        assert text == "SURFACED"
        assert ok is True
        mgr._surfacing_engine.surface.assert_awaited_once()
