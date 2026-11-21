"""OpenAI Codex session logs: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl.

Observed records: {"type": "session_meta", "payload": {"cwd": ...}} and
{"type": "event_msg", "payload": {"type": "user_message" | "task_complete", ...}}.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..db import add_evidence

DEFAULT_ROOT = Path.home() / ".codex" / "sessions"


def ingest_file(conn, path: Path, max_prompts: int = 20) -> int:
    cwd = None
    prompts, outcomes = [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = o.get("payload") or {}
            if o.get("type") == "session_meta":
                cwd = p.get("cwd")
            elif o.get("type") == "event_msg" and p.get("type") == "user_message":
                msg = (p.get("message") or "").strip()
                if len(msg) >= 15:
                    prompts.append((lineno, o.get("timestamp"), msg))
            elif o.get("type") == "event_msg" and p.get("type") == "task_complete" and p.get("last_agent_message"):
                outcomes.append((lineno, o.get("timestamp"), p["last_agent_message"].strip()))
    if not cwd:
        return 0
    project = os.path.basename(cwd.rstrip("/"))
    n = 0
    for lineno, ts, text in prompts[:max_prompts]:
        add_evidence(conn, source="codex", kind="prompt", project_name=project, ref=f"{path}:{lineno}",
                     content=text[:2000], ts=ts)
        n += 1
    for lineno, ts, text in outcomes[:5]:
        add_evidence(conn, source="codex", kind="outcome", project_name=project, ref=f"{path}:{lineno}",
                     content="Agent reported: " + text[:1500], ts=ts)
        n += 1
    return n


def ingest(conn, root: Path = DEFAULT_ROOT) -> int:
    n = sum(ingest_file(conn, p) for p in sorted(Path(root).glob("**/*.jsonl")))
    conn.commit()
    return n
