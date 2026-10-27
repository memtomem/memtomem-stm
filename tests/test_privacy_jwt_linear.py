"""The JWT credential rule finds what it found before, in linear time and memory.

The old rule tried a match after every ``-`` inside a run of base64url
characters, and each attempt scanned to the end of the run, so a response of
``eyJ-`` repeated held the event loop for seconds. The rewrite must give the
same answers — checked against the old rule, kept here as the oracle — and
must finish, without building up regex state, on the inputs that used to
stall. Those checks run in a child process so a regression fails instead of
hanging the suite.
"""

from __future__ import annotations

import random
import re
import subprocess
import sys
import textwrap

import pytest

from memtomem_stm.proxy import privacy

OLD_JWT_RULE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
NEW_JWT_RULE = re.compile(next(p for p in privacy.CREDENTIAL_PATTERNS if "eyJ" in p))

_ALPHABET = [*"ab9_-.", "eyJ", "-eyJ", "eyJAAAAAAAA", " ", "AAAAAAAA", "é", "\n", "가", "--"]

STRUCTURED = [
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcDEF_-12345",
    "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpMeJf36",
    "x-eyJAAAAAAAA.BBBBBBBB.CCCCCCCC",
    "eyJAAAAAAAA-eyJBBBBBBBB.CCCCCCCC.DDDDDDDD",
    "eyJAAAAAAAA-eyJBB.CCCCCCCC.DDDDDDDD",
    "eyJAAAAAAA.BBBBBBBB.CCCCCCCC",
    "eyJAAAAAAAA.BBBBBBBB.CCCCCCC",
    "eyJAAAAAAAA.BBBBBBBB.CCCCCCCC-",
    "_eyJAAAAAAAA.BBBBBBBB.CCCCCCCC",
    "éeyJAAAAAAAA.BBBBBBBB.CCCCCCCC",
    "eyJ-" * 20,
    "eyJ-" * 20 + "AAAAAAAA.BBBBBBBB.CCCCCCCC",
    "hdr.eyJAAAAAAAA.BBBBBBBB.CCCCCCCC.extra",
]


def _strings() -> list[str]:
    rng = random.Random(21)
    fuzz = ["".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 30))) for _ in range(20000)]
    return fuzz + STRUCTURED


def test_same_answer_count_and_ends_as_the_old_rule():
    for text in _strings():
        old = [m.span() for m in OLD_JWT_RULE.finditer(text)]
        new = [m.span() for m in NEW_JWT_RULE.finditer(text)]
        assert bool(NEW_JWT_RULE.search(text)) == bool(OLD_JWT_RULE.search(text)), repr(text)
        assert [end for _, end in new] == [end for _, end in old], repr(text)
        # A match may begin at a later ``eyJ`` in the same run, never earlier.
        assert all(n >= o for (o, _), (n, _) in zip(old, new, strict=True)), repr(text)


def test_a_later_start_in_the_same_run_keeps_the_end():
    text = "eyJAAAAAAAA-eyJBBBBBBBB.CCCCCCCC.DDDDDDDD"
    assert [m.span() for m in OLD_JWT_RULE.finditer(text)] == [(0, len(text))]
    assert [m.span() for m in NEW_JWT_RULE.finditer(text)] == [(12, len(text))]


_CHILD = """
import sys, time, tracemalloc
from memtomem_stm.proxy.privacy import contains_sensitive_content
text = {payload}
contains_sensitive_content("warm up")
tracemalloc.start()
start = time.perf_counter()
contains_sensitive_content(text)
elapsed = time.perf_counter() - start
peak = tracemalloc.get_traced_memory()[1]
print(f"{{elapsed:.3f}}s {{peak / 2**20:.1f}}MiB")
sys.exit(0 if elapsed < 2.0 and peak < 8 * 2**20 else 1)
"""

# Inputs that stalled the old rule (``eyJ-`` repeated: 7.7 s at 256 KB), and
# the shapes that made a per-character rewrite hold a backtracking frame per
# character (146 MiB for one 1 MB segment).
STALL_CASES = {
    "repeated-starts": "'eyJ-' * 64_000",
    "one-long-segment": "'eyJ' + 'A' * 1_024_000",
    "hyphen-dense-segment": "'eyJ' + 'a-' * 512_000",
    "viable-starts-without-tail": "'-eyJAAAAAAAA' * 80_000",
}


@pytest.mark.parametrize("case", sorted(STALL_CASES))
def test_crafted_input_does_not_stall(case):
    code = textwrap.dedent(_CHILD).format(payload=STALL_CASES[case])
    try:
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{case}: still scanning after 60 s", pytrace=False)
    assert done.returncode == 0, f"{case}: {done.stdout.strip()} {done.stderr.strip()[-500:]}"
