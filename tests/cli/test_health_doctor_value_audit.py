"""Health and doctor never print credential-bearing configuration values (#1077).

``args``, ``env`` and ``headers`` values and URL userinfo are the parts of a
server entry that commonly carry credentials. Each case below plants a distinct
canary in every such leaf, runs the *real* probes (no fake ``_probe_servers``,
unlike ``test_runtime_health_doctor.py``), and scans the text output and every
decoded string of the ``--json`` document. A positive control per case proves
the path that could leak actually ran, so a silent pass means clean output,
not an unreached branch.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from click.testing import CliRunner

from helpers import set_home
from memtomem_stm.cli.proxy import cli

# Port 9 (discard) is closed on test hosts: connecting fails fast.
_DEAD_URL = "http://cnryUrlUser:cnryUrlPass@127.0.0.1:9/mcp?token=cnryQueryTok#cnryFrag"

_UPSTREAM_CANARIES = (
    "cnryArgStr",
    "cnryArgKey",
    "cnryArgVal",
    "cnryEnvVal",
    "cnryHdrVal",
    "cnryUrlUser",
    "cnryUrlPass",
    "cnryQueryTok",
    "cnryFrag",
)
_LTM_CANARIES = (
    "cnryLtmArg",
    "cnryLtmHdr",
    "cnryLtmUser",
    "cnryLtmPass",
    "cnryLtmQuery",
    "cnryLtmFrag",
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    set_home(monkeypatch, tmp_path / "home")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)


def _write_upstreams(path) -> None:
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "cache": {"tool_annotation_policy": "strict"},
                "upstream_servers": {
                    # A non-string ``args`` item: the SDK's parameter model
                    # rejects it before any process is spawned.
                    "badargs": {
                        "prefix": "badargs",
                        "command": sys.executable,
                        "args": ["cnryArgStr", {"cnryArgKey": "cnryArgVal"}],
                        "env": {"TOKEN": "cnryEnvVal"},
                    },
                    "remote": {
                        "prefix": "remote",
                        "transport": "streamable_http",
                        "url": _DEAD_URL,
                        "headers": {"Authorization": "Bearer cnryHdrVal"},
                    },
                },
            }
        )
    )


def _string_leaves(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _string_leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_leaves(item)


def _assert_clean(output: str, canaries: tuple[str, ...], *, as_json: bool) -> None:
    leaked = [c for c in canaries if c in output]
    assert leaked == [], f"canaries in output: {leaked}\n{output}"
    if as_json:
        decoded = [c for leaf in _string_leaves(json.loads(output)) for c in canaries if c in leaf]
        assert decoded == [], f"canaries in decoded JSON: {decoded}"


def _run(args: list[str]) -> str:
    result = CliRunner().invoke(cli, args)
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    return result.output


@pytest.mark.parametrize("command", ["health", "doctor"])
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_upstream_probe_output_has_no_credential_values(tmp_path, monkeypatch, command, as_json):
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "false")
    config = tmp_path / "stm_proxy.json"
    _write_upstreams(config)
    extra = ["--json"] if as_json else []
    output = _run([command, "--config", str(config), "--timeout", "3", *extra])

    if as_json:
        servers = json.loads(output)["servers"]
        # Positive controls: both probes ran and failed at connect time.
        bad = servers["badargs"]
        assert bad["stage"] == "configured"
        assert bad["error"] == "invalid server entry: args.1 (string_type)"
        assert servers["remote"]["error"]
    else:
        assert "badargs" in output and "remote" in output
    _assert_clean(output, _UPSTREAM_CANARIES, as_json=as_json)


def _ltm_stdio_env(monkeypatch) -> None:
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "true")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND", sys.executable)
    # The child exits at once, so the probe fails after spawning it.
    monkeypatch.setenv(
        "MEMTOMEM_STM_SURFACING__LTM_MCP_ARGS",
        json.dumps(["-c", "pass", "--token", "cnryLtmArg"]),
    )


def _ltm_network_env(monkeypatch) -> None:
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "true")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__LTM_MCP_TRANSPORT", "streamable_http")
    monkeypatch.setenv(
        "MEMTOMEM_STM_SURFACING__LTM_MCP_URL",
        "http://cnryLtmUser:cnryLtmPass@127.0.0.1:9/mcp?token=cnryLtmQuery#cnryLtmFrag",
    )
    monkeypatch.setenv(
        "MEMTOMEM_STM_SURFACING__LTM_MCP_HEADERS",
        json.dumps({"Authorization": "Bearer cnryLtmHdr"}),
    )
    # Present on the network route too: the status block reports it.
    monkeypatch.setenv(
        "MEMTOMEM_STM_SURFACING__LTM_MCP_ARGS", json.dumps(["--token", "cnryLtmArg"])
    )


def _ltm_block(output: str) -> dict[str, Any]:
    block = json.loads(output)["surfacing"]["ltm_server"]
    assert isinstance(block, dict)
    return block


@pytest.mark.parametrize("ltm_env", [_ltm_stdio_env, _ltm_network_env], ids=["stdio", "network"])
@pytest.mark.parametrize("command", ["health", "doctor"])
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_ltm_direct_route_has_no_credential_values(
    tmp_path, monkeypatch, ltm_env, command, as_json
):
    ltm_env(monkeypatch)
    # ``doctor`` otherwise follows the hook daemon (``hook.use_daemon``
    # defaults true); ``health`` takes the direct route by default.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "false")
    config = tmp_path / "stm_proxy.json"
    config.write_text(json.dumps({"upstream_servers": {}}))
    extra = ["--json"] if as_json else []
    output = _run([command, "--config", str(config), "--timeout", "3", *extra])

    if as_json:
        block = _ltm_block(output)
        # Positive control: the direct probe ran and failed.
        assert block["route"] == "direct"
        assert "skipped" not in block
        assert block["connected"] is False and block["error"]
    else:
        # Positive control: the failed direct probe's line names its target
        # (the hidden-args display for stdio, the redacted URL for network).
        marker = "[args hidden]" if ltm_env is _ltm_stdio_env else "***@127.0.0.1:9"
        assert marker in output
    _assert_clean(output, _LTM_CANARIES, as_json=as_json)


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_ltm_daemon_route_has_no_credential_values(tmp_path, monkeypatch, as_json):
    _ltm_stdio_env(monkeypatch)
    config = tmp_path / "stm_proxy.json"
    config.write_text(json.dumps({"upstream_servers": {}}))
    extra = ["--json"] if as_json else []
    output = _run(["doctor", "--config", str(config), "--timeout", "3", *extra])

    if as_json:
        # Positive control: doctor followed the hook daemon, which is not
        # running; the status block is filled from config before the ping.
        block = _ltm_block(output)
        assert block["route"] == "daemon"
        assert block["error"]
    else:
        assert "shared daemon is not reachable" in output
    _assert_clean(output, _LTM_CANARIES, as_json=as_json)


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_doctor_still_names_colliding_prefixes(tmp_path, monkeypatch, as_json):
    """Prefixes are shown deliberately: the operator has to edit them."""
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "false")
    config = tmp_path / "stm_proxy.json"
    config.write_text(
        json.dumps(
            {
                "upstream_servers": {
                    "one": {"prefix": "sharedpfx", "command": "echo"},
                    "two": {"prefix": "sharedpfx", "command": "echo"},
                }
            }
        )
    )
    extra = ["--json"] if as_json else []
    output = _run(["doctor", "--config", str(config), "--timeout", "3", *extra])
    assert "sharedpfx" in output


def _validation_error() -> Exception:
    from pydantic import TypeAdapter, ValidationError

    try:
        TypeAdapter(int).validate_python("cnryRejected")
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def test_probe_labels_a_response_validation_error_by_stage(monkeypatch):
    """A reply that fails validation after connect is a server fault, not a
    config one, and is rendered by location and type like the entry case."""
    import asyncio
    from contextlib import asynccontextmanager

    import mcp
    import mcp.client.stdio

    from memtomem_stm.cli.proxy import _probe_one

    @asynccontextmanager
    async def fake_stdio_client(params):
        yield (object(), object())

    class FakeSession:
        def __init__(self, *streams):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return None

        async def initialize(self):
            return None

        async def list_tools(self):
            raise _validation_error()

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp, "ClientSession", FakeSession)

    result = asyncio.run(_probe_one({"command": "echo"}, 3.0))
    assert result.stage.value == "mcp_initialized"
    assert result.error == "invalid server response: int_parsing"
    assert "cnryRejected" not in result.error


def test_probe_labels_an_entry_validation_error():
    from memtomem_stm.cli.proxy import _probe_failure_message
    from memtomem_stm.proxy.staged_status import ProbeStage

    message = _probe_failure_message(_validation_error(), ProbeStage.CONFIGURED)
    assert message == "invalid server entry: int_parsing"


def test_ltm_probe_error_that_echoes_an_arg_is_scrubbed(monkeypatch):
    """The status hides the args, so an exception quoting one must not
    bring it back through the error text."""
    from types import SimpleNamespace

    from memtomem_stm.cli import proxy as proxy_mod

    async def failing_probe(*args, **kwargs):
        raise RuntimeError("launch failed: --token cnryLtmArg rejected")

    monkeypatch.setattr(proxy_mod, "_probe_ltm_mcp_server", failing_probe)
    surfacing = SimpleNamespace(
        enabled=True,
        ltm_mcp_transport="stdio",
        ltm_mcp_command=sys.executable,
        ltm_mcp_args=["--token", "cnryLtmArg"],
        ltm_mcp_url="",
        ltm_mcp_headers=None,
    )
    status = proxy_mod._ltm_mcp_status(surfacing, 3.0)
    # Positive control: the failure reached the status, by type (#1079).
    assert status["error"].endswith(": RuntimeError")
    assert "cnryLtmArg" not in status["error"]


_ECHO_CANARIES = ("cnryEchoArg", "cnryEchoEnv", "cnryEchoHdr", "cnryEchoUser", "cnryEchoPass")


def _patch_echoing_transports(monkeypatch) -> None:
    """Each transport fails with a message quoting its own server's
    configured values, as an SDK or a server's error reply can."""
    import mcp.client.stdio

    import memtomem_stm.utils.mcp_transport as transport_mod

    def failing(echoed: str):
        def transport(*args, **kwargs):
            raise RuntimeError(f"boom: {echoed}")

        return transport

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", failing("cnryEchoArg cnryEchoEnv"))
    monkeypatch.setattr(
        transport_mod,
        "streamable_http_transport",
        failing("http://cnryEchoUser:cnryEchoPass@127.0.0.1:9/mcp cnryEchoHdr cnryEchoPass"),
    )


@pytest.mark.parametrize("command", ["health", "doctor"])
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_probe_error_that_echoes_configured_values_is_scrubbed(
    tmp_path, monkeypatch, command, as_json
):
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "false")
    _patch_echoing_transports(monkeypatch)
    config = tmp_path / "stm_proxy.json"
    config.write_text(
        json.dumps(
            {
                "upstream_servers": {
                    "local": {
                        "prefix": "local",
                        "command": "echo",
                        "args": ["--token", "cnryEchoArg"],
                        "env": {"TOKEN": "cnryEchoEnv"},
                    },
                    "remote": {
                        "prefix": "remote",
                        "transport": "streamable_http",
                        "url": "http://cnryEchoUser:cnryEchoPass@127.0.0.1:9/mcp",
                        "headers": {"Authorization": "cnryEchoHdr"},
                    },
                }
            }
        )
    )
    extra = ["--json"] if as_json else []
    output = _run([command, "--config", str(config), "--timeout", "3", *extra])
    # Positive control: the injected failure reached both rows (by type,
    # since #1079 renders no exception message).
    assert output.count("RuntimeError") >= 2
    _assert_clean(output, _ECHO_CANARIES, as_json=as_json)


def test_probe_unwraps_a_grouped_entry_validation_error(monkeypatch):
    """anyio task groups wrap transport failures; the label and the
    value-free rendering apply to the leaf."""
    import asyncio

    import mcp.client.stdio

    from memtomem_stm.cli.proxy import _probe_one

    def grouped_failure(*args, **kwargs):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [_validation_error()])

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", grouped_failure)
    result = asyncio.run(_probe_one({"command": "echo"}, 3.0))
    assert result.stage.value == "configured"
    assert result.error == "invalid server entry: int_parsing"


def test_last_resort_probe_guard_renders_validation_errors_value_free(monkeypatch):
    import asyncio

    from memtomem_stm.cli import proxy as proxy_mod

    async def escaping_probe(cfg, timeout):
        raise _validation_error()

    monkeypatch.setattr(proxy_mod, "_probe_one", escaping_probe)
    results = asyncio.run(proxy_mod._probe_servers({"s": {"command": "echo"}}, 3.0))
    assert results["s"].error == "invalid server entry: int_parsing"


def test_ltm_probe_error_that_echoes_url_credentials_is_scrubbed(monkeypatch):
    """A message quoting only the username or password, or the header value,
    is scrubbed like an upstream probe error."""
    from types import SimpleNamespace

    from memtomem_stm.cli import proxy as proxy_mod

    async def failing_probe(*args, **kwargs):
        raise RuntimeError("auth rejected for cnryLtmUser / cnryLtmPass with cnryLtmHdr")

    monkeypatch.setattr(proxy_mod, "_probe_ltm_mcp_server", failing_probe)
    surfacing = SimpleNamespace(
        enabled=True,
        ltm_mcp_transport="streamable_http",
        ltm_mcp_command="",
        ltm_mcp_args=[],
        ltm_mcp_url="http://cnryLtmUser:cnryLtmPass@127.0.0.1:9/mcp",
        ltm_mcp_headers={"Authorization": "cnryLtmHdr"},
    )
    status = proxy_mod._ltm_mcp_status(surfacing, 3.0)
    assert status["error"] == "http://***@127.0.0.1:9/mcp: RuntimeError"
    assert [c for c in _LTM_CANARIES if c in status["error"]] == []


# --- #1079: free-form exception text, URL query/fragment, malformed authority


_PARTIAL_CANARIES = ("cnryPartQuery", "cnryPartFrag", "cnryKvArg", "cnryBearerTok")
_PARTIAL_URL = "http://127.0.0.1:9/mcp?token=cnryPartQuery#cnryPartFrag"


def _patch_partial_echoes(monkeypatch) -> None:
    """Transports fail quoting only *parts* of their configured values — the
    shape a server error takes that whole-value scrubbing cannot catch."""
    import mcp.client.stdio

    import memtomem_stm.utils.mcp_transport as transport_mod

    def failing(echoed: str):
        def transport(*args, **kwargs):
            raise RuntimeError(f"boom: {echoed}")

        return transport

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", failing("invalid key cnryKvArg"))
    monkeypatch.setattr(
        transport_mod,
        "streamable_http_transport",
        failing(
            "Client error '401 Unauthorized' for url "
            "'http://127.0.0.1:9/mcp?token=cnryPartQuery#cnryPartFrag'; "
            "token cnryPartQuery / cnryPartFrag rejected; bearer cnryBearerTok"
        ),
    )


@pytest.mark.parametrize("command", ["health", "doctor"])
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_probe_error_is_rendered_without_its_message(tmp_path, monkeypatch, command, as_json):
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "false")
    _patch_partial_echoes(monkeypatch)
    config = tmp_path / "stm_proxy.json"
    config.write_text(
        json.dumps(
            {
                "upstream_servers": {
                    "local": {
                        "prefix": "local",
                        "command": "echo",
                        "args": ["--api-key=cnryKvArg"],
                    },
                    "remote": {
                        "prefix": "remote",
                        "transport": "streamable_http",
                        "url": _PARTIAL_URL,
                        "headers": {"Authorization": "Bearer cnryBearerTok"},
                    },
                }
            }
        )
    )
    extra = ["--json"] if as_json else []
    output = _run([command, "--config", str(config), "--timeout", "3", *extra])
    # Positive control: each probe's failure reached the report, by type name.
    if as_json:
        servers = json.loads(output)["servers"]
        assert servers["local"]["error"] == "RuntimeError"
        assert servers["remote"]["error"] == "RuntimeError"
    else:
        lines = output.splitlines()
        for name in ("local", "remote"):
            assert any(name in line and "RuntimeError" in line for line in lines), name
    assert "boom" not in output
    _assert_clean(output, _PARTIAL_CANARIES, as_json=as_json)


def _http_status_error(module_name: str, url: str) -> Exception:
    import importlib

    httpx_mod = importlib.import_module(module_name)
    request = httpx_mod.Request("POST", url)
    response = httpx_mod.Response(401, request=request, text="bad token cnryBody")
    return httpx_mod.HTTPStatusError(
        f"Client error '401 Unauthorized' for url '{url}'", request=request, response=response
    )


@pytest.mark.parametrize("module_name", ["httpx", "httpx2"])
def test_probe_renders_an_http_status_error_as_its_code(monkeypatch, module_name):
    import asyncio

    import memtomem_stm.utils.mcp_transport as transport_mod
    from memtomem_stm.cli.proxy import _probe_one

    def failing_transport(url, *args, **kwargs):
        raise _http_status_error(module_name, url)

    monkeypatch.setattr(transport_mod, "streamable_http_transport", failing_transport)
    result = asyncio.run(_probe_one({"transport": "streamable_http", "url": _PARTIAL_URL}, 3.0))
    assert result.error == "HTTP 401 (HTTPStatusError)"


def test_status_code_is_only_read_from_http_status_errors():
    """Any other exception with a ``response`` attribute is rendered by type:
    its attributes are not trusted to be an integer status code."""
    from types import SimpleNamespace

    from memtomem_stm.cli.proxy import _probe_failure_message
    from memtomem_stm.proxy.staged_status import ProbeStage

    class LooksLikeHttp(Exception):
        response = SimpleNamespace(status_code="cnryStatus")

    message = _probe_failure_message(LooksLikeHttp("cnryMessage"), ProbeStage.CONFIGURED)
    assert message == "LooksLikeHttp"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://127.0.0.1:9/mcp?token=cnryQ#cnryF", "http://127.0.0.1:9/mcp"),
        ("https://u:cnryPw@host.test/mcp?k=v", "https://***@host.test/mcp"),
        # An '@' outside the netloc cannot be told apart from a split
        # userinfo, so even a legitimate one fails closed.
        ("http://host.test/users/@me", "<unparseable url>"),
        ("http://host.test/mcp?email=a@b", "<unparseable url>"),
        ("http://alice/cnryPw@host.test/mcp", "<unparseable url>"),
        ("http://alice/cnryPw?x@host.test/mcp", "<unparseable url>"),
        # ``?``/``#`` split the userinfo: the parser reads ``alice:cnryPw`` as
        # the authority, so the only safe rendering is none at all.
        ("http://alice:cnryPw?x@host.test/mcp", "<unparseable url>"),
        ("http://alice:cnryPw#x@host.test/mcp", "<unparseable url>"),
        # Without a ``:`` the port reads fine, so only the ``@`` count
        # catches a token-only userinfo split by ``?``.
        ("http://cnryTok?x@host.test/mcp", "<unparseable url>"),
        # A '/' after the '?'/'#' changes nothing: the '@' is still outside.
        ("http://cnryTok?x/y@host.test/mcp", "<unparseable url>"),
        ("http://cnryTok#x/y@host.test/mcp", "<unparseable url>"),
        # Without a path, likewise.
        ("http://host.test?email=a@b", "<unparseable url>"),
        # An encoded ``@`` leaves a netloc whose port cannot be read ...
        ("http://alice:cnryPw%40host.test/mcp", "<unparseable url>"),
        # ... and without a port it still hides what precedes it.
        ("http://cnryPw%40host.test/mcp", "<unparseable url>"),
        ("alice:cnryPw@host.test/mcp", "<unparseable url>"),
        ("", ""),
    ],
)
def test_diagnostic_url_shape(url, expected):
    from memtomem_stm.cli.proxy import _diagnostic_url

    assert _diagnostic_url(url) == expected


def test_ltm_status_url_fields_drop_query_and_malformed_authority():
    from types import SimpleNamespace

    from memtomem_stm.cli import proxy as proxy_mod

    def status_for(url: str) -> dict[str, Any]:
        surfacing = SimpleNamespace(
            enabled=False,
            ltm_mcp_transport="streamable_http",
            ltm_mcp_command="",
            ltm_mcp_args=[],
            ltm_mcp_url=url,
            ltm_mcp_headers=None,
        )
        return proxy_mod._ltm_mcp_status(surfacing, 3.0)

    status = status_for("http://h.test/mcp?token=cnryQ#cnryF")
    assert status["url"] == status["display"] == "http://h.test/mcp"
    status = status_for("http://alice:cnryPw?x@h.test/mcp")
    assert status["url"] == status["display"] == "<unparseable url>"


def test_ltm_daemon_state_outside_the_known_set_is_not_echoed(monkeypatch):
    from memtomem_stm.cli import proxy as proxy_mod
    from memtomem_stm.config import STMConfig
    from memtomem_stm.daemon import client as daemon_client

    config = STMConfig()
    config.surfacing.enabled = True
    config.surfacing.ltm_mcp_url = "http://h.test/mcp?token=cnryDaemonQuery"

    def status_with(state: object) -> dict[str, Any]:
        async def fake_ping(*args, **kwargs):
            return {"ltm": state}

        monkeypatch.setattr(daemon_client, "ping", fake_ping)
        return proxy_mod._ltm_daemon_status(config, 3.0)

    for known in ("warming", "down", "cold"):
        status = status_with(known)
        assert status["ltm_state"] == known
        assert status["error"] == f"shared daemon is reachable but LTM is {known}"
    assert status_with("warm")["connected"] is True

    for invalid in ("", None, 0):
        assert status_with(invalid)["ltm_state"] == "unknown"

    status = status_with("cnryState")
    assert status["ltm_state"] == "unknown"
    assert status["error"] == "shared daemon is reachable but LTM is unknown"
    assert status["url"] == "http://h.test/mcp"
    assert "cnry" not in json.dumps(status)


def test_surfacing_bootstrap_error_names_only_the_type():
    from memtomem_stm.cli.proxy import _surfacing_bootstrap_error

    assert _surfacing_bootstrap_error(RuntimeError("cnryBootstrap")) == "RuntimeError"


def test_ollama_probe_failure_is_rendered_by_type(monkeypatch):
    import asyncio

    import httpx

    from memtomem_stm.cli import proxy as proxy_mod

    base_url = "http://alice:cnryOllamaPw@ollama.test:11434"
    dependency = proxy_mod._OllamaDependency(base_url, (("nomic-embed-text", ("scorer",)),))
    real_async_client = httpx.AsyncClient

    def handler(request):
        raise httpx.ConnectError(
            f"could not connect to {request.url} cnryOllamaPw", request=request
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_async_client(transport=transport, **kwargs)
    )
    result = asyncio.run(proxy_mod._probe_ollama_dependencies([dependency], 3))[0]
    assert result.error == "ConnectError"


@pytest.mark.parametrize(
    "first,second",
    [
        ("http://alice:cnryPw1?x@ollama.test:11434", "http://alice:cnryPw2?x@ollama.test:11434"),
        ("http://alice:cnryPw1%40ollama.test:11434", "http://alice:cnryPw2%40ollama.test:11434"),
        ("http://cnryPw1%40ollama.test", "http://cnryPw2%40ollama.test"),
        ("http://cnryPw1?x/y@ollama.test", "http://cnryPw2?x/y@ollama.test"),
        ("http://alice/cnryPw1@ollama.test", "http://alice/cnryPw2@ollama.test"),
        ("http://ollama.test:11434?token=cnryQ1", "http://ollama.test:11434?token=cnryQ2"),
    ],
)
def test_ollama_check_id_does_not_depend_on_credential_parts(first, second):
    from memtomem_stm.cli.proxy import _ollama_check_id

    assert _ollama_check_id(first) == _ollama_check_id(second)
    assert "cnry" not in _ollama_check_id(first)


def test_refreshed_daemon_state_that_is_invalid_is_unknown(monkeypatch):
    """An invalid refreshed value must not keep the earlier "warm" state."""
    from memtomem_stm.cli import proxy as proxy_mod
    from memtomem_stm.config import STMConfig
    from memtomem_stm.daemon import client as daemon_client

    config = STMConfig()
    config.surfacing.enabled = True

    async def fake_ping(*args, **kwargs):
        return {"ltm": "warm"}

    async def fake_measure(config, *, initial_state, timeout):
        return {}, {"ltm": ""}

    monkeypatch.setattr(daemon_client, "ping", fake_ping)
    monkeypatch.setattr(proxy_mod, "_measure_warm_daemon_ltm", fake_measure)
    status = proxy_mod._ltm_daemon_status(config, 3.0, measure_ltm=True)
    assert status["ltm_state"] == "unknown"
    assert status["connected"] is False


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_doctor_reports_an_unknown_daemon_state(tmp_path, monkeypatch, as_json):
    from memtomem_stm.daemon import client as daemon_client

    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "true")

    async def fake_ping(*args, **kwargs):
        return {"ltm": "cnryState"}

    monkeypatch.setattr(daemon_client, "ping", fake_ping)
    config = tmp_path / "stm_proxy.json"
    config.write_text(json.dumps({"upstream_servers": {}}))
    extra = ["--json"] if as_json else []
    output = _run(["doctor", "--config", str(config), "--timeout", "3", *extra])
    if as_json:
        block = _ltm_block(output)
        assert block["route"] == "daemon"
        assert block["ltm_state"] == "unknown"
    assert "shared daemon is reachable but LTM is unknown" in output
    _assert_clean(output, ("cnryState",), as_json=as_json)


def test_probe_failure_message_is_not_logged(caplog):
    """A dropped message must not reappear in a DEBUG log (the #1075 rule)."""
    import logging

    from memtomem_stm.cli.proxy import _probe_failure_message

    with caplog.at_level(logging.DEBUG):
        assert _probe_failure_message(RuntimeError("cnryLogged"), None) == "RuntimeError"
    assert "cnryLogged" not in caplog.text


def test_bootstrap_failure_log_carries_no_exception_text(monkeypatch, caplog):
    """An unexpected bootstrap failure logs its rendered type, not a
    traceback that would repeat the message."""
    import logging

    import memtomem_stm.config as config_mod
    from memtomem_stm.cli import proxy as proxy_mod

    def failing_config(*args, **kwargs):
        raise RuntimeError("cnryBootstrapLog")

    monkeypatch.setattr(config_mod, "stm_config_for_cli", failing_config)
    with caplog.at_level(logging.DEBUG):
        status = proxy_mod._surfacing_bootstrap_status(3.0)
    # Positive control: the failure path ran and was logged.
    assert status["error"] == "RuntimeError"
    assert "Surfacing bootstrap status inspection failed: RuntimeError" in caplog.text
    assert "cnryBootstrapLog" not in caplog.text


def test_ollama_local_hint_uses_the_diagnostic_url():
    """The local-Ollama hint is reached by a valid loopback URL, so an '@'
    in its path must fail closed here as it does in the check detail."""
    from memtomem_stm.cli.proxy import _ollama_next_action

    hint = _ollama_next_action("http://localhost/cnryPw@host.test/mcp", ["qwen3:4b"])
    # Positive control: the local branch that renders the URL ran.
    assert hint.startswith("verify the local Ollama at <unparseable url>")
    assert "cnryPw" not in hint


# ── Server- and DB-supplied text (#1082 part 3) ──────────────────────────
#
# The cases above plant canaries in configuration. These plant them in what a
# store or the LTM server hands back: a SQLite diagnostic that quotes a table
# name out of the schema, and Core's ``version`` / ``runtime_profile``. What
# is left is a type, a result-code name, a version number, or a value from the
# closed sets STM reads.

_DB_CANARIES = ("cnryDbTable",)


def _malformed_db(path) -> None:
    """A real SQLite file whose schema no longer parses.

    Reading it raises ``DatabaseError: malformed database schema
    (cnryDbTable) - ...``: SQLite quotes the stored table name back, so the
    canary reaches the message the way any schema text would.
    """
    import sqlite3

    db = sqlite3.connect(path)
    db.execute("CREATE TABLE cnryDbTable (x)")
    db.commit()
    db.execute("PRAGMA writable_schema=ON")
    db.execute("UPDATE sqlite_master SET sql = 'CREATE TABLE cnryDbTable (x' WHERE type='table'")
    db.commit()
    db.close()


@pytest.mark.parametrize(
    ("command", "as_json"),
    [("health", False), ("health", True), ("doctor", True)],
    ids=["health-text", "health-json", "doctor-json"],
)
def test_feedback_db_error_names_only_type_and_code(tmp_path, monkeypatch, command, as_json):
    # Doctor text renders no feedback-DB error, so it has no arm here.
    db = tmp_path / "feedback.db"
    _malformed_db(db)
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "false")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH", str(db))
    config = tmp_path / "stm_proxy.json"
    config.write_text(json.dumps({"upstream_servers": {}}))
    extra = ["--json"] if as_json else []
    output = _run([command, "--config", str(config), "--timeout", "3", *extra])

    # Positive control: both readers hit the malformed schema.
    expected = "DatabaseError (SQLITE_CORRUPT)"
    if as_json:
        surfacing = json.loads(output)["surfacing"]
        assert surfacing["feedback_db"]["error"] == expected
        assert surfacing["feedback_summary"]["error"] == expected
    else:
        assert f"feedback tables: error — {expected}" in output
    _assert_clean(output, _DB_CANARIES, as_json=as_json)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory permissions")
@pytest.mark.parametrize(
    ("command", "as_json"),
    [("health", False), ("health", True), ("doctor", True)],
    ids=["health-text", "health-json", "doctor-json"],
)
def test_feedback_db_under_unreadable_directory(tmp_path, monkeypatch, command, as_json):
    """A DB under a directory the process cannot search. Both readers report
    ``PermissionError`` by type, so the surfacing block keeps its other fields
    instead of collapsing into one bootstrap error (#1092) — and it is not
    reported as a missing DB, which ``Path.exists()`` does on Python 3.14."""
    import os

    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    locked = tmp_path / "cnryLockedDir"
    locked.mkdir()
    db = locked / "sub" / "feedback.db"
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "false")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH", str(db))
    config = tmp_path / "stm_proxy.json"
    config.write_text(json.dumps({"upstream_servers": {}}))
    extra = ["--json"] if as_json else []
    locked.chmod(0)
    try:
        output = _run([command, "--config", str(config), "--timeout", "3", *extra])
    finally:
        locked.chmod(0o755)

    if as_json:
        surfacing = json.loads(output)["surfacing"]
        assert "error" not in surfacing
        assert surfacing["feedback_db"]["error"] == "PermissionError"
        assert surfacing["feedback_summary"]["error"] == "PermissionError"
    else:
        assert "feedback tables: error — PermissionError" in output


def test_stats_db_errors_name_only_type_and_code(tmp_path, monkeypatch):
    metrics = tmp_path / "metrics.db"
    feedback = tmp_path / "feedback.db"
    _malformed_db(metrics)
    _malformed_db(feedback)
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH", str(feedback))
    config = tmp_path / "stm_proxy.json"
    config.write_text(json.dumps({"upstream_servers": {}, "metrics": {"db_path": str(metrics)}}))
    output = _run(["stats", "--config", str(config), "--json"])

    data = json.loads(output)
    assert data["compression"]["error"] == "DatabaseError (SQLITE_CORRUPT)"
    assert data["surfacing"]["error"] == "DatabaseError (SQLITE_CORRUPT)"
    _assert_clean(output, _DB_CANARIES, as_json=True)


@pytest.mark.parametrize(
    ("module", "reader"),
    [
        ("memtomem_stm.surfacing.feedback_store", "inspect_feedback_db"),
        ("memtomem_stm.surfacing.feedback_store", "read_surfacing_summary"),
        ("memtomem_stm.proxy.metrics_store", "read_compression_summary"),
    ],
)
def test_db_reader_open_failure_names_only_type_and_code(tmp_path, monkeypatch, module, reader):
    """The open itself failing: a malformed file only fails at the first query,
    so the ``connect`` branch needs its own error. Its message carries the
    canary the way a path or VFS diagnostic would."""
    import importlib
    import sqlite3

    mod = importlib.import_module(module)

    def _connect(*_a: Any, **_kw: Any) -> Any:
        exc = sqlite3.OperationalError("unable to open cnryDbTable")
        exc.sqlite_errorcode = 14
        raise exc

    monkeypatch.setattr(mod.sqlite3, "connect", _connect)
    db = tmp_path / "store.db"
    db.write_bytes(b"")
    result = getattr(mod, reader)(db)
    assert result["error"] == "OperationalError (SQLITE_CANTOPEN)"


_CORE_CANARIES = (
    "cnryVersionTok",
    "cnryLocal",
    "cnryAnsi",
    "cnryModel",
    "cnryTokenizer",
    "cnryProvider",
    "cnryMode",
    "cnryExtra",
    "cnryRequiredFor",
    "cnryDepVersion",
    "cnryKey",
    "cnryVal",
    "cnryFormat",
)


def _hostile_profile() -> dict[str, Any]:
    """Core's schema-1 shape with a canary in every free-text slot.

    ``configured_mode`` is a canary while ``effective_mode`` is ``bm25_only``:
    that is the combination doctor words as "configured mode X degraded".
    """
    return {
        "schema_version": 1,
        "config_state": "ok",
        "embedding": {"provider": "cnryProvider", "model": "cnryModel", "dimension": 384},
        "search": {
            "rrf_k": 10**12,
            "rrf_weights": [1.0, 1.0],
            "bm25_candidates": 50,
            "dense_candidates": 50,
            "enable_bm25": True,
            "enable_dense": True,
            "tokenizer": "cnryTokenizer",
            "configured_mode": "cnryMode",
            "effective_mode": "bm25_only",
        },
        "rerank": {"enabled": False, "provider": "cnryProvider"},
        "dependencies": {
            "fastembed": {
                "available": True,
                "version": "cnryDepVersion",
                "required_for": ["embedding", "cnryRequiredFor"],
            },
            "kiwipiepy": {"available": False, "version": None, "required_for": []},
        },
        "missing_extras": ["cnryExtra"],
        "cnryKey": "cnryVal",
    }


@pytest.mark.parametrize(
    ("version", "shown"),
    [
        ("0.3.0+cnryLocal", "0.3.0"),
        ("0.1.0.post1", "0.1.0.post1"),
        ("1.2.0rc1.dev3", "1.2.0rc1.dev3"),
        ("cnryVersionTok", None),
        ("\u0661.\u0662.\u0663", None),
        ("0.3.0\x1b[31mcnryAnsi\n", None),
    ],
    ids=["local-label", "post", "pre-dev", "bare-token", "unicode-digits", "control-chars"],
)
@pytest.mark.parametrize("command", ["health", "doctor"])
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_ltm_core_metadata_is_rendered_from_known_values(
    tmp_path, monkeypatch, version, shown, command, as_json
):
    """Core's ``mem_do(version)`` reply crosses the real parser; only the MCP
    session is replaced (``CliRunner`` has no stderr fd for a stdio child —
    ``test_proxy_cli.py`` covers the real subprocess)."""
    from mcp.types import CallToolResult, TextContent

    from memtomem_stm.cli import proxy

    payload = {"version": version, "runtime_profile": _hostile_profile()}

    async def _probe(*_a: Any, **_kw: Any) -> dict[str, Any]:
        reply = CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))])
        return {"connected": True, "error": None, **proxy._ltm_metadata_from_tool_result(reply)}

    monkeypatch.setattr(proxy, "_probe_ltm_mcp_server", _probe)
    _ltm_stdio_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "false")
    config = tmp_path / "stm_proxy.json"
    config.write_text(json.dumps({"upstream_servers": {}}))
    extra = ["--json"] if as_json else []
    output = _run([command, "--config", str(config), "--timeout", "3", *extra])

    # Positive controls, one per renderer that could echo the reply.
    if as_json:
        block = _ltm_block(output)
        assert block["connected"] is True
        assert block["version"] == shown
        assert block["runtime_profile"]["search"]["effective_mode"] == "bm25_only"
    elif command == "health":
        line = next(ln for ln in output.splitlines() if "ltm server:" in ln)
        assert "connectable" in line
        assert line.endswith(f", version {shown})") if shown else "version" not in line
    else:
        assert "degraded to effective mode bm25_only" in output
    _assert_clean(output, _CORE_CANARIES, as_json=as_json)


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_ltm_daemon_core_metadata_is_rendered_from_known_values(tmp_path, monkeypatch, as_json):
    from memtomem_stm.daemon import client

    async def _ping(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return {
            "ltm": "warm",
            "core": {
                "runtime_profile": _hostile_profile(),
                "effective_result_format": "cnryFormat",
            },
        }

    monkeypatch.setattr(client, "ping", _ping)
    _ltm_stdio_env(monkeypatch)
    config = tmp_path / "stm_proxy.json"
    config.write_text(json.dumps({"upstream_servers": {}}))
    extra = ["--json"] if as_json else []
    output = _run(["doctor", "--config", str(config), "--timeout", "3", *extra])

    # Positive control: doctor followed the daemon and read its profile.
    if as_json:
        block = _ltm_block(output)
        assert block["route"] == "daemon"
        assert block["runtime_profile"]["search"]["effective_mode"] == "bm25_only"
        assert block["effective_result_format"] is None
    else:
        assert "degraded to effective mode bm25_only" in output
    _assert_clean(output, _CORE_CANARIES, as_json=as_json)


@pytest.mark.parametrize(
    ("fastembed", "status"),
    [
        # A use Core may add later: still "required", so still FAIL.
        ({"available": False, "required_for": ["cnryNewUse"]}, "FAIL"),
        # Truthy but not a bool: judged as sent, not as the projection's null.
        ({"available": "yes", "required_for": ["embedding"]}, "PASS"),
    ],
    ids=["unknown-required-for", "non-bool-available"],
)
def test_doctor_judges_the_raw_profile_and_renders_the_projection(
    tmp_path, monkeypatch, fastembed, status
):
    """The projection is for display: a value it cannot represent must not
    change a verdict (#1082 review)."""
    from memtomem_stm.daemon import client

    profile = _hostile_profile()
    profile["missing_extras"] = []
    profile["dependencies"]["fastembed"] = fastembed

    async def _ping(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return {"ltm": "warm", "core": {"runtime_profile": profile}}

    monkeypatch.setattr(client, "ping", _ping)
    _ltm_stdio_env(monkeypatch)
    config = tmp_path / "stm_proxy.json"
    config.write_text(json.dumps({"upstream_servers": {}}))
    output = _run(["doctor", "--config", str(config), "--timeout", "3", "--json"])

    data = json.loads(output)
    checks = {c["id"]: c for c in data["checks"]}
    assert checks["ltm_dependencies"]["status"] == status
    shown = data["surfacing"]["ltm_server"]["runtime_profile"]["dependencies"]["fastembed"]
    assert shown["required_for"] == [r for r in fastembed["required_for"] if r == "embedding"]
    _assert_clean(output, _CORE_CANARIES + ("cnryNewUse",), as_json=True)


@pytest.mark.parametrize("command", ["health", "doctor"])
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_unhashable_mode_does_not_break_the_report(tmp_path, monkeypatch, command, as_json):
    """The raw profile is judged in the bootstrap status that ``health`` shares:
    a JSON list where a mode string belongs must not collapse it (#1082 review)."""
    from memtomem_stm.daemon import client

    profile = _hostile_profile()
    profile["search"]["effective_mode"] = ["cnryMode"]

    async def _ping(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return {"ltm": "warm", "core": {"runtime_profile": profile}}

    monkeypatch.setattr(client, "ping", _ping)
    _ltm_stdio_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__USE_DAEMON", "true")
    config = tmp_path / "stm_proxy.json"
    config.write_text(json.dumps({"upstream_servers": {}}))
    extra = ["--json"] if as_json else []
    output = _run([command, "--config", str(config), "--timeout", "3", *extra])

    if as_json:
        surfacing = json.loads(output)["surfacing"]
        assert "error" not in surfacing
        assert surfacing["ltm_server"]["runtime_profile"]["search"]["effective_mode"] == (
            "unrecognized"
        )
    else:
        assert "TypeError" not in output
    if command == "doctor" and not as_json:
        assert "did not report a recognized retrieval mode" in output
    _assert_clean(output, _CORE_CANARIES, as_json=as_json)


@pytest.mark.parametrize("schema_version", [True, 1.0], ids=["bool", "float"])
@pytest.mark.parametrize("route", ["direct", "daemon"])
def test_non_integer_schema_version_is_not_schema_one(tmp_path, monkeypatch, schema_version, route):
    """JSON ``true`` and ``1.0`` equal ``1`` in Python. The ingest check and the
    projection must agree on what schema 1 is, or doctor judges a profile the
    report then shows as ``null`` (PR #1094 review)."""
    from mcp.types import CallToolResult, TextContent

    from memtomem_stm.cli import proxy
    from memtomem_stm.daemon import client

    profile = _hostile_profile()
    profile["schema_version"] = schema_version
    profile["search"]["configured_mode"] = "hybrid"
    profile["search"]["effective_mode"] = "hybrid"
    _ltm_stdio_env(monkeypatch)
    if route == "direct":
        payload = {"version": "0.3.0", "runtime_profile": profile}

        async def _probe(*_a: Any, **_kw: Any) -> dict[str, Any]:
            reply = CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))])
            return {"connected": True, "error": None, **proxy._ltm_metadata_from_tool_result(reply)}

        monkeypatch.setattr(proxy, "_probe_ltm_mcp_server", _probe)
        monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "false")
    else:

        async def _ping(*_a: Any, **_kw: Any) -> dict[str, Any]:
            return {"ltm": "warm", "core": {"runtime_profile": profile}}

        monkeypatch.setattr(client, "ping", _ping)
    config = tmp_path / "stm_proxy.json"
    config.write_text(json.dumps({"upstream_servers": {}}))
    output = _run(["doctor", "--config", str(config), "--timeout", "3", "--json"])

    data = json.loads(output)
    assert data["surfacing"]["ltm_server"]["route"] == route
    assert data["surfacing"]["ltm_server"]["runtime_profile"] is None
    checks = {c["id"]: c for c in data["checks"]}
    assert checks["ltm_runtime_profile"]["status"] == "WARN"
    assert "ltm_retrieval_mode" not in checks
