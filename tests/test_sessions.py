import json

import pytest
from typer.testing import CliRunner

from resumeforge import cli, db
from resumeforge.ingest import antigravity, claude_code, codex, common, exports

from . import fixtures as fx

runner = CliRunner()


@pytest.fixture
def with_projects(conn):
    for i, name in enumerate(["alpha", "beta", "gamma", "Gamma-App-Old"]):
        db.upsert_project(conn, source="github", external_id=str(i), name=name, url=f"https://github.com/dev/{name}")
    conn.commit()
    return conn


# ---------- Claude Code ----------

def test_claude_code_parse(tmp_path):
    s = claude_code.parse(fx.claude_code(tmp_path))
    users = [m.text for m in s.messages if m.role == "user"]
    assistants = [m.text for m in s.messages if m.role == "assistant"]
    assert users == [f"Add OAuth login. key {fx.FAKE_KEY}", "Now write tests. Mail me at dev@fake.com"]
    assert assistants == ["Added Google OAuth in app/auth.py.", "Wrote 4 tests; all pass."]  # turn-final text only
    assert s.title == "Add OAuth login" and s.project_path == "/Users/dev/Desktop/alpha-main"
    assert s.files_edited == ["/Users/dev/Desktop/alpha-main/app/auth.py", "/Users/dev/Desktop/alpha-main/tests/test_auth.py"]
    assert s.files_read == ["/Users/dev/Desktop/alpha-main/app/main.py"]
    assert (s.started_at, s.ended_at) == ("2026-11-01T09:59:00Z", "2026-11-01T10:07:00Z")
    assert s.messages[0].ref.endswith("s1.jsonl:3")


def test_claude_code_store_redacts_and_links(with_projects, tmp_path):
    conn = with_projects
    common.store(conn, claude_code.parse(fx.claude_code(tmp_path)))
    row = conn.execute("SELECT s.*, p.name FROM sessions s JOIN projects p ON p.id=s.project_id").fetchone()
    assert row["name"] == "alpha" and row["link_method"] == "folder name"
    assert (row["user_message_count"], row["assistant_message_count"]) == (2, 2)
    assert json.loads(row["files_touched"]) == {"edited": ["app/auth.py", "tests/test_auth.py"], "read": ["app/main.py"]}
    text = " ".join(r[0] for r in conn.execute("SELECT snippet FROM evidence"))
    assert "sk-ant-" not in text and "dev@fake.com" not in text and "[REDACTED_ANTHROPIC_KEY]" in text
    kinds = sorted(r[0] for r in conn.execute("SELECT kind FROM evidence"))
    assert kinds == ["assistant_summary"] * 2 + ["files_touched"] + ["user_message"] * 2
    assert conn.execute("SELECT COUNT(*) FROM evidence WHERE project_id=?", (row["project_id"],)).fetchone()[0] == 5


def test_rerun_is_idempotent(with_projects, tmp_path):
    conn = with_projects
    p = fx.claude_code(tmp_path)
    for _ in range(2):
        common.store(conn, claude_code.parse(p))
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 5


# ---------- Codex ----------

def test_codex_parse(tmp_path):
    p = fx.codex(tmp_path)
    s = codex.parse(p, codex.titles(tmp_path))
    assert s.external_id == "cx1" and s.title == "Fix flaky tests" and s.project_path == "/Users/dev/code/beta"
    assert [(m.role, m.text) for m in s.messages] == [
        ("user", "Fix the flaky login test. token=ghp_FAKEfakeFAKEfakeFAKEfake1234567890"),
        ("assistant", "Fixed: added an explicit wait.")]  # task_complete duplicate of final_answer is dropped
    assert codex.find(tmp_path) == [p]


def test_codex_links_by_folder_and_redacts(with_projects, tmp_path):
    common.store(with_projects, codex.parse(fx.codex(tmp_path), codex.titles(tmp_path)))
    r = with_projects.execute("SELECT p.name FROM sessions s JOIN projects p ON p.id=s.project_id").fetchone()
    assert r[0] == "beta"
    assert "ghp_" not in with_projects.execute("SELECT group_concat(snippet) FROM evidence").fetchone()[0]


# ---------- Antigravity ----------

def test_antigravity_byte_offsets_and_turns(tmp_path):
    old, s = antigravity.parse_db(fx.antigravity(tmp_path))
    assert old.title == "Debugging And Model Execution" and old.messages == []
    assert s.title == "Push gamma to GitHub" and s.project_path == "/Users/dev/Desktop/Gamma App"
    assert s.ended_at.startswith("2026-11-16T17:59:34")
    assert [(m.role, m.text) for m in s.messages] == [
        ("user", "push to github\nhttps://github.com/dev/gamma"),
        ("assistant", "Pushed to main ✅ — done.")]
    assert s.messages[0].ref.endswith("#g1/step/0")


def test_split_steps_handles_multibyte():
    body = "é one\nü two\n"
    off = json.dumps([[0, 0], [2, len("é one\n".encode())]])
    assert antigravity.split_steps(body, off) == [(0, "é one"), (2, "ü two")]


def test_antigravity_links_by_github_url(with_projects, tmp_path):
    # folder "Gamma App" is ambiguous-free but doesn't match; the URL in the user message does
    s = antigravity.parse_db(fx.antigravity(tmp_path))[-1]
    s.project_path = "/Users/dev/Desktop/some-other-name"
    common.store(with_projects, s)
    r = with_projects.execute("SELECT p.name, s.link_method FROM sessions s JOIN projects p ON p.id=s.project_id").fetchone()
    assert tuple(r) == ("gamma", "GitHub URL in messages")


def test_linking_via_git_remote(with_projects, tmp_path, monkeypatch):
    monkeypatch.setattr(common, "git_remote", lambda path: "git@github.com:dev/beta.git")
    s = common.ParsedSession(source="x", external_id="1", kind="coding", path="p", project_path="/tmp/unrelated")
    assert common.link_project(with_projects, s) == (2, "git remote")


def test_no_projects_means_no_link(conn, tmp_path):
    sid = common.store(conn, claude_code.parse(fx.claude_code(tmp_path)))
    assert conn.execute("SELECT project_id FROM sessions WHERE id=?", (sid,)).fetchone()[0] is None


# ---------- Exports ----------

@pytest.mark.parametrize("as_zip", [False, True])
def test_chatgpt_export(tmp_path, as_zip):
    p = fx.chatgpt_export(tmp_path / ("export.zip" if as_zip else "conversations.json"), as_zip=as_zip)
    fmt, sessions, skipped = exports.parse(p)
    assert fmt == "chatgpt" and len(sessions) == 1 and skipped == 1
    s = sessions[0]
    assert s.title == "Gamma deployment help" and s.started_at == "2026-01-01T00:00:00+00:00"
    assert [(m.role, m.text) for m in s.messages] == [
        ("user", "How do I deploy a FastAPI app? my password: hunter22"),
        ("assistant", "Use uvicorn behind a reverse proxy.")]  # active branch only; system/tool dropped
    assert s.messages[0].ref == "https://chatgpt.com/c/c-123#n2"


def test_claude_export(tmp_path):
    fmt, [s], skipped = exports.parse(fx.claude_export(tmp_path / "conversations.json"))
    assert fmt == "claude_ai" and skipped == 0
    assert [(m.role, m.text[:19]) for m in s.messages] == [("user", "Review my JWT setup"), ("assistant", "Rotate the secret a")]
    assert s.messages[1].ref == "https://claude.ai/chat/cl-1#m2"


def test_unknown_export_rejected(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('[{"foo": 1}]')
    with pytest.raises(exports.ExportError):
        exports.parse(p)


def test_chat_title_links_project(with_projects, tmp_path):
    _, [s], _ = exports.parse(fx.claude_export(tmp_path / "c.json"))
    common.store(with_projects, s)
    r = with_projects.execute("SELECT p.name, s.link_method, s.kind FROM sessions s JOIN projects p ON p.id=s.project_id").fetchone()
    assert tuple(r) == ("alpha", "repo name in title", "chat")
    assert "fakeSECRETvalue123" not in with_projects.execute("SELECT group_concat(snippet) FROM evidence").fetchone()[0]


# ---------- CLI ----------

def test_cli_logs_export_and_stats(tmp_path):
    dbp = str(tmp_path / "c.db")
    fx.claude_code(tmp_path / "claude")
    fx.codex(tmp_path / "codex")
    fx.antigravity(tmp_path / "ag")
    r = runner.invoke(cli.app, ["--db", dbp, "ingest", "logs", "--claude-root", str(tmp_path / "claude"),
                                "--codex-root", str(tmp_path / "codex"), "--antigravity-root", str(tmp_path / "ag")])
    assert r.exit_code == 0, r.output
    assert "claude_code  1 sessions" in r.output and "codex        1 sessions" in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "ingest", "export", str(fx.chatgpt_export(tmp_path / "e.zip", as_zip=True))])
    assert r.exit_code == 0 and "chatgpt: 1 conversations, 2 messages, 1 skipped" in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "sessions", "--stats"])
    lines = {l.split()[0]: l.split() for l in r.output.splitlines()[1:]}
    assert set(lines) == {"claude_code", "codex", "antigravity", "chatgpt"}
    assert lines["claude_code"][2:6] == ["1", "0", "2", "2"]
    assert lines["chatgpt"][1] == "chat" and int(lines["chatgpt"][7]) >= 1  # password redacted
    r = runner.invoke(cli.app, ["--db", dbp, "sessions"])
    assert "Add OAuth login" in r.output


def test_cli_missing_roots(tmp_path):
    r = runner.invoke(cli.app, ["--db", str(tmp_path / "c.db"), "ingest", "logs", "--claude-root", str(tmp_path / "n1"),
                                "--codex-root", str(tmp_path / "n2"), "--antigravity-root", str(tmp_path / "n3")])
    assert r.exit_code == 0 and r.output.count("not found") == 3
