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
uv run resume extract --dry-run    # build prompts, estimate tokens and cost, no API calls
export ANTHROPIC_API_KEY=...
uv run resume extract              # LLM facts per project (cached; re-runs are free)
uv run resume skills               # each skill with its count of projects and evidence
uv run resume llm-usage            # tokens and cost from the call log
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

- **`resume extract`** sends each project's redacted README, commit stats, deployment, languages and linked
  session evidence to the LLM. The reply must match a JSON schema with a summary, technologies, skills, an outcome
  (shipped, prototype or abandoned) and an authorship estimate (self_written, ai_assisted, mostly_ai or unknown)
  with a reason. Every technology, skill, outcome and authorship claim carries evidence ids.
- **No evidence, no claim.** A claim is rejected unless it cites at least one evidence id that was in the prompt
  for that project. Invalid ids are dropped from otherwise supported claims. An unsupported outcome becomes empty,
  and an unsupported authorship becomes `unknown`. Rejected claims are kept in `project_facts.rejected` for review.
  Accepted technologies and skills go into `skills_observed`.
- **Authorship needs logs.** The model is told that missing session logs are not proof of self-written code, so
  projects without linked sessions should come back `unknown`. `unknown` is a fourth value beyond the three requested.
- **Skill names are normalized** through the alias table in `skills.py`, so React.js, ReactJS and React are one skill.
- **`resume skills`** lists each skill with its number of projects and evidence rows, filterable by kind
  (`language` from GitHub, `technology` and `skill` from extraction).

LLM wrapper (`llm.py`):
- Sends the pydantic model's JSON schema as the required output format and validates the reply against it.
- Retries invalid JSON up to three attempts, telling the model what was wrong, and doubles `max_tokens` after a truncated reply.
  The Anthropic SDK retries rate limits, server errors and connection failures with backoff.
- Caches validated results in `llm_cache` by a SHA-256 hash of model, prompt and schema.
- Logs every call, cache hits and failures included, in `llm_calls` with tokens and cost.
  Prices are a dated snapshot in `PRICES`. Default model is `claude-opus-5-5` with server-side refusal fallback.

Not verified yet: no API key was available while building this stage, so live extraction has not run.
Tests mock the model. A run on a copy of the real database with a stand-in model covered all 53 projects and
confirmed that unsupported claims are rejected. The dry run estimates about $2 for all projects.

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
| `skills_observed` | `skill, project_id, evidence_id, kind, origin`; the evidence id is required |
| `sessions` | One row per AI-tool session or chat: tool, kind, dates, project path, linked project and how it was linked, message counts, files touched, last summary |
| `settings` | Key-value JSON, such as the GitHub username and the last ingest time |
| `project_facts` | One row per extracted project: summary, outcome, authorship, validated JSON, rejected claims |
| `llm_cache` | Validated LLM results keyed by input hash |
| `llm_calls` | One row per LLM call: purpose, model, cached, attempts, tokens, cost, error |

## Other modules

- `redact.py` removes API keys, tokens, JWTs, database URLs with passwords, private keys, secret assignments and emails.

## Tests

```bash
uv run pytest
```

GitHub tests use a fake HTTP transport. Log and export tests use small fake files in `tests/fixtures.py`
that copy the record shapes of the real logs. All secrets in tests are fake. No network access is needed.
