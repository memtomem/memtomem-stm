"""Tests for ``memtomem_stm.utils.fileio.atomic_write_text``.

Codifies the contract the proxy depends on: callers (CLI ``_save``,
``memory_ops.auto_index_response`` and ``extract_and_store``) need both
crash-safety (no partial file ever observable on the target path) and
proper cleanup of the temp file on failure.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from memtomem_stm.utils.fileio import _WIN_REPLACE_ATTEMPTS, atomic_write_text


class TestAtomicWriteText:
    def test_basic_write(self, tmp_path: Path):
        target = tmp_path / "out.txt"
        atomic_write_text(target, "hello")
        assert target.read_text(encoding="utf-8") == "hello"

    def test_overwrite_existing(self, tmp_path: Path):
        target = tmp_path / "out.txt"
        target.write_text("old", encoding="utf-8")
        atomic_write_text(target, "new")
        assert target.read_text(encoding="utf-8") == "new"

    def test_unicode_round_trip(self, tmp_path: Path):
        target = tmp_path / "out.txt"
        payload = "한글 + emoji 🚀 + accents éàü"
        atomic_write_text(target, payload)
        assert target.read_text(encoding="utf-8") == payload

    def test_mode_applied(self, tmp_path: Path):
        target = tmp_path / "secret.json"
        atomic_write_text(target, "{}", mode=0o600)
        # Bottom 9 bits = permission bits.
        assert (target.stat().st_mode & 0o777) == 0o600

    def test_no_mode_inherits_mkstemp_default(self, tmp_path: Path):
        """When ``mode`` is None we don't ``chmod`` — the file keeps the
        mode ``tempfile.mkstemp`` assigned, which is ``0o600`` on POSIX.
        This is intentional: even non-sensitive callers (e.g.
        ``memory_ops``) get a private file by default rather than a
        permissive one. Document the contract here so the next person
        reading "mode=None" doesn't assume it means "system default"."""
        target = tmp_path / "out.txt"
        atomic_write_text(target, "x")
        # On POSIX, ``mkstemp`` always opens the file at 0o600. We only
        # assert the upper bits aren't more permissive — a future change
        # that loosens the default would trip this.
        bits = target.stat().st_mode & 0o077
        assert bits == 0, f"unexpected world/group bits: {oct(bits)}"

    def test_ensure_parent_creates_missing_dirs(self, tmp_path: Path):
        target = tmp_path / "deep" / "nested" / "file.txt"
        atomic_write_text(target, "x")
        assert target.read_text(encoding="utf-8") == "x"

    def test_ensure_parent_false_requires_existing_parent(self, tmp_path: Path):
        target = tmp_path / "missing-dir" / "file.txt"
        with pytest.raises(FileNotFoundError):
            atomic_write_text(target, "x", ensure_parent=False)

    def test_replace_failure_leaves_target_untouched_and_cleans_temp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """If ``os.replace`` raises mid-write (simulated disk full), the
        original file must be intact and no leftover ``.tmp`` may remain."""
        target = tmp_path / "out.txt"
        target.write_text("original", encoding="utf-8")

        def boom(*args, **kwargs):
            raise OSError("simulated disk full")

        monkeypatch.setattr("memtomem_stm.utils.fileio.os.replace", boom)

        with pytest.raises(OSError, match="simulated disk full"):
            atomic_write_text(target, "new")

        assert target.read_text(encoding="utf-8") == "original"
        leftovers = list(target.parent.glob(target.name + ".*.tmp"))
        assert leftovers == [], f"tempfile not cleaned up: {leftovers}"

    def test_concurrent_reader_never_sees_partial(self, tmp_path: Path):
        """A reader looping ``read_text`` while a writer flips the same path
        between two distinct payloads must only ever observe a complete
        payload, never a truncation or in-flight write. This is the actual
        property the indexer/hot-reload watcher relies on."""
        target = tmp_path / "race.txt"
        small = "small"
        big = "B" * 50_000  # large enough that a non-atomic write would split
        target.write_text(small, encoding="utf-8")

        seen: list[str] = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    seen.append(target.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    pass

        t = threading.Thread(target=reader)
        t.start()
        try:
            for _ in range(40):
                atomic_write_text(target, big)
                atomic_write_text(target, small)
        finally:
            stop.set()
            t.join()

        # Every observation must be one of the two complete payloads.
        bad = [s for s in seen if s not in (small, big)]
        assert not bad, f"observed {len(bad)} partial reads, e.g. len={len(bad[0])}"

    def test_windows_retries_transient_permission_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """On Windows, ``MoveFileEx`` raises ``PermissionError`` (``WinError 5``)
        when a concurrent reader holds a transient handle on the destination.
        The retry loop must ride out a short burst of failures and succeed
        without surfacing the error to the caller. Pinned cross-platform by
        forcing ``sys.platform == "win32"`` so the POSIX CI leg also exercises
        the retry path."""
        monkeypatch.setattr("memtomem_stm.utils.fileio.sys.platform", "win32")

        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise PermissionError(5, "Access is denied", str(dst))
            real_replace(src, dst)

        monkeypatch.setattr("memtomem_stm.utils.fileio.os.replace", flaky)

        target = tmp_path / "out.txt"
        atomic_write_text(target, "payload")

        assert target.read_text(encoding="utf-8") == "payload"
        assert calls["n"] == 4, f"expected 3 retries + 1 success, got {calls['n']} calls"

    def test_windows_retry_gives_up_after_bounded_attempts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Bound on the retry loop — if ``os.replace`` keeps failing with
        ``PermissionError``, surface it to the caller rather than stalling
        forever. The exact attempt count is sourced from
        ``_WIN_REPLACE_ATTEMPTS`` so a future tuning of the budget keeps
        this test honest."""
        monkeypatch.setattr("memtomem_stm.utils.fileio.sys.platform", "win32")

        calls = {"n": 0}

        def always_locked(src, dst):
            calls["n"] += 1
            raise PermissionError(5, "Access is denied", str(dst))

        monkeypatch.setattr("memtomem_stm.utils.fileio.os.replace", always_locked)

        target = tmp_path / "out.txt"
        with pytest.raises(PermissionError):
            atomic_write_text(target, "payload")

        assert calls["n"] == _WIN_REPLACE_ATTEMPTS, (
            f"expected {_WIN_REPLACE_ATTEMPTS} attempts, got {calls['n']}"
        )
        # Temp must still be cleaned up despite the exhaustion path.
        assert list(tmp_path.glob(target.name + ".*.tmp")) == []


class TestSaveProxyConfigStillAtomic:
    """Regression for PR #115: the CLI's ``_save`` was migrated to use
    ``atomic_write_text`` — the externally-observable behaviour
    (``stm_proxy.json`` ends up with mode 0o600 and JSON content) must
    not have drifted.
    """

    def test_save_writes_json_at_mode_0o600(self, tmp_path: Path):
        from memtomem_stm.cli.proxy import _save

        target = tmp_path / "stm_proxy.json"
        _save(target, {"enabled": True, "upstream_servers": {"x": {"prefix": "x"}}})

        assert (target.stat().st_mode & 0o777) == 0o600
        assert "upstream_servers" in target.read_text(encoding="utf-8")
        # No leftover tempfile.
        assert list(target.parent.glob("stm_proxy.json.*.tmp")) == []

    def test_save_overwrite_does_not_duplicate(self, tmp_path: Path):
        from memtomem_stm.cli.proxy import _save

        target = tmp_path / "stm_proxy.json"
        _save(target, {"enabled": True, "upstream_servers": {}})
        _save(target, {"enabled": False, "upstream_servers": {"y": {"prefix": "y"}}})

        body = target.read_text(encoding="utf-8")
        assert '"enabled": false' in body
        assert '"y"' in body
        # First payload is fully replaced (no merged JSON, no leftover keys).
        assert '"enabled": true' not in body

    def test_save_failure_preserves_old_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from memtomem_stm.cli.proxy import _save

        target = tmp_path / "stm_proxy.json"
        _save(target, {"enabled": True, "upstream_servers": {}})
        original = target.read_text(encoding="utf-8")

        monkeypatch.setattr(
            "memtomem_stm.utils.fileio.os.replace",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )

        with pytest.raises(OSError):
            _save(target, {"enabled": False, "upstream_servers": {"z": {"prefix": "z"}}})

        assert target.read_text(encoding="utf-8") == original


class TestMemoryOpsUsesAtomicWrite:
    """``auto_index_response`` and ``extract_and_store`` write Markdown
    files that the auto-indexer immediately reads — a partial file would
    poison the index. Verify both paths now route through
    ``atomic_write_text`` (no leftover ``.tmp`` fragments after a normal
    write, no truncated-then-flushed window)."""

    @pytest.mark.asyncio
    async def test_auto_index_response_writes_atomically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from memtomem_stm.proxy.config import AutoIndexConfig
        from memtomem_stm.proxy.memory_ops import auto_index_response

        # Stub indexer
        class _Stats:
            indexed_chunks = 0

        class _Indexer:
            async def index_file(self, *_a, **_k):
                return _Stats()

        cfg = AutoIndexConfig(
            enabled=True,
            memory_dir=tmp_path / "mem",
            namespace="ns/{server}/{tool}",
        )
        (tmp_path / "mem").mkdir()

        await auto_index_response(
            index_engine=_Indexer(),
            ai_cfg=cfg,
            server="srv",
            tool="t",
            arguments={"q": "x"},
            text="payload",
            agent_summary="ok",
        )

        files = list((tmp_path / "mem").iterdir())
        assert any(f.suffix == ".md" for f in files), files
        # No tempfile residue from the atomic write.
        assert not any(f.name.endswith(".tmp") for f in files), files
