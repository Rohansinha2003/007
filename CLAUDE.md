# Project: evidence-based AI resume builder (24-hour MVP)

Goal: read a developer's real work history (GitHub, local AI-tool logs, chat exports),
extract projects and skills with evidence, evaluate what belongs on a resume against
market demand, and generate a tailored resume where every claim links to evidence.

Rules:
- Python 3.11+, SQLite (single file), Typer CLI, pytest. Keep dependencies minimal.
- LLM calls go through one `llm.py` wrapper (Anthropic API, key from ANTHROPIC_API_KEY)
  so the provider can be swapped later. Always request structured JSON output and validate it.
- Local-first: never send data anywhere except the LLM call. Redact secrets before any LLM call.
- Every skill and resume bullet must link to at least one evidence row. No evidence, no claim.
- Do not assume file formats or paths for external tools. Inspect real files on this machine first.
- After each step: run the tests, update README.md with what works, and commit.
- Prefer working and simple over complete. Cut scope before missing the time box.
