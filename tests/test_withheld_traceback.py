"""Logged tracebacks keep their frames and withhold every message (#1086).

The last line of each exception in a traceback repeats the exception's text,
and an upstream, the LTM server or an LLM provider writes that text: it can
quote a URL query, a tool argument or part of a header. STM's handlers render
tracebacks from an allowlist instead — class names, a fixed-table code, frames,
the chain and group structure — and ``STMConfig.log_exception_text`` restores
the stdlib rendering.

The canary is assembled at runtime so no source line a traceback shows can
contain it; a hit can only come from an exception's message, args or notes.
"""

from __future__ import annotations

import io
import logging
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from memtomem_stm.config import STMConfig
from memtomem_stm.logging_setup import (
    WITHHELD_CACHED_TRACEBACK,
    WITHHELD_RENDER_FAILED,
    DisplayEscapingFormatter,
    configure_hook_logging,
    configure_server_logging,
    configure_stderr_logging,
    render_withheld_traceback,
)

CANARY = "TB" + "CANARY" + "5e0d"


@pytest.fixture
def _restore_logging():
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
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


def _caught(fn) -> BaseException:
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001 - the test wants whatever was raised
        return exc
    raise AssertionError("expected a raise")


# ── distinct frames per branch, so each branch's presence is observable ──


def raise_cause_marker():
    raise KeyError(CANARY)


def raise_context_marker():
    raise OSError(2, CANARY, f"/tmp/{CANARY}")


def raise_member_one_marker():
    raise ValueError(CANARY)


def raise_member_two_marker():
    raise RuntimeError(f"multi\nline {CANARY}")


def raise_top_marker():
    try:
        raise_context_marker()
    except OSError:
        try:
            raise_cause_marker()
        except KeyError as cause:
            members = [_caught(raise_member_one_marker), _caught(raise_member_two_marker)]
            group = ExceptionGroup(CANARY, members)
            group.add_note(CANARY)
            raise group from cause


def test_chain_and_group_render_every_branch_without_any_message():
    out = render_withheld_traceback(_caught(raise_top_marker))

    assert CANARY not in out
    # Every branch is rendered: each frame's function name appears.
    for marker in (
        "raise_context_marker",
        "raise_cause_marker",
        "raise_member_one_marker",
        "raise_member_two_marker",
        "raise_top_marker",
    ):
        assert f"in {marker}" in out, marker
    # Print order, oldest first: the cause's own context, the cause, the group.
    context_at = out.index("FileNotFoundError: [message withheld]")
    during_at = out.index("During handling of the above exception, another exception occurred:")
    cause_at = out.index("KeyError: [message withheld]")
    direct_at = out.index("The above exception was the direct cause of the following exception:")
    group_at = out.index("ExceptionGroup: [message withheld]")
    assert context_at < during_at < cause_at < direct_at < group_at
    assert "+-- sub-exception 1 of 2 --" in out and "+-- sub-exception 2 of 2 --" in out
    assert "| ValueError: [message withheld]" in out
    assert "| RuntimeError: [message withheld]" in out
    assert "[1 note(s) withheld]" in out


def test_context_is_shown_when_there_is_no_cause_and_hidden_when_suppressed():
    def with_context():
        try:
            raise_context_marker()
        except OSError:
            raise_cause_marker()

    def suppressed():
        try:
            raise_context_marker()
        except OSError:
            raise ValueError(CANARY) from None

    shown = render_withheld_traceback(_caught(with_context))
    hidden = render_withheld_traceback(_caught(suppressed))

    assert "in raise_context_marker" in shown
    assert "During handling of the above exception, another exception occurred:" in shown
    assert "FileNotFoundError: [message withheld]" in shown
    assert "raise_context_marker" not in hidden
    assert CANARY not in shown + hidden


class _SilentSyntaxError(SyntaxError):
    def __str__(self) -> str:
        return ""


class _BrokenStr(Exception):
    def __str__(self) -> str:
        raise RuntimeError(CANARY)


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(SyntaxError(CANARY, ("f.py", 1, 2, CANARY, 1, 3)), id="syntax-error"),
        pytest.param(_SilentSyntaxError(CANARY, ("f.py", 1, 2, CANARY, 1, 3)), id="empty-str"),
        pytest.param(KeyError(CANARY), id="key-error"),
        pytest.param(_BrokenStr(CANARY), id="failing-str"),
        pytest.param(OSError(13, CANARY, f"/{CANARY}"), id="os-error-path"),
    ],
)
def test_message_bearing_fields_never_render(exc):
    assert CANARY not in render_withheld_traceback(exc)


def test_notes_are_counted_never_shown_even_on_an_empty_message():
    exc = Exception()
    exc.add_note(CANARY)
    exc.add_note(CANARY)
    out = render_withheld_traceback(exc)
    assert CANARY not in out
    assert out == "Exception: [message withheld]\n[2 note(s) withheld]"

    odd = Exception()
    odd.__notes__ = CANARY  # not a sequence: the stdlib would print its repr
    assert render_withheld_traceback(odd).endswith("[notes withheld]")


def test_aliases_and_cycles_terminate_and_stay_withheld():
    shared = ValueError(CANARY)
    nested = ExceptionGroup("n", [shared, ExceptionGroup("m", [shared])])
    out = render_withheld_traceback(nested)
    assert CANARY not in out
    # The first occurrence renders, the second points back to it.
    assert out.count("| ValueError: [message withheld]") == 1
    assert out.count("ValueError: [shown above]") == 1

    a, b = ValueError(CANARY), ValueError(CANARY)
    a.__context__, b.__context__ = b, a
    assert CANARY not in render_withheld_traceback(a)


def test_width_depth_and_chain_length_are_bounded():
    wide = ExceptionGroup("w", [ValueError(CANARY) for _ in range(20)])
    assert "+-- 5 more sub-exceptions omitted --" in render_withheld_traceback(wide)

    deep: BaseException = ValueError(CANARY)
    for _ in range(12):
        deep = ExceptionGroup("d", [deep])
    assert "[nested exception groups omitted]" in render_withheld_traceback(deep)

    head = ValueError(CANARY)
    current = head
    for _ in range(40):
        nxt = ValueError(CANARY)
        current.__cause__ = nxt
        current = nxt
    out = render_withheld_traceback(head)
    assert out.startswith("[earlier exceptions in the chain omitted]")
    assert out.count("ValueError: [message withheld]") == 32
    assert CANARY not in out


def _nested_groups(levels: int) -> BaseException:
    exc: BaseException = ValueError(CANARY)
    for _ in range(levels):
        exc = ExceptionGroup("g", [exc])
    return exc


@pytest.mark.parametrize(("levels", "omitted"), [(10, False), (11, True)])
def test_group_depth_boundary_is_ten_levels(levels, omitted):
    out = render_withheld_traceback(_nested_groups(levels))
    assert out.count("ExceptionGroup: [message withheld]") == 10
    assert ("[nested exception groups omitted]" in out) is omitted
    assert ("ValueError: [message withheld]" in out) is not omitted


def test_budgets_apply_while_rendering_not_after():
    """A 15-wide, 4-deep group holds ~54k exceptions; the render stops at the
    exception budget instead of building them all and cutting afterwards."""
    import time

    def wide(levels: int) -> BaseException:
        if levels == 0:
            return ValueError(CANARY)
        return ExceptionGroup("w", [wide(levels - 1) for _ in range(15)])

    tree = wide(4)
    started = time.perf_counter()
    out = render_withheld_traceback(tree)
    elapsed = time.perf_counter() - started

    assert out.endswith("[traceback truncated]")
    assert out.count("[message withheld]") <= 256
    assert len(out.splitlines()) <= 2001
    assert elapsed < 2.0
    assert CANARY not in out


def test_line_budget_cuts_with_a_marker(monkeypatch):
    import memtomem_stm.logging_setup as setup

    monkeypatch.setattr(setup, "_MAX_LINES", 3)
    out = render_withheld_traceback(ExceptionGroup("g", [ValueError(CANARY)] * 3))
    assert out.splitlines()[-1] == "[traceback truncated]"
    assert len(out.splitlines()) == 4


# ── the code beside each withheld message ──


class _Response:
    status_code = 503


def test_codes_come_from_numbers_and_tables_only():
    import httpx2
    from mcp.shared.exceptions import MCPError
    from mcp.types import ErrorData

    http = httpx2.HTTPStatusError(CANARY, request=None, response=_Response())  # type: ignore[arg-type]
    assert render_withheld_traceback(http).endswith("[message withheld; HTTP 503]")

    db = _caught(lambda: sqlite3.connect(":memory:").execute(f"select {CANARY}"))
    assert render_withheld_traceback(db).endswith("[message withheld; SQLITE_ERROR]")

    rpc = MCPError.from_error_data(ErrorData(code=-32603, message=CANARY))
    assert render_withheld_traceback(rpc).endswith("[message withheld; -32603 (Internal error)]")


@pytest.mark.parametrize("status", [99, 600, 846219073351])
def test_a_status_outside_the_status_line_range_is_not_shown(status):
    import httpx2

    class Response:
        status_code = status

    exc = httpx2.HTTPStatusError(CANARY, request=None, response=Response())  # type: ignore[arg-type]
    assert render_withheld_traceback(exc).endswith(": [message withheld]")


def test_counts_come_from_built_in_types_not_overrides():
    """A note list or a group whose count is computed from the message would
    print the message as a number; only the built-in lengths are used."""

    class Leaky(list):
        def __len__(self) -> int:
            return 846219073351

    exc = Exception(CANARY)
    exc.__notes__ = Leaky([CANARY])
    assert render_withheld_traceback(exc).endswith("[notes withheld]")

    class LeakyGroup(ExceptionGroup):
        @property
        def exceptions(self):  # type: ignore[override]
            return tuple([ValueError(CANARY)] * 7)

    out = render_withheld_traceback(LeakyGroup("g", [ValueError(CANARY)]))
    assert "+-- sub-exception 1 of 1 --" in out
    assert "of 7" not in out


def test_frames_are_capped_before_they_are_extracted(monkeypatch):
    import sys as _sys

    import memtomem_stm.logging_setup as setup

    def deep(n: int) -> None:
        if n == 0:
            raise ValueError(CANARY)
        deep(n - 1)

    exc = _caught(lambda: deep(100))
    consumed = 0
    real_walk = setup.traceback.walk_tb

    def counting_walk(tb):
        nonlocal consumed
        for item in real_walk(tb):
            consumed += 1
            yield item

    monkeypatch.setattr(setup, "_MAX_LINES", 5)
    monkeypatch.setattr(setup.traceback, "walk_tb", counting_walk)
    out = render_withheld_traceback(exc)
    assert consumed <= 6  # the cap, plus the one look-ahead islice takes
    assert out.endswith("[traceback truncated]")
    assert _sys.getrecursionlimit() > 100


def test_a_non_traceback_in_exc_info_gives_the_constant():
    assert render_withheld_traceback(ValueError(CANARY), object()) == WITHHELD_RENDER_FAILED  # type: ignore[arg-type]


def test_a_non_exception_in_exc_info_gives_the_constant():
    class Legacy:
        __cause__ = __context__ = __traceback__ = None
        __suppress_context__ = False

        def __str__(self) -> str:
            return CANARY

    record = logging.LogRecord(
        "memtomem_stm.t", logging.WARNING, __file__, 1, "m", None, (Legacy, Legacy(), None)
    )
    out = DisplayEscapingFormatter("%(message)s").format(record)
    assert out == f"m\n{WITHHELD_RENDER_FAILED}"


def test_free_text_codes_are_not_used():
    """``exception_summary`` keeps a ``ResponseShapeError``'s text and pydantic
    error types; neither is fixed vocabulary, so tracebacks leave them out."""
    from pydantic import BaseModel, ValidationError, field_validator
    from pydantic_core import PydanticCustomError

    from memtomem_stm.utils.redact import ResponseShapeError

    class Model(BaseModel):
        x: str

        @field_validator("x")
        @classmethod
        def check(cls, value: str) -> str:
            raise PydanticCustomError(value, "fixed")

    shape = ResponseShapeError(f"'data[].index' is {CANARY}")
    custom = _caught(lambda: Model(x=CANARY))
    assert isinstance(custom, ValidationError)
    for exc in (shape, custom):
        out = render_withheld_traceback(exc)
        assert CANARY not in out
        assert out.endswith(": [message withheld]")


# ── fail closed ──


def test_any_failure_while_rendering_gives_the_constant(monkeypatch):
    import memtomem_stm.utils.redact as redact

    def boom(exc):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(redact, "exception_code_label", boom)
    assert render_withheld_traceback(ValueError(CANARY)) == WITHHELD_RENDER_FAILED


def test_a_cancellation_raised_while_rendering_gives_the_constant():
    import asyncio

    import httpx2

    class Hostile(httpx2.HTTPStatusError):
        @property
        def response(self):  # type: ignore[override]
            raise asyncio.CancelledError(CANARY)

    exc = Hostile.__new__(Hostile)
    Exception.__init__(exc, CANARY)
    assert render_withheld_traceback(exc) == WITHHELD_RENDER_FAILED


def test_an_interrupt_raised_while_rendering_still_propagates(monkeypatch):
    import memtomem_stm.utils.redact as redact

    def interrupt(exc):
        raise KeyboardInterrupt

    monkeypatch.setattr(redact, "exception_code_label", interrupt)
    with pytest.raises(KeyboardInterrupt):
        render_withheld_traceback(ValueError(CANARY))


def test_the_traceback_of_an_exc_info_tuple_is_used():
    """``exc_info`` carries the traceback separately; the stdlib formats that
    one even when the exception's own ``__traceback__`` was cleared."""
    exc = _caught(raise_cause_marker)
    saved = exc.__traceback__
    exc.__traceback__ = None
    record = logging.LogRecord(
        "memtomem_stm.t", logging.WARNING, __file__, 1, "m", None, (type(exc), exc, saved)
    )
    out = DisplayEscapingFormatter("%(message)s").format(record)
    assert "in raise_cause_marker" in out
    assert CANARY not in out


def test_a_type_whose_name_lookup_raises_gives_the_constant():
    class Meta(type):
        def __getattribute__(cls, name: str):
            if name == "__qualname__":
                raise RuntimeError(CANARY)
            return super().__getattribute__(name)

    class Hostile(Exception, metaclass=Meta):
        pass

    assert render_withheld_traceback(Hostile(CANARY)) == WITHHELD_RENDER_FAILED


# ── the formatter and the record cache ──


def _record(exc: BaseException | None, cached: str | None = None) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    record = logging.LogRecord("memtomem_stm.t", logging.WARNING, __file__, 1, "m", None, exc_info)
    record.exc_text = cached
    return record


def test_cache_is_ignored_and_restored_in_both_handler_orders():
    exc = _caught(raise_cause_marker)
    raw = logging.Formatter("%(message)s")
    stm = DisplayEscapingFormatter("%(message)s")

    record = _record(exc)
    first_raw = raw.format(record)
    then_stm = stm.format(record)
    assert CANARY in first_raw and CANARY not in then_stm
    assert CANARY in (record.exc_text or "")  # the raw handler's cache is back

    record = _record(exc)
    first_stm = stm.format(record)
    then_raw = raw.format(record)
    assert CANARY not in first_stm and CANARY in then_raw
    assert stm.format(record) == first_stm  # repeated formatting is stable


def test_withholding_and_opt_in_formatters_in_one_process():
    exc = _caught(raise_cause_marker)
    record = _record(exc)
    kept = DisplayEscapingFormatter("%(message)s", withhold_exception_text=False).format(record)
    withheld = DisplayEscapingFormatter("%(message)s").format(record)
    assert CANARY in kept and CANARY not in withheld


def test_a_cached_traceback_without_exc_info_is_replaced():
    record = _record(None, cached=f"Traceback ...\nValueError: {CANARY}")
    out = DisplayEscapingFormatter("%(message)s").format(record)
    assert out == f"m\n{WITHHELD_CACHED_TRACEBACK}"
    assert CANARY in (record.exc_text or "")


# ── every handler STM installs ──


def _log_chain(name: str = "memtomem_stm.test_withheld") -> None:
    try:
        raise_top_marker()
    except ExceptionGroup:
        logging.getLogger(name).warning("failed", exc_info=True)


def _assert_withheld(out: str) -> None:
    assert "failed" in out
    assert CANARY not in out
    assert "in raise_member_one_marker" in out
    assert "ExceptionGroup: [message withheld]" in out


@pytest.mark.parametrize("opt_in", [False, True])
def test_server_handlers(tmp_path, capsys, opt_in, _restore_logging):
    path = tmp_path / "stm.log"
    configure_server_logging(STMConfig(log_file=path, log_exception_text=opt_in))
    _log_chain()
    for h in logging.getLogger().handlers:
        h.flush()
    for out in (capsys.readouterr().err, path.read_text(encoding="utf-8")):
        if opt_in:
            assert CANARY in out
        else:
            _assert_withheld(out)


@pytest.mark.parametrize("detached", [True, False])
@pytest.mark.parametrize("opt_in", [False, True])
def test_daemon_handlers(tmp_path, capsys, detached, opt_in, _restore_logging):
    from memtomem_stm.cli.daemon_cmd import _configure_logging

    _configure_logging(STMConfig(data_dir=tmp_path, log_exception_text=opt_in), detached=detached)
    _log_chain()
    for h in logging.getLogger().handlers:
        h.flush()
    out = (
        (tmp_path / "stm-daemon.log").read_text(encoding="utf-8")
        if detached
        else capsys.readouterr().err
    )
    if opt_in:
        assert CANARY in out
    else:
        _assert_withheld(out)


def test_early_failure_and_hook_handlers_always_withhold(capsys, _restore_logging):
    configure_stderr_logging(level=logging.WARNING, force=True)
    _log_chain()
    _assert_withheld(capsys.readouterr().err)

    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
    configure_hook_logging()
    _log_chain()
    _assert_withheld(capsys.readouterr().err)


def test_sdk_logger_exception_record_is_withheld(capsys, _restore_logging):
    """The MCP SDK's own ``logger.exception`` sites log at ERROR, which the
    dependency filter passes; the traceback still loses its message."""
    configure_server_logging(STMConfig())
    try:
        raise_cause_marker()
    except KeyError:
        logging.getLogger("mcp.client.streamable_http").exception("Error in post_writer")
    err = capsys.readouterr().err
    assert "Error in post_writer" in err
    assert CANARY not in err
    assert "KeyError: [message withheld]" in err


class _Echo500(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = f"failed for {self.path}".encode()
        self.send_response(500)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.mark.parametrize("opt_in", [False, True])
def test_real_http_status_error_keeps_the_token_out(tmp_path, opt_in, _restore_logging):
    """httpx2's ``HTTPStatusError`` message quotes the request URL, query
    included; a site that logs it with ``exc_info`` repeated it."""
    import httpx2

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Echo500)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = tmp_path / "stm.log"
    configure_server_logging(STMConfig(log_file=path, log_exception_text=opt_in))
    try:
        with httpx2.Client(trust_env=False, timeout=10.0) as client:
            response = client.get(f"http://127.0.0.1:{server.server_address[1]}/x?token={CANARY}")
        try:
            response.raise_for_status()
        except httpx2.HTTPStatusError:
            logging.getLogger("memtomem_stm.test_withheld").warning("upstream", exc_info=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    for h in logging.getLogger().handlers:
        h.flush()
    text = path.read_text(encoding="utf-8")
    assert (CANARY in text) is opt_in
    if not opt_in:
        assert "httpx2.HTTPStatusError: [message withheld; HTTP 500]" in text


def test_opt_in_from_env(monkeypatch):
    monkeypatch.setenv("MEMTOMEM_STM_LOG_EXCEPTION_TEXT", "true")
    assert STMConfig().log_exception_text is True
    monkeypatch.delenv("MEMTOMEM_STM_LOG_EXCEPTION_TEXT")
    assert STMConfig().log_exception_text is False


def test_stack_info_is_unaffected():
    """``stack_info`` carries frames only; it is not withheld."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(DisplayEscapingFormatter("%(message)s"))
    log = logging.getLogger("memtomem_stm.test_withheld.stack")
    log.handlers[:] = [handler]
    log.propagate = False
    log.warning("here", stack_info=True)
    assert "Stack (most recent call last):" in buf.getvalue()
