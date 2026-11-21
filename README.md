# resumeforge: an evidence-based resume builder

resumeforge reads your real work history and turns it into a resume where every claim links to evidence. The history comes from three places:
- your GitHub repos;
- the AI coding sessions on your machine;
- your ChatGPT and Claude.ai exports.

It also scores what deserves a place on the resume, measures demand from job postings you collect, and writes a tailored resume. That resume comes as DOCX and PDF, along with an `evidence.md` file you can use to check every bullet.

**The rule throughout: no evidence, no claim.** Every skill and every bullet points to rows in the `evidence` table. A claim without valid evidence is dropped and reported, never written.

```bash
uv run resume run-all --github <username> --jd job.txt
# -> out/resume.docx, out/resume.pdf, out/evidence.md
```

## Contents

- [Install](#install)
- [Setup](#setup)
- [The pipeline in one command](#the-pipeline-in-one-command)
- [Commands](#commands)
- [How decisions are made](#how-decisions-are-made)
- [Data privacy](#data-privacy)
- [Known limitations](#known-limitations)
- [Development](#development)
- [What to build next](#what-to-build-next)

## Install

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this repo> && cd resumeforge
uv sync                 # creates .venv and installs dependencies
uv run resume --help
```

The dependencies are anthropic, typer, pydantic, pyyaml, python-docx and fpdf2, plus pytest for development. Everything is stored in a single SQLite file.

## Setup

| Setting | Needed for | Default |
|---|---|---|
| `GITHUB_TOKEN` | Optional. Raises the GitHub limit from 60 to 5000 requests per hour. A full import of 60 repos takes about 320 requests. | none. Tip: `GITHUB_TOKEN=$(gh auth token) uv run resume ...` |
| `ANTHROPIC_API_KEY` | The LLM stages: extraction, the depth judge, posting and job-description skills, bullet writing and the claim check. Without it every stage falls back to an offline method; see below. | none |
| `RESUME_DB` or `--db PATH` | Where the database lives | `./resume.db` |
| `RESUMEFORGE_MODEL` | Model for LLM calls | `claude-opus-5-5` |

What happens without an API key:

| Stage | With a key | Without a key |
|---|---|---|
| Extraction (`resume extract`) | Summary, technologies, skills, outcome and authorship, each cited | Skipped. Skills come only from GitHub language stats. |
| Depth judge (`resume evaluate`) | LLM rates depth and complexity, with a one-line reason | Heuristic from README size, commits, files edited, sessions, deployment and code size |
| Skills in postings and the job description | LLM, with an exact quote required for each skill | Keyword matching on the alias table |
| Bullets (`resume generate`) | LLM writes action + result bullets citing evidence ids | Template bullets filled from evidence rows |
| Claim check | An exact lexical check, then a second LLM pass | The lexical check only |

## The pipeline in one command

```bash
uv run resume run-all --github <username> --jd job.txt [--out out/resume.docx] [--no-logs] [--role "ai engineer"] [--strict]
```

1. **GitHub.** Imports your public non-fork repos. Re-runs are safe.
2. **AI coding sessions.** Imports Claude Code, Codex and Antigravity sessions found on this machine. Skip this with `--no-logs`.
3. **Extraction.** Runs only with an API key.
4. **Evaluation.** Gives each project and skill a decision: keep, rewrite or drop. Uses the market role if you have postings on file.
5. **Generation.** Picks projects and skills, writes bullets, checks every bullet against its cited evidence, and writes:
   - `resume.docx`;
   - `resume.pdf`;
   - `evidence.md`, which lists each bullet with its source links.

Each step is also a separate command, described below.

## Commands

### Import

| Command | What it does |
|---|---|
| `resume ingest github <user>` | Imports each non-fork public repo, storing the fields below. Handles rate limits and pagination. Safe to re-run. |
| `resume ingest logs` | Imports AI coding sessions with their user messages, assistant summaries and files touched, and links each session to a GitHub project. Override the default paths with `--claude-root`, `--codex-root` and `--antigravity-root`. |
| `resume ingest export <file>` | Imports a ChatGPT or Claude.ai data export, given as the `.zip` or its `conversations.json`. The format is detected automatically. |
| `resume projects` | Lists imported projects. Add `--json` for full rows. |
| `resume sessions [--stats]` | Lists sessions, or shows counts per source. |

For each repo, the GitHub import stores:
- description, languages, topics and stars;
- README text;
- the number of commits credited to you, and the first and last commit dates;
- whether the repo has a deployment (homepage field, GitHub Pages or the Deployments API).

Session formats and paths are documented in [docs/sources.md](docs/sources.md). Sessions are linked to projects by trying these in order:
1. the git remote;
2. the folder name;
3. a GitHub URL mentioned in the conversation;
4. for chats only, the repo name in the title.

### Extract

| Command | What it does |
|---|---|
| `resume extract [--dry-run] [--project NAME] [--limit N]` | Sends each project's redacted evidence to the LLM. Gets back a summary, technologies, skills, an outcome and an authorship estimate, each with evidence ids. `--dry-run` estimates tokens and cost without calling the API. |
| `resume skills [--kind language\|technology\|skill]` | Lists each skill with its number of projects and evidence rows. |
| `resume llm-usage` | Shows tokens and cost per purpose, from the LLM call log. |

A claim is rejected unless it cites an evidence id that was in that project's prompt. Rejected claims are kept for review in `project_facts.rejected`. Skill names are normalized through the alias table in `skills.py`, so React.js, ReactJS and React are one skill.

### Evaluate

| Command | What it does |
|---|---|
| `resume evaluate [--role R] [--judge auto\|llm\|heuristic] [--rubric my.yaml]` | Scores every project and skill and prints keep, rewrite or drop, with per-criterion scores and a reason. |
| `resume explain <id>` | Prints the full weighted sum and inputs for one item, such as `p7` or `s:python`. |
| `resume override <id> keep\|drop [--note ...]` / `--clear` | Your decision. Overrides are stored separately and later runs never change them. Keep is refused for an item with no evidence. |

### Market demand

You collect postings yourself; nothing is scraped.

| Command | What it does |
|---|---|
| `resume market add <source> --role "ai engineer"` | Adds postings from a `.txt` or `.md` file, a CSV (`title, company, location, description`), a folder, `-` for stdin, or pasted text. Postings are redacted and stored once per role. |
| `resume market extract [--role R] [--force] [--extractor llm]` | Re-extracts required and preferred skills, for example with the LLM once you have a key. |
| `resume market demand [--role R]` | Per skill: share of postings, required versus nice-to-have counts, and a demand score. |
| `resume market list` / `remove` | Manages stored postings. |
| `resume gap --role R` | Prints three lists: (a) in-demand skills you have evidence for, (b) in-demand skills without evidence, (c) skills you have that nobody asks for. Also prints how much of the role's demand you cover. |

For each posting, a skill is extracted only with a quote from the posting text. The demand score is `(required + 0.5 x preferred) / number of postings`. A sample CSV with 10 fake postings is in `tests/data/postings_ai_engineer.csv`.

### Generate

```bash
uv run resume generate --jd job.txt --out out/resume.docx \
    --name "Your Name" --email you@example.com --phone "..." --link https://your.site
```

| Option | Default | Meaning |
|---|---|---|
| `--jd FILE` or `-` | required | The job description |
| `--out` | `resume.docx` | DOCX path. The PDF and `evidence.md` are written next to it. |
| `--projects` / `--skills` / `--bullets` | 4 / 16 / 4 | How many projects and skills to include, and the bullet limit per project |
| `--writer auto\|llm\|template` | auto | Who writes the bullets |
| `--checker auto\|llm\|lexical\|both` | auto | `auto` means both checkers with a key and lexical without one |
| `--strict` | off | Also remove flagged bullets, not only unsupported ones |
| `--no-pdf`, `--no-hidden-ids` | | Skip the PDF, or leave hidden ids out of the DOCX |
| `--name --email --phone --location --link` | GitHub profile | Header fields. They are saved locally for the next run. |

How it works:
1. **Read the job description.** Its required and preferred skills are extracted the same way as postings.
2. **Select.** Projects with an effective decision of keep or rewrite (your overrides win) are ranked by
   `0.6 x evaluator score + 0.4 x fit + 0.1 if keep`. Fit is the share of the job's top-3 skill demand covered by the project's skills.
   Skills come from keep decisions, plus rewrite decisions the job asks for. Each needs at least one evidence row.
3. **Write bullets.** Bullets follow the form action + what + how + result, and each cites evidence ids. A bullet citing no evidence of its own project is removed.
4. **Check claims.** The lexical checker requires every number, URL and known technology in a bullet to appear in that bullet's cited evidence. The LLM checker then reads only the cited evidence and returns one of:
   - `supported`: kept;
   - `partial`: flagged, and removed with `--strict`;
   - `unsupported`: removed.

   Every removed or flagged bullet is printed with the reason, and listed in `evidence.md`.
5. **Export.**
   - **DOCX and PDF:** a single column with standard section headings (SKILLS, PROJECTS), plain bullets, and no tables, images, text boxes or headers.
   - **Hidden evidence ids in the DOCX:** each bullet carries its ids as a hidden run, `[evidence: E12 E40]`. Word doesn't show or print hidden text; turn on Hidden text in Word to see it. The PDF has no ids.
   - **`evidence.md`:** each bullet with its check result, and each cited row's kind, date, link and snippet. It also lists the job's skills you couldn't claim, and the removed bullets.

The resume covers projects and skills only, because that is what the evidence shows. Add work experience and education by hand in the DOCX.

## How decisions are made

`src/resumeforge/rubric.yaml` holds every weight, threshold and rule. Pass `--rubric my.yaml` to use your own. Weights are normalized per group, and the rubric's hash is stored with each evaluation.

| Projects | 0 to 1 value |
|---|---|
| depth | Judged depth and complexity (LLM or heuristic), each 1 to 5, scaled |
| originality | Extracted authorship: self_written 1.0, ai_assisted 0.7, mostly_ai 0.35, unknown 0.5 |
| evidence | Log-scaled number of evidence rows, mixed with how many kinds of evidence exist |
| shipped | Extracted outcome. Without extraction, a GitHub deployment counts as shipped. |
| recency | Half-life decay from the last commit or session (one year) |
| demand | Share of the role's top-3 demand covered by the project's top-3 skills. Used only with postings on file. |

| Skills | 0 to 1 value |
|---|---|
| breadth | Number of kept projects using the skill, saturating at 4 |
| depth | Byte share for languages, evidence density for extracted skills |
| recency, evidence | Same as for projects |
| demand | The skill's demand score for the role. Used only with postings on file. |

Score bands: keep at 0.55 or above for projects (0.50 for skills), rewrite at 0.40 or above (0.30), drop below that.

Rules applied after scoring:
- **Projects dropped regardless of score:** the profile README repo, and repos with no commits by you and no linked AI session.
- **Trace skills:** a skill that makes up only a trace of every project it appears in is capped at rewrite.
- **Skills follow projects:** a skill seen only in dropped projects is dropped.
- **Claimed without evidence:** a skill extraction claimed but couldn't back is `needs_evidence` if it was claimed at least twice, otherwise `drop`. It can never be kept.

To swap the LLM judge for a local model, implement the `Judge` protocol in `evaluator.py`: a `name` plus `judge(ProjectInput) -> Judgment`. Writers (`generate.Writer`), checkers (`generate.Checker`) and posting extractors (`market.Extractor`) follow the same pattern.

## Data privacy

- **Local first.** All data stays in the local SQLite file. The tool makes network requests in only two cases:
  - the GitHub API, to read your public repos and profile;
  - the Anthropic API, for the LLM stages, and only when `ANTHROPIC_API_KEY` is set.
- **Redaction at ingestion.** Every text field passes through `redact.py` before it is written. That covers evidence snippets, session titles and summaries, project text and posting text. The redactor removes:
  - vendor API keys and tokens: Anthropic, OpenAI, GitHub, GitLab, AWS, Google, Hugging Face, npm, Slack, Stripe, SendGrid and Twilio;
  - JWTs, private keys and `Authorization` headers;
  - secret and password assignments, and `--password` flags;
  - URLs with credentials, and secret query parameters;
  - private-network URLs such as localhost, 10.x and `.internal`;
  - emails.
- **Redaction again before every LLM call.** The LLM wrapper redacts its whole prompt again.
- **What the LLM sees.** Only redacted evidence for one project at a time, the posting or job description text, and the bullets being checked. Your header fields (name, email, phone) are never sent; they are stored in the local `settings` table.
- **Caching.** LLM results are cached in the database by input hash, so re-runs make no new calls. Every call is logged with its token count and cost in `llm_calls`.
- **Session paths in `evidence.md`.** It links to local session log paths. Review it before sharing.
- **The GitHub token is never stored.**

## Known limitations

- **No API key on the development machine.** Extraction, the LLM judge, LLM bullets and the LLM claim check have been tested only with mocked responses. Every offline fallback has run on real data.
- **Template bullets are plain.** They state what the evidence states: what was built, the stack, the deployment and the commit span. They contain no metrics and only the results the evidence states. LLM bullets read better, and the claim check keeps them honest.
- **Projects only.** There is no employment history or education, because no source provides them.
- **GitHub:** only public repos and the default branch are read, and only commits credited to your account are counted.
- **Committed dependency folders inflate code size.** A committed virtualenv or `node_modules` inflates the heuristic's code-size signal and adds odd languages.
- **Sessions:** Cursor and Gemini CLI are not imported. Antigravity and Codex sessions have no files-touched list. Long messages are truncated: user messages at 4000 characters, assistant text at 2000.
- **Chat exports:** the parsers were built from documented formats and fake fixtures, not a real export.
- **Keyword extraction** only knows skills in the alias table.
- **No skill hierarchy:** "Machine learning" in a posting is not matched by PyTorch evidence.
- **The lexical claim check is literal.** It catches invented numbers, links and technologies, but not reworded claims; the LLM pass covers those.
- **The PDF uses a built-in font,** so characters outside Latin-1 are replaced with plain equivalents.

## Development

```bash
uv run pytest          # 168 tests, no network
```

GitHub tests use a fake HTTP transport. Log and export tests use small fake files (`tests/fixtures.py`). LLM stages use fake backends. All secrets in the tests are fake.

| Module | Role |
|---|---|
| `cli.py` | Typer CLI |
| `db.py` | Schema migrations (`PRAGMA user_version`), upserts, redaction on write |
| `redact.py` | Secret and PII patterns |
| `llm.py` | The only LLM entry point: JSON-schema output, validation and retries, cache, cost log |
| `ingest/` | GitHub, Claude Code, Codex, Antigravity, ChatGPT and Claude.ai export parsers |
| `extract.py` | Evidence pack, then validated project facts |
| `skills.py` | Alias table, used by every stage |
| `evaluator.py` + `rubric.yaml` | Scores, decisions, overrides, `Judge` interface |
| `market.py` | Postings, skill extraction, demand, gap |
| `generate.py` | Selection, bullet writers, claim checkers |
| `render.py` | DOCX, PDF and evidence.md |

Schema (append a migration to change it; never edit an applied one):

| Table | Holds |
|---|---|
| `projects` | One row per repo |
| `evidence` | `id, project_id, source, kind, url_or_path, snippet, created_at, session_id`. Every claim points here. |
| `skills_observed` | Skill, project, evidence id (required), kind, origin |
| `sessions` | AI-tool sessions and chats: tool, dates, project link and how it was made, counts, files touched |
| `project_facts` | Extracted facts plus rejected claims |
| `evaluations` / `overrides` | The latest computed decisions, and your overrides |
| `postings` / `posting_skills` | Job postings per role, and their skills with quotes |
| `llm_cache` / `llm_calls` | Cached LLM results, and the call log |
| `settings` | GitHub username and profile, the saved header, the target role |

## What to build next

1. **MCP connector.** Expose the evidence store and `generate` as an MCP server. An AI assistant could then answer "which projects show Kubernetes?" or tailor a resume mid-conversation, still citing evidence ids.
2. **Browser extension.** One click on a job page saves the posting with `market add`, shows the gap report, and generates a tailored resume. This replaces copy and paste, and keeps the no-scraping rule because you choose each posting.
3. **Web UI.** A local review screen with the resume on one side and the cited evidence on the other. You could accept, edit or override bullets and decisions there, instead of reading `evidence.md` and running `override`.
4. **Live market data.** Pull postings from job board APIs that allow it, on a schedule, so demand scores stay current and show trends per role and region.
5. **Local small model.** Implement the `Judge`, `Writer`, `Checker` and `Extractor` protocols on a small local model, for example through llama.cpp or Ollama, so the whole pipeline runs offline at no cost. The existing LLM results can serve as its evaluation set.
