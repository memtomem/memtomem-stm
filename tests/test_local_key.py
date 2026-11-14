"""Sidecar key files (``utils/local_key``) and the keyed digest (#1416)."""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from memtomem_stm.utils import local_key
from memtomem_stm.utils.digest import framed_digest, keyed_framed_digest
from memtomem_stm.utils.local_key import (
    LocalKeyError,
    key_path_for,
    load_key,
    load_or_create_key,
)

POSIX = sys.platform != "win32"


class TestLoadOrCreate:
    def test_absent_is_none_and_creates_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "s.db.key"
        assert load_key(path) is None
        assert not path.exists()

    def test_create_then_load_returns_the_same_key(self, tmp_path: Path) -> None:
        path = tmp_path / "s.db.key"
        created = load_or_create_key(path)
        assert len(created) == 32
        assert load_key(path) == created
        assert load_or_create_key(path) == created
        # Nothing but the key is left in the directory (the temp file is gone).
        assert sorted(p.name for p in tmp_path.iterdir()) == ["s.db.key"]

    def test_creates_the_parent_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "s.db.key"
        assert len(load_or_create_key(path)) == 32
        assert path.is_file()

    @pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
    def test_new_key_is_private(self, tmp_path: Path) -> None:
        path = tmp_path / "s.db.key"
        load_or_create_key(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    @pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
    def test_permissive_mode_is_tightened_on_load(self, tmp_path: Path) -> None:
        path = tmp_path / "s.db.key"
        key = load_or_create_key(path)
        path.chmod(0o644)
        assert load_key(path) == key
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_concurrent_creators_in_threads_agree(self, tmp_path: Path) -> None:
        path = tmp_path / "s.db.key"
        barrier = threading.Barrier(8)
        results: list[bytes] = []
        errors: list[BaseException] = []

        def _create() -> None:
            try:
                barrier.wait(timeout=5.0)
                results.append(load_or_create_key(path))
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=_create) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        assert errors == []
        assert len(results) == 8
        assert len(set(results)) == 1
        assert load_key(path) == results[0]

    def test_concurrent_creators_in_processes_agree(self, tmp_path: Path) -> None:
        path = tmp_path / "s.db.key"
        code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from memtomem_stm.utils.local_key import load_or_create_key\n"
            "print(load_or_create_key(Path(sys.argv[1])).hex())\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        outputs = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, err
            outputs.append(out.strip())
        assert len(set(outputs)) == 1
        assert load_key(path) == bytes.fromhex(outputs[0])


class TestUnusableKeyFile:
    @pytest.mark.parametrize(
        "content",
        [
            b"",
            b"abc\n",
            b"00" * 31 + b"\n",
            b"00" * 33,
            b"zz" * 32,
            b"AB" * 32,  # uppercase is not what the writer produces
            b"00" * 32 + b"\n\n",
            b"00" * 32 + b" ",
        ],
    )
    def test_malformed_content_raises_and_leaves_the_file(
        self, tmp_path: Path, content: bytes
    ) -> None:
        path = tmp_path / "s.db.key"
        path.write_bytes(content)
        with pytest.raises(LocalKeyError, match=str(path.name)):
            load_key(path)
        with pytest.raises(LocalKeyError):
            load_or_create_key(path)
        assert path.read_bytes() == content

    def test_trailing_newline_is_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "s.db.key"
        path.write_bytes(b"ab" * 32)
        assert load_key(path) == bytes.fromhex("ab" * 32)
        path.write_bytes(b"ab" * 32 + b"\n")
        assert load_key(path) == bytes.fromhex("ab" * 32)

    @pytest.mark.skipif(not POSIX, reason="symlinks without privileges are POSIX-only")
    def test_dangling_symlink_is_unusable_not_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "s.db.key"
        path.symlink_to(tmp_path / "missing-target")
        with pytest.raises(LocalKeyError):
            load_key(path)
        with pytest.raises(LocalKeyError):
            load_or_create_key(path)
        assert path.is_symlink()
        assert not (tmp_path / "missing-target").exists()

    @pytest.mark.skipif(
        not POSIX or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="needs POSIX permission enforcement (not root)",
    )
    def test_unreadable_file_raises_and_is_not_replaced(self, tmp_path: Path) -> None:
        path = tmp_path / "s.db.key"
        load_or_create_key(path)
        before = path.stat().st_ino
        path.chmod(0)
        try:
            with pytest.raises(LocalKeyError):
                load_or_create_key(path)
            assert path.stat().st_ino == before
        finally:
            path.chmod(0o600)

    def test_filesystem_without_hard_links_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_links(src: object, dst: object) -> None:
            raise OSError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(local_key.os, "link", _no_links)
        path = tmp_path / "s.db.key"
        with pytest.raises(LocalKeyError, match="hard links"):
            load_or_create_key(path)
        # No key and no temp file left behind.
        assert list(tmp_path.iterdir()) == []


class TestKeyPathFor:
    def test_sidecar_name(self, tmp_path: Path) -> None:
        assert key_path_for(tmp_path / "proxy_cache.db") == (
            tmp_path.resolve() / "proxy_cache.db.key"
        )

    def test_home_relative_path_is_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert key_path_for(Path("~/c.db")) == tmp_path.resolve() / "c.db.key"

    @pytest.mark.skipif(not POSIX, reason="symlinks without privileges are POSIX-only")
    def test_symlinked_store_maps_to_the_real_store_key(self, tmp_path: Path) -> None:
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        alias_dir = tmp_path / "alias"
        alias_dir.symlink_to(real_dir, target_is_directory=True)
        assert key_path_for(alias_dir / "c.db") == key_path_for(real_dir / "c.db")


class TestKeyedFramedDigest:
    KEY = bytes(range(32))

    def test_known_vector(self) -> None:
        subkey = hmac.new(self.KEY, b"domain-a", hashlib.sha256).digest()
        expected = hmac.new(subkey, b"1:a2:bc", hashlib.sha256).hexdigest()
        assert keyed_framed_digest(("a", "bc"), key=self.KEY, domain="domain-a") == expected

    def test_differs_from_the_unkeyed_digest(self) -> None:
        parts = ("s", "t", "{}")
        assert keyed_framed_digest(parts, key=self.KEY, domain="d") != framed_digest(parts)

    def test_varies_with_key_and_domain(self) -> None:
        parts = ("s", "t")
        base = keyed_framed_digest(parts, key=self.KEY, domain="d")
        assert keyed_framed_digest(parts, key=bytes(32), domain="d") != base
        assert keyed_framed_digest(parts, key=self.KEY, domain="e") != base

    def test_framing_stays_injective(self) -> None:
        assert keyed_framed_digest(("a\x00b", "c"), key=self.KEY, domain="d") != (
            keyed_framed_digest(("a", "b\x00c"), key=self.KEY, domain="d")
        )
        assert keyed_framed_digest(("ab", ""), key=self.KEY, domain="d") != (
            keyed_framed_digest(("a", "b"), key=self.KEY, domain="d")
        )

    def test_lone_surrogate_does_not_raise(self) -> None:
        assert len(keyed_framed_digest(("\ud800",), key=self.KEY, domain="d")) == 64


class TestCreateWritePath:
    def test_short_writes_still_publish_a_whole_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_write = os.write

        def _short_write(fd: int, data: bytes) -> int:
            return real_write(fd, bytes(data[:10]))

        monkeypatch.setattr(local_key.os, "write", _short_write)
        path = tmp_path / "s.db.key"
        key = load_or_create_key(path)
        monkeypatch.undo()
        assert load_key(path) == key

    def test_write_failure_leaves_nothing_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _full_disk(fd: int, data: bytes) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(local_key.os, "write", _full_disk)
        path = tmp_path / "s.db.key"
        with pytest.raises(LocalKeyError, match="No space"):
            load_or_create_key(path)
        assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_at_the_key_path_fails_instead_of_hanging(tmp_path: Path) -> None:
    path = tmp_path / "s.db.key"
    os.mkfifo(path)
    outcome: list[BaseException | bytes | None] = []

    def _load() -> None:
        try:
            outcome.append(load_or_create_key(path))
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=_load, daemon=True)
    worker.start()
    worker.join(timeout=5.0)
    assert not worker.is_alive(), "load_or_create_key blocked on a FIFO"
    assert len(outcome) == 1 and isinstance(outcome[0], LocalKeyError)
    assert "regular file" in str(outcome[0])


class TestPermissionHandling:
    def test_crlf_line_ending_is_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "s.db.key"
        path.write_bytes(b"ab" * 32 + b"\r\n")
        assert load_key(path) == bytes.fromhex("ab" * 32)

    @pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
    def test_a_non_key_file_keeps_its_mode(self, tmp_path: Path) -> None:
        other = tmp_path / "notes.txt"
        other.write_text("my notes\n")
        other.chmod(0o644)
        path = tmp_path / "s.db.key"
        path.symlink_to(other)
        with pytest.raises(LocalKeyError):
            load_key(path)
        assert stat.S_IMODE(other.stat().st_mode) == 0o644

    @pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
    def test_a_key_that_cannot_be_chmodded_still_loads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "s.db.key"
        key = load_or_create_key(path)
        path.chmod(0o644)

        def _read_only_mount(fd: int, mode: int) -> None:
            raise OSError(errno.EROFS, "Read-only file system")

        monkeypatch.setattr(local_key.os, "fchmod", _read_only_mount)
        assert load_key(path) == key
