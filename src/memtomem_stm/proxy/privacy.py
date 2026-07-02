"""Privacy-aware content scanning for proxy compression routing.

The secret-class set here is kept coherent with memtomem LTM's
trust-boundary guard (``memtomem/privacy.py``), but the two scanners serve
different contracts: STM's scan is a compression-ROUTING signal (does this
block carry anything sensitive enough to skip an external-LLM strategy?),
while LTM's is a write-REJECTION gate. Sync is asymmetric:

- Secret-class patterns (provider tokens, key formats, PEM headers) are
  mirrored both ways for routing coherence — a payload LTM would block as
  secret-bearing should also steer STM's routing.
- PII-class patterns (email, etc.) do NOT cross over: STM keeps email in
  ``PII_PATTERNS`` (storage-gating only), and LTM excludes it by design
  because a PII block default would over-refuse ordinary prose ingress.

The seven provider-token patterns at the end of ``CREDENTIAL_PATTERNS``
originated at the LTM boundary (issue #1488) and were mirrored back here
(reverse sync, issue #1491).
"""

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
    # Quoted-JSON / dict-repr forms of the generic labels above. The two
    # label rules end in ``\s*[:=]``, and a quoted key's closing quote sits
    # between the label and the colon — ``"password": "hunter2"``,
    # ``"api_key": "sk-…"``, ``"accessToken": "ya29.…"``, ``{'password':
    # 'hunter2'}`` match none of them — and JSON-serialized credentials are
    # high-frequency tool output (``docker inspect``, ``kubectl get secret
    # -o json``, DB connection configs). Same FP-guard shape as the AWS
    # quoted branch below: the quote must sit DIRECTLY on both sides of the
    # label and the value must open as a string, so a JSON-Schema property
    # (``"access_token": {"type"…`` — object value; login/OAuth tool schemas
    # carry these constantly), an identifier that merely embeds a label
    # (``"my_api_key_name": …``), and a prefixed key (``"tools.api_key": …``
    # — telemetry dicts keyed by tool name) never fire. The ``[_-]?``
    # separator is optional, so camelCase JSON keys (``"apiKey"``,
    # ``"accessToken"``) match under ``(?i)``. ``pwd`` is deliberately NOT
    # in this vocabulary (unlike the unquoted rule above): shell/file tools
    # legitimately return working-directory fields (``"pwd": "/home/user"``).
    # The AWS spellings are listed so the vocabulary is self-contained,
    # though the #553 rule below already covers their quoted forms.
    r"(?i)[\"'](?:api[_-]?key|secret[_-]?(?:access[_-]?)?key"
    r"|(?:access|session)[_-]?token|password|passwd)[\"']\s*:\s*[\"']",
    # AWS secret material by label. The generic label rule above misses both
    # spellings the AWS toolchain actually emits: ``secret[_-]?key`` needs its
    # two words adjacent (``secret_access_key`` splits them) and
    # ``access[_-]?token`` needs the literal ``access`` (``session_token`` has
    # neither). Two alternatives in one rule:
    #
    # - quoted form — ``"SessionToken": "…"`` / ``"SecretAccessKey": "…"``
    #   (STS JSON, python-dict repr) plus kebab-case quoted keys
    #   (``"session-token": "…"`` — serialized header dicts), so both
    #   branches share the same ``[_-]?`` separator charset. The quote must
    #   sit DIRECTLY on both sides of the label and the value must open as
    #   a string: a JSON-Schema
    #   property (``"session_token": {"type"…`` — object value) and a
    #   prefixed key (``"aws.get_session_token": "unhealthy"`` — telemetry
    #   dicts keyed by tool name) must NOT fire, or the eligibility scanner
    #   hides benign tools and the selection log drops whole records. The
    #   pre-existing label rules never fire on bare JSON keys (the closing
    #   quote blocks their ``[:=]``); this form must not regress that.
    # - unquoted form — ``AWS_SECRET_ACCESS_KEY=`` / ``aws_session_token =``
    #   (env output, ~/.aws/credentials INI). Unlike the generic label rule
    #   above, this branch carries a LEFT boundary: the label must start a
    #   token (``SESSION_TOKEN=``, namespaced ``TF_VAR_aws_secret_access_key=``)
    #   or directly follow an ``aws`` separator (``aws.secret_access_key =``,
    #   TOML dotted key). Without it, identifiers that merely EMBED the label
    #   fire the rule — ``get_session_token: unhealthy`` (method name),
    #   ``supports_session_token: true`` (capability flag),
    #   ``rotateSecretAccessKey: done`` — and the scanning consumers mask
    #   benign tools/records. A plain optional ``(?:aws[_-])?`` prefix
    #   instead of the lookbehind pair would drop the namespaced env-var
    #   forms above, so don't "simplify" it that way.
    #
    # The AKIA/ASIA rule below catches the key *IDs*; this one catches the
    # *material* those IDs unlock. STM-origin; forward-synced into LTM's
    # mirrored set as memtomem#1533 (the inverse of the #1488→#1491
    # reverse-sync direction; see the count-pin note in
    # tests/test_privacy.py).
    r"(?i)(?:[\"'](?:secret[_-]?access[_-]?key|session[_-]?token)[\"']\s*:\s*[\"']"
    r"|(?:(?<![A-Za-z0-9_.-])|(?<=aws[._-]))"
    r"(?:secret[_-]?access[_-]?key|session[_-]?token)\s*[:=])",
    # The label AWS actually puts on the WIRE for that same session-token
    # material: botocore DEBUG logs emit the ``x-amz-security-token`` request
    # header verbatim, and every presigned URL generated with temporary
    # credentials carries the ``X-Amz-Security-Token=…`` query parameter.
    # No rule above reaches either: ``session[_-]?token`` cannot cross the
    # ``security-token`` spelling, and the kebab header shape has no ``aws``
    # separator directly before the label, so the left-boundary form above
    # misses it too — catching these payloads otherwise relies on an
    # ``ASIA…`` key ID co-occurring in the same text. Two alternatives,
    # mirroring the rule above:
    #
    # - quoted form — ``"x-amz-security-token": "…"`` (serialized header
    #   dicts, JSON or python repr). Quote directly on both sides of the
    #   label and a string-opening value, so an OpenAPI/JSON-Schema header
    #   *definition* (``"X-Amz-Security-Token": {"type"…`` — object value,
    #   ubiquitous in AWS API specs) never fires.
    # - unquoted form — the raw header line (``x-amz-security-token: FwoG…``)
    #   and the presigned-URL query param (``…&X-Amz-Security-Token=FwoG…``).
    #   The left boundary rejects only a directly preceding SEPARATOR
    #   (``[_.-]``), not alphanumerics — a narrower boundary than the rule
    #   above, on purpose. Kebab/dotted compounds that merely NAME the
    #   header must not fire — ``forward-x-amz-security-token: true``
    #   (config knob about the header), ``proxy.headers.x-amz-security-token``
    #   (flattened telemetry key — dotted heads are negative by design, as
    #   in the rule above) — but a directly preceding alphanumeric must
    #   stay a match: a bytes-repr header dump renders the newline before
    #   the label as a LITERAL ``\r\n``, putting ``n`` right before the
    #   ``x`` (``send: b'…\r\nx-amz-security-token: FwoG…'`` —
    #   http.client debuglevel / botocore DEBUG wire dumps). Real left
    #   neighbors (``?``, ``&``, quotes, line starts) pass either way.
    #   Prose that merely NAMES the header (``set the x-amz-security-token
    #   header``, ``header: x-amz-security-token``) stays negative — the
    #   separator must directly follow the label.
    r"(?i)(?:[\"']x-amz-security-token[\"']\s*:\s*[\"']"
    r"|(?<![_.\-])x-amz-security-token\s*[:=])",
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
    # --- Secret-class patterns mirrored from memtomem LTM (issue #1488) -----
    # These provider-token patterns originated at the LTM trust boundary and
    # are mirrored back here for routing coherence (reverse sync, issue
    # #1491). All are case-SENSITIVE on purpose: provider prefixes are
    # fixed-case (``sk-``, ``AIza``, ``glpat-``, ``hf_``, ``gh*_``) — wrapping
    # them under ``(?i)`` reintroduces false positives (``AIZA``, ``GLPAT-``).
    # Segments are bounded ({20,200}), digit-lookaheads capped ({0,33}), and
    # the hyphen-inclusive tokens use exact lengths, so scanning stays
    # linear-time — a crafted ``sk-proj-``/``glpat-`` repetition cannot drive
    # O(n^2) backtracking.
    #
    # Modern OpenAI keys (sk-proj-/sk-svcacct-/sk-admin-): the legacy
    # ``sk-[a-zA-Z0-9]{20,}`` rule above MISSES these — the hyphen after the
    # class word halts the alphanumeric run. Anchored on the embedded
    # ``T3BlbkFJ`` marker (base64 of "OpenAI") between two base64url segments.
    # The trailing ``(?![A-Za-z0-9_-])`` guard stops the capped greedy from
    # matching only the first 200 chars of a longer token-char run.
    r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,200}T3BlbkFJ[A-Za-z0-9_-]{20,200}(?![A-Za-z0-9_-])",
    # Anthropic keys (sk-ant-apiNN-/adminNN-): also missed by the legacy rule.
    # Anchored on the exact canonical body — 93 base64url chars + the literal
    # "AA" terminal (a bit-alignment artifact every real key carries). The
    # exact length + AA terminal rejects digit-bearing kebab slugs like
    # "sk-ant-api03-release-notes-2026-migration-guide". The terminal guard is
    # ``(?![A-Za-z0-9_-])``, NOT ``\b``: ``-`` is in the body charset, so a
    # bare ``\b`` would treat ``AA-`` as a boundary. The OAuth token
    # (sk-ant-oat01-) has no canonical length and is omitted.
    r"\bsk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_-]{93}AA(?![A-Za-z0-9_-])",
    # GitHub token family completion: gho_ (OAuth), ghu_ (user-to-server),
    # ghs_ (server-to-server / Actions GITHUB_TOKEN), ghr_ (refresh). Same
    # base62 shape as the already-covered ghp_. {36,} min — ghs_ runs longer.
    r"\bgh[ousr]_[A-Za-z0-9]{36,}\b",
    # Google API key (GCP / Firebase / Maps / Gemini): fixed AIza + 35
    # base64url chars (39 total). Trailing lookahead pins the exact length so
    # it won't fire on the AIza-prefixed head of a longer blob.
    r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])",
    # GitLab personal access token: the ``glpat-`` literal + the exact classic
    # 20-char body with a terminal guard. Exact length rejects digit-bearing
    # kebab slugs like "glpat-form-builder-component-name-2026". Newer 17.x
    # routable tokens (longer body + ".<checksum>") are a separate follow-up.
    r"\bglpat-[0-9A-Za-z_-]{20}(?![0-9A-Za-z_-])",
    # Hugging Face access token. The short ``hf_`` prefix is collision-prone
    # (hf_hub, hf_model), so: exact 34-char alnum body, both word boundaries,
    # and a digit-in-body lookahead to reject letter-only identifiers.
    r"\bhf_(?=[A-Za-z0-9]{0,33}[0-9])[A-Za-z0-9]{34}\b",
    # PyPI / TestPyPI upload token. The macaroon header ``AgEIcHlwaS5vcmc`` /
    # ``AgENdGVzdC5weXBpLm9yZw`` (base64 of "pypi.org" / "test.pypi.org") is
    # an exact fingerprint → lowest FP of the set.
    r"\bpypi-Ag(?:EIcHlwaS5vcmc|ENdGVzdC5weXBpLm9yZw)[A-Za-z0-9_-]{50,}",
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
