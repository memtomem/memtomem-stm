"""Below WARNING, STM's handlers pass only STM's own records (#1086).

The MCP SDK and the HTTP clients under it log at INFO and DEBUG what no STM call
site rendered — measured on mcp 2.2.0: ``httpx2`` logs every upstream request
with its full URL (a ``?token=`` included), the SDK logs the MCP session id,
and at DEBUG ``httpcore2`` adds response headers. ``DependencyRecordFilter``
keeps those records off every handler STM installs unless
``STMConfig.log_dependencies`` is set.

The contract is per handler, not per logger: dependency logger levels are left
alone and another handler on the root logger still receives the records, which
is what a ``logger.setLevel`` implementation would break.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from memtomem_stm.config import STMConfig
from memtomem_stm.logging_setup import (
    DependencyRecordFilter,
    configure_hook_logging,
    configure_server_logging,
    configure_stderr_logging,
)

CANARY = "DEPLOGCANARY4c1e"

# Each a logger STM's runtime reaches: the SDK's module loggers, the top-level
# "client" logger its session module uses, the HTTP clients, and urllib3 under
# the OTLP exporter.
DEPENDENCY_LOGGERS = (
    "mcp.client.streamable_http",
    "client",
    "httpx2",
    "httpx",
    "httpcore2.http11",
    "urllib3.connectionpool",
)


@pytest.fixture
def _restore_logging():
    """Snapshot root handlers/level and the dependency loggers' levels."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    names = (*DEPENDENCY_LOGGERS, "memtomem_stm.test_dep_filter")
    saved_levels = {name: logging.getLogger(name).level for name in names}
    yield
    for h in root.handlers[:]:
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


class _Observer(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.NOTSET)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _emit_all(levels: tuple[int, ...] = (logging.DEBUG, logging.INFO)) -> None:
    for name in DEPENDENCY_LOGGERS:
        for level in levels:
            logging.getLogger(name).log(level, "%s %s %s", CANARY, name, level)


def _read(path) -> str:
    for h in logging.getLogger().handlers:
        h.flush()
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ── the rule ──────────────────────────────────────────────────────────────


def _record(name: str, level: int) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 0, "m", None, None)


@pytest.mark.parametrize(
    ("name", "level", "passes"),
    [
        ("memtomem_stm", logging.DEBUG, True),
        ("memtomem_stm.proxy.manager", logging.DEBUG, True),
        ("memtomem_stmx", logging.INFO, False),
        ("memtomem", logging.INFO, False),
        ("__main__", logging.INFO, False),
        ("root", logging.INFO, False),
        ("httpx2", logging.INFO, False),
        ("httpx2", logging.WARNING, True),
        ("mcp.client.sse", logging.ERROR, True),
        ("client", logging.DEBUG, False),
    ],
)
def test_filter_rule(name, level, passes):
    assert DependencyRecordFilter().filter(_record(name, level)) is passes


# ── server ────────────────────────────────────────────────────────────────


class TestServerHandlers:
    def test_dependency_records_reach_neither_stderr_nor_file(
        self, tmp_path, capsys, _restore_logging
    ):
        path = tmp_path / "stm.log"
        configure_server_logging(STMConfig(log_level="DEBUG", log_file=path))
        _emit_all()
        logging.getLogger("memtomem_stm.test_dep_filter").debug("stm debug line")

        err = capsys.readouterr().err
        text = _read(path)
        assert CANARY not in err
        assert CANARY not in text
        # Positive control: STM's own DEBUG records still pass both handlers.
        assert "stm debug line" in err
        assert "stm debug line" in text

    def test_dependency_warnings_pass(self, tmp_path, capsys, _restore_logging):
        path = tmp_path / "stm.log"
        configure_server_logging(STMConfig(log_level="DEBUG", log_file=path))
        _emit_all(levels=(logging.WARNING,))

        err = capsys.readouterr().err
        text = _read(path)
        for name in DEPENDENCY_LOGGERS:
            assert f"{CANARY} {name} {logging.WARNING}" in err
            assert f"{CANARY} {name} {logging.WARNING}" in text

    def test_handler_filter_leaves_loggers_and_other_handlers_alone(
        self, tmp_path, capsys, _restore_logging
    ):
        """An explicitly DEBUG-enabled dependency child is still kept off STM's
        handlers, while a handler installed after configuration receives it and
        the logger's own level is unchanged — not a ``setLevel`` clamp."""
        for name in DEPENDENCY_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
        path = tmp_path / "stm.log"
        configure_server_logging(STMConfig(log_level="WARNING", log_file=path))
        observer = _Observer()
        logging.getLogger().addHandler(observer)

        _emit_all()

        assert CANARY not in capsys.readouterr().err
        assert CANARY not in _read(path)
        assert len(observer.messages) == 2 * len(DEPENDENCY_LOGGERS)
        for name in DEPENDENCY_LOGGERS:
            assert logging.getLogger(name).level == logging.DEBUG

    def test_opt_in_lets_dependency_records_through(self, tmp_path, capsys, _restore_logging):
        path = tmp_path / "stm.log"
        configure_server_logging(STMConfig(log_level="DEBUG", log_file=path, log_dependencies=True))
        _emit_all()

        err = capsys.readouterr().err
        text = _read(path)
        for name in DEPENDENCY_LOGGERS:
            for level in (logging.DEBUG, logging.INFO):
                assert f"{CANARY} {name} {level}" in err
                assert f"{CANARY} {name} {level}" in text

    def test_opt_in_from_env(self, monkeypatch):
        monkeypatch.setenv("MEMTOMEM_STM_LOG_DEPENDENCIES", "true")
        assert STMConfig().log_dependencies is True
        monkeypatch.delenv("MEMTOMEM_STM_LOG_DEPENDENCIES")
        assert STMConfig().log_dependencies is False


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def http_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/mcp?token={CANARY}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("opt_in", [False, True])
def test_real_httpx2_request_line(tmp_path, http_url, opt_in, _restore_logging):
    """The measured leak itself: httpx2's INFO request line carries the URL
    with its query string. Kept out by default, present when opted in."""
    import httpx2

    path = tmp_path / "stm.log"
    configure_server_logging(STMConfig(log_level="INFO", log_file=path, log_dependencies=opt_in))
    with httpx2.Client(trust_env=False, timeout=10.0) as client:
        assert client.get(http_url).status_code == 200

    assert (f"token={CANARY}" in _read(path)) is opt_in


# ── daemon ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("detached", [True, False])
@pytest.mark.parametrize("opt_in", [False, True])
def test_daemon_handlers(tmp_path, capsys, detached, opt_in, _restore_logging):
    from memtomem_stm.cli.daemon_cmd import _configure_logging

    for name in DEPENDENCY_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    _configure_logging(
        STMConfig(data_dir=tmp_path, log_level="DEBUG", log_dependencies=opt_in),
        detached=detached,
    )
    observer = _Observer()
    logging.getLogger().addHandler(observer)
    _emit_all()
    logging.getLogger("memtomem_stm.test_dep_filter").debug("stm debug line")

    out = _read(tmp_path / "stm-daemon.log") if detached else capsys.readouterr().err
    assert "stm debug line" in out
    assert (CANARY in out) is opt_in
    # Handler-only either way: another handler still gets every record and the
    # dependency loggers keep their levels.
    assert sum(CANARY in m for m in observer.messages) == 2 * len(DEPENDENCY_LOGGERS)
    for name in DEPENDENCY_LOGGERS:
        assert logging.getLogger(name).level == logging.DEBUG


# ── early failure and hook ────────────────────────────────────────────────


def test_early_failure_stderr_drops_debug_enabled_dependency(capsys, _restore_logging):
    """The early-failure handler's own level is NOTSET, so before #1086 a
    dependency logger set to DEBUG reached it despite the WARNING root."""
    for name in DEPENDENCY_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    configure_stderr_logging(level=logging.WARNING, force=True)
    _emit_all()
    logging.getLogger("httpx2").warning("dependency warning")

    err = capsys.readouterr().err
    assert CANARY not in err
    assert "dependency warning" in err


def test_hook_handler_carries_the_filter(_restore_logging):
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
    configure_hook_logging()
    [handler] = root.handlers
    assert any(isinstance(f, DependencyRecordFilter) for f in handler.filters)


def test_server_module_logger_is_in_the_stm_tree():
    """``python -m memtomem_stm.server`` runs the module as ``__main__``; its
    logger must still be named inside the tree the filter passes. Run under a
    foreign module name in a subprocess: importing the server constructs the
    MCP server, which installs a handler on this process's root logger."""
    import subprocess
    import sys

    code = (
        "import importlib.util, sys; "
        "spec = importlib.util.find_spec('memtomem_stm.server'); "
        "spec = importlib.util.spec_from_file_location('__not_main__', spec.origin); "
        "mod = importlib.util.module_from_spec(spec); "
        "sys.modules['__not_main__'] = mod; "
        "spec.loader.exec_module(mod); "
        "print(mod.logger.name)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "memtomem_stm.server"
