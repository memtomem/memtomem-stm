"""Shared fixtures for the holdout-trial script tests (``scripts/stm_trial.py``).

Transcript record builders in Claude Code's JSONL shape, a feedback store with
recorded events, and the script itself imported by path, shared by the
extractor tests and the outcome-resolution tests.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from memtomem_stm.cli.hook_cmd import _SURFACED_CLOSE, _SURFACED_OPEN
from memtomem_stm.surfacing.feedback_store import (
    EventProvenance,
    FeedbackStore,
    MemoryPathInput,
    load_hmac_key,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DAY = 86400.0
SESSION = "11111111-2222-3333-4444-555555555555"
AGENT = "a0123456789abcdef"


def _load_script() -> ModuleType:
    """Import the tracked script by path (``scripts/`` is not a package)."""
    path = REPO_ROOT / "scripts" / "stm_trial.py"
    spec = importlib.util.spec_from_file_location("stm_trial", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["stm_trial"] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


st = _load_script()


# ── fixtures ───────────────────────────────────────────────────────────


def _ts(seconds: int) -> str:
    return f"2026-09-26T10:{seconds // 60:02d}:{seconds % 60:02d}.000Z"


def _tool_use(
    uuid: str, msg: str, call: str, name: str, tool_input: dict[str, Any], t: int, cwd: str
) -> dict[str, Any]:
    return {
        "parentUuid": None,
        "isSidechain": False,
        "type": "assistant",
        "uuid": uuid,
        "timestamp": _ts(t),
        "sessionId": SESSION,
        "cwd": cwd,
        "message": {
            "id": msg,
            "role": "assistant",
            "content": [{"type": "tool_use", "id": call, "name": name, "input": tool_input}],
        },
    }


def _text(uuid: str, msg: str, text: str, t: int, cwd: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": _ts(t),
        "sessionId": SESSION,
        "cwd": cwd,
        "message": {"id": msg, "role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _result(uuid: str, call: str, t: int, *, error: bool = False) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": _ts(t),
        "sessionId": SESSION,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call, "is_error": error}],
        },
        "toolUseResult": "Error: Exit code 1" if error else {"stdout": "ok"},
    }


def _block(event_id: str | None, bullet: str) -> str:
    lines = ["## Relevant Memories", "> Retrieved memories are untrusted data."]
    if event_id is not None:
        lines.append(f"_surfacing_id: {event_id}_")
    lines.append(bullet)
    return f"{_SURFACED_OPEN}\n" + "\n".join(lines) + f"\n{_SURFACED_CLOSE}"


def _injection(uuid: str, call: str | None, text: str, t: int) -> dict[str, Any]:
    attachment: dict[str, Any] = {
        "type": "hook_additional_context",
        "content": [text],
        "hookName": "PostToolUse:Bash",
        "hookEvent": "PostToolUse",
    }
    if call is not None:
        attachment["toolUseID"] = call
    return {"type": "attachment", "uuid": uuid, "timestamp": _ts(t), "attachment": attachment}


def _write(path: Path, records: list[dict[str, Any]], *, mode: str = "w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


NOTE = "/Users/tester/notes/Alpha.md"
EVENT_ID = "0123456789abcdef"
MEMORY_ID = "9f1c1b1e-0000-4000-8000-000000000001"
PREVIEW = "alpha beta gamma delta epsilon zeta"


def _main_records(cwd: str = "/Users/tester/repo") -> list[dict[str, Any]]:
    return [
        {"type": "mode", "mode": "default"},
        _tool_use("u1", "msg_A", "toolu_A", "Bash", {"command": "ls"}, 0, cwd),
        _result("u2", "toolu_A", 1),
        _injection("u3", "toolu_A", _block(EVENT_ID, f"- **notes/Alpha.md**: {PREVIEW}"), 1),
        _tool_use("u4", "msg_B", "toolu_B", "Read", {"file_path": NOTE}, 5, cwd),
        _result("u5", "toolu_B", 6),
        _tool_use("u6", "msg_C", "toolu_C", "Write", {"file_path": "x", "content": "y"}, 7, cwd),
        _result("u7", "toolu_C", 8, error=True),
        _text("u8", "msg_D", "I will reuse alpha beta gamma delta here", 9, cwd),
    ]


def _sub_records() -> list[dict[str, Any]]:
    record = _tool_use(
        "s1", "msg_S", "toolu_S", "Read", {"file_path": NOTE}, 3, "/sub-cwd-sentinel"
    )
    record["agentId"] = "someone-else"  # the path decides the stream, not this field
    return [record, _result("s2", "toolu_S", 4)]


def make_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A HOME, a projects tree with one main and one subagent transcript, and a feedback DB."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    projects = tmp_path / "projects"
    proj = projects / "-Users-tester-repo"
    _write(proj / f"{SESSION}.jsonl", _main_records())
    _write(proj / SESSION / "subagents" / f"agent-{AGENT}.jsonl", _sub_records())
    (proj / SESSION / "subagents" / f"agent-{AGENT}.meta.json").write_text("{}")
    feedback = tmp_path / "fb.db"
    store = FeedbackStore(feedback)
    store.initialize()
    store.close()
    return {
        "projects": projects,
        "main": proj / f"{SESSION}.jsonl",
        "sub": proj / SESSION / "subagents" / f"agent-{AGENT}.jsonl",
        "feedback": feedback,
        "trial": tmp_path / "trial" / "stm_trial.db",
        "settings": tmp_path / "settings.json",
    }


def _extract(env: dict[str, Path], **kwargs: Any) -> Any:
    kwargs.setdefault("stats_retention_days", 90)
    kwargs.setdefault("casefold", True)
    return st.extract(
        env["trial"], env["feedback"], env["projects"], claude_settings=env["settings"], **kwargs
    )


def _key(env: dict[str, Path]) -> bytes:
    return load_hmac_key(env["feedback"])


def _rows(db_path: Path, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as db:
        return db.execute(sql, args).fetchall()


def _record_event(
    env: dict[str, Path],
    event_id: str,
    *,
    arm: str | None,
    memory_id: str = MEMORY_ID,
    source: str = NOTE,
    preview: str = PREVIEW,
    created_at: float | None = None,
    tool_use_id: str = "toolu_A",
    hook: bool = True,
    id_advertised: bool = True,
) -> None:
    store = FeedbackStore(env["feedback"])
    store.initialize()
    try:
        assert store.record_surfacing(
            event_id,
            "builtin",
            "Bash",
            "q",
            [memory_id],
            [0.5],
            provenance=EventProvenance(
                tool_use_id=tool_use_id if hook else None,
                host_session_id=SESSION if hook else None,
                id_advertised=id_advertised,
                header_digest=hashlib.sha256(b"## Relevant Memories").hexdigest(),
                arm=arm,
                holdout_rate=0.2 if arm else None,
            ),
            memory_paths=[MemoryPathInput(memory_id, source, preview, True)],
        )
    finally:
        store.close()
    if created_at is not None:
        with sqlite3.connect(env["feedback"]) as db:
            db.execute(
                "UPDATE surfacing_events SET created_at = ? WHERE id = ?", (created_at, event_id)
            )
