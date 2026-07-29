"""OTLP span export (#789 stage 2).

The two properties ADR 0001 makes load-bearing get the most coverage here:
lineage is *real* (genuine parent/child, never adopted from another backend's
ambient context) and attributes are *body-free* (an exact key map plus a
privacy screen, with secrets planted in every allowed string field).

No network: the injection seam takes a span *processor*, so these tests attach
``SimpleSpanProcessor(InMemorySpanExporter())`` and read the exported spans
directly.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import threading
import time

import pytest
from unittest.mock import MagicMock
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from memtomem_stm.config import OtlpExportConfig, STMConfig, normalize_otlp_endpoint
from memtomem_stm.observability import otlp as otlp_mod
from memtomem_stm.observability.otlp import (
    _CountingExporter,
    _Counters,
    _screen_attributes,
    get_otlp,
    init_otlp,
    shutdown_otlp,
)
from memtomem_stm.observability.tracing import traced

# A value the shared privacy detector actually matches — asserted below,
# because a screening test whose input is not detectable would pass
# tautologically no matter what the exporter does.
SECRET = "ghp_" + "a" * 36


def test_the_planted_secret_is_actually_detectable():
    """Positive control for every screening assertion in this file."""
    from memtomem_stm.proxy.privacy import contains_sensitive_content

    assert contains_sensitive_content(SECRET)


@pytest.fixture(autouse=True)
def _reset_otlp_singleton(monkeypatch):
    """Never let one test's emitter leak into the next (or into traced())."""
    monkeypatch.setattr(otlp_mod, "_emitter", None)
    monkeypatch.setattr(otlp_mod, "_warned_export_failure", False)


@pytest.fixture
def emitter_and_exporter(monkeypatch):
    """A live emitter writing into an in-memory exporter."""
    exporter = InMemorySpanExporter()
    config = OtlpExportConfig(enabled=True, endpoint="http://localhost:4318")
    emitter = init_otlp(config, span_processor=SimpleSpanProcessor(exporter))
    assert emitter is not None
    monkeypatch.setattr(otlp_mod, "_emitter", emitter)
    return emitter, exporter


def _by_name(exporter):
    return {span.name: span for span in exporter.get_finished_spans()}


# ── Lineage ──────────────────────────────────────────────────────────────


class TestLineage:
    def test_nested_traced_blocks_produce_real_parentage(self, emitter_and_exporter):
        _, exporter = emitter_and_exporter

        with traced("proxy_call", metadata={"server": "srv"}):
            with traced("proxy_call_clean", metadata={"server": "srv"}):
                pass

        spans = _by_name(exporter)
        root, child = spans["proxy_call"], spans["proxy_call_clean"]
        assert root.parent is None
        assert child.parent is not None
        assert child.parent.span_id == root.context.span_id
        assert child.context.trace_id == root.context.trace_id

    def test_root_ignores_an_active_ambient_span(self, emitter_and_exporter):
        """A foreign global-provider span must never become our parent.

        Langfuse installs a global TracerProvider, so without the explicit
        empty Context our root would be parented into *its* trace — a span
        exported to a different backend, i.e. synthesized lineage.
        """
        _, exporter = emitter_and_exporter
        foreign_provider = TracerProvider()
        foreign = foreign_provider.get_tracer("foreign")

        with foreign.start_as_current_span("langfuse_span") as ambient:
            with traced("proxy_call", metadata={"server": "srv"}):
                pass

        root = _by_name(exporter)["proxy_call"]
        assert root.parent is None
        assert root.context.trace_id != ambient.context.trace_id

    def test_concurrent_tasks_do_not_cross_parent(self, emitter_and_exporter):
        """Two tasks opening roots concurrently stay in separate traces."""
        _, exporter = emitter_and_exporter
        started = asyncio.Event()

        async def first():
            with traced("root_a", metadata={"tool": "a"}):
                started.set()
                await asyncio.sleep(0.02)
                with traced("child_a", metadata={"tool": "a"}):
                    pass

        async def second():
            await started.wait()
            with traced("root_b", metadata={"tool": "b"}):
                pass

        asyncio.run(_gather(first(), second()))

        spans = _by_name(exporter)
        assert spans["root_a"].parent is None
        assert spans["root_b"].parent is None
        assert spans["root_a"].context.trace_id != spans["root_b"].context.trace_id
        assert spans["child_a"].parent.span_id == spans["root_a"].context.span_id

    def test_inner_exception_restores_the_outer_parent(self, emitter_and_exporter):
        """A sibling opened after a failed inner span still parents to the root."""
        _, exporter = emitter_and_exporter

        with traced("proxy_call", metadata={"server": "srv"}):
            with pytest.raises(ValueError):
                with traced("proxy_call_clean", metadata={"server": "srv"}):
                    raise ValueError("boom")
            with traced("proxy_call_compress", metadata={"server": "srv"}):
                pass

        spans = _by_name(exporter)
        root_id = spans["proxy_call"].context.span_id
        assert spans["proxy_call_clean"].parent.span_id == root_id
        assert spans["proxy_call_compress"].parent.span_id == root_id


async def _gather(*coros):
    await asyncio.gather(*coros)


# ── Body-free attributes ─────────────────────────────────────────────────


class TestBodyFreeAttributes:
    def test_maps_known_keys_and_drops_everything_else(self):
        attributes, redacted = _screen_attributes(
            "proxy_call",
            {
                "server": "srv",
                "tool": "t",
                "trace_id": "abc123",
                "error_message": "boom: /home/u/secret.txt",
                "arguments": {"path": "/etc/passwd"},
                "unknown_key": "whatever",
            },
        )

        assert attributes == {
            "stm.server": "srv",
            "stm.tool": "t",
            "stm.trace_id": "abc123",
        }
        # An unmapped key is not a redaction — it was never in the vocabulary.
        assert redacted == 0

    def test_rejects_wrong_types_including_bool_for_int(self):
        attributes, _ = _screen_attributes(
            "proxy_call_compress",
            {"server": 123, "max_chars": True, "strategy": 7, "tool": "t"},
        )

        assert attributes == {"stm.tool": "t"}

    @pytest.mark.parametrize("field", ["server", "tool", "trace_id"])
    def test_secrets_in_allowed_string_fields_are_screened(self, field):
        attributes, redacted = _screen_attributes(
            "proxy_call", {field: SECRET, "server": "srv"} if field != "server" else {field: SECRET}
        )

        assert redacted == 1
        assert SECRET not in str(attributes)

    def test_caller_supplied_metadata_spans_export_nothing(self):
        """The whole class of MCP-argument-sourced values, not one field.

        The privacy screen only recognizes *known* secret shapes, so ordinary
        non-secret caller text would sail through it. These spans are excluded
        by provenance instead: they have no vocabulary at all.
        """
        for span_name, metadata in [
            (
                "stm_surfacing_feedback",
                {
                    "surfacing_id": "whatever the caller typed",
                    "rating": "helpful",
                    "memory_id": "/home/u/private/notes.md",
                    "ratings_count": 3,
                },
            ),
            (
                "stm_surfacing_stats",
                {"tool": "internal-project-codename", "since": "x", "limit": 5},
            ),
            ("stm_selection_stats", {"tool": "t"}),
            ("surfacing_feedback_boost", {"surfacing_id": "s", "chunk_count": 2}),
        ]:
            attributes, redacted = _screen_attributes(span_name, metadata)
            assert attributes == {}, span_name
            assert redacted == 0, span_name

    def test_same_key_is_admitted_or_refused_by_span_provenance(self):
        """`tool` is STM's routing target on proxy_call, caller text on the stats tool."""
        routed, _ = _screen_attributes("proxy_call", {"tool": "mem_search"})
        typed, _ = _screen_attributes("stm_surfacing_stats", {"tool": "mem_search"})

        assert routed == {"stm.tool": "mem_search"}
        assert typed == {}

    def test_an_unknown_span_name_exports_nothing(self):
        attributes, redacted = _screen_attributes("some_future_span", {"server": "srv"})

        assert attributes == {}
        assert redacted == 0

    def test_control_tool_span_exports_no_attributes_end_to_end(self, emitter_and_exporter):
        """Through traced(), not just the screen: the real call path."""
        _, exporter = emitter_and_exporter

        with traced(
            "stm_surfacing_feedback",
            metadata={"surfacing_id": "caller-text", "rating": "helpful", "memory_id": "m1"},
        ):
            pass

        span = _by_name(exporter)["stm_surfacing_feedback"]
        assert dict(span.attributes) == {}

    def test_progressive_read_key_never_exports(self, emitter_and_exporter):
        _, exporter = emitter_and_exporter

        with traced("proxy_call_read_more", metadata={"server": "s", "key": "handle-123"}):
            pass

        attributes = dict(_by_name(exporter)["proxy_call_read_more"].attributes)
        assert attributes == {"stm.server": "s"}

    def test_exception_exports_class_name_only(self, emitter_and_exporter):
        """No message anywhere: not as an event, not as a status description."""
        _, exporter = emitter_and_exporter

        class CustomFailure(RuntimeError):
            pass

        with pytest.raises(CustomFailure):
            with traced("proxy_call", metadata={"server": "srv"}):
                raise CustomFailure(f"connection to {SECRET} refused")

        span = _by_name(exporter)["proxy_call"]
        assert span.attributes["error.type"] == "CustomFailure"
        assert span.status.status_code is trace_api.StatusCode.ERROR
        assert span.status.description is None
        assert span.events == ()
        assert SECRET not in span.to_json()

    def test_a_credential_shaped_exception_class_name_is_screened(self, emitter_and_exporter):
        """A class name is usually a literal — but it need not be.

        A dynamically constructed exception type carries whatever built it, so
        `error.type` is screened like every other exported string rather than
        trusted for being "just a class name".
        """
        _, exporter = emitter_and_exporter
        Planted = type(f"Failure_{SECRET}", (RuntimeError,), {})

        with pytest.raises(Planted):
            with traced("proxy_call", metadata={"server": "srv"}):
                raise Planted("boom")

        span = _by_name(exporter)["proxy_call"]
        assert span.attributes["error.type"] == "redacted"
        assert span.status.status_code is trace_api.StatusCode.ERROR
        assert SECRET not in span.to_json()

    def test_an_ordinary_exception_class_name_still_exports(self, emitter_and_exporter):
        """Positive control: screening must not blank out normal error types."""
        _, exporter = emitter_and_exporter

        with pytest.raises(TimeoutError):
            with traced("proxy_call", metadata={"server": "srv"}):
                raise TimeoutError("upstream slow")

        assert _by_name(exporter)["proxy_call"].attributes["error.type"] == "TimeoutError"

    def test_resource_ignores_environment_attributes(self, monkeypatch):
        """OTEL_RESOURCE_ATTRIBUTES bypasses the attribute map — so it is not read."""
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"deployment.environment={SECRET}")
        exporter = InMemorySpanExporter()
        emitter = init_otlp(
            OtlpExportConfig(enabled=True, endpoint="http://localhost:4318"),
            span_processor=SimpleSpanProcessor(exporter),
        )
        assert emitter is not None

        with emitter.span("proxy_call", {"server": "srv"}):
            pass

        resource = dict(exporter.get_finished_spans()[0].resource.attributes)
        assert resource == {"service.name": "memtomem-stm", "service.version": _version()}
        assert SECRET not in str(resource)


def _version() -> str:
    from memtomem_stm import __version__

    return __version__


# ── Coexistence with Langfuse ────────────────────────────────────────────


class TestCoexistence:
    def test_both_consumers_receive_the_same_boundary(self, emitter_and_exporter, monkeypatch):
        from unittest.mock import MagicMock

        _, exporter = emitter_and_exporter
        client = MagicMock()
        monkeypatch.setattr("memtomem_stm.observability.tracing._langfuse_client", client)

        with traced("proxy_call", metadata={"server": "srv"}):
            pass

        assert client.start_as_current_observation.call_count == 1
        assert "proxy_call" in _by_name(exporter)

    def test_a_raising_langfuse_leg_does_not_lose_the_otlp_span(
        self, emitter_and_exporter, monkeypatch
    ):
        from unittest.mock import MagicMock

        _, exporter = emitter_and_exporter
        client = MagicMock()
        client.start_as_current_observation.side_effect = RuntimeError("exporter down")
        monkeypatch.setattr("memtomem_stm.observability.tracing._langfuse_client", client)

        with traced("proxy_call", metadata={"server": "srv"}):
            pass

        assert "proxy_call" in _by_name(exporter)

    def test_global_tracer_provider_is_left_alone(self, emitter_and_exporter):
        emitter, _ = emitter_and_exporter

        assert trace_api.get_tracer_provider() is not emitter._provider


# ── Degradation ──────────────────────────────────────────────────────────


class _FailingExporter:
    def __init__(self, *, raises: bool) -> None:
        self._raises = raises

    def export(self, spans):
        if self._raises:
            raise ConnectionError("collector unreachable")
        return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        pass


class TestDegradation:
    @pytest.mark.parametrize("raises", [True, False])
    def test_export_failures_are_counted_not_raised(self, raises):
        counters = _Counters()
        wrapped = _CountingExporter(_FailingExporter(raises=raises), counters)

        assert wrapped.export([]) is SpanExportResult.FAILURE
        assert counters.snapshot()["export_failures"] == 1

    def test_first_failure_warns_then_repeats_at_debug(self, caplog):
        counters = _Counters()
        wrapped = _CountingExporter(_FailingExporter(raises=True), counters)

        with caplog.at_level("DEBUG", logger="memtomem_stm.observability.otlp"):
            wrapped.export([])
            wrapped.export([])

        levels = [r.levelname for r in caplog.records if "OTLP span export failed" in r.message]
        assert levels == ["WARNING", "DEBUG"]

    def test_a_broken_exporter_does_not_break_the_traced_call(self, monkeypatch):
        emitter = init_otlp(
            OtlpExportConfig(enabled=True, endpoint="http://localhost:4318"),
            span_processor=SimpleSpanProcessor(_FailingExporter(raises=True)),
        )
        monkeypatch.setattr(otlp_mod, "_emitter", emitter)
        ran = False

        with traced("proxy_call", metadata={"server": "srv"}):
            ran = True

        assert ran

    @pytest.mark.parametrize(
        "target,message",
        [
            ("_screen_attributes", "screening blew up"),
            ("start_span", "provider blew up"),
            ("end", "end blew up"),
        ],
    )
    def test_the_traced_call_survives_any_emitter_failure(
        self, emitter_and_exporter, monkeypatch, target, message
    ):
        """_FanOut assumes legs are self-safe — pin that on every internal path."""
        emitter, _ = emitter_and_exporter

        def boom(*args, **kwargs):
            raise RuntimeError(message)

        if target == "_screen_attributes":
            monkeypatch.setattr(otlp_mod, "_screen_attributes", boom)
        elif target == "start_span":
            monkeypatch.setattr(emitter._tracer, "start_span", boom)
        else:
            real_start = emitter._tracer.start_span

            def start_with_broken_end(*args, **kwargs):
                span = real_start(*args, **kwargs)
                monkeypatch.setattr(span, "end", boom)
                return span

            monkeypatch.setattr(emitter._tracer, "start_span", start_with_broken_end)

        ran = False
        with traced("proxy_call", metadata={"server": "srv"}):
            ran = True

        assert ran

    def test_a_body_exception_still_propagates_through_the_otlp_leg(self, emitter_and_exporter):
        with pytest.raises(ValueError, match="from the body"):
            with traced("proxy_call", metadata={"server": "srv"}):
                raise ValueError("from the body")

    def test_initialization_failure_is_not_degraded_away(self, monkeypatch):
        """Enabled-but-unbuildable must fail startup, not run silently inert.

        Reachable through the standard OTel environment surface: an invalid
        `OTEL_EXPORTER_OTLP_COMPRESSION` is rejected by the exporter
        constructor, not by config validation.
        """
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_COMPRESSION", "bogus")

        with pytest.raises(ValueError):
            init_otlp(OtlpExportConfig(enabled=True, endpoint="http://localhost:4318"))

    async def test_lifespan_surfaces_an_unbuildable_exporter(self, monkeypatch):
        """The same contract through the server lifespan, not just init_otlp."""
        from memtomem_stm.server import app_lifespan

        monkeypatch.setenv("MEMTOMEM_STM_OTLP__ENABLED", "1")
        monkeypatch.setenv("MEMTOMEM_STM_OTLP__ENDPOINT", "http://localhost:4318")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_COMPRESSION", "bogus")

        with pytest.raises(ValueError):
            async with app_lifespan(MagicMock()):
                pass

    def test_a_broken_sdk_install_is_not_degraded_away(self, monkeypatch):
        """Config validation already proves the packages are declared present.

        An ImportError here therefore means a broken or partial install, not
        an unconfigured one — degrading would leave enabled telemetry inert.
        """
        import sys

        monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", None)

        with pytest.raises(ImportError):
            init_otlp(OtlpExportConfig(enabled=True, endpoint="http://localhost:4318"))

    def test_disabled_config_builds_no_emitter(self):
        assert init_otlp(OtlpExportConfig()) is None
        assert get_otlp() is None
        assert type(traced("proxy_call")).__name__ == "nullcontext"


# ── Config ───────────────────────────────────────────────────────────────


class TestConfig:
    def test_enabled_requires_an_endpoint(self):
        with pytest.raises(ValueError, match="requires endpoint"):
            OtlpExportConfig(enabled=True)

    def test_missing_packages_names_the_install_command(self, monkeypatch):
        monkeypatch.setattr("importlib.util.find_spec", lambda name: None)

        with pytest.raises(ValueError, match=r"memtomem-stm\[otlp\]"):
            OtlpExportConfig(enabled=True, endpoint="http://localhost:4318")

    def test_validation_error_never_echoes_a_header_value(self):
        with pytest.raises(ValueError) as excinfo:
            OtlpExportConfig(
                enabled=True,
                endpoint="http://localhost:4318",
                headers={"authorization": SECRET},
                timeout_seconds=-1,
            )

        assert SECRET not in str(excinfo.value)

    def test_root_config_error_never_echoes_the_block(self, monkeypatch):
        """Through STMConfig, not just the child model.

        A child's ``hide_input_in_errors`` does not govern an error raised on
        the *parent* field, so a whole block that fails to coerce would render
        verbatim — headers and all. These errors are logged by the server.
        """
        monkeypatch.setenv("MEMTOMEM_STM_OTLP", f'"{SECRET}"')

        with pytest.raises(ValueError) as excinfo:
            STMConfig()

        rendered = str(excinfo.value)
        assert SECRET not in rendered
        assert "input_value" not in rendered

    @pytest.mark.parametrize(
        "template,reason",
        [
            ("sc heme://{s}", "urlsplit sees no scheme"),
            ("http://host:{s}", "the port parser quotes the offending text"),
            ("http://ho\u2100st-{s}/x", "urlsplit raises on NFKC normalization"),
            ("http://[::1-{s}", "urlsplit raises on the malformed IPv6 literal"),
        ],
    )
    def test_rejected_endpoints_are_never_echoed(self, template, reason):
        """A rejected URL is the one most likely to carry the token.

        Each case plants the sentinel and asserts it is *there* first —
        otherwise "SECRET not in error" would hold trivially for a URL that
        never contained it.
        """
        planted = template.format(s=SECRET)
        assert SECRET in planted

        with pytest.raises(ValueError) as excinfo:
            normalize_otlp_endpoint(planted)

        assert SECRET not in str(excinfo.value), reason

    @pytest.mark.parametrize(
        "endpoint,expected",
        [
            ("http://[::1]:4318", "http://[::1]:4318/v1/traces"),
            ("https://collector.example", "https://collector.example/v1/traces"),
            ("http://host.", "http://host./v1/traces"),
        ],
    )
    def test_withholding_does_not_reject_legitimate_urls(self, endpoint, expected):
        """Positive control: the hardening must not have narrowed what is valid."""
        assert normalize_otlp_endpoint(endpoint) == expected

    @pytest.mark.parametrize(
        "endpoint,expected",
        [
            ("http://localhost:4318", "http://localhost:4318/v1/traces"),
            ("http://localhost:4318/", "http://localhost:4318/v1/traces"),
            ("https://collector.example/custom/path", "https://collector.example/custom/path"),
        ],
    )
    def test_endpoint_normalization(self, endpoint, expected):
        assert normalize_otlp_endpoint(endpoint) == expected

    @pytest.mark.parametrize(
        "headers",
        [
            {"authorization": "{s}\ninvalid"},
            {"authorization": "{s}\r\nX-Injected: y"},
            {"authorization": "{s}\x00"},
            {"authorization": " {s}"},
            {"auth orization": "{s}"},
        ],
    )
    def test_malformed_headers_fail_startup_without_echoing(self, headers):
        """A header the HTTP layer would refuse must not reach it.

        Requests rejects such a value deep in the export path, and the OTLP
        exporter logs the rejection *with the value*. Startup is the last
        place the failure can be reported without printing the credential.
        """
        planted = {k.format(s=SECRET): v.format(s=SECRET) for k, v in headers.items()}
        assert any(SECRET in part for part in (*planted, *planted.values()))

        with pytest.raises(ValueError) as excinfo:
            OtlpExportConfig(enabled=True, endpoint="http://localhost:4318", headers=planted)

        assert SECRET not in str(excinfo.value)

    def test_a_secret_shaped_header_name_is_withheld_too(self):
        """A credential is itself a valid RFC 7230 token, so it can be the name."""
        with pytest.raises(ValueError) as excinfo:
            OtlpExportConfig(enabled=True, endpoint="http://localhost:4318", headers={SECRET: "\n"})

        assert SECRET not in str(excinfo.value)

    @pytest.mark.parametrize(
        "var", ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"]
    )
    def test_standard_header_env_is_refused_when_enabled(self, monkeypatch, var, caplog):
        """The SDK would consume these without STM's syntax check.

        `headers or parse_env_headers(...)` means an empty STM mapping hands
        the channel to the environment, and the SDK's parser logs a malformed
        value verbatim while constructing the exporter — the same credential
        leak the syntax check closes, through a different door.
        """
        monkeypatch.setenv(var, f"authorization={SECRET}\ninvalid")

        with caplog.at_level("DEBUG"):
            with pytest.raises(ValueError) as excinfo:
                OtlpExportConfig(enabled=True, endpoint="http://localhost:4318")

        assert var in str(excinfo.value)
        assert SECRET not in str(excinfo.value)
        assert all(SECRET not in record.getMessage() for record in caplog.records)

    @pytest.mark.parametrize(
        "var", ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"]
    )
    def test_standard_header_env_is_ignored_when_disabled(self, monkeypatch, var):
        """Positive control: the refusal is scoped to enabled export."""
        monkeypatch.setenv(var, f"authorization={SECRET}")

        assert OtlpExportConfig().enabled is False

    @pytest.mark.parametrize(
        "headers",
        [
            {"authorization": "Bearer token-123"},
            {"x-api-key": "abc123"},
            {"authorization": ""},
            {"x-custom_header.name": "v"},
        ],
    )
    def test_wellformed_headers_are_accepted(self, headers):
        """Positive control: the syntax check must not reject usable headers."""
        assert (
            OtlpExportConfig(
                enabled=True, endpoint="http://localhost:4318", headers=headers
            ).headers
            == headers
        )

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://{s}.collector.example/v1/traces",
            "http://collector.example/custom/{s}",
            "http://collector.example/custom/%73k-{s}",
        ],
    )
    def test_credential_shaped_endpoints_are_refused(self, endpoint):
        """The transport logs `scheme://host:port "POST path"` on every
        successful export, so host and path are both loggable."""
        planted = endpoint.format(s=SECRET)
        assert SECRET in planted

        with pytest.raises(ValueError) as excinfo:
            OtlpExportConfig(enabled=True, endpoint=planted)

        assert SECRET not in str(excinfo.value)

    def test_redirects_are_refused_so_headers_cannot_walk_hosts(self):
        """requests keeps non-Authorization headers across hosts.

        Verified against a local redirecting server: without this the second
        hop received `x-api-key` in full.
        """
        emitter = init_otlp(
            OtlpExportConfig(
                enabled=True,
                endpoint="http://localhost:4318",
                headers={"x-api-key": "token-123"},
            )
        )
        assert emitter is not None
        processor = emitter._provider._active_span_processor._span_processors[0]

        assert processor.span_exporter._delegate._session.max_redirects == 0

    @pytest.mark.parametrize(
        "endpoint",
        [
            "not a url",
            "ftp://collector:4318",
            "localhost:4318",
            "http://",
            "http://user:pass@collector:4318",
            "http://collector:4318?token=x",
            "http://collector:4318#frag",
        ],
    )
    def test_malformed_endpoints_fail_startup(self, endpoint):
        with pytest.raises(ValueError):
            OtlpExportConfig(enabled=True, endpoint=endpoint)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("timeout_seconds", 0),
            ("timeout_seconds", -1.0),
            ("timeout_seconds", float("inf")),
            ("timeout_seconds", float("nan")),
            ("flush_timeout_seconds", float("inf")),
            ("sampling_rate", 1.1),
            ("sampling_rate", -0.1),
            ("max_queue_size", 0),
            ("max_export_batch_size", 0),
        ],
    )
    def test_out_of_range_numbers_fail_startup(self, field, value):
        with pytest.raises(ValueError):
            OtlpExportConfig(enabled=True, endpoint="http://localhost:4318", **{field: value})

    def test_batch_size_must_fit_the_queue(self):
        with pytest.raises(ValueError, match="must not exceed max_queue_size"):
            OtlpExportConfig(
                enabled=True,
                endpoint="http://localhost:4318",
                max_queue_size=100,
                max_export_batch_size=101,
            )

    def test_sampling_rate_zero_is_legal(self):
        assert OtlpExportConfig(sampling_rate=0.0).sampling_rate == 0.0

    def test_env_configures_the_block(self, monkeypatch):
        monkeypatch.setenv("MEMTOMEM_STM_OTLP__ENDPOINT", "http://collector:4318")
        monkeypatch.setenv("MEMTOMEM_STM_OTLP__SAMPLING_RATE", "0.25")

        config = STMConfig()

        assert config.otlp.endpoint == "http://collector:4318"
        assert config.otlp.sampling_rate == 0.25

    def test_explicit_endpoint_beats_the_otel_env_var(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://env-collector:9999")

        config = OtlpExportConfig(enabled=True, endpoint="http://configured:4318")

        assert normalize_otlp_endpoint(config.endpoint) == "http://configured:4318/v1/traces"


# ── Shutdown ─────────────────────────────────────────────────────────────


class _HangingExporter:
    """An exporter whose flush never returns — the worst case for shutdown."""

    def __init__(self) -> None:
        self.released = threading.Event()

    def export(self, spans):
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        self.released.wait()
        return False

    def shutdown(self) -> None:
        self.released.wait()


class TestShutdown:
    def test_flush_delivers_queued_spans(self, emitter_and_exporter):
        emitter, exporter = emitter_and_exporter

        with traced("proxy_call", metadata={"server": "srv"}):
            pass
        shutdown_otlp(emitter, timeout_seconds=5.0)

        assert "proxy_call" in _by_name(exporter)
        assert emitter.snapshot()["shutdown_flush_timeout"] == 0

    def test_open_span_that_ends_in_time_is_drained_and_exported(self, emitter_and_exporter):
        emitter, exporter = emitter_and_exporter
        opened = threading.Event()

        def slow_call():
            with traced("proxy_call", metadata={"server": "srv"}):
                opened.set()
                time.sleep(0.1)

        worker = threading.Thread(target=slow_call)
        worker.start()
        opened.wait()
        shutdown_otlp(emitter, timeout_seconds=5.0)
        worker.join()

        assert "proxy_call" in _by_name(exporter)
        assert emitter.snapshot()["shutdown_flush_timeout"] == 0

    def test_hanging_flush_returns_within_the_budget(self):
        hanging = _HangingExporter()
        emitter = init_otlp(
            OtlpExportConfig(enabled=True, endpoint="http://localhost:4318"),
            span_processor=SimpleSpanProcessor(hanging),
        )
        assert emitter is not None

        started = time.monotonic()
        shutdown_otlp(emitter, timeout_seconds=0.5)
        elapsed = time.monotonic() - started
        hanging.released.set()

        assert 0.5 <= elapsed < 2.0
        assert emitter.snapshot()["shutdown_flush_timeout"] == 1

    def test_a_slow_span_and_a_hanging_flush_share_one_deadline(self):
        """The drain must not get a full budget and then the flush another."""
        hanging = _HangingExporter()
        emitter = init_otlp(
            OtlpExportConfig(enabled=True, endpoint="http://localhost:4318"),
            span_processor=SimpleSpanProcessor(hanging),
        )
        assert emitter is not None
        opened = threading.Event()

        def slow_call():
            with emitter.span("proxy_call", {"server": "srv"}):
                opened.set()
                time.sleep(5.0)

        worker = threading.Thread(target=slow_call, daemon=True)
        worker.start()
        opened.wait()

        started = time.monotonic()
        shutdown_otlp(emitter, timeout_seconds=0.5)
        elapsed = time.monotonic() - started
        hanging.released.set()

        # One 0.5s budget covers drain + flush; two would take ~1.0s.
        assert 0.5 <= elapsed < 0.9
        assert emitter.snapshot()["shutdown_flush_timeout"] == 1

    def test_admission_and_shutdown_cannot_interleave(self, emitter_and_exporter):
        """Force the exact interleaving a check-then-increment bug would lose.

        The opener stalls *inside* ``_admit``, at the point where a split
        implementation would already have observed "open". Shutdown then runs
        to the drain. If admission were not one critical section, shutdown
        would see zero active spans, flush, and the opener would afterwards
        start a span into an already-shut-down provider. The lock makes the
        two outcomes exhaustive: either the opener holds the lock and is
        admitted (shutdown then waits for it), or shutdown holds it first and
        the opener is refused.
        """
        emitter, exporter = emitter_and_exporter
        inside_admit = threading.Event()
        closer_at_the_lock = threading.Event()
        outcomes: list[bool] = []

        # Test-only seam: wrap the emitter's condition so the *first* acquirer
        # stalls while holding it. Done here rather than with a hook in the
        # emitter, so production carries no injectable callback on the path
        # that must never raise.
        class StallingOnce:
            def __init__(self, inner):
                self._inner = inner
                self._acquisitions = 0
                self._lock = threading.Lock()
                self.closer_arrived = False

            def __enter__(self):
                with self._lock:
                    self._acquisitions += 1
                    mine = self._acquisitions
                if mine == 2:
                    # The second acquirer is the closer, and it is about to
                    # block on the real lock the opener is holding. Signal
                    # from *here* — signalling from the closer thread before
                    # it calls shutdown would let the opener proceed while the
                    # closer had not yet reached the lock, which is the
                    # sequential run this test exists to rule out.
                    self.closer_arrived = True
                    closer_at_the_lock.set()
                result = self._inner.__enter__()
                if mine == 1:
                    inside_admit.set()
                    # Hold the critical section until the closer is provably
                    # blocked on it.
                    closer_at_the_lock.wait(timeout=5.0)
                return result

            def __exit__(self, *args):
                return self._inner.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        stalling = StallingOnce(emitter._state)
        emitter._state = stalling

        def opener():
            with emitter.span("proxy_call", {"server": "srv"}) as span:
                outcomes.append(span is not None)

        def closer():
            inside_admit.wait()
            shutdown_otlp(emitter, timeout_seconds=2.0)

        threads = [threading.Thread(target=opener), threading.Thread(target=closer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive(), "admission/shutdown deadlocked"

        assert stalling.closer_arrived, (
            "shutdown never reached the emitter while admission held the lock — "
            "the threads ran sequentially and the interleaving was not exercised"
        )
        admitted = outcomes[0]
        counters = emitter.snapshot()
        # The opener won the lock, so it must have been admitted — and the
        # span must have reached the exporter, i.e. shutdown waited for it.
        assert admitted
        assert counters["spans_started"] == 1
        assert counters["spans_started"] == counters["spans_ended"]
        assert "proxy_call" in _by_name(exporter)
        assert counters["shutdown_flush_timeout"] == 0

    def test_a_refused_span_is_not_counted(self, emitter_and_exporter):
        """The other side of the same contract: refusal leaves the books clean."""
        emitter, exporter = emitter_and_exporter
        shutdown_otlp(emitter, timeout_seconds=1.0)

        with emitter.span("proxy_call", {"server": "srv"}) as span:
            assert span is None

        counters = emitter.snapshot()
        assert counters["spans_started"] == 0
        assert counters["spans_ended"] == 0
        assert _by_name(exporter) == {}

    def test_an_unsuccessful_flush_counts_as_loss(self):
        """A processor reporting an incomplete flush is explicit loss.

        ``BatchSpanProcessor.force_flush()`` returns False when it hits its own
        timeout with spans still queued. Reading that result is the only way
        the loss is visible — so the stub is a *processor*, not an exporter:
        ``SimpleSpanProcessor.force_flush()`` is unconditionally True and would
        never surface it.
        """

        class RefusingFlushProcessor(SpanProcessor):
            def on_start(self, span, parent_context=None):
                pass

            def on_end(self, span):
                pass

            def force_flush(self, timeout_millis: int = 30_000) -> bool:
                return False

            def shutdown(self) -> None:
                pass

        emitter = init_otlp(
            OtlpExportConfig(enabled=True, endpoint="http://localhost:4318"),
            span_processor=RefusingFlushProcessor(),
        )
        assert emitter is not None

        shutdown_otlp(emitter, timeout_seconds=5.0)

        assert emitter.snapshot()["shutdown_flush_timeout"] == 1

    def test_shutdown_detaches_the_emitter_and_is_idempotent(self, emitter_and_exporter):
        emitter, exporter = emitter_and_exporter

        shutdown_otlp(emitter, timeout_seconds=1.0)

        assert get_otlp() is None
        assert type(traced("proxy_call")).__name__ == "nullcontext"
        shutdown_otlp(emitter, timeout_seconds=1.0)  # must not raise or re-count
        assert emitter.snapshot()["shutdown_flush_timeout"] == 0

    def test_process_exits_within_the_budget_despite_a_hanging_flush(self):
        """The flush thread is a daemon, so it can never hold the interpreter open."""
        script = """
import threading, time
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from memtomem_stm.config import OtlpExportConfig
from memtomem_stm.observability.otlp import init_otlp, shutdown_otlp

class Hanging:
    def export(self, spans): return SpanExportResult.SUCCESS
    def force_flush(self, timeout_millis=30000): time.sleep(600); return False
    def shutdown(self): time.sleep(600)

emitter = init_otlp(
    OtlpExportConfig(enabled=True, endpoint="http://localhost:4318"),
    span_processor=SimpleSpanProcessor(Hanging()),
)
with emitter.span("proxy_call", {"server": "srv"}):
    pass
shutdown_otlp(emitter, timeout_seconds=0.5)
print("exited-cleanly")
"""
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
        elapsed = time.monotonic() - started

        assert result.returncode == 0, result.stderr
        assert "exited-cleanly" in result.stdout
        assert elapsed < 20.0


# ── Health surface ───────────────────────────────────────────────────────


def _installed_screen():
    """The _LogScreen currently attached to the SDK trace-exporter logger."""
    from memtomem_stm.observability.otlp import _LogScreen

    return next(
        f
        for f in logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter").filters
        if isinstance(f, _LogScreen)
    )


def _log_with_stack_info(logger_obj, secret):
    """stack_info is its own formatter-visible channel."""
    record = logger_obj.makeRecord(
        logger_obj.name, logging.ERROR, "f", 1, "export failed", None, None
    )
    record.stack_info = f"Stack (most recent call last):\n  token {secret}"
    logger_obj.handle(record)


def _log_with_cached_exc_text(logger_obj, secret):
    """The formatter appends exc_text even when exc_info is gone."""
    record = logger_obj.makeRecord(
        logger_obj.name, logging.ERROR, "f", 1, "export failed", None, None
    )
    record.exc_text = f"RuntimeError: token {secret}"
    logger_obj.handle(record)


def _log_with_exc(logger_obj, secret):
    try:
        raise RuntimeError(f"token {secret}")
    except RuntimeError:
        logger_obj.exception("export failed")


class _CapturingHandler(logging.Handler):
    """Renders through a real Formatter, as a deployed handler would."""

    def __init__(self) -> None:
        super().__init__()
        self.rendered: list[str] = []
        self.setFormatter(logging.Formatter("%(message)s %(exc_text)s"))

    def emit(self, record):
        try:
            self.rendered.append(self.format(record) + str(vars(record)))
        except Exception:
            self.rendered.append("<unformattable>")


class TestSdkLogScreen:
    """The SDK logs before the exporter wrapper can sanitize what it says."""

    @pytest.mark.parametrize(
        "emit",
        [
            pytest.param(lambda lg, s: lg.error("reason: %s", s), id="message-args"),
            pytest.param(lambda lg, s: lg.error(f"reason: {s}"), id="message-literal"),
            pytest.param(lambda lg, s: _log_with_exc(lg, s), id="exc_info"),
            pytest.param(lambda lg, s: lg.error("x", extra={"tok": s}), id="extra"),
            pytest.param(lambda lg, s: _log_with_stack_info(lg, s), id="stack_info"),
            pytest.param(lambda lg, s: _log_with_cached_exc_text(lg, s), id="exc_text"),
        ],
    )
    def test_every_formatter_visible_channel_is_screened(self, emitter_and_exporter, emit):
        """A formatter can render more than the message.

        `getMessage()` alone leaves `exc_info`, `stack_info` and `extra=`
        fields untouched, and a custom formatter renders all of them.
        """
        emitter, _ = emitter_and_exporter
        sdk_logger = logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter")
        captured = _CapturingHandler()
        sdk_logger.addHandler(captured)
        try:
            emit(sdk_logger, SECRET)
        finally:
            sdk_logger.removeHandler(captured)

        assert all(SECRET not in text for text in captured.rendered)

    def test_an_unformattable_record_is_dropped(self, emitter_and_exporter):
        """Fail closed: a record we cannot read is one we cannot clear."""
        emitter, _ = emitter_and_exporter
        sdk_logger = logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter")
        captured = _CapturingHandler()
        sdk_logger.addHandler(captured)
        try:
            sdk_logger.error("needs two %s %s", "only-one")
        finally:
            sdk_logger.removeHandler(captured)

        assert captured.rendered == []
        assert emitter.snapshot()["logs_redacted"] == 1

    def test_shutdown_detaches_the_screen(self, emitter_and_exporter):
        """A filter left on a third-party logger would outlive the exporter."""
        emitter, _ = emitter_and_exporter
        from memtomem_stm.observability.otlp import _LogScreen

        shutdown_otlp(emitter, timeout_seconds=1.0)

        sdk_logger = logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter")
        assert not any(isinstance(f, _LogScreen) for f in sdk_logger.filters)

    @pytest.mark.parametrize("bad", [object(), 42, b"\xff"])
    def test_a_nonstring_channel_never_raises_out_of_the_filter(self, emitter_and_exporter, bad):
        """A filter that raises takes the whole log call down with it.

        The scan therefore runs inside the guard and every channel is coerced
        to text first. Whether logging's own formatter later chokes on a
        malformed record is its business, not the screen's — the contract here
        is that the screen itself is total.
        """
        emitter, _ = emitter_and_exporter
        screen = _installed_screen()
        record = logging.LogRecord("x", logging.ERROR, "f", 1, "msg", None, None)
        record.stack_info = bad

        assert screen.filter(record) in (True, False)

    def test_a_channel_that_cannot_be_read_is_dropped(self, emitter_and_exporter):
        """Fail closed: a record we cannot read is one we cannot clear."""
        emitter, _ = emitter_and_exporter
        screen = _installed_screen()

        class Explodes:
            def __str__(self):
                raise RuntimeError("cannot render")

        record = logging.LogRecord("x", logging.ERROR, "f", 1, "msg", None, None)
        record.stack_info = Explodes()

        assert screen.filter(record) is False
        assert emitter.snapshot()["logs_redacted"] == 1

    def test_shared_transport_loggers_are_left_alone(self):
        """urllib3/requests are shared by the whole process — not STM's to filter.

        The endpoint that would appear in their request-line logs is refused
        at config instead.
        """
        from memtomem_stm.observability.otlp import _SDK_LOGGERS

        assert not any(name.startswith(("urllib3", "requests")) for name in _SDK_LOGGERS)

    def test_a_reflected_secret_in_an_sdk_log_is_redacted(self, emitter_and_exporter, caplog):
        """A collector echoing the token into its reason phrase must not log it.

        Reproduced against the locked SDK before the screen existed: the whole
        token appeared in `Failed to export span batch code: 401, reason: …`.
        """
        emitter, _ = emitter_and_exporter
        sdk_logger = logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter")

        with caplog.at_level("DEBUG"):
            sdk_logger.error("Failed to export span batch code: 401, reason: token %s", SECRET)

        # Dropped, not rewritten: a partially-cleared record is a record we
        # are guessing about, and the counter is the operator's signal that
        # something was withheld.
        assert all(SECRET not in record.getMessage() for record in caplog.records)
        assert emitter.snapshot()["logs_redacted"] == 1

    def test_a_clean_sdk_log_is_left_alone(self, emitter_and_exporter, caplog):
        """Positive control: the retry detail an operator needs must survive."""
        emitter, _ = emitter_and_exporter
        sdk_logger = logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter")

        with caplog.at_level("DEBUG"):
            sdk_logger.error("Failed to export span batch code: 503, reason: Service Unavailable")

        assert any("Service Unavailable" in record.getMessage() for record in caplog.records)
        assert emitter.snapshot()["logs_redacted"] == 0

    def test_the_screen_is_installed_once(self, emitter_and_exporter):
        """Re-initialization must not stack filters on the SDK logger."""
        from memtomem_stm.observability.otlp import _LogScreen

        init_otlp(
            OtlpExportConfig(enabled=True, endpoint="http://localhost:4318"),
            span_processor=SimpleSpanProcessor(InMemorySpanExporter()),
        )

        sdk_logger = logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter")
        assert sum(isinstance(f, _LogScreen) for f in sdk_logger.filters) == 1


class TestHealthLines:
    def test_absent_when_export_is_off(self):
        from memtomem_stm.server import _otlp_health_lines

        assert _otlp_health_lines() == []

    def test_present_in_control_only_mode(self, emitter_and_exporter):
        """No upstreams configured is exactly when export must stay visible."""
        from types import SimpleNamespace

        from memtomem_stm.server import stm_proxy_health

        app = SimpleNamespace(
            proxy_manager=SimpleNamespace(get_upstream_health=lambda: {}),
            proxy_config_error=None,
            config=STMConfig(),
            surfacing_engine=None,
        )
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))

        rendered = asyncio.run(stm_proxy_health(ctx=ctx))

        assert "No upstream servers configured." in rendered
        assert "OTLP Span Export" in rendered

    def test_reports_the_counter_snapshot(self, emitter_and_exporter):
        from memtomem_stm.server import _otlp_health_lines

        with traced("proxy_call", metadata={"server": "srv"}):
            pass

        rendered = "\n".join(_otlp_health_lines())

        assert "OTLP Span Export" in rendered
        assert "1 started, 1 ended" in rendered
        assert "export failures: 0" in rendered
