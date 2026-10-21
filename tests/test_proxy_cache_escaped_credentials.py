"""The response cache sees credentials hidden behind JSON string escaping (#1211).

Tool results often carry a JSON document inside a JSON string, so every quote
around a label is escaped and the raw text matches no privacy pattern. The
cache's rule decodes JSON escapes before scanning; a false positive costs one
uncached response.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memtomem_stm.proxy import cache as cache_module
from memtomem_stm.proxy import privacy
from memtomem_stm.proxy.cache import ProxyCache, _privacy_policy_fingerprint

SECRET = "hunter2SECRET"
_ESCAPED_KEY = '{"\\u0070assword": "%s"}' % SECRET

HIDDEN = {
    "escaped_key": _ESCAPED_KEY,
    "escaped_value": '{"cfg": "password \\u003d %s"}' % SECRET,
    "json_in_json": json.dumps({"text": json.dumps({"password": SECRET})}),
    "json_in_json_nested_aws": json.dumps({"text": json.dumps({"db": {"session_token": SECRET}})}),
    "newline_before_aws_label": json.dumps({"cfg": "x\nsession_token=" + SECRET}),
    "three_levels": json.dumps({"a": json.dumps({"text": json.dumps({"password": SECRET})})}),
    # Decoding has not finished after the pass limit: refused without a match.
    "escaped_backslash_chain": '{"\\u005cu005cu005cu0070assword": "%s"}' % SECRET,
    "escaped_key_in_three_wrappers": json.dumps(
        {"a": json.dumps({"b": json.dumps({"c": _ESCAPED_KEY})})}
    ),
}

CACHED = {
    "plain_escapes": json.dumps({"msg": 'he said "hi"\nthen left\ttabbed', "path": "C:\\tmp"}),
    "json_in_json_benign": json.dumps({"text": json.dumps({"status": "ok", "count": 3})}),
}


def _open(path: Path) -> ProxyCache:
    cache = ProxyCache(path, max_entries=100)
    cache.initialize()
    return cache


def _rows(path: Path) -> int:
    db = sqlite3.connect(str(path))
    try:
        return db.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0]
    finally:
        db.close()


@pytest.mark.parametrize("name", sorted(HIDDEN))
def test_decoded_scan_detects(name):
    assert not privacy.contains_sensitive_content(HIDDEN[name])
    assert privacy.contains_sensitive_content_decoded(HIDDEN[name])


@pytest.mark.parametrize("name", sorted(CACHED))
def test_decoded_scan_passes_benign_escapes(name):
    assert not privacy.contains_sensitive_content_decoded(CACHED[name])


@pytest.mark.parametrize("name", sorted(HIDDEN))
def test_set_refuses(tmp_path, name):
    cache = _open(tmp_path / "c.db")
    try:
        cache.set("s", name, {}, HIDDEN[name], ttl_seconds=60)
        assert cache.stats()["total_entries"] == 0
    finally:
        cache.close()


@pytest.mark.parametrize("name", sorted(CACHED))
def test_set_keeps_benign(tmp_path, name):
    cache = _open(tmp_path / "c.db")
    try:
        cache.set("s", name, {}, CACHED[name], ttl_seconds=60)
        assert cache.get("s", name, {}) == CACHED[name]
    finally:
        cache.close()


def _raw_rule(text: str) -> bool:
    return privacy.contains_sensitive_content(text)


def test_get_evicts_a_row_written_under_the_raw_rule(tmp_path, monkeypatch):
    """The read-side guard applies the same rule as ``set()``."""
    cache = _open(tmp_path / "c.db")
    try:
        monkeypatch.setattr(cache_module, "_text_is_sensitive", _raw_rule)
        cache.set("s", "t", {}, HIDDEN["json_in_json"], ttl_seconds=60)
        assert cache.stats()["total_entries"] == 1
        monkeypatch.undo()
        assert cache.get("s", "t", {}) is None
        assert cache.stats()["total_entries"] == 0
    finally:
        cache.close()


def test_upgrade_sweeps_rows_the_previous_rule_accepted(tmp_path, monkeypatch):
    """A row an epoch-2 build cached as clean is dequeued under that build's
    stamp, so only the epoch bump makes the next start sweep it."""
    path = tmp_path / "c.db"
    with monkeypatch.context() as old_build:
        old_build.setattr(cache_module, "_PRIVACY_POLICY_EPOCH", 2)
        old_build.setattr(cache_module, "_text_is_sensitive", _raw_rule)
        seed = _open(path)
        try:
            seed.set("s", "t", {}, HIDDEN["json_in_json"], ttl_seconds=None)
            queued = seed._db.execute(
                "SELECT COUNT(*) FROM proxy_cache_privacy_unverified"
            ).fetchone()[0]
        finally:
            seed.close()
    assert _rows(path) == 1
    assert queued == 0

    _open(path).close()
    assert _rows(path) == 0
    db = sqlite3.connect(str(path))
    try:
        stamp = db.execute(
            "SELECT value FROM proxy_cache_meta WHERE key = ?",
            (cache_module._PRIVACY_POLICY_FINGERPRINT_KEY,),
        ).fetchone()[0]
    finally:
        db.close()
    assert stamp == _privacy_policy_fingerprint()
