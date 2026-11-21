# resumeforge

Evidence-based resume builder. It reads your real work history, extracts projects and skills,
scores them against market demand and an optional job description, and writes a resume where
every skill and bullet cites the evidence that proves it. No evidence, no claim.

## Quick start

```bash
uv sync
uv run resumeforge ingest                 # GitHub (via gh), Claude Code logs, Codex logs -> resumeforge.db
export ANTHROPIC_API_KEY=...              # or skip and use --offline
uv run resumeforge extract                # LLM: projects + evidence-linked skills
uv run resumeforge evaluate --jd jd.txt   # scores + rationale per skill
uv run resumeforge generate --jd jd.txt   # out/resume.md, out/resume.clean.md, out/resume.evidence.md
uv run resumeforge status
```

`--offline` on `extract` and `generate` swaps the LLM for keyword heuristics and template bullets.
It is useful for a no-key demo and for checking the pipeline, but the bullets are plain.

## What works

| Step | Status |
|---|---|
| GitHub ingest | Works. Uses the locally authenticated `gh` CLI. Owned, non-fork repos: metadata, language bytes, README, your commits. |
| Claude Code ingest | Works. Reads `~/.claude/projects/*/*.jsonl`: user prompts (tool results and system tags stripped), AI session titles, files written/edited. |
| Codex ingest | Works. Reads `~/.codex/sessions/**/*.jsonl`: `user_message` and `task_complete` events. |
| Cursor | Not supported. Chats live in SQLite blobs (`state.vscdb`); cut for time. |
| ChatGPT / Claude.ai exports | Not supported. No export files were found on the dev machine to build against. |
| Redaction | API keys, tokens, JWTs, DB URLs with passwords, private keys, `*_KEY=`/`*_SECRET=` assignments, emails. Applied at ingest and again before every LLM call. |
| Extraction (LLM) | One structured-JSON call per project. Skills citing ids outside the project are dropped. Untested against the live API in this repo (tests use a fake backend). |
| Extraction (offline) | Regex vocabulary in `market_demand.json`. Needs two supporting items or one hard artifact. |
| Evaluation | Deterministic score: evidence strength, recency, market demand, job-description match. Each score has a rationale. |
| Generation | LLM or template bullets. Bullets without valid same-project evidence are dropped and counted. |

## Evidence model

- `evidence`: one row per verifiable item (`source`, `kind`, `ref` = URL or `file:line`, redacted `content`).
- `skills` + `skill_evidence`, `bullets` + `bullet_evidence`: `db.add_skill` and `db.add_bullet` raise `NoEvidenceError`
  without at least one valid evidence id. Bullets may only cite their own project's evidence.
- Projects from different sources merge on a normalized name (`Small_CRM` = `Small-CRM`).

## Outputs

- `resume.md`: resume with `[E#]` markers.
- `resume.evidence.md`: every cited `E#` with source, date, ref and a snippet. Contains local file paths; keep it private.
- `resume.clean.md`: same resume without markers, for sending.

## Market demand

`src/resumeforge/market_demand.json` is a static, hand-curated table of ~50 skills with a 0–1 demand weight and match
patterns. It is a heuristic snapshot, not live job-market data. Edit it, or pass `--jd` so the job description drives ranking.

## Privacy

Only the LLM call sends data off the machine, and only redacted evidence text. GitHub is read, never written.
`resumeforge.db` and `out/` are git-ignored.

## Configuration

- `ANTHROPIC_API_KEY`: required for LLM steps.
- `RESUMEFORGE_MODEL`: default `claude-opus-5-5`. Requests use server-side refusal fallback (`fallbacks="default"`).
- `RESUMEFORGE_DB`: default `resumeforge.db`.

To swap providers, replace `_call_anthropic` in `src/resumeforge/llm.py`; callers only see `complete_json`.

## Tests

```bash
uv run pytest
```
