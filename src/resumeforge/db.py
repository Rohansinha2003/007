"""SQLite store with in-code migrations tracked by PRAGMA user_version.

To change the schema, append a new SQL script to MIGRATIONS. Never edit an applied one.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .redact import redact

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE projects (
        id              INTEGER PRIMARY KEY,
        source          TEXT NOT NULL,            -- github, ...
        external_id     TEXT NOT NULL,            -- stable id in the source (GitHub repo id)
        name            TEXT NOT NULL,
        url             TEXT,
        description     TEXT,
        primary_language TEXT,
        languages       TEXT NOT NULL DEFAULT '{}',  -- JSON {language: bytes}
        topics          TEXT NOT NULL DEFAULT '[]',  -- JSON list
        stars           INTEGER NOT NULL DEFAULT 0,
        commit_count    INTEGER,                  -- commits authored by the user
        first_commit_at TEXT,
        last_commit_at  TEXT,
        deployed_url    TEXT,
        has_deployment  INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        UNIQUE (source, external_id)
    );
    CREATE TABLE evidence (
        id          INTEGER PRIMARY KEY,
        project_id  INTEGER REFERENCES projects(id) ON DELETE CASCADE,
        source      TEXT NOT NULL,
        kind        TEXT NOT NULL,
        url_or_path TEXT NOT NULL,
        snippet     TEXT NOT NULL,                -- always redacted
        created_at  TEXT NOT NULL,
        UNIQUE (source, kind, url_or_path)
    );
    CREATE INDEX evidence_project ON evidence(project_id);
    CREATE TABLE skills_observed (
        skill       TEXT NOT NULL,
        project_id  INTEGER REFERENCES projects(id) ON DELETE CASCADE,
        evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
        PRIMARY KEY (skill, evidence_id)
    );
    CREATE TABLE sessions (
        id          INTEGER PRIMARY KEY,
        source      TEXT NOT NULL,                -- claude_code, codex, ...
        external_id TEXT NOT NULL,
        project_id  INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        path        TEXT,
        title       TEXT,
        started_at  TEXT,
        ended_at    TEXT,
        UNIQUE (source, external_id)
    );
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL                       -- JSON
    );
    """,
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:  # one transaction per migration
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {i};\nCOMMIT;")
    return conn.execute("PRAGMA user_version").fetchone()[0]


class NoEvidenceError(ValueError):
    """A skill or claim was recorded without a valid evidence row."""


def upsert_project(conn, *, source: str, external_id: str, name: str, **fields) -> int:
    """Insert or update by (source, external_id). JSON-encodes dict/list fields."""
    fields = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in fields.items()}
    cols = ["source", "external_id", "name", *fields, "created_at", "updated_at"]
    ts = now()
    vals = [source, str(external_id), name, *fields.values(), ts, ts]
    updates = ", ".join(f"{c}=excluded.{c}" for c in ["name", *fields, "updated_at"])
    conn.execute(
        f"INSERT INTO projects ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(source, external_id) DO UPDATE SET {updates}",
        vals,
    )
    return conn.execute("SELECT id FROM projects WHERE source=? AND external_id=?",
                        (source, str(external_id))).fetchone()[0]


def upsert_evidence(conn, *, project_id: int | None, source: str, kind: str, url_or_path: str,
                    snippet: str, created_at: str | None = None) -> int:
    """Insert or refresh by (source, kind, url_or_path). Snippet is redacted before storage."""
    conn.execute(
        """INSERT INTO evidence (project_id, source, kind, url_or_path, snippet, created_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(source, kind, url_or_path) DO UPDATE SET
               project_id=excluded.project_id, snippet=excluded.snippet, created_at=excluded.created_at""",
        (project_id, source, kind, url_or_path, redact(snippet), created_at or now()),
    )
    return conn.execute("SELECT id FROM evidence WHERE source=? AND kind=? AND url_or_path=?",
                        (source, kind, url_or_path)).fetchone()[0]


def observe_skill(conn, *, skill: str, evidence_id: int) -> None:
    """Record a skill against an evidence row. The project is taken from the evidence row."""
    row = conn.execute("SELECT project_id FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    if row is None:
        raise NoEvidenceError(f"skill {skill!r}: evidence {evidence_id} does not exist")
    conn.execute("INSERT OR IGNORE INTO skills_observed (skill, project_id, evidence_id) VALUES (?,?,?)",
                 (skill.strip(), row[0], evidence_id))


def set_setting(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))


def get_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def list_projects(conn) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.*, (SELECT COUNT(*) FROM evidence e WHERE e.project_id=p.id) AS evidence_count
           FROM projects p ORDER BY p.last_commit_at IS NULL, p.last_commit_at DESC, p.name"""
    ).fetchall()
