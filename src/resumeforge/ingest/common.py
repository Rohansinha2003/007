"""Shared session model, storage, and project linking for all log and export parsers."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .. import db

USER_CHARS = 4000
ASSISTANT_CHARS = 2000


@dataclass
class Message:
    role: str            # "user" or "assistant"
    text: str
    ref: str             # url_or_path that locates this message
    ts: str | None = None


@dataclass
class ParsedSession:
    source: str
    external_id: str
    kind: str                         # "coding" or "chat"
    path: str                         # log or export file
    title: str | None = None
    project_path: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    messages: list[Message] = field(default_factory=list)
    files_edited: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    git_remote: str | None = None     # remote URL if the tool recorded one


def normalize(name: str) -> str:
    """'Cognix_Agent-main' -> 'cognixagent'. Used to match folder names to repo names."""
    n = name.strip().lower()
    n = re.sub(r"[-_ ](main|master)$", "", n)
    return re.sub(r"[^a-z0-9]", "", n)


_GH_URL = re.compile(r"github\.com[/:]([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?(?=[/#?\s)'\"`>]|$)")


def git_remote(path: str | None) -> str | None:
    """Origin URL of a local checkout, read with git. None if the folder is gone or has no remote."""
    if not path or not os.path.isdir(os.path.join(path, ".git")):
        return None
    try:
        out = subprocess.run(["git", "-C", path, "config", "--get", "remote.origin.url"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def link_project(conn, s: ParsedSession) -> tuple[int | None, str | None]:
    """Match a session to an imported GitHub project. Returns (project_id, method)."""
    projects = conn.execute("SELECT id, name, url FROM projects WHERE source='github'").fetchall()
    if not projects:
        return None, None
    by_repo = {p["name"].lower(): p["id"] for p in projects}
    by_norm: dict[str, list[int]] = {}
    for p in projects:
        by_norm.setdefault(normalize(p["name"]), []).append(p["id"])

    for remote, method in ((s.git_remote, "git remote (recorded)"), (git_remote(s.project_path), "git remote")):
        m = _GH_URL.search(remote or "")
        if m and m.group(2).lower() in by_repo:
            return by_repo[m.group(2).lower()], method
    if s.project_path:
        ids = by_norm.get(normalize(os.path.basename(s.project_path.rstrip("/"))), [])
        if len(ids) == 1:
            return ids[0], "folder name"
    mentioned = {by_repo[m.group(2).lower()] for msg in s.messages if msg.role == "user"
                 for m in _GH_URL.finditer(msg.text) if m.group(2).lower() in by_repo}
    if len(mentioned) == 1:
        return mentioned.pop(), "GitHub URL in messages"
    if s.kind == "chat" and s.title:
        words = {normalize(w) for w in re.findall(r"[A-Za-z0-9_\-]{5,}", s.title)}
        hits = {i for w in words for i in by_norm.get(w, [])}
        if len(hits) == 1:
            return hits.pop(), "repo name in title"
    return None, None


def _rel(paths: list[str], root: str | None) -> list[str]:
    home = str(Path.home())
    out = []
    for p in paths:
        if root and p.startswith(root.rstrip("/") + "/"):
            p = p[len(root.rstrip("/")) + 1:]
        elif p.startswith(home):
            p = "~" + p[len(home):]
        out.append(p)
    return sorted(set(out))


def store(conn, s: ParsedSession) -> int:
    """Upsert the session and its evidence. Re-running replaces this session's evidence set."""
    project_id, method = link_project(conn, s)
    users = [m for m in s.messages if m.role == "user"]
    assistants = [m for m in s.messages if m.role == "assistant"]
    edited, read = _rel(s.files_edited, s.project_path), _rel(s.files_read, s.project_path)
    sid = db.upsert_session(
        conn, source=s.source, external_id=s.external_id, kind=s.kind, path=s.path, title=s.title,
        project_path=s.project_path, project_id=project_id, link_method=method,
        started_at=s.started_at, ended_at=s.ended_at, user_message_count=len(users),
        assistant_message_count=len(assistants), files_touched={"edited": edited, "read": read},
        summary=assistants[-1].text[:ASSISTANT_CHARS] if assistants else None,
    )
    kept = []
    for m in s.messages:
        kind = "user_message" if m.role == "user" else (
            "assistant_summary" if s.kind == "coding" else "assistant_message")
        limit = USER_CHARS if m.role == "user" else ASSISTANT_CHARS
        kept.append(db.upsert_evidence(conn, project_id=project_id, session_id=sid, source=s.source, kind=kind,
                                       url_or_path=m.ref, snippet=m.text[:limit], created_at=m.ts or s.started_at))
    if edited or read:
        snippet = ""
        if edited:
            snippet += f"Files edited ({len(edited)}): " + ", ".join(edited[:200])
        if read:
            snippet += ("\n" if snippet else "") + f"Files read ({len(read)}): " + ", ".join(read[:200])
        kept.append(db.upsert_evidence(conn, project_id=project_id, session_id=sid, source=s.source,
                                       kind="files_touched", url_or_path=f"{s.path}#files", snippet=snippet,
                                       created_at=s.ended_at))
    conn.execute(f"DELETE FROM evidence WHERE session_id=? AND id NOT IN ({','.join('?' * len(kept)) or 'NULL'})",
                 (sid, *kept))
    return sid
