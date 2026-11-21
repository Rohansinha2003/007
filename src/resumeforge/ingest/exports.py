"""ChatGPT and Claude.ai data exports.

No export file exists on the development machine, so these parsers follow the documented
export layouts and accept missing or extra fields. Input: the export .zip or its conversations.json.

ChatGPT (Settings > Data controls > Export): conversations.json = list of
  {id | conversation_id, title, create_time (epoch s), update_time, current_node,
   mapping: {node_id: {message: {id, author: {role}, create_time, content: {content_type, parts | text}},
                       parent, children}}}
  The visible thread is the path from current_node back to the root.

Claude.ai (Settings > Privacy > Export data): conversations.json = list of
  {uuid, name, created_at, updated_at, chat_messages: [{uuid, sender: human | assistant,
   text, content: [{type: "text", text}], created_at}]}
"""
from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .common import Message, ParsedSession


class ExportError(ValueError):
    pass


def load(path: Path) -> list:
    """All conversations from a .json file or every conversations*.json inside a .zip."""
    path = Path(path)
    if zipfile.is_zipfile(path):
        out = []
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if Path(n).name.startswith("conversations") and n.endswith(".json")]
            if not names:
                raise ExportError(f"{path.name}: no conversations*.json inside the zip")
            for n in sorted(names):
                data = json.loads(z.read(n))
                out.extend(data if isinstance(data, list) else data.get("conversations", []))
        return out
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("conversations", [])


def detect(convs: list) -> str:
    for c in convs[:50]:
        if isinstance(c, dict) and "mapping" in c:
            return "chatgpt"
        if isinstance(c, dict) and "chat_messages" in c:
            return "claude_ai"
    raise ExportError("unrecognized export: expected ChatGPT ('mapping') or Claude ('chat_messages') conversations")


def _epoch(t) -> str | None:
    if t in (None, ""):
        return None
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(float(t), timezone.utc).isoformat(timespec="seconds")
    return str(t)


# ---------- ChatGPT ----------

def _chatgpt_text(msg: dict) -> str:
    content = msg.get("content") or {}
    parts = content.get("parts")
    if isinstance(parts, list):
        return "\n".join(p for p in parts if isinstance(p, str)).strip()
    return str(content.get("text") or "").strip()


def _chatgpt_thread(conv: dict) -> list[dict]:
    mapping = conv.get("mapping") or {}
    node_id, chain, seen = conv.get("current_node"), [], set()
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id]
        if node.get("message"):
            chain.append(node["message"])
        node_id = node.get("parent")
    if chain:
        return chain[::-1]
    msgs = [n["message"] for n in mapping.values() if n.get("message")]
    return sorted(msgs, key=lambda m: m.get("create_time") or 0)


def parse_chatgpt(conv: dict, path: Path) -> ParsedSession:
    cid = conv.get("conversation_id") or conv.get("id")
    if not cid:
        raise ExportError("conversation without id")
    s = ParsedSession(source="chatgpt", external_id=cid, kind="chat", path=str(path),
                      title=conv.get("title"), started_at=_epoch(conv.get("create_time")),
                      ended_at=_epoch(conv.get("update_time")))
    for i, m in enumerate(_chatgpt_thread(conv)):
        role = (m.get("author") or {}).get("role")
        if role not in ("user", "assistant") or (m.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
            continue
        text = _chatgpt_text(m)
        if text:
            s.messages.append(Message(role, text, f"https://chatgpt.com/c/{cid}#{m.get('id') or i}",
                                      _epoch(m.get("create_time"))))
    return s


# ---------- Claude.ai ----------

def _claude_text(m: dict) -> str:
    blocks = [b.get("text", "") for b in m.get("content") or [] if isinstance(b, dict) and b.get("type") == "text"]
    return ("\n".join(blocks) if any(blocks) else (m.get("text") or "")).strip()


def parse_claude(conv: dict, path: Path) -> ParsedSession:
    cid = conv.get("uuid") or conv.get("id")
    if not cid:
        raise ExportError("conversation without uuid")
    s = ParsedSession(source="claude_ai", external_id=cid, kind="chat", path=str(path),
                      title=conv.get("name") or None, started_at=conv.get("created_at"),
                      ended_at=conv.get("updated_at"))
    for i, m in enumerate(conv.get("chat_messages") or []):
        role = {"human": "user", "user": "user", "assistant": "assistant"}.get(m.get("sender") or m.get("role"))
        text = _claude_text(m)
        if role and text:
            s.messages.append(Message(role, text, f"https://claude.ai/chat/{cid}#{m.get('uuid') or i}",
                                      m.get("created_at")))
    return s


def parse(path: Path) -> tuple[str, list[ParsedSession], int]:
    """(format, sessions, skipped_count)."""
    convs = load(path)
    fmt = detect(convs)
    fn = parse_chatgpt if fmt == "chatgpt" else parse_claude
    out, skipped = [], 0
    for c in convs:
        try:
            out.append(fn(c, Path(path).resolve()))
        except (ExportError, AttributeError, TypeError, KeyError):
            skipped += 1
    return fmt, out, skipped
