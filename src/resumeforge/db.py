"""SQLite store with in-code migrations tracked by PRAGMA user_version.

To change the schema, append a new SQL script to MIGRATIONS. Never edit an applied one.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .redact import redact, redact_obj

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
    # 2: richer sessions; evidence can belong to a session
    """
    ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'coding';   -- coding | chat
    ALTER TABLE sessions ADD COLUMN project_path TEXT;
    ALTER TABLE sessions ADD COLUMN link_method TEXT;                     -- how project_id was matched
    ALTER TABLE sessions ADD COLUMN user_message_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE sessions ADD COLUMN assistant_message_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE sessions ADD COLUMN files_touched TEXT NOT NULL DEFAULT '{}';  -- {"edited": [...], "read": [...]}
    ALTER TABLE sessions ADD COLUMN summary TEXT;                         -- last assistant summary
    ALTER TABLE evidence ADD COLUMN session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE;
    CREATE INDEX evidence_session ON evidence(session_id);
    CREATE INDEX sessions_project ON sessions(project_id);
    """,
    # 3: extraction stage
    """
    ALTER TABLE skills_observed ADD COLUMN kind TEXT NOT NULL DEFAULT 'skill';       -- skill | technology | language
    ALTER TABLE skills_observed ADD COLUMN origin TEXT NOT NULL DEFAULT 'github';    -- github | llm
    UPDATE skills_observed SET kind = 'language';
    CREATE TABLE project_facts (
        project_id        INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
        summary           TEXT,
        outcome           TEXT,          -- shipped | prototype | abandoned | NULL when unsupported
        outcome_reason    TEXT,
        authorship        TEXT,          -- self_written | ai_assisted | mostly_ai | unknown
        authorship_reason TEXT,
        facts             TEXT NOT NULL, -- validated JSON with evidence ids
        rejected          TEXT NOT NULL DEFAULT '[]',  -- claims dropped for lack of valid evidence
        model             TEXT,
        input_hash        TEXT NOT NULL,
        extracted_at      TEXT NOT NULL
    );
    CREATE TABLE llm_cache (
        key        TEXT PRIMARY KEY,     -- sha256 of model, prompt and schema
        model      TEXT NOT NULL,
        response   TEXT NOT NULL,        -- validated JSON
        created_at TEXT NOT NULL
    );
    CREATE TABLE llm_calls (
        id                INTEGER PRIMARY KEY,
        ts                TEXT NOT NULL,
        purpose           TEXT,
        model             TEXT,
        cached            INTEGER NOT NULL DEFAULT 0,
        ok                INTEGER NOT NULL,
        attempts          INTEGER NOT NULL DEFAULT 1,
        input_tokens      INTEGER NOT NULL DEFAULT 0,
        output_tokens     INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd          REAL,
        error             TEXT
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
    fields = {k: json.dumps(redact_obj(v)) if isinstance(v, (dict, list)) else
              redact(v) if isinstance(v, str) and k not in ("url", "deployed_url") else v
              for k, v in fields.items()}
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
                    snippet: str, created_at: str | None = None, session_id: int | None = None) -> int:
    """Insert or refresh by (source, kind, url_or_path). Snippet is redacted before storage."""
    conn.execute(
        """INSERT INTO evidence (project_id, source, kind, url_or_path, snippet, created_at, session_id)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(source, kind, url_or_path) DO UPDATE SET
               project_id=excluded.project_id, snippet=excluded.snippet, created_at=excluded.created_at,
               session_id=excluded.session_id""",
        (project_id, source, kind, url_or_path, redact(snippet), created_at or now(), session_id),
    )
    return conn.execute("SELECT id FROM evidence WHERE source=? AND kind=? AND url_or_path=?",
                        (source, kind, url_or_path)).fetchone()[0]


def observe_skill(conn, *, skill: str, evidence_id: int, kind: str = "skill", origin: str = "github") -> None:
    """Record a skill against an evidence row. The project is taken from the evidence row.
    The name is normalized through the alias table in skills.py."""
    from .skills import canonical
    row = conn.execute("SELECT project_id FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    if row is None:
        raise NoEvidenceError(f"skill {skill!r}: evidence {evidence_id} does not exist")
    name = canonical(skill)
    if not name:
        raise ValueError("empty skill name")
    conn.execute("INSERT OR IGNORE INTO skills_observed (skill, project_id, evidence_id, kind, origin) VALUES (?,?,?,?,?)",
                 (name, row[0], evidence_id, kind, origin))


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


SESSION_TEXT_FIELDS = ("title", "summary")


def upsert_session(conn, *, source: str, external_id: str, **fields) -> int:
    """Insert or update by (source, external_id). Text fields are redacted; dict/list fields JSON-encoded."""
    clean = {}
    for k, v in fields.items():
        if isinstance(v, (dict, list)):
            v = json.dumps(redact_obj(v))
        elif k in SESSION_TEXT_FIELDS and isinstance(v, str):
            v = redact(v)
        clean[k] = v
    cols = ["source", "external_id", *clean]
    updates = ", ".join(f"{c}=excluded.{c}" for c in clean) or "source=excluded.source"
    conn.execute(
        f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(source, external_id) DO UPDATE SET {updates}",
        [source, str(external_id), *clean.values()],
    )
    return conn.execute("SELECT id FROM sessions WHERE source=? AND external_id=?",
                        (source, str(external_id))).fetchone()[0]
