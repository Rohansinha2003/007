"""SQLite store. One file. Every skill and bullet links to evidence rows."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .redact import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,          -- github | claude_code | codex
    kind        TEXT NOT NULL,          -- repo | readme | languages | commit | prompt | session_title | files_edited
    project_key TEXT NOT NULL,          -- normalized project name used to merge sources
    project_name TEXT NOT NULL,         -- display name
    ref         TEXT NOT NULL,          -- URL or file path (+ locator) the claim can be checked against
    content     TEXT NOT NULL,          -- redacted text
    ts          TEXT,                   -- ISO timestamp when known
    meta        TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source, ref)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    project_key TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    summary     TEXT NOT NULL,
    first_ts    TEXT,
    last_ts     TEXT
);
CREATE TABLE IF NOT EXISTS skills (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,   -- canonical lowercase name
    display     TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'other',
    score       REAL,
    include     INTEGER,
    rationale   TEXT
);
CREATE TABLE IF NOT EXISTS skill_evidence (
    skill_id    INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    PRIMARY KEY (skill_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS bullets (
    id          INTEGER PRIMARY KEY,
    resume_id   TEXT NOT NULL,
    project_key TEXT NOT NULL,
    text        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bullet_evidence (
    bullet_id   INTEGER NOT NULL REFERENCES bullets(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    PRIMARY KEY (bullet_id, evidence_id)
);
"""


class NoEvidenceError(ValueError):
    """Raised when a claim is written without valid evidence."""


def project_key(name: str) -> str:
    """Normalize project names so 'Small_CRM', 'Small-CRM' and 'small-crm-main' merge."""
    k = name.strip().lower()
    k = re.sub(r"[-_]main$", "", k)
    return re.sub(r"[^a-z0-9]", "", k)


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def add_evidence(conn, *, source, kind, project_name, ref, content, ts=None, meta=None) -> int:
    """Insert or update evidence (idempotent on source+ref). Content is always redacted."""
    conn.execute(
        """INSERT INTO evidence(source, kind, project_key, project_name, ref, content, ts, meta)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(source, ref) DO UPDATE SET content=excluded.content, ts=excluded.ts,
               meta=excluded.meta, kind=excluded.kind""",
        (source, kind, project_key(project_name), project_name, ref, redact(content), ts,
         json.dumps(meta or {})),
    )
    return conn.execute("SELECT id FROM evidence WHERE source=? AND ref=?", (source, ref)).fetchone()[0]


def _valid_ids(conn, ids, project_key_=None) -> list[int]:
    ids = sorted({int(i) for i in ids})
    if not ids:
        return []
    q = f"SELECT id FROM evidence WHERE id IN ({','.join('?' * len(ids))})"
    args: list = list(ids)
    if project_key_ is not None:
        q += " AND project_key=?"
        args.append(project_key_)
    return [r[0] for r in conn.execute(q, args)]


def add_skill(conn, *, name, display=None, category="other", evidence_ids) -> int:
    valid = _valid_ids(conn, evidence_ids)
    if not valid:
        raise NoEvidenceError(f"skill {name!r} has no valid evidence")
    canon = name.strip().lower()
    conn.execute(
        """INSERT INTO skills(name, display, category) VALUES (?,?,?)
           ON CONFLICT(name) DO NOTHING""",
        (canon, display or name.strip(), category),
    )
    sid = conn.execute("SELECT id FROM skills WHERE name=?", (canon,)).fetchone()[0]
    conn.executemany("INSERT OR IGNORE INTO skill_evidence VALUES (?,?)", [(sid, e) for e in valid])
    return sid


def add_bullet(conn, *, resume_id, project_key_, text, evidence_ids) -> int:
    """Bullets may only cite evidence belonging to their own project."""
    valid = _valid_ids(conn, evidence_ids, project_key_)
    if not valid:
        raise NoEvidenceError(f"bullet has no valid evidence: {text[:60]!r}")
    cur = conn.execute("INSERT INTO bullets(resume_id, project_key, text) VALUES (?,?,?)",
                       (resume_id, project_key_, text))
    conn.executemany("INSERT INTO bullet_evidence VALUES (?,?)", [(cur.lastrowid, e) for e in valid])
    return cur.lastrowid


def evidence_for_project(conn, key: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM evidence WHERE project_key=? ORDER BY ts, id", (key,)).fetchall()


def project_keys(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT project_key FROM evidence ORDER BY project_key")]


def set_meta(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))
    conn.commit()


def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default
