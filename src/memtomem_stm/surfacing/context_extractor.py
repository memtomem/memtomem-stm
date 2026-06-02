"""Extract search queries from MCP tool call arguments."""

from __future__ import annotations

import re
from typing import Any

from memtomem_stm.surfacing.config import SurfacingConfig


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX_RE = re.compile(r"^[0-9a-f]{24,}$", re.I)
_SEMANTIC_KEYS = {"query", "search", "path", "file", "url", "topic", "name", "title", "description"}
_PATH_KEYS = {"path", "file", "filepath", "file_path", "filename"}
# Grep/Glob carry their search target in ``pattern`` (or ``glob``); tokenizing
# it turns one opaque regex/glob token into searchable words so the query can
# clear ``min_query_tokens`` and surfacing actually fires for those tools.
_PATTERN_KEYS = {"pattern", "glob"}


class ContextExtractor:
    """Extract a search query from tool call context."""

    def extract_query(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        config: SurfacingConfig,
        *,
        context_query: str | None = None,
    ) -> str | None:
        # 1. Per-tool template
        tool_cfg = config.context_tools.get(tool)
        if tool_cfg and tool_cfg.query_template:
            return self._apply_template(tool_cfg.query_template, server, tool, arguments)

        # 2. Explicit context_query parameter (preferred path from the proxy,
        # which extracts ``_context_query`` from upstream args and forwards it
        # via this kwarg without re-inserting it into ``arguments``).
        if isinstance(context_query, str) and context_query.strip():
            return context_query.strip()

        # 3. Agent-provided context (legacy: kept for direct engine callers
        # and tests that still pass ``_context_query`` inside ``arguments``).
        if "_context_query" in arguments:
            cq = arguments["_context_query"]
            if isinstance(cq, str) and cq.strip():
                return cq.strip()

        # 3. Heuristic extraction — prioritize argument values over tool name.
        # Iterate in sorted key order so two identical calls whose arguments
        # were built in a different order produce the same query string (the
        # surfacing cache is keyed on that string, and a stable query keeps the
        # cooldown/dedup heuristics consistent).
        parts: list[str] = []

        for key, value in sorted(arguments.items()):
            if key.startswith("_"):
                continue
            if isinstance(value, str) and len(value) > 2 and not self._is_identifier(value):
                # Tokenize file paths and Grep/Glob patterns into meaningful
                # words; otherwise keep the first sentence of free text.
                if key in _PATH_KEYS and ("/" in value or "\\" in value) and " " not in value:
                    parts.append(self._tokenize_path(value))
                elif key in _PATTERN_KEYS:
                    parts.append(self._tokenize_pattern(value))
                else:
                    parts.append(self._first_sentence(value, max_chars=200))
            elif key in _SEMANTIC_KEYS and not self._is_identifier(str(value)):
                # A semantic key whose value is an opaque id (uuid/hex/bool) is
                # filtered here too — pre-fix only the free-text branch applied
                # the identifier filter, so a semantic-keyed id leaked into the
                # query (inconsistent and a poor search term).
                parts.append(str(value))

        # Fall back to the tool name when the extracted args alone do not clear
        # ``min_query_tokens`` — not only when ``parts`` is empty. KR-4.2: a
        # short-but-non-empty extraction (e.g. ``/etc/hosts`` → "etc hosts",
        # 2 tokens < 3) otherwise returned None below and surfacing never fired,
        # even though appending the tool-name token(s) would clear the floor.
        # The empty-parts case is subsumed (``"".split()`` → 0 tokens, always
        # below ``min_query_tokens`` which is validated ``gt=0``), and the
        # append runs after the deterministic sorted loop so ordering is stable.
        # Not strictly free: a marginal query now consumes a rate-limit /
        # cooldown slot, and a large ``min_query_tokens`` widens how often the
        # tool name dilutes the args — tune it there if this gets noisy.
        if len(" ".join(parts).split()) < config.min_query_tokens:
            parts.append(tool.replace("_", " "))

        query = " ".join(parts).strip()
        if len(query.split()) < config.min_query_tokens:
            return None
        return query

    def _apply_template(
        self,
        template: str,
        server: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> str:
        result = template.replace("{tool_name}", tool).replace("{server}", server)
        for key, value in arguments.items():
            result = result.replace(f"{{arg.{key}}}", str(value))
        return result.strip()

    @staticmethod
    def _is_identifier(value: str) -> bool:
        if _UUID_RE.match(value):
            return True
        if _HEX_RE.match(value):
            return True
        if value.lower() in ("true", "false", "null", "none"):
            return True
        return False

    @staticmethod
    def _tokenize_path(path: str) -> str:
        """Convert a file path into space-separated meaningful tokens.

        /src/auth/jwt_handler.py → "src auth jwt handler py"
        """
        # Strip leading slashes and split by / . _ -
        parts = re.split(r"[/._\-]+", path.strip("/"))
        # Filter out empty, very short, or purely numeric parts
        tokens = [p for p in parts if len(p) > 1 and not p.isdigit()]
        return " ".join(tokens)

    @staticmethod
    def _tokenize_pattern(pattern: str) -> str:
        """Convert a Grep/Glob pattern into space-separated searchable words.

        Splits on regex/glob metacharacters and separators so a pattern like
        ``auth.*token`` or ``**/*.jwt_handler.py`` yields real terms
        (``auth token`` / ``jwt handler py``) instead of one opaque token that
        trips ``min_query_tokens`` and suppresses surfacing. Drops empty,
        single-char, and purely numeric fragments; Unicode word characters
        (e.g. Hangul) are preserved.
        """
        parts = re.split(r"[\W_]+", pattern)
        tokens = [p for p in parts if len(p) > 1 and not p.isdigit()]
        return " ".join(tokens)

    @staticmethod
    def _first_sentence(text: str, max_chars: int = 200) -> str:
        text = text[: max_chars * 2]
        text = text.replace("\n", " ").strip()
        for delim in (". ", "! ", "? ", "\n"):
            idx = text.find(delim)
            if 0 < idx < max_chars:
                return text[: idx + 1]
        return text[:max_chars]
