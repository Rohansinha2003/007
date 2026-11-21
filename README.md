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
uv run resume evaluate             # keep / rewrite / drop for every project and skill
uv run resume explain p7           # full score breakdown for one item
uv run resume override p7 drop     # your decision wins over every later evaluate run
uv run resume override p7 --clear
uv run resume market add postings.csv --role "ai engineer"   # also a .txt/.md file, a folder, or '-' for stdin
uv run resume market demand        # share of postings per skill, required vs nice-to-have
uv run resume gap --role "ai engineer"
uv run resume evaluate --role "ai engineer"   # demand becomes a weighted criterion
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

### Evaluator

`resume evaluate` scores every project and skill against `src/resumeforge/rubric.yaml` and prints a table
with the score breakdown, a decision and a reason. Pass `--rubric my.yaml` to use your own weights and thresholds.
Weights in each group are normalized to sum to 1, and the rubric hash is stored with each evaluation.

| Projects | How the 0 to 1 value is computed |
|---|---|
| depth | Judged depth and complexity, each 1 to 5, averaged and scaled |
| originality | Extracted authorship mapped through the rubric. Without extraction it is `unknown` and scores 0.5 |
| evidence | Log-scaled evidence row count, mixed with how many kinds of evidence exist |
| shipped | Extracted outcome. Without extraction, a GitHub deployment counts as shipped |
| recency | Half-life decay from the last commit or session |
| demand | Share of the role's top-3 skill demand covered by the project's top-3 skills. Only used with `--role` and postings on file |

| Skills | How the 0 to 1 value is computed |
|---|---|
| breadth | Number of kept projects that use it, saturating at 4 |
| depth | For languages, the byte share in the projects that use it. For extracted skills, evidence rows per project |
| recency | Half-life decay from the newest evidence |
| evidence | Log-scaled evidence row count |
| demand | The skill's demand score for the role. Only used with `--role` and postings on file |

Decisions:
- A score at or above `keep` means keep, at or above `rewrite` means rewrite, and anything lower means drop.
- **Rules apply after the score.** The profile README repo and repos with no commits by you and no linked AI session are dropped.
  A skill that is only a trace in every project, like 0.3% Shell, is capped at rewrite.
- **Skills only count kept projects.** A skill used only in dropped projects is dropped.
- **No evidence, no claim.** A skill claimed during extraction without valid evidence gets no score.
  It is `needs_evidence` if it was claimed at least twice, otherwise `drop`, and it cannot be overridden to keep.
- **Overrides** live in their own table, which only `resume override` writes. Evaluation runs never change them.
  The table marks overridden rows with `*`.

Judges:
- The LLM only rates depth and complexity and writes the one-line reason. Every other criterion is computed in code.
- With `ANTHROPIC_API_KEY` set, `--judge auto` uses the LLM judge. It returns validated JSON, results are cached,
  and a judgment with no valid evidence id is discarded in favor of the heuristic judge.
- Without a key, the deterministic heuristic judge rates depth from README size, commits, files edited,
  sessions, deployment, code size and language count.
- To replace the LLM with a small local model, implement the `Judge` protocol in `evaluator.py` and pass it to
  `evaluate()`. The protocol is a `name` plus `judge(ProjectInput) -> Judgment`.

On the real data, so far only with the heuristic judge: 12 projects keep, 10 rewrite and 38 drop, and 8 skills keep.
Known weak spot: a repo with a committed virtualenv inflates the heuristic's code-size signal (AI-Agent shows 49 MB in 12 languages).

### Market demand

There is no scraping. You collect postings yourself and add them under a role label.

- **`resume market add <source> --role <name>`** accepts a `.txt` or `.md` file (one posting), a CSV with the columns
  `title, company, location, description`, a folder of those files, `-` to paste on stdin, or the posting text itself.
  Postings are redacted and stored once per role: re-adding the same text is skipped. Role labels are lowercased.
- **Skill extraction.** With `ANTHROPIC_API_KEY`, the LLM returns validated JSON: each skill with `required` or
  `preferred` and the exact words from the posting. A skill whose quote is not in the posting is rejected and kept in
  `postings.rejected`. Without a key, or if a call fails, a keyword extractor uses the alias table. It reads
  required versus preferred from section headings such as "Requirements" and "Nice to have" and from cues such as
  "is a plus". Ambiguous words like Go, REST, React and Express only count with that capitalization. Names go
  through the same alias table as extraction, so `ReactJS` in a posting matches `React` in your history.
  `resume market extract --force --extractor llm` re-extracts with the LLM later.
- **`resume market demand`** shows each skill's share of the role's postings, its required and preferred counts, a
  flag (`required` if at least half of the postings that mention it require it, else `nice-to-have`), and a demand score:
  `(required + 0.5 x preferred) / postings`. The weights are in the `market` section of the rubric.
- **`resume gap --role <name>`** prints three lists:
  - (a) in-demand skills you have evidence for, with project and evidence counts and the current evaluation;
  - (b) in-demand skills with no evidence, noting any that extraction claimed without valid evidence;
  - (c) skills you have evidence for that no posting asks for.

  It also prints how much of the role's total demand your evidence covers.
- **Demand in the evaluator.** `resume evaluate --role <name>` adds a `demand` criterion, with weight 0.25 before normalization in both groups:
  - **Skills:** the skill's demand score.
  - **Projects:** the summed demand of the project's top 3 skills, divided by the summed demand of the role's top 3 skills.
    Languages under 5% of a project's code are ignored.

  The role is remembered for later runs. With no postings on file, the criterion is left out and the other
  weights are rescaled, so scores match what they were before this step.
- `resume market list` and `resume market remove` manage stored postings. `tests/data/postings_ai_engineer.csv` holds
  10 fake postings for trying the feature offline.

Tried on a copy of the real database with the 10 fake postings, keyword extraction and the heuristic judge:
- Your evidence covers 27% of the role's demand. Python, Docker, PostgreSQL, TypeScript and C++ are in demand and backed.
  LLM APIs, RAG and AWS are the top gaps, mostly because extraction has not run, so only languages count as evidence.
- Projects went from 12 keep, 10 rewrite and 38 drop to 8 keep, 11 rewrite and 41 drop.
  Chest_X_ray moved up from drop to rewrite on Python and Docker demand.
  four-pillars, finance-buddy and ResearchOS moved down from keep to rewrite, because their TypeScript stack has little demand for this role.
- HTML and Jupyter Notebook dropped from keep to rewrite, because no posting asks for them.

The real database has no postings yet. The fake ones are test data only.

Known limits for market demand:
- The keyword extractor only knows skills in the alias table. The LLM extractor is not limited this way, but it has not run live.
- There is no skill hierarchy, so a posting asking for "Machine learning" is not matched by evidence for PyTorch.

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
| `evaluations` | Latest computed score, decision, breakdown and reason per item (`p<id>` or `s:<skill>`) |
| `overrides` | Your keep or drop decisions. Written only by `resume override` |
| `postings` | Job postings you added: role label, title, company, location, redacted text, content hash, extractor, rejected skills |
| `posting_skills` | Skills per posting: canonical name and key, `required` or `preferred`, and the quote that shows it |

## Other modules

- `redact.py` removes API keys, tokens, JWTs, database URLs with passwords, private keys, secret assignments and emails.

## Tests

```bash
uv run pytest
```

GitHub tests use a fake HTTP transport. Log and export tests use small fake files in `tests/fixtures.py`
that copy the record shapes of the real logs. Market tests use `tests/data/postings_ai_engineer.csv`. All secrets in tests are fake. No network access is needed.
