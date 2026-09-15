"""Benchmark retention rounding and per-call truncation boundaries."""

import math

import pytest

from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.compression import (
    SchemaPruningCompressor,
    SkeletonCompressor,
    TruncateCompressor,
)
from memtomem_stm.proxy.config import CleaningConfig

from bench.harness import BenchHarness, BenchTask


@pytest.mark.parametrize(
    "text",
    ["word " * 6000, "This sentence has useful context. " * 1000, "x" * 30001],
    ids=["word-boundary", "sentence-boundary", "fractional-floor"],
)
@pytest.mark.parametrize(
    "compressor_type", [TruncateCompressor, SchemaPruningCompressor, SkeletonCompressor]
)
@pytest.mark.parametrize("mode", ["plain", "query", "surfacing"])
async def test_benchmark_retention_boundaries(text, mode, compressor_type):
    compressor = compressor_type()
    harness = BenchHarness(
        cleaner=DefaultContentCleaner(CleaningConfig(enabled=False)),
        compressor=compressor,
    )
    task = BenchTask("retention", "boundary regression", text, "text", 1000)
    if mode == "surfacing":
        results = [await harness.run_stm_with_surfacing(task)]
    elif mode == "query":
        task.context_query = "useful context"
        comparison = harness.run_query_aware_comparison(task)
        results = [comparison.direct, comparison.stm]
    else:
        results = [harness.run_stm(task)]
    for result in results:
        assert result.error is None
        assert math.ceil(len(text) * 0.5) <= len(result.text) < len(text)

    # A prior large response must not raise the floor of this shared instance.
    assert compressor._min_chars == 0
    assert len(compressor.compress("word " * 1000, max_chars=1000)) <= 1000


def test_retention_minimum_does_not_replace_larger_budget():
    assert BenchHarness._apply_retention(30001, 20000) == 20000
    assert BenchHarness._apply_retention(30001, 0) == 15001
