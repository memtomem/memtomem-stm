"""Privacy-aware content scanning for proxy compression routing."""

from __future__ import annotations

import logging
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

# Secrets proper: anything whose disclosure grants access. Consumers that
# gate an ACTION on sensitivity (e.g. "may this response be sent to an
# external LLM provider?") should use this set — matching here means the
# payload likely carries a credential, which is categorically worth a
# degraded compression strategy.
CREDENTIAL_PATTERNS = [
    r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]",
    r"(?i)(password|passwd|pwd)\s*[:=]",
    # Provider-prefixed token formats. Anchored by prefix so false positives
    # on arbitrary high-entropy strings are rare.
    r"(?i)(sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{36}|xox[bps]-[0-9A-Za-z-]+)",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"(?:(?:sk|pk|rk)_(?:live|test)|whsec)_[A-Za-z0-9]{20,}",
    r"\bnpm_[A-Za-z0-9]{20,}\b",
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    # JWT-ish: three base64url segments separated by dots, anchored to the
    # canonical ``eyJ`` header prefix to limit false positives on arbitrary
    # dotted identifiers.
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
    r"(?i)(BEGIN\s+(RSA|EC|OPENSSH|DSA|PGP)\s+PRIVATE\s+KEY)",
]

# PII that is sensitive to PERSIST but not a credential. Email addresses
# appear in legitimately compressible content all the time (git logs,
# issue threads, contact pages), so treating them as credentials made the
# LLM routing gate fire on ordinary responses and silently degrade the
# operator's chosen strategy to truncation. Consumers that gate STORAGE
# (e.g. surfacing query persistence) should keep scanning the full
# DEFAULT_PATTERNS.
PII_PATTERNS = [
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
]

# Backwards-compatible union — the default for contains_sensitive_content
# and the right set wherever the question is "is anything here sensitive?"
# rather than "is this specifically a credential?".
DEFAULT_PATTERNS = [*CREDENTIAL_PATTERNS, *PII_PATTERNS]


@lru_cache(maxsize=1)
def _compile_patterns(patterns: tuple[str, ...]) -> list[re.Pattern[str]]:
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p))
        except re.error as exc:
            logger.warning("Invalid privacy pattern %r: %s", p, exc)
    return compiled


def contains_sensitive_content(text: str, patterns: list[str] | None = None) -> bool:
    """Check if text contains any sensitive patterns."""
    effective = patterns if patterns else DEFAULT_PATTERNS
    if not effective:
        return False
    compiled = _compile_patterns(tuple(effective))
    return any(p.search(text) for p in compiled)
