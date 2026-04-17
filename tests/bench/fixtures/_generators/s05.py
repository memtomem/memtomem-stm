"""Deterministic generator for ``s05.json`` — 40-turn chat transcript.

SKELETON is the target compressor. Each turn is encoded as a markdown
heading (``## Turn N — <speaker>``) followed by a first content line
capped at ``FIRST_LINE_CAP`` chars and a longer detail block. The
skeleton compressor preserves every heading plus the first body line
per section, so the probes can target the heading + first-line tokens
and a unique sentinel on one specific turn.

Regenerate after edits::

    uv run python -m tests.bench.fixtures._generators.s05

The committed ``tests/bench/fixtures/s05.json`` is the canonical source;
``tests/bench/test_bench_qa_generators.py`` asserts that re-running this
generator reproduces it byte-for-byte.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

NUM_TURNS = 40
FIRST_LINE_CAP = 60
SENTINEL_TURN = 17
SENTINEL_TOKEN = "alpha-bravo-17"

# Unique first lines per role. Each entry ends up inside a heading +
# first-line region that SKELETON must preserve — keep every string
# strictly <= FIRST_LINE_CAP chars (asserted below).
USER_FIRST_LINES: tuple[str, ...] = (
    "Gateway is returning 504 on write paths since this morning.",
    "P99 latency climbed from 120ms to 880ms on /orders only.",
    "Read replicas started lagging around forty seconds ago.",
    "We just rolled ingest-worker v1.8.3 to the canary region.",
    "Three synthetic probes are red; traffic is otherwise fine.",
    "OOMKills spiked on nodes tagged ingest-pool-blue.",
    "The scheduler is refusing new pods with taint mismatch.",
    "Checkout failure rate jumped to 2.4 percent since 09:12.",
    "A cold cache after failover is doubling p95 for ten minutes.",
    "Two shard leaders flapped between 09:40 and 09:44.",
    "Extraction queue depth is climbing past ten thousand now.",
    "Kafka consumer lag on billing-topic is now seven minutes.",
    "Primary DB is reporting checkpoint contention every minute.",
    "The LB dropped two backends for failing health checks.",
    "Our circuit breaker opened on the pricing service at 10:02.",
    "Escalation mentions duplicate confirmation emails sent.",
    "Signup form returns 500 intermittently under concurrency.",
    "New flag rollout touched the same module as yesterday.",
    "Canary is green but aggregate error rate remains elevated.",
    "Saturation alert fired for the auth service connection pool.",
)

ASSISTANT_FIRST_LINES: tuple[str, ...] = (
    "Can you share the trace for one failing request timings?",
    "Let us pull the last ten minutes of gateway access logs.",
    "Which downstream service does the 504 page point at first?",
    "Is the lag on all replicas or only one availability zone?",
    "Roll back the canary and compare p99 against the baseline.",
    "Inspect node conditions and drain any unhealthy worker.",
    "Check the pod tolerations against the new taint definition.",
    "What is the breakdown of 2.4 percent across payment methods?",
    "Keep warmup traffic on for ten more minutes and recheck p95.",
    "Look at leader election logs for the flapping shards now.",
    "Is the extraction worker pool hitting the CPU quota cap?",
    "Increase billing-topic partitions and rebalance consumers.",
    "Inspect WAL archival throughput on the primary database.",
    "Verify backend pod readiness probes match the new version.",
    "Open the circuit breaker dashboard and check rolling window.",
    "Can you correlate the duplicates with a retry on the mailer?",
    "Profile the signup path for a shared mutex under load.",
    "Revert the flag and compare error curves over the window.",
    "Sample the tail requests and compare paths between buckets.",
    "Raise the pool size and add timeout tracing on auth calls.",
)

USER_DETAIL_LINES: tuple[str, ...] = (
    "The error rate is concentrated on orders-write and inventory-reserve.",
    "Upstream traces show a 40ms bump just before the response is served.",
    "We enabled the new ingest path last Friday as part of release 23.4.",
    "No schema migrations ran in the last 24 hours according to the release log.",
    "The pod autoscaler scaled out to 18 replicas at the same moment.",
    "Our paging policy is on-call only so there was no ack delay.",
    "Customer support opened two tickets tagged billing-duplicate-email.",
    "We have a runbook for failover cold-cache but it is six months stale.",
    "The retry budget is configured to two attempts with fifty ms jitter.",
    "Observability shows an increase in memory pressure on only blue nodes.",
)

ASSISTANT_DETAIL_LINES: tuple[str, ...] = (
    "Please attach the trace ID so we can line it up with the metrics pipeline.",
    "A flamegraph from the backend during the failure window would help rule out GC.",
    "We should compare error budgets across regions to narrow the blast radius.",
    "If it is consumer lag we can bump the partitions and restart the worker pool.",
    "Check whether the new flag crosses the shared connection pool threshold.",
    "I will prepare the rollback playbook while you gather the trace samples.",
    "A dry-run of the schema-pruning compressor against the payload will confirm the hunch.",
    "Verify that the extraction queue is not starving the write path for IO.",
    "Look at the percentile drift over a fifteen minute window split by pod.",
    "If this repeats, we should cut a permanent dashboard for the cold-cache path.",
)


def _turn_body(speaker: str, idx: int) -> str:
    """Render body for turn ``idx`` (1-indexed)."""
    first_pool = USER_FIRST_LINES if speaker == "user" else ASSISTANT_FIRST_LINES
    detail_pool = USER_DETAIL_LINES if speaker == "user" else ASSISTANT_DETAIL_LINES

    # Deterministic pick — use the 1-indexed turn number so Turn 17 always
    # hits the same slot even if NUM_TURNS changes.
    first = first_pool[(idx - 1) // 2 % len(first_pool)]
    # Sentinel injection: Turn 17 (user) gets a unique token in its first
    # line so the QA probe can pin "Turn 17 body first line survived".
    if idx == SENTINEL_TURN:
        # Sentinel lives in the preserved first line — keep the merged
        # string within FIRST_LINE_CAP.
        first = f"{SENTINEL_TOKEN} sentinel first line content for probe."
    assert len(first) <= FIRST_LINE_CAP, (
        f"first_line for turn {idx} is {len(first)} chars (> {FIRST_LINE_CAP}): {first!r}"
    )

    detail_a = detail_pool[(idx * 3 + 1) % len(detail_pool)]
    detail_b = detail_pool[(idx * 5 + 2) % len(detail_pool)]
    detail_c = detail_pool[(idx * 7 + 3) % len(detail_pool)]
    return "\n".join([first, detail_a, detail_b, detail_c])


def build_payload() -> str:
    sections: list[str] = []
    for idx in range(1, NUM_TURNS + 1):
        speaker = "user" if idx % 2 == 1 else "assistant"
        body = _turn_body(speaker, idx)
        sections.append(f"## Turn {idx} — {speaker}\n{body}")
    return "\n\n".join(sections) + "\n"


def build_fixture() -> dict[str, Any]:
    payload = build_payload()
    return {
        "schema_version": 1,
        "scenario_id": "s05",
        "description": (
            "40-turn chat transcript encoded as one markdown section per turn. "
            "SKELETON forced. Verifies per-turn heading (speaker) survives "
            "compression and that a sentinel planted on Turn 17's first body "
            "line is preserved (SKELETON keeps headings + first content line "
            "per section)."
        ),
        "content_type": "markdown",
        "expected_compressor": "skeleton",
        "max_result_chars": 5000,
        # SKELETON on a 40-section document compresses to ~27% of cleaned
        # chars (heading + first body line per section). Production default
        # 0.65 would trip the ratio-guard fallback ladder and swap SKELETON
        # for progressive/hybrid — defeating the scenario's purpose. Pin a
        # low floor here so the SKELETON contract itself is what gets gated.
        "min_retention": 0.2,
        "qa_gate_min": 0.66,
        "expected_keywords": [
            "Turn 1",
            "Turn 40",
            "user",
            "assistant",
            SENTINEL_TOKEN,
        ],
        "qa_probes": [
            {
                "question": "Who initiated the conversation at Turn 1?",
                "expected_keywords": ["Turn 1", "user"],
            },
            {
                "question": "Who responded at Turn 40?",
                "expected_keywords": ["Turn 40", "assistant"],
            },
            {
                "question": "Did the sentinel on Turn 17's first body line survive?",
                "expected_keywords": ["Turn 17", SENTINEL_TOKEN],
            },
        ],
        "payload": payload,
    }


def canonical_dump(fixture: dict[str, Any]) -> str:
    """Canonical JSON emit. Drift gate compares bytes, so the format
    must be stable across runs: sorted keys, two-space indent, no ASCII
    escaping, trailing newline."""
    return json.dumps(fixture, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> None:
    fixtures_dir = Path(__file__).resolve().parents[1]
    out = fixtures_dir / "s05.json"
    out.write_text(canonical_dump(build_fixture()), encoding="utf-8")


if __name__ == "__main__":
    main()
