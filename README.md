# Evidence-based resume builder

Reads a developer's real work history, stores every fact as an evidence row, and (in later steps)
builds a resume where each claim links to that evidence. Project rules live in `CLAUDE.md`.

## Quick start

```bash
uv sync
export GITHUB_TOKEN=...            # optional; e.g. GITHUB_TOKEN=$(gh auth token)
uv run resume ingest github <username>
uv run resume projects             # table; add --json for full rows
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
- **Redaction.** Every evidence snippet passes through the secret and email redactor before it is stored.
- **Skills from languages.** Each language GitHub reports is recorded in `skills_observed` against the
  languages evidence row. A skill cannot be recorded without an existing evidence row.

How the facts are derived:

| Field | Source |
|---|---|
| Commit count, first and last dates | Commits on the default branch where GitHub credits the user as author. Empty repos count 0. |
| Deployment | The homepage field if it is not a GitHub link, then GitHub Pages, then the Deployments API (for example Vercel). |
| README | First 8000 characters of the raw README. |

Known limits:
- Only public repos are imported.
- Commits made under an email that is not linked to the account are not counted.
- Branches other than the default are not counted.

## Schema

Migrations are SQL scripts in `src/resumeforge/db.py`, applied in order and tracked with `PRAGMA user_version`.
To change the schema, append a new script. Never edit an applied one.

| Table | Purpose |
|---|---|
| `projects` | One row per repo, unique on source plus external id |
| `evidence` | `id, project_id, source, kind, url_or_path, snippet, created_at` |
| `skills_observed` | `skill, project_id, evidence_id`; the evidence id is required |
| `sessions` | AI-tool sessions; created now, filled by a later step |
| `settings` | Key-value JSON, such as the GitHub username and the last ingest time |

## Other modules

- `llm.py` is the single LLM wrapper. It uses the Anthropic API with `ANTHROPIC_API_KEY`, requests
  structured JSON, validates it with pydantic, and redacts input first. Nothing calls it yet.
- `redact.py` removes API keys, tokens, JWTs, database URLs with passwords, private keys, secret assignments and emails.

## Tests

```bash
uv run pytest
```

GitHub tests use a fake HTTP transport. No network access is needed.
