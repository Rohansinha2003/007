"""Claude Code session logs: ~/.claude/projects/<encoded-cwd>/<session>.jsonl.

Observed record types used here:
  user       -> message.content (str or list of blocks); real prompts are text blocks, tool results are not
  assistant  -> message.content tool_use blocks (Write/Edit/MultiEdit/NotebookEdit carry file_path)
  ai-title   -> aiTitle (session summary)
Every record carries cwd, timestamp, gitBranch.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ..db import add_evidence

DEFAULT_ROOT = Path.home() / ".claude" / "projects"
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_NOISE = re.compile(r"<(system-reminder|local-command-[a-z]+|command-[a-z]+|ide_[a-z]+)>.*?</\1>", re.S)


def _prompt_text(content) -> str:
    if isinstance(content, str):
        parts = [content]
    else:
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    text = _NOISE.sub("", "\n".join(parts)).strip()
    if text.startswith("<") or text.startswith("Caveat:") or text.startswith("[Request interrupted"):
        return ""
    return text


def ingest_file(conn, path: Path, home: str | None = None, max_prompts: int = 20) -> int:
    home = home or str(Path.home())
    cwd = title = branch = None
    first_ts = last_ts = None
    prompts: list[tuple[int, str, str]] = []
    edited: set[str] = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            if o.get("cwd") and not cwd:
                cwd = o["cwd"]
            branch = branch or o.get("gitBranch")
            ts = o.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            if t == "ai-title":
                title = o.get("aiTitle")
            elif t == "user" and not o.get("isSidechain") and "toolUseResult" not in o:
                text = _prompt_text((o.get("message") or {}).get("content", ""))
                if len(text) >= 15:
                    prompts.append((lineno, ts, text))
            elif t == "assistant":
                for b in (o.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in EDIT_TOOLS:
                        fp = (b.get("input") or {}).get("file_path") or (b.get("input") or {}).get("notebook_path")
                        if fp:
                            edited.add(fp)
    if not cwd:
        return 0
    project = os.path.basename(cwd.rstrip("/")) or "home"
    ref_base = str(path)
    n = 0
    if title:
        add_evidence(conn, source="claude_code", kind="session_title", project_name=project,
                     ref=f"{ref_base}#title", content=f"AI coding session: {title}", ts=first_ts,
                     meta={"branch": branch})
        n += 1
    for lineno, ts, text in prompts[:max_prompts]:
        add_evidence(conn, source="claude_code", kind="prompt", project_name=project,
                     ref=f"{ref_base}:{lineno}", content=text[:2000], ts=ts)
        n += 1
    if edited:
        rel = sorted(p[len(cwd) + 1:] if p.startswith(cwd + "/") else p.replace(home, "~") for p in edited)
        add_evidence(conn, source="claude_code", kind="files_edited", project_name=project,
                     ref=f"{ref_base}#files", content="Files written or edited in session: " + ", ".join(rel[:150]),
                     ts=last_ts, meta={"count": len(rel)})
        n += 1
    return n


def ingest(conn, root: Path = DEFAULT_ROOT, exclude_cwd: str | None = None) -> int:
    n = 0
    for p in sorted(Path(root).glob("*/*.jsonl")):
        if exclude_cwd and p.parent.name == re.sub(r"[^A-Za-z0-9]", "-", exclude_cwd):
            continue
        n += ingest_file(conn, p)
    conn.commit()
    return n
