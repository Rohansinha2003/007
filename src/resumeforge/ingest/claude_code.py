"""Claude Code transcripts: ~/.claude/projects/<cwd with / replaced by ->/<session-id>.jsonl.

One JSON object per line. Formats verified on this machine (see docs/sources.md):
  type=user       message.content is a string or a list of blocks. Real prompts are text blocks.
                  Tool results carry `toolUseResult` and tool_result blocks. Injected text has
                  isMeta=true or is wrapped in tags such as <system-reminder> or <command-name>.
  type=assistant  one record per content block; message.id groups blocks of one reply.
                  tool_use blocks name the tool (Write, Edit, Read, Bash, ...) and its input.
  type=ai-title   aiTitle: generated session title.
Every conversational record carries cwd, timestamp, uuid, isSidechain.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .common import Message, ParsedSession

DEFAULT_ROOT = Path.home() / ".claude" / "projects"
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read"}
_DROP_TAGS = re.compile(
    r"<(system-reminder|ide_opened_file|ide_selection|ide_diagnostics|local-command-[a-z]+|command-[a-z]+|"
    r"task-notification|user-prompt-submit-hook)\b[^>]*>.*?</\1>", re.S)
_UNWRAP_TAGS = re.compile(r"</?pasted_content\b[^>]*>")
_SKIP_PREFIXES = ("[Request interrupted", "Caveat: The messages below")


def clean_prompt(content) -> str:
    if isinstance(content, str):
        parts = [content]
    else:
        parts = [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
    text = _UNWRAP_TAGS.sub("", _DROP_TAGS.sub("", "\n".join(parts))).strip()
    return "" if text.startswith(_SKIP_PREFIXES) else text


def parse(path: Path) -> ParsedSession | None:
    s = ParsedSession(source="claude_code", external_id=path.stem, kind="coding", path=str(path))
    seen_uuid: set[str] = set()
    turn_reply: dict[str, list[str]] = {}   # message.id -> text blocks, for the current turn
    turn_last_id: str | None = None
    turn_last_ts: str | None = None
    edited: set[str] = set()
    read: set[str] = set()

    def flush_turn(lineno_ref: str):
        nonlocal turn_reply, turn_last_id
        if turn_last_id and turn_reply.get(turn_last_id):
            s.messages.append(Message("assistant", "\n\n".join(turn_reply[turn_last_id]).strip(),
                                      lineno_ref, turn_last_ts))
        turn_reply, turn_last_id = {}, None

    last_assistant_ref = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            if t in ("ai-title", "summary"):
                s.title = o.get("aiTitle") or o.get("summary") or s.title
                continue
            if t not in ("user", "assistant") or o.get("isSidechain"):
                continue
            uid = o.get("uuid")
            if uid in seen_uuid:
                continue
            seen_uuid.add(uid)
            ts = o.get("timestamp")
            s.project_path = s.project_path or o.get("cwd")
            if ts:
                s.started_at = s.started_at or ts
                s.ended_at = ts
            msg = o.get("message") or {}
            if t == "user":
                if o.get("isMeta") or "toolUseResult" in o:
                    continue
                content = msg.get("content")
                if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    continue
                text = clean_prompt(content)
                if text:
                    flush_turn(last_assistant_ref)
                    s.messages.append(Message("user", text, f"{path}:{lineno}", ts))
            else:
                for b in msg.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():
                        turn_reply.setdefault(msg.get("id") or uid, []).append(b["text"])
                        turn_last_id, turn_last_ts, last_assistant_ref = msg.get("id") or uid, ts, f"{path}:{lineno}"
                    elif b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if fp and b.get("name") in EDIT_TOOLS:
                            edited.add(fp)
                        elif fp and b.get("name") in READ_TOOLS:
                            read.add(fp)
    flush_turn(last_assistant_ref)
    if not s.project_path and not s.messages:
        return None
    s.files_edited, s.files_read = sorted(edited), sorted(read - edited)
    return s


def find(root: Path = DEFAULT_ROOT) -> list[Path]:
    return sorted(Path(root).glob("*/*.jsonl"))
