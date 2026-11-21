# Evidence-based resume builder

Reads a developer's real work history, stores every fact as an evidence row, and (in later steps)
builds a resume where each claim links to that evidence. Project rules live in `CLAUDE.md`.

## Quick start

```bash
uv sync
export GITHUB_TOKEN=...            # optional; e.g. GITHUB_TOKEN=$(gh auth token)
uv run resume ingest github <username>
uv run resume projects             # table; add --json for full rows
uv run resume ingest logs          # Claude Code, Codex and Antigravity sessions on this machine
uv run resume ingest export ~/Downloads/chatgpt-export.zip   # ChatGPT or Claude.ai export
uv run resume sessions --stats     # counts per source
uv run resume sessions             # recent sessions with their linked project
```

The database is `resume.db` in the current folder. Override with `--db PATH` or `RESUME_DB`.

## What works

- **`resume ingest github <username>`** imports every non-fork public repo through the GitHub REST API.
  Per repo it stores description, languages, README text, topics, stars, commit count by the user,
  first and last commit dates, and deployment status. It writes one project row plus evidence rows.
- **`resume projects`** lists imported projects, newest commit first.
- **Re-running is safe.** Projects upsert on the GitHub repo id. Evidence upserts on source, kind and URL.
  Evidence a repo no longer has, such as a deleted README, is removed on the next run.
- **Rate limits.** The importer waits for the reset time on a primary rate limit and honors `retry-after`
  on secondary limits. Waits longer than 15 minutes stop the run with a clear message; finished repos stay saved.
  Server errors and network failures retry with backoff.
- **Redaction.** Every text field written to the database passes through `redact.py` first: evidence snippets,
  session titles and summaries, and project text fields. It removes vendor API keys and tokens (Anthropic, OpenAI,
  GitHub, GitLab, AWS, Google, Hugging Face, npm, Slack, Stripe, SendGrid, Twilio), JWTs, private keys,
  `Authorization` headers, secret and password assignments, `--password` flags, URLs with embedded credentials,
  secret query parameters on signed URLs, private-network URLs (localhost, 10.x, 192.168.x, `.internal`, ...)
  and emails. The LLM wrapper redacts again before any request.
- **Skills from languages.** Each language GitHub reports is recorded in `skills_observed` against the
  languages evidence row. A skill cannot be recorded without an existing evidence row.

- **`resume ingest logs`** imports AI coding sessions from Claude Code, Codex and Google Antigravity.
  Per session it stores the tool, dates, project folder, user messages, assistant summaries and files touched.
  Each message becomes an evidence row. Sessions are linked to GitHub projects by git remote, folder name,
  or a repo URL in the messages. Formats and paths are documented in `docs/sources.md`.
- **`resume ingest export <file>`** imports a ChatGPT or Claude.ai data export, as the `.zip` or its
  `conversations.json`. The format is detected automatically. These parsers are tested with small fake files only,
  because no real export was available.
- **`resume sessions --stats`** shows counts per source: sessions, linked sessions, user and assistant messages,
  evidence rows, and rows with redactions.

How GitHub facts are derived:

| Field | Source |
|---|---|
| Commit count, first and last dates | Commits on the default branch where GitHub credits the user as author. Empty repos count 0. |
| Deployment | The homepage field if it is not a GitHub link, then GitHub Pages, then the Deployments API (for example Vercel). |
| README | First 8000 characters of the raw README. |

Known limits for sessions:
- Cursor chats and Gemini CLI are not imported. Cursor stores chats in a binary database, and Gemini CLI data was not on the dev machine.
- Antigravity and Codex sessions have no files-touched list. See `docs/sources.md` for why.
- User messages are capped at 4000 characters and assistant text at 2000.

Known limits for GitHub:
- Only public repos are imported.
- Commits made under an email that is not linked to the account are not counted.
- Branches other than the default are not counted.

## Schema

Migrations are SQL scripts in `src/resumeforge/db.py`, applied in order and tracked with `PRAGMA user_version`.
To change the schema, append a new script. Never edit an applied one.

| Table | Purpose |
|---|---|
| `projects` | One row per repo, unique on source plus external id |
| `evidence` | `id, project_id, source, kind, url_or_path, snippet, created_at`, plus `session_id` for session evidence |
| `skills_observed` | `skill, project_id, evidence_id`; the evidence id is required |
| `sessions` | One row per AI-tool session or chat: tool, kind, dates, project path, linked project and how it was linked, message counts, files touched, last summary |
| `settings` | Key-value JSON, such as the GitHub username and the last ingest time |

## Other modules

- `llm.py` is the single LLM wrapper. It uses the Anthropic API with `ANTHROPIC_API_KEY`, requests
  structured JSON, validates it with pydantic, and redacts input first. Nothing calls it yet.
- `redact.py` removes API keys, tokens, JWTs, database URLs with passwords, private keys, secret assignments and emails.

## Tests

```bash
uv run pytest
```

GitHub tests use a fake HTTP transport. Log and export tests use small fake files in `tests/fixtures.py`
that copy the record shapes of the real logs. All secrets in tests are fake. No network access is needed.
