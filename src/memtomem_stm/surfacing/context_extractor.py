"""Extract search queries from MCP tool call arguments."""

from __future__ import annotations

import re
import shlex
from typing import Any

from memtomem_stm.proxy.privacy import contains_sensitive_content
from memtomem_stm.surfacing.config import SurfacingConfig


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX_RE = re.compile(r"^[0-9a-f]{24,}$", re.I)
_SEMANTIC_KEYS = {"query", "search", "path", "file", "url", "topic", "name", "title", "description"}
_PATH_KEYS = {"path", "file", "filepath", "file_path", "filename"}
# Grep/Glob carry their search target in ``pattern`` (or ``glob``); tokenizing
# it turns one opaque regex/glob token into searchable words so the query can
# clear ``min_query_tokens`` and surfacing actually fires for those tools.
_PATTERN_KEYS = {"pattern", "glob"}

# --- Native-tool (``server == "builtin"``) extraction --------------------------
# Hook calls carry raw shell commands and absolute paths. Sent verbatim, shell
# syntax and home-directory prefixes matched whatever memory happened to mention
# them. The rules below are deliberately regex-level: the text is never
# executed, so a quoting-exact shell parse buys nothing, and ``shlex`` both
# mis-splits operators glued to words (``a.py|head``) and raises on heredoc
# bodies containing an apostrophe.
_BUILTIN_SERVER = "builtin"
_COMMAND_KEYS = {"command", "cmd"}
# Only the head of a command is read. The rules below are regex-level and some
# rescan runs of digits or dashes, so their cost grows with the square of the
# input; capping it bounds a hook call's extraction at a few milliseconds. The
# privacy scan still reads the whole value. (The pre-builtin path read 400.)
_MAX_COMMAND_CHARS = 1000
_PATH_STOP_TOKENS = frozenset({"src", "lib", "tests", "test", "packages", "pkg"})
_HEX8_RE = re.compile(r"^[0-9a-f]{8}$", re.I)
_HOME_PREFIX_RE = re.compile(r"^(?:~/|/Users/[^/]+/|/home/[^/]+/)")
# Two scratchpad rules. Shell commands use the broad one: a command naming any
# temp path is mostly a throwaway probe (``/tmp/rv1/x.py``), and the replay
# measured ``/tmp`` commands as low-yield. A path *argument* (Read, Grep, Glob)
# under a non-Claude ``/tmp/<dir>/`` was, in a month of transcripts, a
# repository worktree or clone, so path arguments use the narrow rule: only Claude Code's own temp
# root, which it names ``claude-<uid>`` under the OS temp dir, plus the
# per-user ``$TMPDIR`` (``/var/folders/…`` on macOS).
_SCRATCH_PREFIXES = ("/private/tmp/", "/tmp/", "/var/folders/", "$TMPDIR", "${TMPDIR}")
_ARG_SCRATCH_PREFIXES = ("/var/folders/", "$TMPDIR", "${TMPDIR}")
_CLAUDE_TEMP_ROOT_RE = re.compile(r"^/(?:private/)?tmp/claude-[0-9]+(?:/|$)")
_TEMP_ROOT_RE = re.compile(r"^/(?:private/)?tmp/")
_SEGMENT_SPLIT_RE = re.compile(r"\n|;|&&|\|\||\|")
_LEADING_ASSIGNMENTS_RE = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
_INLINE_PYTHON_RE = re.compile(r"^\s*(?:uv\s+run\s+)?python[0-9.]*\s+(?:-c\b|-(?:\s|$))")
# The quoted program after ``-c`` is emptied before segment splitting, so a
# ``;`` or newline inside it cannot split it into pseudo-commands.
_INLINE_CODE_RE = re.compile(r"""(python[0-9.]*\s+-c\s+)('[^']*'|"(?:[^"\\]|\\.)*")""")
# ``2>&1`` / ``>&-`` duplicate a descriptor and name no file; the next word
# is an argument. Other redirections are removed together with their target.
_FD_DUP_RE = re.compile(r"\d*[<>]&(?:\d+|-)(?!\S)")
_REDIRECT_RE = re.compile(r"&?\d*[<>]+&?\s*\S+")
_PATH_TOKEN_RE = re.compile(
    r"(?<![\w$])(?:(?:~|\$\{?\w+\}?)(?:/[\w.@-]+)+|/?[\w.@-]+(?:/[\w.@-]+)+)"
)
_BARE_FILE_RE = re.compile(r"^[\w@-]+(?:\.[\w-]+)+$")
_WORD_RE = re.compile(r"^[A-Za-z][\w-]*$")
_SHELL_NOISE_WORDS = frozenset(
    {
        # inspection / plumbing verbs
        "sed", "cat", "head", "tail", "wc", "ls", "echo", "grep", "rg", "find", "awk",
        "xargs", "sleep", "true", "false", "git", "gh", "uv", "run", "cd", "printf",
        "sort", "uniq", "cut", "tr", "jq", "python", "python3", "bash", "sh", "env",
        "export", "source", "mkdir", "rm", "cp", "mv", "touch", "diff", "which",
        "time", "timeout", "nohup", "sudo", "exit",
        # shell keywords
        "for", "in", "do", "done", "if", "then", "else", "elif", "fi", "while", "set",
    }
)  # fmt: skip


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
        cwd: str | None = None,
    ) -> str | None:
        """``cwd`` is the hook call's working directory; it only affects
        native-tool (``server == "builtin"``) extraction, where paths under it
        are made relative before tokenizing."""
        # 1. Per-tool template
        tool_cfg = config.context_tools.get(tool)
        if tool_cfg and tool_cfg.query_template:
            return self._validate_query(
                self._apply_template(tool_cfg.query_template, server, tool, arguments),
                config,
                server,
                tool,
            )

        # 2. Explicit context_query parameter (preferred path from the proxy,
        # which extracts ``_context_query`` from upstream args and forwards it
        # via this kwarg without re-inserting it into ``arguments``).
        if isinstance(context_query, str) and context_query.strip():
            return self._validate_query(context_query, config, server, tool)

        # 3. Agent-provided context (legacy: kept for direct engine callers
        # and tests that still pass ``_context_query`` inside ``arguments``).
        if "_context_query" in arguments:
            cq = arguments["_context_query"]
            if isinstance(cq, str) and cq.strip():
                return self._validate_query(cq, config, server, tool)

        if server == _BUILTIN_SERVER:
            return self._extract_builtin(arguments, config, cwd)

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
            elif (
                key in _SEMANTIC_KEYS
                and isinstance(value, (str, int, float))
                and not isinstance(value, bool)
            ):
                # Short strings (<= 2 chars) and numbers still count; containers
                # are skipped so a nested dict or list is never stringified.
                val_str = str(value)
                if not self._is_identifier(val_str):
                    parts.append(val_str)

        # Proxied (non-builtin) tools only — native-tool calls return from
        # ``_extract_builtin`` above and are never padded, because a bare
        # ``Bash`` / ``Read`` token only adds tool-name noise to the search.
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

    def _extract_builtin(
        self, arguments: dict[str, Any], config: SurfacingConfig, cwd: str | None
    ) -> str | None:
        """Query for a native-tool hook call (Bash, Read, Grep, …).

        Sensitive input is checked on the raw values *before* any of the lossy
        transforms below: tokenizing ``?api_key=…`` into ``api key …`` would
        hide it from the engine's only privacy check, which digests a
        sensitive query instead of sending it. The raw values are returned in
        sorted-key order so that digest does not depend on argument order.
        """
        items = sorted((k, v) for k, v in arguments.items() if not k.startswith("_"))
        # Every value a transform below reads, in the form it reads it: an
        # argv-style command is scanned joined, exactly as it is tokenized.
        raw: list[str] = []
        for k, v in items:
            if isinstance(v, str):
                raw.append(v)
            elif k in _COMMAND_KEYS and isinstance(v, list):
                raw.append(self._join_argv(v))
        if any(contains_sensitive_content(v) for v in raw):
            return " ".join(v.strip() for v in raw if v.strip())

        lead: list[str] = []
        parts: list[str] = []
        saw_command = False
        command_contributed = False
        for key, value in items:
            if key in _COMMAND_KEYS and isinstance(value, (str, list)):
                saw_command = True
                text = value if isinstance(value, str) else self._join_argv(value)
                tokens = self._command_tokens(text, cwd)
                if tokens is not None:
                    command_contributed = True
                    parts.extend(tokens)
                continue
            if key == "description" and isinstance(value, str):
                if value.strip():
                    lead.append(self._first_sentence(value, max_chars=200))
                continue
            if key in _PATH_KEYS and isinstance(value, str):
                # Every path argument, with or without a slash or a space:
                # the tokenizer is also where scratchpad paths are dropped.
                parts.extend(self._builtin_path_tokens(value, cwd, broad=False))
                continue
            if isinstance(value, str) and len(value) > 2 and not self._is_identifier(value):
                if key in _PATTERN_KEYS:
                    parts.append(self._tokenize_pattern(value))
                else:
                    parts.append(self._first_sentence(value, max_chars=200))
            elif (
                key in _SEMANTIC_KEYS
                and isinstance(value, (str, int, float))
                and not isinstance(value, bool)
            ):
                val_str = str(value)
                if not self._is_identifier(val_str):
                    parts.append(val_str)

        # A command whose every segment is ``cd``, inline Python or a
        # scratchpad read has no retrieval intent, and its description does
        # not rescue it: Claude Code attaches one to every Bash call.
        if saw_command and not command_contributed:
            return None
        query = " ".join([*lead, *parts]).strip()
        if len(query.split()) < config.min_query_tokens:
            return None
        return query

    @staticmethod
    def _join_argv(argv: list[Any]) -> str:
        """An argv-style command as shell text, each element quoted as needed,
        so ``["python3", "-c", "a; b"]`` keeps its program in one quoted word."""
        return shlex.join(str(a) for a in argv)

    @classmethod
    def _command_tokens(cls, command: str, cwd: str | None) -> list[str] | None:
        """Meaningful tokens of a shell command; ``None`` if no segment contributes.

        Heredoc bodies are cut off unread. Each ``;`` / ``&&`` / ``||`` / ``|``
        / newline segment is classified on its own, so a scratchpad read or
        inline script next to a real command does not discard the real one.
        """
        contributed = False
        tokens: list[str] = []
        head = _INLINE_CODE_RE.sub(r"\1''", command[:_MAX_COMMAND_CHARS].split("<<", 1)[0])
        for segment in _SEGMENT_SPLIT_RE.split(head):
            segment = _LEADING_ASSIGNMENTS_RE.sub("", segment)
            words = segment.split()
            if not words or words[0] == "cd" or _INLINE_PYTHON_RE.match(segment):
                continue
            # Scratchpad classification sees every path in the segment —
            # inside a flag (``--file=/tmp/patterns.txt``) or a redirection
            # (``</tmp/build.log``, ``> /tmp/out.txt``) — before either is
            # dropped below. This matches the replay's class definition.
            if any(
                cls._is_scratch_path(m.group(0), cwd, broad=True)
                for m in _PATH_TOKEN_RE.finditer(segment)
            ):
                continue
            segment = _REDIRECT_RE.sub(" ", _FD_DUP_RE.sub(" ", segment))
            seg_tokens: list[str] = []
            for word in segment.split():
                # Quotes are stripped, not rejected: a quoted search term
                # (``grep -rn "SurfacingConfig"``) is often the topic itself.
                bare = word.strip("'\"(),")
                if not bare or bare.startswith("-"):
                    continue
                match = _PATH_TOKEN_RE.search(bare)
                path = match.group(0) if match else (bare if _BARE_FILE_RE.match(bare) else None)
                if path is not None:
                    seg_tokens.extend(cls._builtin_path_tokens(path, cwd, broad=True))
                elif _WORD_RE.match(bare) and bare.lower() not in _SHELL_NOISE_WORDS:
                    seg_tokens.append(bare)
            contributed = True
            tokens.extend(seg_tokens)
        return tokens if contributed else None

    @staticmethod
    def _is_scratch_path(path: str, cwd: str | None, *, broad: bool) -> bool:
        """A temp-directory path, unless it lies inside the call's ``cwd``:
        a repository checked out under ``/tmp`` is the work, not a scratchpad.

        ``broad`` (shell commands) counts every temp root; otherwise (path
        arguments) only Claude Code's temp root and ``$TMPDIR`` count, so a
        repository under ``/tmp`` read from another ``cwd`` still yields a query.
        """
        p = path.replace("\\", "/")
        if broad:
            if not p.startswith(_SCRATCH_PREFIXES):
                return False
        elif not (p.startswith(_ARG_SCRATCH_PREFIXES) or _CLAUDE_TEMP_ROOT_RE.match(p)):
            return False
        if cwd:
            root = cwd.replace("\\", "/").rstrip("/")
            if p == root or p.startswith(root + "/"):
                return False
        return True

    @classmethod
    def _builtin_path_tokens(cls, path: str, cwd: str | None, *, broad: bool) -> list[str]:
        """Tokens of a path seen in a native-tool call, in path order.

        ``broad`` selects the scratchpad rule (see :meth:`_is_scratch_path`).
        Relative to ``cwd`` when under it; otherwise without a home or temp-root
        (``/tmp/``, ``/private/tmp/``) prefix.
        Scratchpad paths, ``$VAR`` / ``~`` segments, id-like segments (uuid,
        long hex, 8-hex), a small structural stop-list and the file extension
        carry no topic and are dropped: ``src/memtomem_stm/surfacing/context_extractor.py`` becomes
        ``memtomem stm surfacing context extractor``.
        """
        p = path.strip().replace("\\", "/")
        if cls._is_scratch_path(p, cwd, broad=broad):
            return []
        if cwd:
            root = cwd.replace("\\", "/").rstrip("/")
            if p == root:
                p = ""
            elif p.startswith(root + "/"):
                p = p[len(root) + 1 :]
        p = _TEMP_ROOT_RE.sub("", _HOME_PREFIX_RE.sub("", p))
        segments = [s for s in p.split("/") if s]
        tokens: list[str] = []
        for i, seg in enumerate(segments):
            if seg.startswith(("$", "~")):
                continue
            if i == len(segments) - 1 and "." in seg.lstrip("."):
                seg = seg.rsplit(".", 1)[0]
            # Checked on the stem, so ``<uuid>.json`` is an identifier too.
            if _UUID_RE.match(seg) or _HEX_RE.match(seg) or _HEX8_RE.match(seg):
                continue
            for t in re.split(r"[._\-\s]+", seg):
                if len(t) <= 1 or t.isdigit() or _HEX8_RE.match(t):
                    continue
                if t.lower() in _PATH_STOP_TOKENS:
                    continue
                tokens.append(t)
        return tokens

    @staticmethod
    def _validate_query(query: str, config: SurfacingConfig, server: str, tool: str) -> str | None:
        normalized = query.strip()
        # Sensitive queries are transformed to a safe digest by the engine;
        # preserve the original here so that digest is stable and auditable.
        if contains_sensitive_content(normalized):
            return normalized
        if len(normalized.split()) < config.min_query_tokens:
            return None
        return normalized

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
