"""Codepoint-weighted token estimator (currently unused at gate time).

STM uses character counts everywhere by default. For users with
non-Latin-script workloads (Korean, Japanese, Chinese), a fixed char
budget under-triggers compression — Korean content has roughly 2x more
tokens per character than English at the same information density. The
``effective_max_result_chars()`` path historically used a hardcoded
``3.5`` chars/token multiplier that is English-biased.

The current PR introduces an opt-in *operator-supplied* path:
``ProxyConfig.chars_per_token`` (per-proxy / per-server / per-tool) plus
``max_result_tokens`` on server / tool. Gate decisions multiply those
two operator-supplied values via :func:`tokens_to_chars` — no runtime
text inspection happens. ``approx_tokens`` below is **not yet wired
into the gate path**; it is published for a follow-up that estimates
real response token counts at runtime instead of relying on the
operator's static ratio.

This module provides a fast, dependency-free codepoint-weighted token
estimator. It was calibrated against ``tiktoken`` ``cl100k_base``
(GPT-3.5/4 tokenizer) on a 13-pair EN/KO documentation corpus from
``memtomem-com``. Median absolute error on that corpus is ~13% in the
over-estimate direction (intentional — borderline responses compress
slightly earlier rather than slip past the gate). Gate-flip rate is 0
at the 5000-token threshold on the test corpus.

Coefficients can be re-tuned without API impact: only the
``approx_tokens`` numeric output changes. Suitable for budget gates and
threshold decisions, not for billing.
"""

from __future__ import annotations

import math
import sys

# Tokens-per-character by Unicode block, calibrated 2026-04-29 against
# cl100k_base on a 13-pair EN/KO doc corpus. See module docstring for
# methodology.
TOK_PER_CHAR_ASCII = 0.30
TOK_PER_CHAR_HANGUL = 1.25
TOK_PER_CHAR_CJK_IDEOGRAPH = 1.40
TOK_PER_CHAR_KANA = 1.00
TOK_PER_CHAR_OTHER = 0.45


def approx_tokens(text: str) -> int:
    """Estimate token count from ``text`` by Unicode-block weighting.

    O(N) in text length. Returns ``0`` for empty input.
    """
    if not text:
        return 0
    total_chars = len(text)
    ascii_count = 0
    hangul = 0
    cjk = 0
    kana = 0
    for ch in text:
        cp = ord(ch)
        if cp < 0x80:
            ascii_count += 1
        elif 0xAC00 <= cp <= 0xD7AF:
            hangul += 1
        elif 0x4E00 <= cp <= 0x9FFF:
            cjk += 1
        elif 0x3040 <= cp <= 0x30FF:
            kana += 1
    other = total_chars - ascii_count - hangul - cjk - kana
    return int(
        ascii_count * TOK_PER_CHAR_ASCII
        + hangul * TOK_PER_CHAR_HANGUL
        + cjk * TOK_PER_CHAR_CJK_IDEOGRAPH
        + kana * TOK_PER_CHAR_KANA
        + other * TOK_PER_CHAR_OTHER
    )


def tokens_to_chars(tokens: int, chars_per_token: float) -> int:
    """Convert a token budget to a char budget using the operator-supplied ratio.

    For Latin-script content, ``chars_per_token`` typically ranges 3.5-4.0.
    For Korean (Hangul-dominant) content, 1.8-2.0 is realistic.
    For Chinese (CJK-ideograph-dominant), 1.0-1.5.

    Returns ``0`` for non-positive inputs, and ``sys.maxsize`` when the product
    overflows to infinity. Both ends are saturating rather than raising: this
    runs inside the budget resolution of every proxied call, and two finite
    values a config can legally hold (a ratio near the float ceiling, or an
    enormous token budget) multiply to ``inf``, which ``int()`` refuses. A
    config that absurd asks for an unbounded budget, so answering with the
    largest representable one says that, where an ``OverflowError`` would fail
    the tool call instead and name neither field. Non-finite *inputs* are
    refused where they are written (``chars_per_token`` at all three levels).
    """
    if tokens <= 0 or chars_per_token <= 0:
        return 0
    try:
        product = tokens * chars_per_token
    except OverflowError:
        # A token budget past the float range raises in the multiplication
        # itself rather than producing ``inf`` — ``max_result_tokens`` is an
        # unbounded int and JSON can carry one.
        return sys.maxsize
    if not math.isfinite(product):
        return sys.maxsize
    return int(product)
