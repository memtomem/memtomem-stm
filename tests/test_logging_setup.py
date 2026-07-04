"""Tests for the shared logging setup (#612).

Covers the opt-in rotating file log: 0o600 file / 0o700 parent on creation
and after rollover, tightening a pre-existing permissive file, graceful
degradation when the file can't be opened (server keeps running on stderr),
and the format/description surfaces. Root-logger handlers are snapshotted and
restored per test — ``basicConfig(force=True)`` closes existing handlers
(including pytest's caplog handler), so leaking them across tests would break
capture and, on Windows, keep the tmp file open.
"""

from __future__ import annotations

import logging
import sys

import pytest

from memtomem_stm.config import STMConfig
from memtomem_stm.logging_setup import (
    FILE_FORMAT,
    STDERR_FORMAT,
    PrivateRotatingFileHandler,
    configure_server_logging,
    describe_log_destination,
    log_file_writable,
    open_private_log_handler,
)

_skip_on_windows = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX 0o700/0o600 modes are unenforceable on Windows",
)


@pytest.fixture
def _restore_root_logging():
    """Snapshot/restore root handlers + level around a test that reconfigures
    the root logger, so ``basicConfig(force=True)`` can't leak closed handlers
    into pytest's capture or other tests."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
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


def _emit(name: str = "test.logging") -> None:
    logging.getLogger(name).warning("a log line")


class TestFilePermissions:
    @_skip_on_windows
    def test_fresh_file_0600_parent_0700(self, tmp_path):
        path = tmp_path / "sub" / "stm.log"
        handler = open_private_log_handler(path)
        try:
            handler.emit(logging.makeLogRecord({"msg": "x"}))
        finally:
            handler.close()
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700

    @_skip_on_windows
    def test_preexisting_permissive_file_tightened(self, tmp_path):
        path = tmp_path / "stm.log"
        path.touch()
        path.chmod(0o644)
        handler = open_private_log_handler(path)
        try:
            handler.emit(logging.makeLogRecord({"msg": "x"}))
        finally:
            handler.close()
        assert path.stat().st_mode & 0o777 == 0o600

    @_skip_on_windows
    def test_preexisting_parent_mode_untouched(self, tmp_path):
        # A pre-existing parent dir keeps its mode (mkdir's mode only applies
        # on creation) — matches the sibling-store policy.
        parent = tmp_path / "existing"
        parent.mkdir(mode=0o755)
        parent.chmod(0o755)
        handler = open_private_log_handler(parent / "stm.log")
        handler.close()
        assert parent.stat().st_mode & 0o777 == 0o755

    @_skip_on_windows
    def test_rollover_keeps_0600_on_base_and_backup(self, tmp_path):
        path = tmp_path / "stm.log"
        handler = open_private_log_handler(path, max_bytes=1, backup_count=1)
        try:
            handler.emit(logging.makeLogRecord({"msg": "first record"}))
            handler.emit(logging.makeLogRecord({"msg": "second record forces rollover"}))
        finally:
            handler.close()
        backup = path.with_name(path.name + ".1")
        assert backup.exists()
        assert path.stat().st_mode & 0o777 == 0o600
        assert backup.stat().st_mode & 0o777 == 0o600

    def test_handler_is_rotating_subclass(self, tmp_path):
        handler = open_private_log_handler(tmp_path / "stm.log")
        try:
            assert isinstance(handler, PrivateRotatingFileHandler)
        finally:
            handler.close()


class TestConfigureServerLogging:
    def test_no_log_file_is_stderr_only(self, _restore_root_logging):
        active = configure_server_logging(STMConfig(log_file=None, log_level="INFO"))
        assert active is None
        handlers = logging.getLogger().handlers
        assert len(handlers) == 1
        assert isinstance(handlers[0], logging.StreamHandler)
        # stderr format is byte-identical to the pre-#612 basicConfig.
        assert handlers[0].formatter is not None
        assert handlers[0].formatter._fmt == STDERR_FORMAT

    @_skip_on_windows
    def test_log_file_adds_second_handler_with_asctime(self, tmp_path, _restore_root_logging):
        path = tmp_path / "stm.log"
        active = configure_server_logging(STMConfig(log_file=path, log_level="INFO"))
        assert active == path.expanduser()
        handlers = logging.getLogger().handlers
        assert len(handlers) == 2
        file_handler = next(h for h in handlers if isinstance(h, PrivateRotatingFileHandler))
        assert file_handler.formatter is not None
        assert file_handler.formatter._fmt == FILE_FORMAT
        _emit()
        assert path.exists()
        assert path.stat().st_mode & 0o777 == 0o600

    @_skip_on_windows
    def test_unwritable_path_degrades_to_stderr(self, tmp_path, _restore_root_logging, capsys):
        # Parent is a read-only dir → the file can't be created. The server
        # must keep running on stderr rather than crash on an opt-in aid.
        # Asserted via capsys, not caplog: basicConfig(force=True) removes
        # caplog's handler before the warning is emitted, so it only reaches
        # the freshly-installed stderr handler.
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o500)
        try:
            cfg = STMConfig(log_file=locked / "sub" / "stm.log", log_level="WARNING")
            active = configure_server_logging(cfg)
            logging.getLogger().handlers[0].flush()
            assert active is None
            handlers = [
                h
                for h in logging.getLogger().handlers
                if isinstance(h, PrivateRotatingFileHandler)
            ]
            assert handlers == []
            assert "continuing with stderr only" in capsys.readouterr().err
        finally:
            locked.chmod(0o700)


class TestLogFileWritable:
    def test_missing_file_writable_parent(self, tmp_path):
        assert log_file_writable(tmp_path / "sub" / "stm.log") is True

    @_skip_on_windows
    def test_missing_file_unwritable_parent(self, tmp_path):
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o500)
        try:
            assert log_file_writable(locked / "sub" / "stm.log") is False
        finally:
            locked.chmod(0o700)

    @_skip_on_windows
    def test_existing_unwritable_file(self, tmp_path):
        path = tmp_path / "stm.log"
        path.touch()
        path.chmod(0o400)
        try:
            assert log_file_writable(path) is False
        finally:
            path.chmod(0o600)

    def test_probe_does_not_create_anything(self, tmp_path):
        path = tmp_path / "sub" / "stm.log"
        log_file_writable(path)
        assert not path.exists()
        assert not path.parent.exists()


class TestDescribeLogDestination:
    def test_unset(self):
        d = describe_log_destination(STMConfig(log_file=None, log_level="WARNING"))
        assert d == {
            "log_level": "WARNING",
            "log_file": None,
            "destination": "stderr",
            "writable": None,
        }

    def test_set(self, tmp_path):
        path = tmp_path / "stm.log"
        d = describe_log_destination(STMConfig(log_file=path, log_level="DEBUG"))
        assert d["log_level"] == "DEBUG"
        assert d["log_file"] == str(path.expanduser())
        assert d["destination"] == "stderr+file"
        assert d["writable"] is True


class TestEnvWiring:
    def test_log_file_from_env(self, monkeypatch, tmp_path):
        path = tmp_path / "stm.log"
        monkeypatch.setenv("MEMTOMEM_STM_LOG_FILE", str(path))
        assert STMConfig().log_file == path

    def test_default_is_none(self, monkeypatch):
        monkeypatch.delenv("MEMTOMEM_STM_LOG_FILE", raising=False)
        assert STMConfig().log_file is None
