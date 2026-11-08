"""Performance fixes in ``proxy/compression.py`` must not change output (#1348).

``_old_json_key_truncate`` is ``TruncateCompressor._json_key_truncate`` as it
was before the key-drop scan stopped assembling every candidate, copied
verbatim (bound to the compressor under test as ``self``) as the oracle.
"""

from __future__ import annotations

import json
import random

import pytest

from memtomem_stm.proxy import compression
from memtomem_stm.proxy.compression import (
    SelectiveCompressor,
    TruncateCompressor,
    _scores_or_none,
)
from memtomem_stm.proxy.relevance import BM25Scorer


def _old_json_key_truncate(
    self, data: dict, max_chars: int, context_query: str | None = None
) -> str:
    """Distribute budget across all top-level JSON keys.

    Each key gets a proportional share of the budget based on its
    serialized size. When context_query is provided, budget is weighted
    by BM25 relevance (blended with size-proportional allocation).
    """
    # Serialize each top-level key separately to measure sizes
    key_sizes: list[tuple[str, str, int]] = []
    for k, v in data.items():
        serialized = json.dumps({k: v}, ensure_ascii=False, indent=2)
        key_sizes.append((k, serialized, len(serialized)))

    total_size = sum(s for _, _, s in key_sizes)
    overhead = 10  # {}, commas, newlines
    available = max_chars - overhead

    # Compute query-aware weights if applicable
    relevance_weights: list[float] | None = None
    if context_query and context_query.strip():
        sections = [(k, ser) for k, ser, _ in key_sizes]
        relevance_weights = _scores_or_none(self._scorer, context_query, sections)

    # Build output: each key gets budget allocation
    parts: list[str] = []
    for idx, (k, serialized, size) in enumerate(key_sizes):
        # Size-proportional base
        size_share = available * size / total_size if total_size else available / len(key_sizes)
        if relevance_weights is not None:
            total_rel = sum(relevance_weights)
            rel_share = available * relevance_weights[idx] / total_rel if total_rel else 0
            # Blend: 40% size-proportional + 60% relevance
            key_budget = max(40, int(0.4 * size_share + 0.6 * rel_share))
        else:
            key_budget = max(40, int(size_share))

        if size <= key_budget:
            inner = serialized.strip()[1:-1].strip()
            parts.append(inner)
        else:
            v = data[k]
            truncated = self._truncate_json_value(v, max(0, key_budget - len(k) - 6))
            part = json.dumps({k: truncated}, ensure_ascii=False, indent=2)
            inner = part.strip()[1:-1].strip()
            parts.append(inner)

    # Assemble as valid JSON. The old path sliced the assembled string to
    # max_chars and appended a note — producing INVALID JSON and STILL
    # overshooting the budget. Instead drop whole trailing keys (recording
    # the count in a valid, collision-safe ``_truncated`` member) until the
    # object fits, so the result always parses and stays within budget.
    #
    # Contract floor: valid JSON cannot be shorter than ``{}`` (2 chars).
    # For any ``max_chars >= 2`` the result is ``<= max_chars``; at a
    # pathological sub-2-char budget (never produced by config or the
    # manager retention ladder, which raises tiny budgets) JSON validity
    # takes precedence and we still return ``{}``.
    key_items = list(data.items())
    marker_key = "_truncated"
    while marker_key in data:
        marker_key += "_"

    def _assemble(kept: list[str], omitted: int) -> str:
        members = list(kept)
        if omitted:
            members.append(f'"{marker_key}": "{omitted} of {len(parts)} keys omitted"')
        return "{\n" + ",\n".join(members) + "\n}"

    result = _assemble(parts, 0)
    if len(result) <= max_chars:
        return result

    def _render(idx: int, value_budget: int) -> str:
        k, v = key_items[idx]
        part = json.dumps(
            {k: self._truncate_json_value(v, value_budget)}, ensure_ascii=False, indent=2
        )
        return part.strip()[1:-1].strip()

    for keep in range(len(parts) - 1, -1, -1):
        candidate = _assemble(parts[:keep], len(parts) - keep)
        if len(candidate) > max_chars:
            continue
        # Refill the freed budget into the BOUNDARY key (the first dropped
        # one). Each part was sized against the FULL key set, so once
        # trailing keys are dropped the assembly could sit far below
        # ``max_chars`` and stay there no matter how much the budget grew
        # (the output used to freeze at the drop point — 66 chars from
        # max_chars=80 through 1500 on a one-huge-key payload). Grow the
        # boundary to the largest ``_truncate_json_value`` form that still
        # fits: the rendered length is monotone in the value budget, so
        # binary-search it (sound because ``_truncate_json_value`` never
        # expands a value, so the rendered length is monotone in the value
        # budget). Only ONE key is refilled: a full-form boundary would
        # re-create the ``keep + 1`` candidate this loop already rejected
        # with a smaller-or-equal boundary, so the boundary always stays
        # partial and no room is left for the keys after it.
        omitted = len(parts) - keep - 1
        lo, hi, best = 0, max_chars, None
        while lo <= hi:
            mid = (lo + hi) // 2
            cand_part = _render(keep, mid)
            if len(_assemble([*parts[:keep], cand_part], omitted)) <= max_chars:
                best, lo = cand_part, mid + 1
            else:
                hi = mid - 1
        if best is not None:
            return _assemble([*parts[:keep], best], omitted)
        return candidate
    return "{}"


def _value(rng: random.Random, depth: int = 0) -> object:
    kind = rng.randrange(6 if depth < 2 else 3)
    if kind == 0:
        return "v" * rng.choice([0, 1, 5, 40, 300, 2_000])
    if kind == 1:
        return rng.choice([0, 7, -3, 2.5, True, None])
    if kind == 2:
        return '\u00e9"\\' * rng.randrange(1, 30)  # escaping changes dumped length
    if kind == 3:
        return [_value(rng, depth + 1) for _ in range(rng.randrange(0, 6))]
    return {f"n{i}": _value(rng, depth + 1) for i in range(rng.randrange(0, 5))}


def _payload(rng: random.Random, keys: int) -> dict:
    data = {f"key{i}": _value(rng) for i in range(keys)}
    if rng.random() < 0.2:
        data["_truncated"] = "a real member the marker must not collide with"
    return data


# 9/10 and 99/100 keys move the marker's "<omitted> of <total>" across a digit.
_KEY_COUNTS = [1, 2, 3, 9, 10, 11, 37, 99, 100, 101]
_QUERIES = [None, "key3 summary v"]


def _cases(keys: int, query: str | None):
    """The ``(data, max_chars)`` pairs the differential test runs for one cell."""
    rng = random.Random(keys * 31 + (query is not None))
    for _ in range(25):
        data = _payload(rng, keys)
        full = len(json.dumps(data, ensure_ascii=False, indent=2))
        for max_chars in sorted({2, 3, 10, 40, 80, 200, full // 3, full // 2, full - 1, full + 5}):
            if max_chars >= 2:
                yield data, max_chars


def _compressor(query: str | None) -> TruncateCompressor:
    return TruncateCompressor(BM25Scorer() if query else None)


@pytest.mark.parametrize("keys", _KEY_COUNTS)
@pytest.mark.parametrize("query", _QUERIES)
def test_json_key_truncate_matches_the_assembling_scan(keys, query):
    for data, max_chars in _cases(keys, query):
        tc = _compressor(query)
        expected = _old_json_key_truncate(tc, data, max_chars, context_query=query)
        assert tc._json_key_truncate(data, max_chars, context_query=query) == expected


def test_the_differential_reaches_every_outcome():
    """Positive control over exactly the differential's cases: they reach the
    full object, a dropped-key result, and the empty-object floor."""
    outcomes = set()
    for keys in _KEY_COUNTS:
        for query in _QUERIES:
            for data, max_chars in _cases(keys, query):
                out = _old_json_key_truncate(_compressor(query), data, max_chars, query)
                if out == "{}":
                    outcomes.add("empty")
                elif "keys omitted" in out:
                    outcomes.add("dropped")
                else:
                    outcomes.add("kept")
    assert outcomes == {"empty", "dropped", "kept"}


def test_dropping_keys_still_refills_the_boundary_key():
    """The scan's one assembled candidate still feeds the boundary refill: a
    one-huge-key payload grows with the budget instead of freezing at the drop
    point (66 chars from ``max_chars=80`` through 1500 before the refill)."""
    data = {"huge": "x" * 5_000, "small": "y"}
    tc = TruncateCompressor()

    out = tc._json_key_truncate(data, 1_500)

    assert out == _old_json_key_truncate(tc, data, 1_500)
    assert "keys omitted" in out, "positive control: a key was dropped"
    assert 1_400 < len(out) <= 1_500, len(out)


def test_selective_infers_each_entry_type_once(monkeypatch):
    """The preview search rebuilds the TOC several times; parsing each JSON
    chunk on every rebuild is what made it slow (#1348)."""
    payload = json.dumps(
        [
            {"id": i, "name": "x" * 50, "tags": ["a"] * 10, "nested": {"k": list(range(30))}}
            for i in range(200)
        ]
    )
    calls: list[str] = []
    real = SelectiveCompressor._infer_type

    def counting(self, key, content, fmt):
        calls.append(key)
        return real(self, key, content, fmt)

    monkeypatch.setattr(SelectiveCompressor, "_infer_type", counting)
    monkeypatch.setattr(compression, "delivery_format", lambda: "legacy")
    out = SelectiveCompressor().compress(payload, max_chars=3_000)

    toc = json.loads(out)
    assert len(toc["entries"]) == 200
    assert len(calls) == 200, len(calls)
    assert {entry["type"] for entry in toc["entries"]} == {"object"}
