import sqlite3

import pytest

from resumeforge import db


def test_migrations_are_versioned_and_idempotent(tmp_path):
    p = tmp_path / "m.db"
    c = db.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    c.close()
    c = db.connect(p)  # re-open: nothing re-applied, no error
    assert db.migrate(c) == len(db.MIGRATIONS)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "evidence", "skills_observed", "sessions", "settings"} <= tables


def test_evidence_columns(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(evidence)")]
    assert cols == ["id", "project_id", "source", "kind", "url_or_path", "snippet", "created_at", "session_id"]


def test_upserts_do_not_duplicate(conn):
    a = db.upsert_project(conn, source="github", external_id="1", name="x", stars=1, topics=["a"])
    b = db.upsert_project(conn, source="github", external_id="1", name="x2", stars=7)
    assert a == b
    row = conn.execute("SELECT * FROM projects").fetchone()
    assert (row["name"], row["stars"], row["topics"]) == ("x2", 7, '["a"]')
    e1 = db.upsert_evidence(conn, project_id=a, source="github", kind="readme", url_or_path="u", snippet="v1")
    e2 = db.upsert_evidence(conn, project_id=a, source="github", kind="readme", url_or_path="u", snippet="v2")
    assert e1 == e2 and conn.execute("SELECT snippet FROM evidence").fetchone()[0] == "v2"


def test_snippets_are_redacted(conn):
    db.upsert_evidence(conn, project_id=None, source="s", kind="k", url_or_path="u",
                       snippet="key sk-ant-api03-abcdefghijklmnop")
    assert "sk-ant" not in conn.execute("SELECT snippet FROM evidence").fetchone()[0]


def test_skill_requires_existing_evidence(conn):
    with pytest.raises(db.NoEvidenceError):
        db.observe_skill(conn, skill="Python", evidence_id=999)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO skills_observed (skill, project_id, evidence_id) VALUES ('x', NULL, NULL)")


def test_settings_roundtrip(conn):
    db.set_setting(conn, "k", {"a": 1})
    assert db.get_setting(conn, "k") == {"a": 1} and db.get_setting(conn, "missing", 5) == 5
