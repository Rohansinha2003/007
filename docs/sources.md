# Data sources

What was found on the development machine (macOS) on 2026-11-21, and the formats the parsers rely on.
"Verified" means the parser was written against real files on this machine.

| Source | Path | Status | Parser |
|---|---|---|---|
| Claude Code | `~/.claude/projects/<encoded cwd>/<session-id>.jsonl` | Verified, 13 sessions | `ingest/claude_code.py` |
| OpenAI Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, titles in `~/.codex/session_index.jsonl` | Verified, 1 session | `ingest/codex.py` |
| Google Antigravity (Gemini agent IDE) | `~/.gemini/antigravity/conversation_summaries.db` | Verified, 36 conversations | `ingest/antigravity.py` |
| Gemini CLI | `~/.gemini/tmp/<project-hash>/chats/` | Not present on this machine | none |
| Cursor | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` | Present, not parsed | none |
| ChatGPT export | user-supplied `.zip` or `conversations.json` | No file on this machine | `ingest/exports.py` |
| Claude.ai export | user-supplied `.zip` or `conversations.json` | No file on this machine | `ingest/exports.py` |

## Claude Code

The folder name is the working directory with `/` replaced by `-`, for example
`-Users-me-Desktop-Small-CRM`. Each `.jsonl` file is one session. Each line is one JSON object.
A sibling folder `<session-id>/tool-results/` holds large tool outputs; it is not read.

Record types used:

| `type` | Fields used | Notes |
|---|---|---|
| `user` | `uuid`, `timestamp`, `cwd`, `isMeta`, `isSidechain`, `toolUseResult`, `message.content` | `content` is a string or a list of blocks |
| `assistant` | `uuid`, `timestamp`, `message.id`, `message.content` | One record per content block. Blocks of one reply share `message.id` |
| `ai-title` | `aiTitle` | Generated session title |

Other types seen and ignored: `api-request`, `api-request-blob`, `api-request-shape`, `attachment`,
`session-owner`, `atis-latch`, `last-prompt`, `queue-operation`, `file-history-snapshot`,
`file-history-delta`, `cost-state`, `tools-used`.

How a real user prompt is told apart from injected text:

- Skip records with `isMeta: true`. These include resume notices, loaded skill text and command caveats.
- Skip records with `toolUseResult` or `tool_result` blocks.
- Skip records with `isSidechain: true` (subagents).
- Remove `<system-reminder>`, `<ide_opened_file>`, `<ide_selection>`, `<command-name>`, `<command-message>`,
  `<command-args>`, `<local-command-stdout>`, `<local-command-caveat>` and `<task-notification>` blocks.
- Unwrap `<pasted_content id="...">` and keep the pasted text.
- Drop `[Request interrupted by user]`.
- Deduplicate by `uuid`.

The assistant summary for a turn is the text of the last assistant reply before the next user prompt.
Files touched come from `tool_use` blocks: `Write`, `Edit`, `MultiEdit` and `NotebookEdit` count as edited,
and `Read` counts as read. Bash commands are not parsed for file paths.

Message reference: `<jsonl path>:<line number>`.

## OpenAI Codex

Each rollout line is `{"timestamp", "type", "payload"}`.

| `type` / `payload.type` | Fields used |
|---|---|
| `session_meta` | `payload.id`, `payload.cwd`, `payload.git.repository_url` when present |
| `event_msg` / `user_message` | `payload.message` |
| `event_msg` / `agent_message` | `payload.message`, `payload.phase` (`commentary` or `final_answer`) |
| `event_msg` / `task_complete` | `payload.last_agent_message`, used only when no matching `final_answer` exists |

`response_item` records repeat the model input and output with system and developer text mixed in, so they are skipped.
`~/.codex/state_5.sqlite` has a `threads` table with `title` and `git_origin_url`. It is not read because
`session_index.jsonl` already gives titles and the only local thread had no remote.
Files touched are not extracted. The one local session edited no files, so there was no real `apply_patch`
record to build against.

## Google Antigravity

`~/.gemini` on this machine belongs to Antigravity, not Gemini CLI. Each conversation's step log in
`conversations/<id>.db` or `.pb` stores steps as binary protobuf blobs, which are not decoded.
The plain-text search database is enough.

`conversation_summaries` columns used: `conversation_id`, `title`, `last_modified_time`, and `workspace_uris`
(a JSON list of `file://` URIs, which gives the project path).

`conversations_fts` (an FTS5 table) holds one header row per conversation (`chunk_index = -1`) and text chunks (`chunk_index >= 0`):

| Column | Meaning |
|---|---|
| `title` | On the header row. Used when the summary title is empty, as it is for older conversations |
| `user_body`, `agent_body` | Concatenated user and agent text for the chunk |
| `user_step_byte_offsets`, `agent_step_byte_offsets` | JSON `[[step_index, offset], ...]` |

The offsets are UTF-8 **byte** offsets. On a conversation containing non-ASCII text, byte slicing split all
segments cleanly, while character slicing broke 29 of them.

Most agent segments are a single newline (tool steps). The assistant summary for a turn is the last non-empty
agent segment before the next user step. Files touched are not available as text.
The start time is the birth time of the conversation file. Message reference: `<db path>#<conversation id>/step/<n>`.

## ChatGPT and Claude.ai exports

No export exists on this machine. The parsers follow the documented layouts, ignore unknown fields,
and skip malformed conversations with a count. They accept the export `.zip`, reading every `conversations*.json`
inside it, or a bare `conversations.json`. The format is detected from the content.

- **ChatGPT:** a list of conversations with `mapping` (a tree of nodes with `parent`) and `current_node`.
  The visible thread is the path from `current_node` back to the root. Abandoned branches are excluded.
  Text comes from string entries in `content.parts`, or `content.text`. Only `user` and `assistant` roles are kept.
  Times are epoch seconds. Reference: `https://chatgpt.com/c/<id>#<message id>`.
- **Claude.ai:** a list of conversations with `uuid`, `name` and `chat_messages`. Each message has `sender` set to
  `human` or `assistant`. Text comes from `content` text blocks, or `text` when those are empty.
  Reference: `https://claude.ai/chat/<uuid>#<message uuid>`.

## Linking sessions to GitHub projects

Tried in order. The first unambiguous match wins and is stored in `sessions.link_method`:

1. The git remote recorded by the tool (Codex `session_meta.git`).
2. `git config remote.origin.url` in the session's folder, if it still exists.
3. The folder name matched to a repo name after normalizing (`Cognix_Agent-main` matches `Cognix_Agent`).
4. Exactly one of your GitHub repo URLs appears in the user's messages.
5. Chats only: exactly one repo name of five or more characters appears in the title.

Run `resume ingest github` before `resume ingest logs`. Re-running `ingest logs` re-links everything.
