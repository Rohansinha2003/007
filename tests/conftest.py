import json
from pathlib import Path

import pytest

from resumeforge import db


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def _w(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


@pytest.fixture
def claude_root(tmp_path):
    """Mirrors the record shapes observed in real ~/.claude/projects logs."""
    cwd = "/Users/dev/Desktop/Small_CRM"
    base = {"cwd": cwd, "gitBranch": "main", "sessionId": "s1", "isSidechain": False}
    recs = [
        {"type": "session-owner", "sessionId": "s1"},
        {**base, "type": "user", "timestamp": "2026-11-07T14:56:48Z",
         "message": {"role": "user", "content": [{"type": "text", "text":
            "<system-reminder>ignore me</system-reminder>Build a WhatsApp CRM SaaS with Next.js and PostgreSQL. key sk-ant-abcdefghijklmnop123"}]}},
        {**base, "type": "assistant", "timestamp": "2026-11-07T15:00:00Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Write", "input": {"file_path": f"{cwd}/src/server/auth.ts", "content": "x"}},
             {"type": "tool_use", "name": "Edit", "input": {"file_path": f"{cwd}/prisma/schema.prisma"}}]}},
        {**base, "type": "user", "timestamp": "2026-11-07T15:00:01Z", "toolUseResult": {},
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "ai-title", "aiTitle": "WhatsApp-based CRM for micro-businesses", "sessionId": "s1"},
    ]
    root = tmp_path / "claude"
    _w(root / "-Users-dev-Desktop-Small-CRM" / "s1.jsonl", recs)
    return root


@pytest.fixture
def codex_root(tmp_path):
    root = tmp_path / "codex"
    _w(root / "2026/06/17/rollout-x.jsonl", [
        {"type": "session_meta", "timestamp": "2026-06-17T09:24:11Z", "payload": {"cwd": "/Users/dev/qa-bot"}},
        {"type": "event_msg", "timestamp": "2026-06-17T09:24:13Z",
         "payload": {"type": "user_message", "message": "Automate QA test cases with Playwright for the login flow"}},
        {"type": "event_msg", "timestamp": "2026-06-17T09:26:14Z",
         "payload": {"type": "task_complete", "last_agent_message": "Added 12 Playwright tests."}},
    ])
    return root


def fake_gh(path: str):
    if path == "user":
        return {"login": "dev", "name": "Dev Person", "html_url": "https://github.com/dev", "location": "Pune"}
    if path.startswith("user/repos"):
        return [
            {"name": "Small_CRM", "html_url": "https://github.com/dev/Small_CRM", "description": "WhatsApp CRM",
             "language": "TypeScript", "topics": ["nextjs"], "fork": False, "pushed_at": "2026-11-12T19:22:39Z",
             "created_at": "2026-11-07T00:00:00Z"},
            {"name": "forked", "html_url": "https://github.com/dev/forked", "fork": True},
            {"name": "dev", "html_url": "https://github.com/dev/dev", "fork": False},
        ]
    if path.endswith("/languages"):
        return {"TypeScript": 9000, "CSS": 1000}
    if path.endswith("/readme"):
        import base64
        return {"content": base64.b64encode(b"# Small CRM\nNext.js + Prisma + PostgreSQL. Contact me@x.com").decode(),
                "html_url": "https://github.com/dev/Small_CRM/blob/main/README.md"}
    if "/commits" in path:
        return [{"html_url": "https://github.com/dev/Small_CRM/commit/abc",
                 "commit": {"message": "Add OAuth login with JWT sessions", "author": {"date": "2026-11-10T00:00:00Z"}}}]
    raise RuntimeError(path)
