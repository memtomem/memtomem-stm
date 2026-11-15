"""A log argument cannot carry a display-hostile character to the log (#1425).

Config does not restrict the characters of an upstream server key or its
``prefix``, and most proxy log lines interpolate one of them. Before #1425 the
formatter escaped only the traceback, so a key with a newline in it split the
line that named it. ``DisplayEscapingFormatter`` now escapes every argument as
``%`` interpolates it.

These tests check the FORMATTED output — what the handler writes — not
``record.getMessage()``, which is the raw record and is unchanged. The first
half drives the formatter directly; the second drives real call sites, one per
kind of value: a server key, a server key the call site used to format into
the message itself, a prefixed tool name, and a configured path.
"""

from __future__ import annotations

import contextlib
import enum
import io
import logging
from collections import OrderedDict, defaultdict
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem_stm.logging_setup import (
    FILE_FORMAT,
    HOOK_STDERR_FORMAT,
    STDERR_FORMAT,
    DisplayEscapingFormatter,
    configure_server_logging,
)
from memtomem_stm.utils.display import _disp_escapes

# A newline that would start a forged record, then a right-to-left override.
HOSTILE = "gh\nERROR memtomem_stm.proxy.manager: forged\u202e"
ESCAPED = "gh\\u000AERROR memtomem_stm.proxy.manager: forged\\u202E"


def _format(msg: str, *args: object, fmt: str = HOOK_STDERR_FORMAT) -> str:
    """*msg* and *args* as a logger call records them, through the formatter."""
    log = logging.getLogger("memtomem_stm.test_logged_argument_escaping")
    record = log.makeRecord(log.name, logging.WARNING, __file__, 1, msg, args, None)
    return DisplayEscapingFormatter(fmt).format(record)


def _stdlib(msg: str, *args: object) -> str:
    log = logging.getLogger("memtomem_stm.test_logged_argument_escaping")
    record = log.makeRecord(log.name, logging.WARNING, __file__, 1, msg, args, None)
    return logging.Formatter(HOOK_STDERR_FORMAT).format(record)


def _assert_one_clean_line(text: str) -> None:
    assert len(text.splitlines()) == 1, text
    hostile = sorted({f"U+{ord(ch):04X}" for ch in text.rstrip("\n") if _disp_escapes(ch)})
    assert hostile == [], f"{hostile} reached the output: {text!r}"


# ---------------------------------------------------------------------------
# The formatter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", [STDERR_FORMAT, HOOK_STDERR_FORMAT, FILE_FORMAT])
def test_a_hostile_argument_stays_on_its_line(fmt):
    out = _format("Connected to '%s' (%s tools discovered)", HOSTILE, 3, fmt=fmt)

    _assert_one_clean_line(out)
    assert out.endswith(f"Connected to '{ESCAPED}' (3 tools discovered)")


def test_every_member_of_the_display_set_is_escaped():
    members = "".join(chr(cp) for cp in range(0x3000) if _disp_escapes(chr(cp)))
    members += "\ud800\udfff"
    assert "\n" in members and "\x1b" in members and "\u202e" in members

    out = _format("value %s end", members)

    _assert_one_clean_line(out)
    assert out.startswith("value \\u0000\\u0001") and out.endswith("\\uDFFF end")


def test_the_literal_text_of_the_message_is_left_alone():
    assert _format("first line\nsecond line %s", "x") == "first line\nsecond line x"
    assert _format("no arguments\tat all") == "no arguments\tat all"


def test_repr_conversion_is_escaped_too():
    class Loud:
        def __repr__(self) -> str:
            return f"<Loud {HOSTILE}>"

    _assert_one_clean_line(_format("got %r", Loud()))
    # ``repr`` of a ``str`` already escapes the set; it is not escaped twice.
    assert _format("got %r", "a\nb") == "got 'a\\nb'"


def test_a_non_string_argument_is_escaped_through_its_str():
    out = _format("opening %s failed: %s", Path("/data") / HOSTILE, ValueError(HOSTILE))

    _assert_one_clean_line(out)
    assert out.count(ESCAPED) == 2


def test_a_numeric_subclass_cannot_carry_a_value_through():
    class Count(int):
        def __str__(self) -> str:
            return HOSTILE

    class Ratio(float):
        def __repr__(self) -> str:
            return HOSTILE

    assert _format("%s", Count(5)) == ESCAPED
    assert _format("%r", Ratio(0.5)) == ESCAPED
    assert _format("%(n)s", {"n": Count(5)}) == ESCAPED
    # The number itself still converts as a number.
    assert _format("%d %.1f", Count(5), Ratio(0.5)) == "5 0.5"


def test_a_mapping_argument_is_escaped_per_lookup():
    assert _format("%(name)s: %(count)d", {"name": HOSTILE, "count": 2}) == f"{ESCAPED}: 2"


class _Transport(enum.StrEnum):
    STDIO = "stdio"


class _Level(enum.IntEnum):
    HIGH = 3


_CLEAN_CASES = [
    ("Connected to '%s' (%s tools discovered)", ("github", 12)),
    ("%s/%s: %d of %d", ("gh", "search", 3, 10)),
    ("ratio %.3f < %.3f, mode %o, id %x, %g", (0.25, 0.5, 0o600, 255, 1.5)),
    ("%5d|%-8s|%10.3s|%r", (7, "ab", "abcdef", "cd")),
    ("%*d|%.*f", (5, 3, 2, 1.23456)),
    ("%s %s %s %d", (None, True, False, True)),
    ("%s at %r", (Path("/a/b"), Path("/a/b"))),
    ("%s %r", (b"by\ntes", ["a", "b\n"])),
    (
        "%d rows, %d rows, %.2f",
        (Decimal("3"), MagicMock(__int__=lambda self: 4), Decimal("3.14159")),
    ),
    ("%s|%r", (_Transport.STDIO, _Transport.STDIO)),
    ("%s|%d|%r", (_Level.HIGH, _Level.HIGH, _Level.HIGH)),
    ("%c%c", (65, "B")),
    ("%a 100%%", ("é",)),
    ("%s", (ValueError("plain"),)),
    ("CJK %s emoji %s", ("한글", "👨‍👩‍👧")),
    ("%(a)s=%(b)d|%(c).1f", ({"a": "x", "b": 2, "c": 1.25},)),
    ("%(missing)s", (defaultdict(lambda: "fallback", {"present": 1}),)),
    ("%s", (OrderedDict(x="clean"),)),
    ("%s", ({"k": "v\n"},)),
]


@pytest.mark.parametrize(("msg", "args"), _CLEAN_CASES)
def test_a_value_with_nothing_to_escape_renders_as_the_stdlib_renders_it(msg, args):
    assert _format(msg, *args) == _stdlib(msg, *args)


@pytest.mark.parametrize(
    ("msg", "args", "error"),
    [
        ("%(nope)s", ({"a": 1},), KeyError),
        ("%s %s", ("only one",), TypeError),
        ("%d", ("not a number",), TypeError),
    ],
)
def test_a_call_the_stdlib_rejects_is_still_rejected(msg, args, error):
    with pytest.raises(error):
        _stdlib(msg, *args)
    with pytest.raises(error):
        _format(msg, *args)


def test_a_record_built_with_one_bare_argument_is_escaped():
    """``LogRecord`` accepts an ``args`` that is neither a tuple nor a mapping;
    ``%`` then treats it as the single argument."""
    record = logging.LogRecord("memtomem_stm.x", logging.WARNING, __file__, 1, "%s", HOSTILE, None)
    assert record.args == HOSTILE

    assert DisplayEscapingFormatter(HOOK_STDERR_FORMAT).format(record) == ESCAPED
    assert record.args == HOSTILE


def test_the_record_keeps_its_arguments_for_the_next_handler():
    log = logging.getLogger("memtomem_stm.test_logged_argument_escaping")
    args = ("gh", HOSTILE, 3)
    record = log.makeRecord(log.name, logging.WARNING, __file__, 1, "%s %s %d", args, None)

    DisplayEscapingFormatter(HOOK_STDERR_FORMAT).format(record)

    assert record.args is args
    assert logging.Formatter("%(message)s").format(record) == f"gh {HOSTILE} 3"


def test_the_record_keeps_its_arguments_when_formatting_raises():
    log = logging.getLogger("memtomem_stm.test_logged_argument_escaping")
    args = (HOSTILE,)
    record = log.makeRecord(log.name, logging.WARNING, __file__, 1, "%d", args, None)

    with pytest.raises((TypeError, ValueError)):
        DisplayEscapingFormatter(HOOK_STDERR_FORMAT).format(record)

    assert record.args is args


def test_a_hostile_argument_and_a_traceback_in_one_record():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(DisplayEscapingFormatter(STDERR_FORMAT))
    log = logging.getLogger("memtomem_stm.test_logged_argument_escaping.traceback")
    log.handlers[:] = [handler]
    log.propagate = False
    try:
        raise ValueError("withheld")
    except ValueError:
        log.warning("Cache store failed for %s/%s", HOSTILE, "search", exc_info=True)
    lines = buf.getvalue().splitlines()

    assert lines[0].endswith(f"Cache store failed for {ESCAPED}/search")
    assert not any(line.startswith("ERROR memtomem_stm") for line in lines)
    assert "\u202e" not in buf.getvalue()
    assert any("Traceback" in line for line in lines[1:])


# ---------------------------------------------------------------------------
# Real call sites, as the server writes them
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _server_log(logger_name: str) -> Iterator[io.StringIO]:
    """What *logger_name* logs, through the formatter the server installs."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(DisplayEscapingFormatter(FILE_FORMAT))
    log = logging.getLogger(logger_name)
    saved_level = log.level
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        log.removeHandler(handler)
        log.setLevel(saved_level)


def test_a_server_key_in_the_tool_listing_warning():
    from memtomem_stm.proxy.manager import ProxyManager

    with _server_log("memtomem_stm.proxy.manager") as stream:
        kept, refused = ProxyManager._ingest_tool_listing(
            HOSTILE, [SimpleNamespace(name="not a conforming name")]
        )

    assert (kept, refused) == ([], 1)
    _assert_one_clean_line(stream.getvalue())
    assert f"Upstream '{ESCAPED}' listed 1 tool(s)" in stream.getvalue()


async def test_a_server_key_in_the_oversize_response_warning():
    """This call site used to build the message with an f-string, so the key
    was not an argument at all."""
    from mcp.server.mcpserver.exceptions import ToolError

    from memtomem_stm.proxy.config import CompressionStrategy, ProxyConfig, UpstreamServerConfig
    from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
    from memtomem_stm.proxy.metrics import TokenTracker

    server_cfg = UpstreamServerConfig(prefix="test", compression=CompressionStrategy.NONE)
    proxy_cfg = ProxyConfig(
        config_path=Path("/tmp/proxy.json"),
        upstream_servers={HOSTILE: server_cfg},
        max_upstream_bytes=64,
    )
    mgr = ProxyManager(proxy_cfg, TokenTracker())
    session = AsyncMock()
    session.call_tool.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="x" * 1_000)], is_error=False
    )
    mgr._connections[HOSTILE] = UpstreamConnection(
        name=HOSTILE, config=server_cfg, session=session, tools=[]
    )

    with _server_log("memtomem_stm.proxy.manager") as stream:
        with pytest.raises(ToolError, match="response exceeded max_upstream_bytes=64"):
            await mgr.call_tool(HOSTILE, "tool", {})

    warnings = [line for line in stream.getvalue().splitlines() if " WARNING " in line]
    assert len(warnings) == 1, stream.getvalue()
    assert warnings[0].endswith(
        f"Upstream '{ESCAPED}/tool' response exceeded max_upstream_bytes=64"
    )
    assert "\u202e" not in stream.getvalue()


def test_a_prefixed_tool_name_in_the_registration_warning():
    from memtomem_stm.proxy._fastmcp_compat import register_proxy_tool

    prefixed = f"{HOSTILE}__search"
    server = MagicMock()
    server._tool_manager._tools = {prefixed: object()}
    info = SimpleNamespace(
        prefixed_name=prefixed,
        description="desc",
        annotations=None,
        server="gh",
        input_schema={"type": "object"},
    )

    with _server_log("memtomem_stm.proxy._fastmcp_compat") as stream:
        assert register_proxy_tool(server, lambda: None, info) is False

    _assert_one_clean_line(stream.getvalue())
    assert f"Not registering proxy tool '{ESCAPED}__search'" in stream.getvalue()


@pytest.fixture
def _restore_root_logging():
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


def test_a_configured_path_in_the_log_file_warning(tmp_path, capsys, _restore_root_logging):
    """``log_file`` under a path that is not a directory: the server keeps
    running on stderr and says which file it could not open."""
    from memtomem_stm.config import STMConfig

    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is expected")
    config = STMConfig(log_file=blocker / HOSTILE / "stm.log")

    assert configure_server_logging(config) is None

    err = capsys.readouterr().err
    _assert_one_clean_line(err)
    assert "could not be opened" in err
    assert ESCAPED in err
