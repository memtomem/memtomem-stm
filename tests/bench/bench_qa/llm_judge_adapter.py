"""Adapter wiring the LLM-as-judge skeleton into the bench_qa report flow.

The adapter deliberately sits next to ``judge.py`` rather than inside it:
``judge.py`` is a pure-stdlib synchronous keyword matcher that every bench_qa
test imports, while the LLM judge is async, httpx-bound and network-dependent.
Keeping them separate avoids pulling httpx into the keyword path and lets the
adapter own bench_qa-specific concerns (``QAProbe`` → ``QAPair`` conversion,
on-disk caching, ``LLMJudgeResultReport`` flattening).

Cache: persistent JSON at ``tests/bench/.llm_judge_cache/<16hex>.json``,
gitignored. Key is ``sha256(provider:model:scenario_id:content_type:compressed)``
— including provider and model means switching models invalidates naturally.
Delete the directory to force a refresh.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from ..harness import BenchTask, QAPair
from ..llm_judge import JudgeDimension, LLMJudge, LLMJudgeResult
from .schema import (
    LLMJudgeAgreementReport,
    LLMJudgeProbeResult,
    LLMJudgeResultReport,
    QAProbe,
)

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / ".llm_judge_cache"

_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4.1-nano",
}


def _resolve_config(provider: str | None, model: str | None) -> tuple[str, str, str]:
    """Mirror ``LLMJudge.__init__`` resolution so the cache key lines up.

    Returns ``(provider, model, api_key)``. ``api_key`` may be an empty
    string when no env var is set — the caller decides whether that is a
    hard skip (local dev) or a soft advisory (production fallback).
    """
    resolved_provider = os.environ.get("BENCH_LLM_JUDGE_PROVIDER", provider or "openai")
    resolved_model = os.environ.get(
        "BENCH_LLM_JUDGE_MODEL",
        model or _DEFAULT_MODELS.get(resolved_provider, "gpt-4.1-nano"),
    )
    if resolved_provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    elif resolved_provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "")
    else:
        api_key = ""
    return resolved_provider, resolved_model, api_key


def _cache_key(
    *,
    provider: str,
    model: str,
    scenario_id: str,
    content_type: str,
    compressed: str,
) -> str:
    payload = f"{provider}:{model}:{scenario_id}:{content_type}:{compressed}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _load_cached(path: Path) -> LLMJudgeResult | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("llm_judge cache unreadable, discarding: %s", path)
        return None
    dims = [JudgeDimension(**d) for d in data.get("dimensions", [])]
    return LLMJudgeResult(
        task_id=data.get("task_id", ""),
        overall=float(data.get("overall", 0.0)),
        dimensions=dims,
        qa_results=list(data.get("qa_results", [])),
        model=data.get("model", ""),
        prompt_tokens=int(data.get("prompt_tokens", 0)),
        completion_tokens=int(data.get("completion_tokens", 0)),
        cached=True,
        error=data.get("error"),
    )


def _save_cache(path: Path, result: LLMJudgeResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(result)
    data["cached"] = False
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


async def score_scenario(
    *,
    scenario_id: str,
    description: str,
    original: str,
    compressed: str,
    probes: Sequence[QAProbe],
    content_type: str = "text",
    cache_dir: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> LLMJudgeResult | None:
    """Score one compressed scenario output with the LLM judge.

    Async because every current caller (the parametrized bench_qa test)
    already runs under ``@pytest.mark.asyncio`` — wrapping the ``await`` in
    a nested ``asyncio.run()`` raises ``RuntimeError``.

    Returns ``None`` when no API key is configured — the caller should
    ``pytest.skip``.  Returns an ``LLMJudgeResult`` with ``.error`` populated
    on transport / parse failure rather than raising: the LLM judge is
    advisory-only today and a single flaky scenario must not fail the run.
    """
    effective_cache_dir = cache_dir or DEFAULT_CACHE_DIR
    resolved_provider, resolved_model, api_key = _resolve_config(provider, model)
    if not api_key:
        logger.info(
            "llm_judge skipping scenario=%s — no API key for provider %s",
            scenario_id,
            resolved_provider,
        )
        return None

    key = _cache_key(
        provider=resolved_provider,
        model=resolved_model,
        scenario_id=scenario_id,
        content_type=content_type,
        compressed=compressed,
    )
    path = effective_cache_dir / f"{key}.json"
    cached = _load_cached(path)
    if cached is not None:
        logger.info("llm_judge cache hit: scenario=%s key=%s", scenario_id, key)
        return cached

    task = BenchTask(
        task_id=scenario_id,
        description=description or f"bench_qa scenario {scenario_id}",
        content=original,
        content_type=content_type,
        max_chars=max(len(compressed), 1),
        qa_pairs=[QAPair(question=p["question"], answer="", source="content") for p in probes],
    )

    judge = LLMJudge(provider=resolved_provider, model=resolved_model, api_key=api_key)
    result = await judge.score(task, compressed)
    _save_cache(path, result)
    return result


def to_report_dict(result: LLMJudgeResult) -> LLMJudgeResultReport:
    """Flatten an ``LLMJudgeResult`` into the ``ScenarioReport`` shape.

    Raw dimension scores are 0–10 from the model; we divide by 10 so they
    line up with ``qa.ratio`` and ``rule_judge.score`` (all 0.0–1.0). The
    ``overall`` score follows the same normalisation.
    """
    dim_by_name = {d.name: d.score / 10.0 for d in result.dimensions}
    per_probe: list[LLMJudgeProbeResult] = []
    for qa in result.qa_results:
        probe: LLMJudgeProbeResult = {
            "question": str(qa.get("question", "")),
            "answerable": bool(qa.get("answerable", False)),
            "confidence": float(qa.get("confidence", 0.0)),
        }
        reasoning = qa.get("reasoning")
        if reasoning:
            probe["reasoning"] = str(reasoning)
        per_probe.append(probe)

    report: LLMJudgeResultReport = {
        "model": result.model,
        "overall": round(result.overall / 10.0, 4),
        "factual_completeness": round(dim_by_name.get("factual_completeness", 0.0), 4),
        "structural_coherence": round(dim_by_name.get("structural_coherence", 0.0), 4),
        "answer_sufficiency": round(dim_by_name.get("answer_sufficiency", 0.0), 4),
        "per_probe": per_probe,
        "cached": result.cached,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
    }
    if result.error:
        report["error"] = str(result.error)
    return report


def compute_agreement(
    report_a: LLMJudgeResultReport,
    report_b: LLMJudgeResultReport,
) -> LLMJudgeAgreementReport:
    """Two-judge cross-provider agreement for one scenario (item 9).

    ``report_a`` / ``report_b`` are :func:`to_report_dict` outputs — already
    normalised to 0.0–1.0 — from two different judges. Returns an
    :class:`LLMJudgeAgreementReport` with per-dimension absolute differences
    and a scalar ``agreement_score = 1 - mean(|a - b|)`` over ``overall`` and
    the three dimensions (``overall`` weighted equally with each component, so
    the headline judgment counts the same as one dimension).
    ``agreement_score == 1.0`` is perfect inter-judge agreement; ``0.0`` is
    maximal disagreement — same polarity and scale as the underlying scores,
    so it slots into logs and any future ratchet without rescaling.

    If *either* report carries an ``error`` (transport / parse failure), the
    numeric fields are omitted and only ``model_a`` / ``model_b`` / ``error``
    are returned: a failed judge's ``overall=0.0`` would otherwise fabricate a
    large diff that poisons the cross-scenario correlation. The headline test
    excludes any scenario whose agreement block carries an ``error``.
    """
    agreement: LLMJudgeAgreementReport = {
        "model_a": str(report_a.get("model", "")),
        "model_b": str(report_b.get("model", "")),
    }
    err_a = report_a.get("error")
    err_b = report_b.get("error")
    if err_a or err_b:
        agreement["error"] = f"a={err_a or None} b={err_b or None}"
        return agreement

    overall_a = report_a.get("overall", 0.0)
    overall_b = report_b.get("overall", 0.0)
    diff_overall = abs(overall_a - overall_b)
    diff_fc = abs(
        report_a.get("factual_completeness", 0.0) - report_b.get("factual_completeness", 0.0)
    )
    diff_sc = abs(
        report_a.get("structural_coherence", 0.0) - report_b.get("structural_coherence", 0.0)
    )
    diff_as = abs(report_a.get("answer_sufficiency", 0.0) - report_b.get("answer_sufficiency", 0.0))
    mean_abs_diff = (diff_overall + diff_fc + diff_sc + diff_as) / 4.0

    agreement["overall_a"] = overall_a
    agreement["overall_b"] = overall_b
    agreement["diff_overall"] = round(diff_overall, 4)
    agreement["diff_factual_completeness"] = round(diff_fc, 4)
    agreement["diff_structural_coherence"] = round(diff_sc, 4)
    agreement["diff_answer_sufficiency"] = round(diff_as, 4)
    agreement["agreement_score"] = round(max(0.0, min(1.0, 1.0 - mean_abs_diff)), 4)
    return agreement
