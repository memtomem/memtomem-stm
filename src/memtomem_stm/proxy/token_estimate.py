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

from typing import Final

# Tokens-per-character by Unicode block, calibrated 2026-04-29 against
# cl100k_base on a 13-pair EN/KO doc corpus. See module docstring for
# methodology.
TOK_PER_CHAR_ASCII = 0.30
TOK_PER_CHAR_HANGUL = 1.25
TOK_PER_CHAR_CJK_IDEOGRAPH = 1.40
TOK_PER_CHAR_KANA = 1.00
TOK_PER_CHAR_OTHER = 0.45

MAX_CHAR_BUDGET: Final = 2**63 - 1
"""Largest char budget :func:`tokens_to_chars` will return.

A saturation ceiling, not a policy limit: it exists so an operator value
that overflows the conversion resolves to "no effective limit" instead of
failing the call. Fixed at the signed-64-bit maximum rather than derived
from ``sys.maxsize``, so the value does not differ between platforms. It
doubles as the upper bound on ``max_result_tokens``, which keeps the token
budget and the char budget it converts to inside one domain.
"""


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


def tokens_to_chars(tokens: float, chars_per_token: float) -> int:
    """Convert a token budget to a char budget using the operator-supplied ratio.

    For Latin-script content, ``chars_per_token`` typically ranges 3.5-4.0.
    For Korean (Hangul-dominant) content, 1.8-2.0 is realistic.
    For Chinese (CJK-ideograph-dominant), 1.0-1.5.

    ``tokens`` is typed ``float`` so a caller that has already scaled a token
    count can hand the scaled value over rather than re-associating the
    multiplication here; see ``ProxyConfig.effective_max_result_chars``.

    Total by contract: every input maps to an int, none raises (#977). This
    runs inside per-call budget resolution, where an exception fails the
    proxied tool call and names neither the offending field nor the level it
    was written at. Saturation degrades to "no effective limit" instead.
    Whether that is then capped is the caller's decision, and they differ: the
    model-aware budget caps against ``default_max_result_chars``, while an
    explicit token budget outranks every char budget and so is returned as-is.

    - Non-positive ``tokens`` or ratio, and ``nan`` in either position (which
      compares false against 0), return ``0``.
    - A product that overflows to infinity, and a ``tokens`` value too wide to
      multiply as a float at all, return :data:`MAX_CHAR_BUDGET`. So does a
      finite product beyond that ceiling. The first and third are reachable
      from a validated config -- ``chars_per_token`` is bounded only away from
      zero and infinity, so a legal ``1e308`` saturates -- while the second
      needs a ``tokens`` value wider than ``max_result_tokens`` now admits,
      and is pinned because this helper is public.
    - A product below 1 truncates to ``0``, deliberately: the two callers read
      a degenerate budget differently. ``ProxyConfig.effective_max_result_chars``
      treats it as "model scaling off" and falls back to its static default,
      while the per-server/per-tool resolver floors it at one char. Flooring
      here would defeat the first.
    """
    if not (tokens > 0 and chars_per_token > 0):
        return 0
    try:
        chars = tokens * chars_per_token
    except OverflowError:
        # ``int * float`` raises when the int is wider than the float range,
        # rather than producing an infinity to compare below.
        return MAX_CHAR_BUDGET
    # One comparison covers an infinite product, a finite one above the
    # ceiling, and an integer product too wide to convert to a float at all.
    # ``math.isinf`` would not: it raises on that last case, which is exactly
    # what a caller passing two ints produces.
    if chars >= MAX_CHAR_BUDGET:
        return MAX_CHAR_BUDGET
    return int(chars)
