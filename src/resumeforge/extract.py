"""Turn evidence into projects and skills. Every skill keeps the evidence ids that support it."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from . import db, llm, market

# ---------- LLM path ----------

class SkillClaim(BaseModel):
    name: str = Field(description="Concrete technology or skill, e.g. 'PostgreSQL', 'LLM fine-tuning'")
    category: str = Field(description="One of: language, frontend, backend, data, ml, infra, practice, other")
    evidence_ids: list[int] = Field(description="Ids of evidence items that directly show this skill being used")


class ProjectExtraction(BaseModel):
    summary: str = Field(description="One or two factual sentences on what the project is and what was built")
    substantive: bool = Field(description="False if evidence shows only trivial, unrelated or non-engineering work")
    skills: list[SkillClaim]


SYSTEM = """You extract resume-relevant facts from a developer's raw work history.
Only state what the evidence shows. Do not infer skills from a single passing mention or from
what an AI assistant said it would do; prefer code, files, commits, READMEs and language stats.
Every skill must cite the ids of the evidence items (the numbers in [E#]) that support it.
If evidence is thin, return fewer skills. Never invent metrics."""

MAX_CHARS = 40000


def format_evidence(rows) -> str:
    out, total = [], 0
    for r in rows:
        line = f"[E{r['id']}] ({r['source']}/{r['kind']}, {(r['ts'] or '')[:10]}) {r['content'][:1500]}"
        if total + len(line) > MAX_CHARS:
            break
        out.append(line)
        total += len(line)
    return "\n\n".join(out)


def _save(conn, key, name, summary, rows, skill_claims):
    ts = [r["ts"] for r in rows if r["ts"]]
    conn.execute("INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?)",
                 (key, name, summary, min(ts) if ts else None, max(ts) if ts else None))
    project_ids = {r["id"] for r in rows}
    kept = 0
    for name_, cat, ids in skill_claims:
        ids = [i for i in ids if i in project_ids]  # claims may only cite this project's evidence
        canon = market.canonical(name_)
        m = market.info(canon)
        try:
            db.add_skill(conn, name=canon, display=m["display"] if m else name_,
                         category=m["category"] if m else cat, evidence_ids=ids)
            kept += 1
        except db.NoEvidenceError:
            pass
    return kept


def extract_project_llm(conn, key: str) -> int:
    rows = db.evidence_for_project(conn, key)
    name = display_name(rows)
    res = llm.complete_json(SYSTEM, f"Project: {name}\n\nEvidence:\n{format_evidence(rows)}", ProjectExtraction)
    if not res.substantive:
        conn.execute("INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?)", (key, name, "", None, None))
        return 0
    return _save(conn, key, name, res.summary, rows, [(s.name, s.category, s.evidence_ids) for s in res.skills])


# ---------- offline heuristic path ----------

def _readme_line(text: str) -> str:
    """First prose line of a README (skips headings, badges, code and blank lines)."""
    for line in text.splitlines():
        t = line.strip()
        if len(t) > 30 and not t.startswith(("#", "!", "[", "<", "`", "|", "-", "*", ">")):
            return t
    return ""


def _offline_summary(rows) -> str:
    for r in rows:
        if r["kind"] == "repo":
            m = re.search(r"Description: (.*?)\. Primary language", r["content"])
            if m and m.group(1) not in ("(none)", ""):
                return m.group(1)
    for r in rows:
        if r["kind"] == "readme" and (line := _readme_line(r["content"])):
            return line[:300]
    for r in rows:
        if r["kind"] == "session_title":
            return r["content"].removeprefix("AI coding session: ")
    return ""


def extract_project_offline(conn, key: str) -> int:
    rows = db.evidence_for_project(conn, key)
    name = display_name(rows)
    by_skill: dict[str, list[int]] = {}
    for r in rows:
        for s in market.match(r["content"]):
            by_skill.setdefault(s, []).append(r["id"])
    # Require two supporting items, or one from a hard artifact (languages, files, readme).
    hard = {r["id"] for r in rows if r["kind"] in ("languages", "files_edited", "readme")}
    claims = [(s, "", ids) for s, ids in by_skill.items() if len(ids) >= 2 or hard & set(ids)]
    return _save(conn, key, name, _offline_summary(rows), rows, claims)


def extract_all(conn, offline: bool = False, only: list[str] | None = None, min_evidence: int = 2) -> dict:
    conn.execute("DELETE FROM skill_evidence")
    conn.execute("DELETE FROM skills")
    conn.execute("DELETE FROM projects")
    stats = {}
    for key in db.project_keys(conn):
        if only and key not in {db.project_key(o) for o in only}:
            continue
        if len(db.evidence_for_project(conn, key)) < min_evidence:
            continue
        fn = extract_project_offline if offline else extract_project_llm
        stats[key] = fn(conn, key)
        conn.commit()
    return stats


def display_name(rows) -> str:
    """Prefer the GitHub repo name over a local folder name."""
    gh = [r for r in rows if r["source"] == "github"]
    return (gh or rows)[-1]["project_name"]
