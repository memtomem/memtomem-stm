"""Compression fitting steps run in time linear in the response.

HYBRID's TOC fitting, SELECTIVE's repeated-heading keys and TRUNCATE's
tail-anomaly path each did quadratic work in the size of an upstream response,
so a crafted one held the event loop for seconds to minutes. The rewrites must
return exactly what the old code returned — checked against the old code, kept
here as the oracle — and must finish on the inputs that used to stall, checked
in a child process so a regression fails instead of hanging the suite.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import sys
import textwrap

import pytest

from memtomem_stm.proxy.compression import HybridCompressor, SelectiveCompressor, TruncateCompressor

# ── The pre-fix code, as the oracle ──────────────────────────────────────────


def old_fit_toc_tail(tail: str, max_chars: int, source_text: str) -> str | None:
    if max_chars < 2:
        return None

    try:
        parsed = json.loads(tail)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or parsed.get("type") != "toc":
        return None

    def dumps(obj: object) -> str:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

    def fill_preview(toc: dict) -> str:
        candidate = dumps(toc)
        if len(candidate) >= max_chars or not source_text:
            return candidate
        lo, hi = 0, len(source_text)
        best = candidate
        while lo <= hi:
            mid = (lo + hi) // 2
            probe_toc = dict(toc)
            probe_toc["tail_preview"] = source_text[:mid]
            probe = dumps(probe_toc)
            if len(probe) <= max_chars:
                best = probe
                lo = mid + 1
            else:
                hi = mid - 1
        if len(best) < max_chars:
            # JSON permits trailing whitespace. Use it only as a final
            # single-budget filler so the retention guard does not reject an
            # otherwise valid TOC for being one or two chars below the floor.
            best += " " * (max_chars - len(best))
        return best

    candidate = dumps(parsed)
    if len(candidate) <= max_chars:
        return fill_preview(parsed)

    toc = dict(parsed)
    entries = toc.get("entries")
    if isinstance(entries, list):
        toc["entries"] = [dict(e) if isinstance(e, dict) else e for e in entries]
        for entry in reversed(toc["entries"]):
            if not isinstance(entry, dict):
                continue
            preview = entry.get("preview")
            if not isinstance(preview, str) or not preview:
                continue
            original = preview
            lo, hi = 0, len(original)
            best: str | None = None
            while lo <= hi:
                mid = (lo + hi) // 2
                entry["preview"] = original[:mid] + ("..." if mid < len(original) else "")
                probe = dumps(toc)
                if len(probe) <= max_chars:
                    best = entry["preview"]
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best is not None:
                entry["preview"] = best
                return fill_preview(toc)
            entry["preview"] = "..."
            candidate = dumps(toc)
            if len(candidate) <= max_chars:
                return fill_preview(toc)

    hint = toc.get("hint")
    selection_key = toc.get("selection_key")
    if isinstance(hint, str) and selection_key:
        toc["hint"] = f"select_chunks key={selection_key} sections=[...]"
        candidate = dumps(toc)
        if len(candidate) <= max_chars:
            return fill_preview(toc)

    if isinstance(entries, list):
        while toc["entries"]:
            original_count = len(entries)
            toc["entries"] = toc["entries"][:-1]
            omitted = original_count - len(toc["entries"])
            toc["truncated_entries"] = omitted
            candidate = dumps(toc)
            if len(candidate) <= max_chars:
                return fill_preview(toc)

    toc["entries"] = []
    toc["truncated_entries"] = len(entries) if isinstance(entries, list) else 0
    candidate = dumps(toc)
    return fill_preview(toc) if len(candidate) <= max_chars else None


def old_tail_anomaly_truncate(cls, text: str, max_chars: int) -> str | None:
    """Detect highly repetitive content and preserve tail anomaly.

    If >50% of lines match a repeated pattern (after stripping timestamps)
    and the tail differs, keep a sample + full tail anomaly.
    Returns None if content is not repetitive.
    """
    lines = text.split("\n")
    if len(lines) < 10:
        return None

    # Fingerprint: strip timestamps/numbers, keep structure
    def _fp(line: str) -> str:
        stripped = cls._TIMESTAMP_RE.sub("", line.strip())
        # Also normalize varying numbers (latency, counts)
        stripped = re.sub(r"\d+", "#", stripped)
        return stripped[:40]

    fingerprints: dict[str, int] = {}
    for line in lines:
        fp = _fp(line)
        if fp:
            fingerprints[fp] = fingerprints.get(fp, 0) + 1

    if not fingerprints:
        return None

    top_fp, top_count = max(fingerprints.items(), key=lambda x: x[1])
    if top_count < len(lines) * 0.5:
        return None  # Not repetitive enough

    # Find non-matching tail lines (anomalies)
    tail_lines: list[str] = []
    for line in reversed(lines):
        if _fp(line) != top_fp:
            tail_lines.insert(0, line)
        else:
            break

    if not tail_lines:
        return None

    # Build: first few lines + count + tail anomaly
    tail_text = "\n".join(tail_lines)
    sample_count = 3
    head_lines = lines[:sample_count]
    head_text = "\n".join(head_lines)
    omitted = max(0, len(lines) - sample_count - len(tail_lines))

    # The tail anomaly is the payload this path exists to surface, so it is
    # reserved first. The old code sliced from the START on overflow, which
    # cut the anomaly off entirely — destroying what it set out to preserve.
    # When even the tail overflows the budget, keep its most-recent end
    # (where anomalies surface) together with the marker.
    marker = f"\n... ({omitted} similar lines omitted)\n"
    head_budget = max_chars - len(marker) - len(tail_text)
    if head_budget < 0:
        body = marker + tail_text
        return body[-max_chars:] if len(body) > max_chars else body
    head_kept = head_text if len(head_text) <= head_budget else head_text[:head_budget]
    return head_kept + marker + tail_text


def old_unique_key(chunks: dict[str, str], key: str, next_suffix: dict[str, int]) -> str:
    if key not in chunks:
        return key
    suffix = 2
    while f"{key}#{suffix}" in chunks:
        suffix += 1
    return f"{key}#{suffix}"


class _OldKeys(SelectiveCompressor):
    _unique_key = staticmethod(old_unique_key)  # type: ignore[assignment]


def _compact(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# Characters that serialize to more than themselves, or to themselves only
# under ensure_ascii=False.
_TEXT = ["a", "b", " ", '"', "\\", "\n", "\x01", "é", "가", "\ud800", "😀", "/"]


def _text(rng: random.Random, max_len: int) -> str:
    return "".join(rng.choice(_TEXT) for _ in range(rng.randint(0, max_len)))


def _toc(entries: object = None, *, hint: bool = True, truncated: int | None = None) -> dict:
    toc: dict = {
        "type": "toc",
        "selection_key": "abc123",
        "format": "markdown",
        "total_chars": 123456,
    }
    if truncated is not None:
        toc["truncated_entries"] = truncated
    toc["ttl_seconds_remaining"] = 300
    if entries is not None:
        toc["entries"] = entries
    if hint:
        toc["hint"] = "Call stm_proxy_select_chunks(key='abc123', sections=[...]) to retrieve."
    return toc


def _entry(index: int, preview: str) -> dict:
    return {"key": f"h{index}", "type": "section", "size": 10 * index, "preview": preview}


def _random_toc(rng: random.Random) -> str:
    entries: list[object] = []
    for index in range(rng.randint(0, 30)):
        roll = rng.random()
        if roll < 0.85:
            entries.append(_entry(index, _text(rng, 40)))
        elif roll < 0.9:
            entries.append({"key": "k", "preview": ""})
        elif roll < 0.95:
            entries.append("preview")
        else:
            entries.append([1, {"preview": "x"}])
    shape = rng.random()
    value: object = entries
    if shape > 0.8:
        value = rng.choice([None, "entries", {"preview": "x"}])
    toc = _toc(
        value,
        hint=rng.random() < 0.8,
        truncated=rng.randint(0, 20) if rng.random() < 0.15 else None,
    )
    if value is None:
        if rng.random() < 0.5:
            toc["entries"] = None
    if rng.random() < 0.1:
        del toc["selection_key"]
    return json.dumps(toc, ensure_ascii=False)


# TOCs swept over every budget: preview shrink that succeeds and that runs out,
# the shortened hint, an omitted count crossing 9 → 10, a count already in the
# envelope, and entries that are absent or not a list.
SWEPT_TOCS = {
    "fifteen-entries": _toc([_entry(i, "preview text é\\" * 3) for i in range(15)]),
    "no-hint": _toc([_entry(i, "x" * 30) for i in range(12)], hint=False),
    "count-already-present": _toc([_entry(i, "y" * 20) for i in range(11)], truncated=4),
    "mixed-elements": _toc([_entry(0, "p" * 50), "s", [1, 2], {"preview": ""}, _entry(4, "")]),
    "entries-absent": _toc(None),
    "entries-null": {**_toc(None), "entries": None},
    "entries-string": _toc("entries"),
    "entries-dict": _toc({"preview": "z" * 40}),
    "empty-list": _toc([]),
}


class TestSameResultsAsTheOldCode:
    @pytest.mark.parametrize("name", sorted(SWEPT_TOCS))
    def test_fit_toc_tail_every_budget(self, name):
        tail = json.dumps(SWEPT_TOCS[name], ensure_ascii=False)
        full = len(_compact(SWEPT_TOCS[name]))
        for source_text in ("", "tail preview \\ é " * 20):
            # Past the full size the TOC fits whole and the rest of the budget
            # goes to tail_preview.
            for max_chars in [*range(full + 3), full + 60, full + 300, full + 2000]:
                new = HybridCompressor._fit_toc_tail(tail, max_chars, source_text)
                old = old_fit_toc_tail(tail, max_chars, source_text)
                assert new == old, (name, max_chars, source_text[:10])

    def test_fit_toc_tail_random(self):
        rng = random.Random(11)
        for _ in range(300):
            tail = _random_toc(rng)
            source_text = rng.choice(["", _text(rng, 30), _text(rng, 400)])
            full = len(_compact(json.loads(tail)))
            budgets = {0, 1, 2, full - 1, full, full + 1}
            budgets.update(rng.randint(0, full + 5) for _ in range(20))
            budgets.update(rng.randint(full, full + 600) for _ in range(5))
            for max_chars in budgets:
                new = HybridCompressor._fit_toc_tail(tail, max_chars, source_text)
                old = old_fit_toc_tail(tail, max_chars, source_text)
                assert new == old, (tail, max_chars, source_text)

    def test_markdown_keys(self):
        rng = random.Random(12)
        headings = ["a", "a", "a", "a#2", "a#3", "a#2#2", "b", "Preamble"]
        texts = [
            "# a\n1\n# a\n2\n# a\n3",
            "# a\n1\n# a#2\nx\n# a\n2\n# a\n3\n# a#4\ny\n# a\n5\n# a\n6",
            "intro\n# Preamble\nx\n# a\ny",
        ]
        for _ in range(500):
            parts = [f"# {rng.choice(headings)}\nbody {i}" for i in range(rng.randint(0, 25))]
            texts.append(rng.choice(["", "preamble\n"]) + "\n".join(parts))
        for text in texts:
            new = list(SelectiveCompressor()._parse_markdown(text).items())
            assert new == list(_OldKeys()._parse_markdown(text).items()), text

    def test_json_keys(self):
        rng = random.Random(13)
        names = ["a", "b", "a.b", "a.b#2", "b#2"]
        for _ in range(500):
            data: dict = {}
            for _ in range(rng.randint(0, 8)):
                key = rng.choice(names)
                if rng.random() < 0.5:
                    data[key] = {rng.choice(names): rng.randint(0, 9) for _ in range(3)}
                else:
                    data[key] = rng.randint(0, 9)
            for depth in (1, 2):
                new = SelectiveCompressor(json_depth=depth)._parse_json_dict(data, "")
                old = _OldKeys(json_depth=depth)._parse_json_dict(data, "")
                assert list(new.items()) == list(old.items()), data

    def test_tail_anomaly(self):
        rng = random.Random(14)
        for _ in range(500):
            lines = [
                f"2026-10-22 12:00:{i % 60:02d} INFO ok {i}" for i in range(rng.randint(5, 30))
            ]
            lines += [
                rng.choice(["ERROR boom", "", "  trace", "INFO ok"])
                for _ in range(rng.randint(0, 20))
            ]
            text = "\n".join(lines)
            for max_chars in (20, 200, 2000):
                new = TruncateCompressor._tail_anomaly_truncate(text, max_chars)
                assert new == old_tail_anomaly_truncate(TruncateCompressor, text, max_chars), text


# A consumer given an input that took tens of seconds before the fix; each must
# now finish well under the bound.
STALL_CASES = {
    "hybrid-distinct-headings": (
        "from memtomem_stm.proxy.compression import HybridCompressor, SelectiveCompressor\n"
        "run = lambda s: HybridCompressor(selective_compressor=SelectiveCompressor())"
        ".compress(s, max_chars=8000)",
        "''.join(f'# h{i}\\nx\\n\\n' for i in range(8000))",
    ),
    "hybrid-repeated-heading": (
        "from memtomem_stm.proxy.compression import HybridCompressor, SelectiveCompressor\n"
        "run = lambda s: HybridCompressor(selective_compressor=SelectiveCompressor())"
        ".compress(s, max_chars=8000)",
        "''.join(f'# a\\nbody {i}\\n\\n' for i in range(8000))",
    ),
    "selective-repeated-heading": (
        "from memtomem_stm.proxy.compression import SelectiveCompressor\n"
        "run = SelectiveCompressor()._parse_markdown",
        "''.join(f'# a\\nbody {i}\\n\\n' for i in range(16000))",
    ),
    "toc-large-envelope": (
        "from memtomem_stm.proxy.compression import HybridCompressor\n"
        "run = lambda s: HybridCompressor._fit_toc_tail(s, 5000, '')",
        "__import__('json').dumps({'type': 'toc', 'entries': [0] * 20000, 'pad': 'x' * 20000})",
    ),
}

_CHILD = """
import sys, time
{setup}
text = {payload}
start = time.perf_counter()
run(text)
elapsed = time.perf_counter() - start
print(f"{{elapsed:.3f}}s")
sys.exit(0 if elapsed < 2.0 else 1)
"""

# The tail-anomaly path also fingerprints every line, which is linear but not
# cheap, so a fixed bound on a large input would track the machine's speed.
# Its check is the growth instead: four times the input should cost about four
# times as much, where the quadratic collection cost about sixteen.
_TAIL_CHILD = """
import sys, time
from memtomem_stm.proxy.compression import TruncateCompressor
def cost(n):
    text = "prefix\\n" * (2 * n) + "tail\\n" * n
    start = time.perf_counter()
    TruncateCompressor._tail_anomaly_truncate(text, 500)
    return time.perf_counter() - start
small, large = min(cost(32_000) for _ in range(3)), min(cost(128_000) for _ in range(3))
print(f"{small:.3f}s -> {large:.3f}s")
sys.exit(0 if large < 8 * small else 1)
"""


def _run_child(code: str, case: str) -> None:
    try:
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{case}: still running after 120 s", pytrace=False)
    assert done.returncode == 0, f"{case}: {done.stdout.strip()} {done.stderr.strip()[-500:]}"


@pytest.mark.parametrize("case", sorted(STALL_CASES))
def test_crafted_input_does_not_stall(case):
    setup, payload = STALL_CASES[case]
    _run_child(textwrap.dedent(_CHILD).format(setup=setup, payload=payload), case)


def test_tail_anomaly_cost_grows_linearly():
    _run_child(textwrap.dedent(_TAIL_CHILD), "tail-anomaly")
