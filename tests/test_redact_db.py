import pytest

from resumeforge import db
from resumeforge.redact import redact


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-abcdefghijklmnopqrstuv",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIAABCDEFGHIJKLMNOP",
    "postgres://user:hunter2pass@db.example.com:5432/app",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
    "someone@example.com",
])
def test_redacts_secrets(secret):
    out = redact(f"before {secret} after")
    assert secret not in out and "REDACTED" in out


def test_redacts_assignments_keeps_name():
    out = redact('OPENAI_API_KEY="abc123def456ghi"')
    assert "abc123def456ghi" not in out and "OPENAI_API_KEY" in out


def test_plain_text_untouched():
    assert redact("Built a REST API in FastAPI") == "Built a REST API in FastAPI"


def test_project_key_merges_variants():
    assert db.project_key("Small_CRM") == db.project_key("Small-CRM") == db.project_key("small-crm-main")


def test_evidence_is_redacted_and_idempotent(conn):
    a = db.add_evidence(conn, source="x", kind="k", project_name="P", ref="r1", content="token ghp_" + "a" * 30)
    b = db.add_evidence(conn, source="x", kind="k", project_name="P", ref="r1", content="updated")
    assert a == b
    assert conn.execute("SELECT content FROM evidence WHERE id=?", (a,)).fetchone()[0] == "updated"
    db.add_evidence(conn, source="x", kind="k", project_name="P", ref="r2", content="token ghp_" + "a" * 30)
    assert "ghp_" not in conn.execute("SELECT content FROM evidence WHERE ref='r2'").fetchone()[0]


def test_no_evidence_no_claim(conn):
    e = db.add_evidence(conn, source="x", kind="k", project_name="P", ref="r", content="c")
    other = db.add_evidence(conn, source="x", kind="k", project_name="Q", ref="q", content="c")
    with pytest.raises(db.NoEvidenceError):
        db.add_skill(conn, name="python", evidence_ids=[])
    with pytest.raises(db.NoEvidenceError):
        db.add_skill(conn, name="python", evidence_ids=[9999])
    with pytest.raises(db.NoEvidenceError):
        db.add_bullet(conn, resume_id="r", project_key_="p", text="t", evidence_ids=[])
    with pytest.raises(db.NoEvidenceError):  # evidence from another project does not count
        db.add_bullet(conn, resume_id="r", project_key_="p", text="t", evidence_ids=[other])
    assert db.add_skill(conn, name="Python", evidence_ids=[e, 9999])
    assert db.add_bullet(conn, resume_id="r", project_key_="p", text="t", evidence_ids=[e])
