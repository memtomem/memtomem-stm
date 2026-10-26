"""PKCS#8 private keys are credentials to every privacy gate (GHSA-mf67-ghgq-cv54).

The PEM rule only knew algorithm-labelled headers (``BEGIN RSA PRIVATE KEY``),
so a PKCS#8 key — what OpenSSL 3 ``genrsa``/``genpkey`` write by default, and the
only PEM form an Ed25519 key has — reached hosted compression and extraction
and was cached, indexed and logged. Each gate inherits the shared pattern set;
these tests pin that every one of them now refuses the key. Keys are generated
per run, so no key material sits in the tree.
"""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
import subprocess
import sys
import textwrap
from functools import cache
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from memtomem_stm.proxy import cache as cache_module
from memtomem_stm.proxy import compression as compression_mod
from memtomem_stm.proxy import privacy
from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.config import (
    AutoIndexConfig,
    CompressionStrategy,
    ExtractionConfig,
    LLMCompressorConfig,
    LLMProvider,
)
from memtomem_stm.proxy.extraction import ExtractedFact, FactExtractor
from memtomem_stm.proxy.memory_ops import auto_index_response, extract_and_store
from memtomem_stm.proxy.protocols import IndexResult
from memtomem_stm.proxy.tool_eligibility import REASON_SENSITIVE_METADATA, filter_tools
from test_privacy_jwt_linear import _CHILD
from test_proxy_manager_pipeline import _make_manager
from test_selection_log import _make_log, _read_events
from test_tool_eligibility import STRICT, _cand, _server_cfg

_ENCRYPTIONS = {
    "plain": ser.NoEncryption(),
    "encrypted": ser.BestAvailableEncryption(b"disposable"),
}


@cache
def _key(kind: str):
    if kind == "rsa":
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if kind == "ec":
        return ec.generate_private_key(ec.SECP256R1())
    return ed25519.Ed25519PrivateKey.generate()


@cache
def _pkcs8(kind: str = "ed25519", encryption: str = "plain") -> str:
    return (
        _key(kind)
        .private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, _ENCRYPTIONS[encryption])
        .decode()
    )


_KEYS = [(k, e) for k in ("rsa", "ec", "ed25519") for e in _ENCRYPTIONS]
_KEY_IDS = [f"{k}-{e}" for k, e in _KEYS]


def _response(pem: str) -> str:
    # Long enough to need compressing, with the key in the middle.
    return ("deploy notes " * 80) + "\n" + pem + "\n" + ("rollout log " * 80)


@pytest.mark.parametrize(("kind", "encryption"), _KEYS, ids=_KEY_IDS)
def test_a_generated_pkcs8_key_is_a_credential(kind, encryption):
    assert privacy.contains_sensitive_content(_pkcs8(kind, encryption), privacy.CREDENTIAL_PATTERNS)


@pytest.mark.parametrize(
    ("prefix", "dump"),
    [
        ("The service account credential for the staging deployment is ", yaml.safe_dump),
        ("스테이징 배포용 서비스 계정 키는 다음과 같다 ", yaml.safe_dump),
        ("key: ", json.dumps),
    ],
    ids=["yaml-plain", "yaml-double-quoted", "json"],
)
def test_a_serialized_key_is_a_credential_wherever_the_line_folds(prefix, dump):
    pem = _pkcs8()
    for pad in range(0, 40):
        value = " " * pad + prefix + pem
        serialized = dump({"note": value})
        loaded = (yaml.safe_load if dump is yaml.safe_dump else json.loads)(serialized)
        assert loaded["note"] == value
        assert privacy.contains_sensitive_content(serialized, privacy.CREDENTIAL_PATTERNS), (
            f"missed at pad={pad}: {serialized[:120]!r}"
        )


def _certificate() -> str:
    key = _key("ec")
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "example.invalid")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.datetime(2026, 1, 1))
        .not_valid_after(datetime.datetime(2027, 1, 1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(ser.Encoding.PEM).decode()


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            lambda: (
                _key("ed25519")
                .public_key()
                .public_bytes(ser.Encoding.PEM, ser.PublicFormat.SubjectPublicKeyInfo)
                .decode()
            ),
            id="public-key",
        ),
        pytest.param(_certificate, id="certificate"),
        pytest.param(lambda: "Rotate first, then begin private key rotation.", id="prose"),
        pytest.param(lambda: "The scan misses BEGIN PRIVATE KEY headers.", id="prose-caps"),
    ],
)
def test_public_material_and_prose_are_not_credentials(text):
    assert not privacy.contains_sensitive_content(text())


# Shapes aimed at the new rule: a start at every header, and one long run of
# separators that never reaches PRIVATE.
PEM_STALL_CASES = {
    "repeated-begin": "'-----BEGIN ' * 90_000",
    "long-separator-run": "'-----BEGIN' + ' \\\\' * 500_000",
}


@pytest.mark.parametrize("case", sorted(PEM_STALL_CASES))
def test_crafted_pem_input_does_not_stall(case):
    code = textwrap.dedent(_CHILD).format(payload=PEM_STALL_CASES[case])
    try:
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{case}: still scanning after 60 s", pytrace=False)
    assert done.returncode == 0, f"{case}: {done.stdout.strip()} {done.stderr.strip()[-500:]}"


def _openai_transport(requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Summary."}}]})

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("encryption", [None, *_ENCRYPTIONS], ids=["control", *_ENCRYPTIONS])
async def test_hosted_compression_sends_no_request_for_a_key(tmp_path, monkeypatch, encryption):
    # The manager's default-on gate, through the real compressor and its real
    # HTTP client: only the transport is replaced. privacy_scan_enabled is left
    # at its default. The control proves the request path is reachable.
    requests: list[httpx.Request] = []
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        compression_mod.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=_openai_transport(requests), **kw),
    )
    mgr = _make_manager(tmp_path=tmp_path)
    llm_cfg = LLMCompressorConfig(provider=LLMProvider.OPENAI, api_key="k")
    body = _pkcs8("rsa", encryption) if encryption else "no credentials in this one"
    text = _response(body)
    result, fallback = await mgr._apply_compression(
        text,
        CompressionStrategy.LLM_SUMMARY,
        max_chars=200,
        sel_cfg=None,
        llm_cfg=llm_cfg,
        hybrid_cfg=None,
        server="srv",
        tool="read_key",
        cfg_snap=mgr._config,
    )
    if encryption is None:
        assert len(requests) == 1
        assert fallback is None
    else:
        assert requests == []
        assert fallback == "privacy"
        assert len(result) <= len(text)


@pytest.mark.parametrize("with_key", [False, True], ids=["control", "key"])
async def test_hosted_extraction_runs_locally(with_key):
    extractor = FactExtractor(ExtractionConfig(enabled=True, min_response_chars=10))
    text = "Decision: rotate the key now.\n" + (_pkcs8() if with_key else "x" * 60)
    with patch.object(extractor, "_call_api", new_callable=AsyncMock, return_value="[]") as call:
        await extractor.extract(text, server="s", tool="t")
    if with_key:
        call.assert_not_awaited()
    else:
        call.assert_awaited()


def _open(path: Path) -> ProxyCache:
    store = ProxyCache(path, max_entries=100)
    store.initialize()
    return store


def _rows(path: Path) -> dict[str, str]:
    db = sqlite3.connect(str(path))
    try:
        return dict(db.execute("SELECT tool, result FROM proxy_cache").fetchall())
    finally:
        db.close()


def _stamp(path: Path) -> str:
    db = sqlite3.connect(str(path))
    try:
        return db.execute(
            "SELECT value FROM proxy_cache_meta WHERE key = ?",
            (cache_module._PRIVACY_POLICY_FINGERPRINT_KEY,),
        ).fetchone()[0]
    finally:
        db.close()


def _vulnerable_patterns() -> list[str]:
    header = "-----BEGIN PRIVATE KEY-----"
    vulnerable = [p for p in privacy.DEFAULT_PATTERNS if not re.search(p, header)]
    assert len(vulnerable) == len(privacy.DEFAULT_PATTERNS) - 1
    return vulnerable


@pytest.mark.parametrize("encryption", list(_ENCRYPTIONS))
def test_the_cache_refuses_to_store_a_key(tmp_path, encryption):
    store = _open(tmp_path / "c.db")
    try:
        store.set("s", "read_key", {}, _response(_pkcs8("ec", encryption)), ttl_seconds=60)
        assert store.stats()["total_entries"] == 0
    finally:
        store.close()


def test_the_cache_evicts_a_key_on_read(tmp_path, monkeypatch):
    # A row written after this build opened the store, by a writer still on
    # the old rule set (an older build sharing the file): the startup repair
    # has already run, so only the read-side check can refuse it.
    path = tmp_path / "c.db"
    store = _open(path)
    try:
        with monkeypatch.context() as old_writer:
            old_writer.setattr(privacy, "DEFAULT_PATTERNS", _vulnerable_patterns())
            store.set("s", "read_key", {}, _response(_pkcs8()), ttl_seconds=None)
        assert set(_rows(path)) == {"read_key"}
        assert store.get("s", "read_key", {}) is None
        assert _rows(path) == {}
    finally:
        store.close()


def test_a_key_cached_before_the_fix_is_swept_on_upgrade(tmp_path, monkeypatch):
    # The new rule changes DEFAULT_PATTERNS, so it changes the policy
    # fingerprint, and the first open under it repairs rows the old set
    # accepted. No epoch bump is needed for a pattern addition. Checked
    # straight after the open, before any read could evict the row instead.
    path = tmp_path / "c.db"
    with monkeypatch.context() as old_build:
        old_build.setattr(privacy, "DEFAULT_PATTERNS", _vulnerable_patterns())
        old_stamp = cache_module._privacy_policy_fingerprint()
        seed = _open(path)
        try:
            seed.set("s", "read_key", {}, _response(_pkcs8()), ttl_seconds=None)
            seed.set("s", "clean", {}, "nothing to see here", ttl_seconds=None)
        finally:
            seed.close()
    assert set(_rows(path)) == {"read_key", "clean"}
    assert _stamp(path) == old_stamp

    _open(path).close()
    assert _rows(path) == {"clean": "nothing to see here"}
    assert _stamp(path) == cache_module._privacy_policy_fingerprint() != old_stamp


class _RecordingIndexer:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    async def index_file(self, path: Path, *, force: bool = False, namespace=None):
        self.paths.append(path)
        return IndexResult(indexed_chunks=1)


def _written(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


@pytest.mark.parametrize("with_key", [False, True], ids=["control", "key"])
async def test_auto_index_does_not_write_or_index_a_key(tmp_path, with_key):
    indexer = _RecordingIndexer()
    root = tmp_path / "idx"
    outcome = await auto_index_response(
        indexer,  # type: ignore[arg-type]
        AutoIndexConfig(enabled=True, min_chars=10, memory_dir=root),
        server="srv",
        tool="read_key",
        arguments={},
        text=_response(_pkcs8() if with_key else "no credentials in this one"),
        agent_summary="summary",
    )
    assert outcome.ok is True
    if with_key:
        assert indexer.paths == [] and _written(root) == []
    else:
        assert len(indexer.paths) == 1 and len(_written(root)) == 1


class _FixedExtractor:
    def __init__(self, content: str) -> None:
        self.content = content

    async def extract(self, text: str, **_: object) -> list[ExtractedFact]:
        return [ExtractedFact(content=self.content, category="fact", confidence=0.9)]


@pytest.mark.parametrize(
    ("text", "fact", "stored"),
    [
        ("Decision: rotate the key now.", "Rotate the key now.", True),
        ("Decision: rotate the key now.\n" + _pkcs8(), "Rotate the key now.", False),
        # Clean input whose extractor writes a key into the fact: the second
        # scan, over the fact files about to be written, refuses it.
        ("Decision: rotate the key now.", "The key is " + _pkcs8(), False),
    ],
    ids=["control", "key-in-input", "key-in-extracted-fact"],
)
async def test_extraction_does_not_write_or_index_a_key(tmp_path, monkeypatch, text, fact, stored):
    indexer = _RecordingIndexer()
    root = tmp_path / "facts"
    cfg = ExtractionConfig(enabled=True, memory_dir=root, dedup_threshold=0)
    outcome = await extract_and_store(
        indexer,  # type: ignore[arg-type]
        _FixedExtractor(fact),  # type: ignore[arg-type]
        cfg,
        server="srv",
        tool="read_key",
        arguments={},
        text=text,
    )
    assert outcome.ok is True
    if stored:
        assert outcome.facts_stored == 1 and len(indexer.paths) == 1
    else:
        assert outcome.facts_stored == 0
        assert indexer.paths == [] and _written(root) == []


def test_a_tool_whose_metadata_carries_a_key_is_rejected_under_strict():
    candidate = _cand("a", _server_cfg(), raw_desc="Example credential: " + _pkcs8())
    result = filter_tools([candidate], STRICT)
    assert result.eligible == []
    assert result.reject_reasons == {"test__a": REASON_SENSITIVE_METADATA}


def test_selection_telemetry_drops_a_record_carrying_a_key(tmp_path):
    # Telemetry carries metadata, not response bodies; the backstop drops a
    # record whose metadata (here a tool name) carries a key header.
    log = _make_log(tmp_path)
    log.log_selection(
        server="srv",
        selected_tool="test__-----BEGIN PRIVATE KEY-----",
        candidate_tools=[],
        arguments={},
        trace_id=None,
    )
    assert _read_events(log) == []
    assert log.redaction_drops == 1
