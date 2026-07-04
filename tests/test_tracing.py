"""Direct tests for ``memtomem_stm.observability.tracing`` (#619).

``traced()`` happy paths (delegation, sampling, proxy integration) are already
covered in ``tests/test_observability.py::TestLangfuseTracing`` — this file
pins the remaining contracts: ``init_langfuse`` construction/degrade behavior,
``_SafeObservation``'s never-break-the-traced-call guarantee, the one-shot
log escalation, and ``shutdown_langfuse``.
"""

import os
import sys
import types
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from memtomem_stm.observability import tracing
from memtomem_stm.observability.tracing import (
    _SafeObservation,
    init_langfuse,
    shutdown_langfuse,
    traced,
)


@pytest.fixture(autouse=True)
def _reset_tracing_globals(monkeypatch):
    """Isolate the module-level singleton state per test (auto-restored)."""
    monkeypatch.setattr(tracing, "_langfuse_client", None)
    monkeypatch.setattr(tracing, "_sampling_rate", 1.0)
    monkeypatch.setattr(tracing, "_warned_observation_failure", False)


def _fake_langfuse_module(ctor):
    """A stand-in ``langfuse`` module whose ``Langfuse`` is *ctor*."""
    mod = types.ModuleType("langfuse")
    mod.Langfuse = ctor
    return mod


class TestInitLangfuse:
    def test_disabled_returns_none_and_leaves_globals_untouched(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(tracing, "_langfuse_client", sentinel)
        monkeypatch.setattr(tracing, "_sampling_rate", 0.25)

        assert init_langfuse(SimpleNamespace(enabled=False)) is None

        assert tracing._langfuse_client is sentinel
        assert tracing._sampling_rate == 0.25

    def test_import_failure_degrades_to_none(self, monkeypatch):
        # ``sys.modules["langfuse"] = None`` makes ``from langfuse import …``
        # raise ImportError regardless of whether the SDK is installed.
        monkeypatch.setitem(sys.modules, "langfuse", None)

        assert init_langfuse(SimpleNamespace(enabled=True)) is None
        assert tracing._langfuse_client is None

    def test_forwards_only_nonempty_credentials(self, monkeypatch):
        captured: dict = {}

        def ctor(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setitem(sys.modules, "langfuse", _fake_langfuse_module(ctor))
        config = SimpleNamespace(
            enabled=True, sampling_rate=0.5, public_key="pk", secret_key="", host="http://lf"
        )

        client = init_langfuse(config)

        assert client is not None
        assert tracing._langfuse_client is client
        assert captured == {"public_key": "pk", "host": "http://lf"}  # empty secret_key omitted
        assert tracing._sampling_rate == 0.5

    def test_otel_service_name_set_only_when_unset(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "langfuse", _fake_langfuse_module(MagicMock()))
        # setenv-then-delenv (not bare delenv): registers a restore even when
        # the variable was absent, so the value init_langfuse writes directly
        # into os.environ cannot leak past this test.
        monkeypatch.setenv("OTEL_SERVICE_NAME", "placeholder")
        monkeypatch.delenv("OTEL_SERVICE_NAME")

        init_langfuse(SimpleNamespace(enabled=True), service_name="custom-svc")
        assert os.environ.get("OTEL_SERVICE_NAME") == "custom-svc"

        monkeypatch.setenv("OTEL_SERVICE_NAME", "pre-existing")
        init_langfuse(SimpleNamespace(enabled=True), service_name="custom-svc")
        assert os.environ.get("OTEL_SERVICE_NAME") == "pre-existing"


class TestSafeObservation:
    def test_enter_failure_degrades_and_body_still_runs(self, caplog):
        inner = MagicMock()
        inner.__enter__ = MagicMock(side_effect=RuntimeError("exporter down"))
        ran = False

        with caplog.at_level("DEBUG", logger="memtomem_stm.observability.tracing"):
            with _SafeObservation(inner) as span:
                ran = True
                assert span is None
        assert ran

    def test_body_exception_propagates(self):
        inner = MagicMock()
        inner.__enter__ = MagicMock(return_value="span")
        inner.__exit__ = MagicMock(return_value=False)

        with pytest.raises(ValueError, match="from the body"):
            with _SafeObservation(inner):
                raise ValueError("from the body")

        # The SDK still saw the body's exception (exc_type is ValueError).
        assert inner.__exit__.call_args.args[0] is ValueError

    def test_exit_failure_swallowed(self):
        inner = MagicMock()
        inner.__enter__ = MagicMock(return_value="span")
        inner.__exit__ = MagicMock(side_effect=RuntimeError("flush failed"))

        with _SafeObservation(inner):
            pass  # __exit__ raising must not escape

    def test_exit_without_enter_does_not_touch_inner(self):
        inner = MagicMock()
        obs = _SafeObservation(inner)

        assert obs.__exit__(None, None, None) is False
        inner.__exit__.assert_not_called()


class TestTracedDegrade:
    def test_constructor_failure_returns_nullcontext(self, monkeypatch):
        client = MagicMock()
        client.start_as_current_observation.side_effect = RuntimeError("bad config")
        monkeypatch.setattr(tracing, "_langfuse_client", client)

        ctx = traced("proxy_call")

        assert isinstance(ctx, nullcontext)
        with ctx as span:
            assert span is None


class TestLogObservationFailureEscalation:
    def test_first_failure_warns_then_repeats_at_debug(self, caplog):
        with caplog.at_level("DEBUG", logger="memtomem_stm.observability.tracing"):
            tracing._log_observation_failure("start")
            tracing._log_observation_failure("start")

        levels = [r.levelname for r in caplog.records]
        assert levels == ["WARNING", "DEBUG"]


class TestShutdownLangfuse:
    def test_calls_shutdown_on_explicit_client(self):
        client = MagicMock()
        shutdown_langfuse(client)
        client.shutdown.assert_called_once_with()

    def test_falls_back_to_global_client(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(tracing, "_langfuse_client", client)
        shutdown_langfuse()
        client.shutdown.assert_called_once_with()

    def test_noop_without_shutdown_attr_or_client(self):
        shutdown_langfuse(object())  # no .shutdown — must not raise
        shutdown_langfuse(None)  # no client anywhere — must not raise
