"""Bench oracle identities must match STM's compact result contract."""

import pytest

from bench.bench_qa.identity import compact_seed_id
from memtomem_stm.surfacing.mcp_client import RemoteSearchResult


@pytest.mark.parametrize(
    "namespace,source", [("default", "a.md"), ("team", "a.md"), ("default", "b.md")]
)
def test_seed_identity_matches_compact_result(namespace, source):
    seed = {"namespace": namespace, "source": source, "content": "한글 😀 same content"}
    actual = RemoteSearchResult(seed["content"], 0.5, source, namespace).chunk.id
    assert compact_seed_id(seed) == actual


def test_seed_default_namespace_and_cross_source_ids():
    seed = {"source": "a.md", "content": "same content"}
    assert compact_seed_id(seed) == compact_seed_id({**seed, "namespace": "default"})
    assert compact_seed_id(seed) != compact_seed_id({**seed, "source": "b.md"})
    assert compact_seed_id(seed) != compact_seed_id({**seed, "namespace": "team"})
