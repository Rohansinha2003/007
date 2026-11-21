"""OpenAI Codex sessions: ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<id>.jsonl.

Each line is {"timestamp", "type", "payload"}. Formats verified on this machine:
  type=session_meta                    payload.id, payload.cwd, payload.timestamp, optional payload.git
  type=event_msg payload.type=user_message    payload.message: the user's text
  type=event_msg payload.type=agent_message   payload.message, payload.phase: commentary | final_answer
  type=event_msg payload.type=task_complete   payload.last_agent_message
Titles come from ~/.codex/session_index.jsonl ({"id", "thread_name", "updated_at"}).
"""
from __future__ import annotations

import json
from pathlib import Path

from .common import Message, ParsedSession

DEFAULT_ROOT = Path.home() / ".codex"


def titles(root: Path) -> dict[str, str]:
    out = {}
    idx = Path(root) / "session_index.jsonl"
    if idx.exists():
        for line in idx.read_text(errors="replace").splitlines():
            try:
                o = json.loads(line)
                out[o["id"]] = o.get("thread_name")
            except (json.JSONDecodeError, KeyError):
                continue
    return out


def parse(path: Path, title_map: dict[str, str] | None = None) -> ParsedSession | None:
    s = ParsedSession(source="codex", external_id=path.stem, kind="coding", path=str(path))
    finals: set[str] = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            p, ts, ref = o.get("payload") or {}, o.get("timestamp"), f"{path}:{lineno}"
            if ts:
                s.started_at = s.started_at or ts
                s.ended_at = ts
            if o.get("type") == "session_meta":
                s.external_id = p.get("id") or s.external_id
                s.project_path = p.get("cwd")
                s.git_remote = (p.get("git") or {}).get("repository_url")
            elif o.get("type") != "event_msg":
                continue
            elif p.get("type") == "user_message" and (p.get("message") or "").strip():
                s.messages.append(Message("user", p["message"].strip(), ref, ts))
            elif p.get("type") == "agent_message" and p.get("phase") == "final_answer" and p.get("message"):
                finals.add(p["message"].strip())
                s.messages.append(Message("assistant", p["message"].strip(), ref, ts))
            elif p.get("type") == "task_complete" and (p.get("last_agent_message") or "").strip() not in finals | {""}:
                s.messages.append(Message("assistant", p["last_agent_message"].strip(), ref, ts))
    if not s.project_path and not s.messages:
        return None
    s.title = (title_map or {}).get(s.external_id) or next(
        (m.text.splitlines()[0][:80] for m in s.messages if m.role == "user"), None)
    return s


def find(root: Path = DEFAULT_ROOT) -> list[Path]:
    root = Path(root)
    return sorted([*root.glob("sessions/**/*.jsonl"), *root.glob("archived_sessions/**/*.jsonl")])
