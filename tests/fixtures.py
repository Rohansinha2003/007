"""Small fake files that mirror the record shapes seen in real logs on the dev machine.
All secrets are fake."""
import json
import sqlite3
import zipfile
from pathlib import Path

FAKE_KEY = "sk-ant-api03-FAKEfakeFAKEfake1234567890"


def jsonl(path: Path, records) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def claude_code(root: Path, cwd="/Users/dev/Desktop/alpha-main") -> Path:
    b = {"cwd": cwd, "sessionId": "s1", "isSidechain": False, "gitBranch": "main"}
    recs = [
        {"type": "session-owner", "sessionId": "s1"},
        {"type": "user", "isMeta": True, "uuid": "m0", "timestamp": "2026-11-01T09:59:00Z",
         "message": {"role": "user", "content": "<local-command-caveat>Caveat...</local-command-caveat>"}, **b},
        {"type": "user", "uuid": "u1", "timestamp": "2026-11-01T10:00:00Z", **b, "message": {"role": "user", "content": [
            {"type": "text", "text": "<ide_opened_file>The user opened x.py</ide_opened_file>"},
            {"type": "text", "text": f"<pasted_content id=\"ab\">Add OAuth login. key {FAKE_KEY}</pasted_content>"}]}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-11-01T10:00:05Z", **b, "message": {
            "id": "msg_1", "role": "assistant", "content": [{"type": "text", "text": "Looking at the code first."}]}},
        {"type": "assistant", "uuid": "a2", "timestamp": "2026-11-01T10:00:06Z", **b, "message": {
            "id": "msg_1", "role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": f"{cwd}/app/main.py"}}]}},
        {"type": "user", "uuid": "t1", "timestamp": "2026-11-01T10:00:07Z", "toolUseResult": {}, **b,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
        {"type": "assistant", "uuid": "a3", "timestamp": "2026-11-01T10:01:00Z", **b, "message": {
            "id": "msg_2", "role": "assistant", "content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": f"{cwd}/app/auth.py", "content": "x"}}]}},
        {"type": "assistant", "uuid": "a4", "timestamp": "2026-11-01T10:02:00Z", **b, "message": {
            "id": "msg_3", "role": "assistant", "content": [{"type": "text", "text": "Added Google OAuth in app/auth.py."}]}},
        {"type": "assistant", "uuid": "a4", "timestamp": "2026-11-01T10:02:00Z", **b, "message": {  # duplicate uuid
            "id": "msg_3", "role": "assistant", "content": [{"type": "text", "text": "Added Google OAuth in app/auth.py."}]}},
        {"type": "user", "uuid": "u2", "timestamp": "2026-11-01T10:05:00Z", **b,
         "message": {"role": "user", "content": "<command-name>/model</command-name>"}},
        {"type": "user", "uuid": "u3", "timestamp": "2026-11-01T10:06:00Z", **b,
         "message": {"role": "user", "content": "Now write tests. Mail me at dev@fake.com"}},
        {"type": "assistant", "uuid": "a5", "timestamp": "2026-11-01T10:07:00Z", **b, "message": {
            "id": "msg_4", "role": "assistant", "content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": f"{cwd}/tests/test_auth.py"}},
                {"type": "text", "text": "Wrote 4 tests; all pass."}]}},
        {"type": "assistant", "uuid": "side", "isSidechain": True, "timestamp": "2026-11-01T10:07:30Z",
         "cwd": cwd, "message": {"id": "msg_x", "content": [{"type": "text", "text": "subagent chatter"}]}},
        {"type": "ai-title", "aiTitle": "Add OAuth login", "sessionId": "s1"},
        "not json",
    ]
    p = root / "-Users-dev-Desktop-alpha-main" / "s1.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in recs) + "\n")
    return p


def codex(root: Path) -> Path:
    jsonl(root / "session_index.jsonl", [{"id": "cx1", "thread_name": "Fix flaky tests", "updated_at": "x"}])
    return jsonl(root / "sessions/2026/06/17/rollout-2026-06-17T09-24-11-cx1.jsonl", [
        {"timestamp": "2026-06-17T09:24:11Z", "type": "session_meta",
         "payload": {"id": "cx1", "cwd": "/Users/dev/code/beta", "timestamp": "2026-06-17T09:24:11Z"}},
        {"timestamp": "2026-06-17T09:24:13Z", "type": "response_item",
         "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "sandbox..."}]}},
        {"timestamp": "2026-06-17T09:24:13Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "Fix the flaky login test. token=ghp_FAKEfakeFAKEfakeFAKEfake1234567890"}},
        {"timestamp": "2026-06-17T09:24:20Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "Checking the test first.", "phase": "commentary"}},
        {"timestamp": "2026-06-17T09:26:00Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "Fixed: added an explicit wait.", "phase": "final_answer"}},
        {"timestamp": "2026-06-17T09:26:01Z", "type": "event_msg",
         "payload": {"type": "task_complete", "last_agent_message": "Fixed: added an explicit wait."}},
    ])


def antigravity(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / "conversation_summaries.db"
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE conversation_summaries (conversation_id text, title text, preview text, step_count integer,
                 last_modified_time datetime, workspace_uris text, parent_conversation_id text,
                 last_user_input_time datetime)""")
    c.execute("""CREATE TABLE conversations_fts (conversation_id, chunk_index, chunk_first_step, chunk_last_step,
                 user_step_byte_offsets, agent_step_byte_offsets, title, user_body, agent_body)""")
    c.execute("INSERT INTO conversation_summaries VALUES ('g1','Push gamma to GitHub','',6,"
              "'2026-11-16 17:59:34.4+00:00','[\"file:///Users/dev/Desktop/Gamma%20App\"]','','2026-11-16 17:50:00+00:00')")
    user = "push to github\nhttps://github.com/dev/gamma\n"
    agent = "Checking repo ✅\nPushed to main ✅ — done.\n"
    off_u = [[0, 0]]
    a1 = len("Checking repo ✅\n".encode())
    c.execute("INSERT INTO conversations_fts VALUES ('g1',-1,0,0,'','','Push gamma','','')")
    c.execute("INSERT INTO conversations_fts VALUES ('g1',0,0,5,?,?,'',?,?)",
              (json.dumps(off_u), json.dumps([[1, 0], [3, a1]]), user, agent))
    # an older conversation whose summary title is empty but whose search header row has one
    c.execute("INSERT INTO conversation_summaries VALUES ('g2','','',2,'2026-07-01 10:00:00+00:00','[]','','')")
    c.execute("INSERT INTO conversations_fts VALUES ('g2',-1,0,0,'','','Debugging And Model Execution','','')")
    c.commit()
    c.close()
    return p


def chatgpt_export(path: Path, as_zip=False) -> Path:
    conv = {
        "id": "c-123", "title": "Gamma deployment help", "create_time": 1767225600.0, "update_time": 1767229200.5,
        "current_node": "n4",
        "mapping": {
            "root": {"id": "root", "message": None, "parent": None, "children": ["n1"]},
            "n1": {"id": "n1", "parent": "root", "children": ["n2", "alt"], "message": {
                "id": "n1", "author": {"role": "system"}, "content": {"content_type": "text", "parts": [""]}}},
            "n2": {"id": "n2", "parent": "n1", "children": ["n3"], "message": {
                "id": "n2", "author": {"role": "user"}, "create_time": 1767225601,
                "content": {"content_type": "text", "parts": ["How do I deploy a FastAPI app? my password: hunter22"]}}},
            "alt": {"id": "alt", "parent": "n1", "children": [], "message": {
                "id": "alt", "author": {"role": "user"}, "content": {"content_type": "text", "parts": ["abandoned branch"]}}},
            "n3": {"id": "n3", "parent": "n2", "children": ["n4"], "message": {
                "id": "n3", "author": {"role": "assistant"}, "create_time": 1767225610,
                "content": {"content_type": "text", "parts": ["Use uvicorn behind a reverse proxy.", {"asset": "img"}]}}},
            "n4": {"id": "n4", "parent": "n3", "children": [], "message": {
                "id": "n4", "author": {"role": "tool"}, "content": {"content_type": "text", "parts": ["tool output"]}}},
        },
    }
    data = json.dumps([conv, {"title": "broken, no id or mapping keys", "mapping": {}}])
    if as_zip:
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("conversations.json", data)
            z.writestr("user.json", "{}")
    else:
        path.write_text(data)
    return path


def claude_export(path: Path) -> Path:
    path.write_text(json.dumps([{
        "uuid": "cl-1", "name": "Review alpha auth flow", "created_at": "2026-10-01T10:00:00Z",
        "updated_at": "2026-10-01T11:00:00Z",
        "chat_messages": [
            {"uuid": "m1", "sender": "human", "text": "Review my JWT setup", "content": [
                {"type": "text", "text": "Review my JWT setup. secret: JWT_SECRET=fakeSECRETvalue123"}],
             "created_at": "2026-10-01T10:00:00Z"},
            {"uuid": "m2", "sender": "assistant", "text": "Rotate the secret and shorten expiry.", "content": [],
             "created_at": "2026-10-01T10:00:30Z"},
            {"uuid": "m3", "sender": "human", "text": "", "content": []},
        ]}]))
    return path
