"""Display escaping at the shared write-lock boundaries (#786).

``_write_lock`` renders a :class:`state.WriteLockTimeout` at four points, and
that exception's message embeds the lock path. Every lock path is built from
``Path.home()``, so on POSIX a byte in ``HOME`` that is not valid UTF-8 decodes
to a lone surrogate via ``surrogateescape`` — unencodable, so the render raises
rather than displaying — and a CR rewrites the line. The boundaries are
reachable through the host, project and hook commands alike.

The JSON leg is the reason these are asserted separately rather than folded
into one "no raw control character anywhere" sweep: the constraint carried over
from #785 is that the envelope must keep the *logical raw* value for its
consumer to decode while only the human stderr text is escaped, so a fix that
escaped both would pass a naive sweep and corrupt the machine-readable half.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from memtomem_stm.cli._write_lock import (
    hook_hosts_write_lock,
    with_config_write_lock,
    with_write_lock,
)
from memtomem_stm.mms import state

HOSTILE_LOCK = Path("/tmp") / "ho\x1b[31m\rme" / ".memtomem" / ".stm_proxy.lock"


def _raise_timeout(*args, **kwargs):
    raise state.WriteLockTimeout(HOSTILE_LOCK, 5.0)


def _assert_escaped(text: str) -> None:
    assert "\x1b" not in text and "\r" not in text
    assert "\\u001B" in text and "\\u000D" in text


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _timeout_everywhere(monkeypatch):
    monkeypatch.setattr(state, "write_lock", _raise_timeout)


class TestWriteLockTimeoutRendersEscaped:
    def test_with_write_lock_escapes_the_path(self, runner):
        @click.command()
        @with_write_lock
        def cmd() -> None:  # pragma: no cover - the lock raises before the body
            click.echo("body ran")

        res = runner.invoke(cmd, [])

        assert res.exit_code != 0
        _assert_escaped(res.output)

    def test_hook_hosts_write_lock_escapes_the_path(self, runner):
        @click.command()
        def cmd() -> None:
            with hook_hosts_write_lock(enabled=True):  # pragma: no cover
                click.echo("body ran")

        res = runner.invoke(cmd, [])

        assert res.exit_code != 0
        _assert_escaped(res.output)

    def test_config_write_lock_text_leg_escapes_the_path(self, runner):
        @click.command()
        @with_config_write_lock()
        def cmd() -> None:  # pragma: no cover
            click.echo("body ran")

        res = runner.invoke(cmd, [])

        assert res.exit_code != 0
        _assert_escaped(res.output)


class TestConfigWriteLockJsonLeg:
    """The `--json` leg escapes the stderr line and NOT the envelope."""

    @staticmethod
    def _invoke(runner: CliRunner):
        @click.command()
        @click.option("--json", "as_json", is_flag=True)
        @with_config_write_lock(json_envelope=True)
        def cmd(as_json: bool) -> None:  # pragma: no cover
            click.echo("body ran")

        return runner.invoke(cmd, ["--json"], standalone_mode=False)

    def test_stderr_line_is_escaped(self, runner):
        res = self._invoke(runner)

        assert res.exit_code == 1
        stderr_line = next(
            ln for ln in res.output.splitlines() if ln.startswith("Error: timed out")
        )
        _assert_escaped(stderr_line)

    def test_envelope_keeps_the_logical_raw_message(self, runner):
        """Escaping this too would corrupt the value a consumer decodes.

        The envelope is emitted through ``utils.json_out``, whose job is to
        make an unencodable value *transportable* — the display escape is the
        wrong half of that pair here.
        """
        res = self._invoke(runner)

        payload = json.loads(res.output[res.output.index("{") :])
        assert payload["error"] == "config_lock_timeout"
        assert str(HOSTILE_LOCK) in payload["message"]
        assert "\x1b" in payload["message"] and "\r" in payload["message"]
