from resumeforge.ingest import claude_code, codex, github

from .conftest import fake_gh


def test_claude_code(conn, claude_root):
    n = claude_code.ingest(conn, claude_root)
    rows = {r["kind"]: r for r in conn.execute("SELECT * FROM evidence")}
    assert n == 3 and set(rows) == {"prompt", "session_title", "files_edited"}
    p = rows["prompt"]["content"]
    assert "system-reminder" not in p and "sk-ant-" not in p and "WhatsApp CRM" in p
    assert rows["prompt"]["project_name"] == "Small_CRM" and rows["prompt"]["ref"].endswith(":2")
    assert "src/server/auth.ts" in rows["files_edited"]["content"]
    assert "WhatsApp-based CRM" in rows["session_title"]["content"]


def test_codex(conn, codex_root):
    assert codex.ingest(conn, codex_root) == 2
    kinds = [r[0] for r in conn.execute("SELECT kind FROM evidence ORDER BY id")]
    assert kinds == ["prompt", "outcome"]


def test_github(conn):
    n = github.ingest(conn, api=fake_gh)
    assert n == 4  # repo, languages, readme, commit; skips fork and profile repo
    readme = conn.execute("SELECT content FROM evidence WHERE kind='readme'").fetchone()[0]
    assert "me@x.com" not in readme
    assert github.profile(api=fake_gh)["name"] == "Dev Person"


def test_sources_merge_into_one_project(conn, claude_root):
    github.ingest(conn, api=fake_gh)
    claude_code.ingest(conn, claude_root)
    assert [r[0] for r in conn.execute("SELECT DISTINCT project_key FROM evidence")] == ["smallcrm"]
