"""Google Antigravity (Gemini agent IDE): ~/.gemini/antigravity/conversation_summaries.db.

Per-conversation step logs (conversations/<id>.db|.pb) are binary protobuf and are not read.
The summaries database holds plain text and is enough (verified on this machine):
  conversation_summaries  conversation_id, title, step_count, last_modified_time,
                          last_user_input_time, workspace_uris (JSON list of file:// URIs),
                          parent_conversation_id
  conversations_fts       per chunk (chunk_index >= 0): user_body, agent_body and
                          user_step_byte_offsets / agent_step_byte_offsets = [[step_index, byte_offset], ...]
                          Offsets are UTF-8 byte offsets into the chunk's body.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.parse
from pathlib import Path

from .common import Message, ParsedSession

DEFAULT_ROOT = Path.home() / ".gemini" / "antigravity"
DB_NAME = "conversation_summaries.db"


def split_steps(body: str, offsets_json: str) -> list[tuple[int, str]]:
    """Cut a chunk body into (step_index, text) using byte offsets."""
    try:
        offsets = json.loads(offsets_json or "[]")
    except json.JSONDecodeError:
        return []
    raw = (body or "").encode("utf-8")
    out = []
    for i, (step, start) in enumerate(offsets):
        end = offsets[i + 1][1] if i + 1 < len(offsets) else len(raw)
        text = raw[start:end].decode("utf-8", "replace").strip()
        if text:
            out.append((int(step), text))
    return out


def _iso(ts: str | None) -> str | None:
    return ts.replace(" ", "T", 1) if ts else None


def _created(root: Path, cid: str) -> str | None:
    """No creation time is stored in text; use the conversation file's birth time when available."""
    from datetime import datetime, timezone
    for ext in (".db", ".pb"):
        f = root / "conversations" / f"{cid}{ext}"
        if f.exists():
            st = f.stat()
            t = getattr(st, "st_birthtime", None) or st.st_mtime
            return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")
    return None


def _fts_title(conn, cid: str) -> str | None:
    """Older conversations have an empty summary title; the search index header row (chunk -1) keeps one."""
    row = conn.execute("SELECT title FROM conversations_fts WHERE conversation_id=? AND chunk_index < 0 "
                       "AND title != '' LIMIT 1", (cid,)).fetchone()
    return row[0] if row else None


def parse_db(db_path: Path) -> list[ParsedSession]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = []
    for c in conn.execute("SELECT * FROM conversation_summaries ORDER BY last_modified_time"):
        cid = c["conversation_id"]
        try:
            uris = json.loads(c["workspace_uris"] or "[]")
        except json.JSONDecodeError:
            uris = []
        project = urllib.parse.unquote(urllib.parse.urlparse(uris[0]).path) if uris else None
        s = ParsedSession(source="antigravity", external_id=cid, kind="coding", path=str(db_path),
                          title=c["title"] or _fts_title(conn, cid), project_path=project,
                          ended_at=_iso(c["last_modified_time"]), started_at=_created(db_path.parent, cid))
        steps: list[tuple[int, str, str]] = []
        for ch in conn.execute("SELECT * FROM conversations_fts WHERE conversation_id=? AND chunk_index >= 0 "
                               "ORDER BY chunk_index", (cid,)):
            steps += [(i, "user", t) for i, t in split_steps(ch["user_body"], ch["user_step_byte_offsets"])]
            steps += [(i, "assistant", t) for i, t in split_steps(ch["agent_body"], ch["agent_step_byte_offsets"])]
        steps.sort()
        # Keep every user step; for the agent keep only the last text step of each turn (its summary).
        pending = None
        for i, role, text in steps:
            ref = f"{db_path}#{cid}/step/{i}"
            if role == "user":
                if pending:
                    s.messages.append(pending)
                    pending = None
                s.messages.append(Message("user", text, ref))
            else:
                pending = Message("assistant", text, ref)
        if pending:
            s.messages.append(pending)
        out.append(s)
    conn.close()
    return out


def find(root: Path = DEFAULT_ROOT) -> list[Path]:
    p = Path(root) / DB_NAME
    return [p] if p.exists() else []
