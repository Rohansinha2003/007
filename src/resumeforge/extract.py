"""Extraction: per project, evidence -> validated structured facts.

Every technology and skill the model returns must cite evidence ids that were in its input
for that project. Claims without a valid id are rejected and recorded, never stored as skills.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from . import db
from .llm import LLM
from .redact import redact
from .skills import canonical, key

PURPOSE = "extract_project"
BUDGET_CHARS = 24000
README_CHARS = 6000
MESSAGE_CHARS = 700


# ---------- output schema ----------

class Claim(BaseModel):
    name: str = Field(description="Technology or skill name, e.g. 'PostgreSQL', 'OAuth', 'LLM fine-tuning'")
    evidence_ids: list[int] = Field(description="Ids of evidence items (the number in [E#]) that show it was used")


class Outcome(BaseModel):
    value: Literal["shipped", "prototype", "abandoned"]
    reason: str = Field(description="One sentence")
    evidence_ids: list[int]


class Authorship(BaseModel):
    value: Literal["self_written", "ai_assisted", "mostly_ai", "unknown"]
    reason: str = Field(description="One sentence, based on the AI session logs")
    evidence_ids: list[int]


class ProjectFacts(BaseModel):
    summary: str = Field(description="One or two factual sentences: what the project is and what was built")
    technologies: list[Claim] = Field(description="Languages, frameworks, libraries, databases, platforms")
    skills: list[Claim] = Field(description="Engineering capabilities shown, e.g. 'REST API design', 'Authentication'")
    outcome: Outcome
    authorship: Authorship


SYSTEM = """You extract resume facts about one software project from evidence of the developer's real work.

Evidence items are marked [E<id>]. Rules:
- Only claim what the evidence shows. Cite every claim with the ids of the items that support it.
- A technology needs concrete support: language stats, README, files edited, or a session where it was used.
  A passing mention or an AI assistant's plan is not enough.
- Skills are capabilities (e.g. authentication, REST API design, LLM fine-tuning, data pipeline), not tools.
- outcome: "shipped" if deployed or released, "prototype" if built but not deployed, "abandoned" if
  the evidence shows it was left incomplete. Cite the items that show it.
- authorship, judged from the AI coding session logs:
  "mostly_ai" when the developer mainly asked an AI tool to build features and the tool wrote most files;
  "ai_assisted" when the developer directed detailed work or wrote code with AI help on parts;
  "self_written" only when the evidence positively indicates the developer wrote it without AI help;
  "unknown" when there are no session logs or they are too thin to judge. Missing logs are not proof of
  self-written code. Cite the session items you used.
- Never invent metrics, users, or outcomes. Keep the lists short and specific."""


# ---------- input ----------

@dataclass
class Pack:
    project_id: int
    name: str
    prompt: str
    evidence_ids: set[int] = field(default_factory=set)


def _line(r, limit: int) -> str:
    text = " ".join(r["snippet"].split()) if r["kind"] != "readme" else r["snippet"].strip()
    if len(text) > limit:
        text = text[:limit] + " …"
    where = f"session '{r['title']}' ({r['tool']})" if r["session_id"] else r["source"]
    return f"[E{r['id']}] {r['kind']} · {where} · {(r['created_at'] or '')[:10]}\n{text}"


def build_pack(conn, project_id: int) -> Pack | None:
    p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    rows = conn.execute(
        """SELECT e.*, s.title, s.source AS tool, s.started_at AS s_started
           FROM evidence e LEFT JOIN sessions s ON s.id = e.session_id
           WHERE e.project_id=?
           ORDER BY e.session_id IS NOT NULL, s.started_at DESC, e.id""", (project_id,)).fetchall()
    if not rows:
        return None
    priority = {"repo": 0, "deployment": 1, "commits": 2, "languages": 3, "topics": 4, "readme": 5,
                "files_touched": 6, "assistant_summary": 7, "user_message": 8, "assistant_message": 9}
    github = sorted([r for r in rows if r["session_id"] is None], key=lambda r: priority.get(r["kind"], 10))
    sessions = [r for r in rows if r["session_id"] is not None]
    # files lists first, then conversation in order, newest session first
    sessions = [r for r in sessions if r["kind"] == "files_touched"] + [r for r in sessions if r["kind"] != "files_touched"]
    parts, used, total = [], set(), 0
    for r in github + sessions:
        line = _line(r, README_CHARS if r["kind"] == "readme" else MESSAGE_CHARS)
        if total + len(line) > BUDGET_CHARS:
            continue
        parts.append(line)
        used.add(r["id"])
        total += len(line)
    n_sessions = conn.execute("SELECT COUNT(*) FROM sessions WHERE project_id=?", (project_id,)).fetchone()[0]
    header = (f"Project: {p['name']}\nRepository: {p['url']}\n"
              f"Linked AI coding sessions: {n_sessions}"
              + ("" if n_sessions else " (none, so authorship cannot be judged from logs)") + "\n")
    omitted = len(rows) - len(used)
    prompt = header + (f"({omitted} more evidence items omitted for length)\n" if omitted else "") + \
        "\nEvidence:\n\n" + "\n\n".join(parts)
    return Pack(project_id, p["name"], prompt, used)


# ---------- validation ----------

def validate(facts: ProjectFacts, allowed: set[int]) -> tuple[dict, list[dict]]:
    """Keep only claims with at least one evidence id from `allowed`. Returns (clean facts, rejected claims)."""
    rejected: list[dict] = []

    def claims(items: list[Claim], kind: str) -> list[dict]:
        merged: dict[str, dict] = {}
        for c in items:
            name = canonical(c.name)
            ids = sorted({i for i in c.evidence_ids if i in allowed})
            if not name:
                continue
            if not ids:
                rejected.append({"type": kind, "name": c.name, "evidence_ids": c.evidence_ids,
                                 "reason": "no valid evidence id"})
                continue
            m = merged.setdefault(key(name), {"name": name, "evidence_ids": []})
            m["evidence_ids"] = sorted(set(m["evidence_ids"]) | set(ids))
        return list(merged.values())

    technologies = claims(facts.technologies, "technology")
    tech_keys = {key(t["name"]) for t in technologies}
    skills = [s for s in claims(facts.skills, "skill") if key(s["name"]) not in tech_keys]

    out_ids = sorted({i for i in facts.outcome.evidence_ids if i in allowed})
    outcome = {"value": facts.outcome.value, "reason": facts.outcome.reason, "evidence_ids": out_ids}
    if not out_ids:
        rejected.append({"type": "outcome", **facts.outcome.model_dump(), "reason_rejected": "no valid evidence id"})
        outcome = {"value": None, "reason": None, "evidence_ids": []}

    a = facts.authorship
    auth_ids = sorted({i for i in a.evidence_ids if i in allowed})
    authorship = {"value": a.value, "reason": a.reason, "evidence_ids": auth_ids}
    if a.value != "unknown" and not auth_ids:
        rejected.append({"type": "authorship", **a.model_dump(), "reason_rejected": "no valid evidence id"})
        authorship = {"value": "unknown", "reason": "Claim had no valid evidence.", "evidence_ids": []}

    clean = {"summary": facts.summary.strip(), "technologies": technologies, "skills": skills,
             "outcome": outcome, "authorship": authorship}
    return clean, rejected


def store(conn, pack: Pack, clean: dict, rejected: list[dict], model: str, input_hash: str) -> int:
    conn.execute("DELETE FROM skills_observed WHERE project_id=? AND origin='llm'", (pack.project_id,))
    n = 0
    for kind, items in (("technology", clean["technologies"]), ("skill", clean["skills"])):
        for c in items:
            for eid in c["evidence_ids"]:
                db.observe_skill(conn, skill=c["name"], evidence_id=eid, kind=kind, origin="llm")
                n += 1
    conn.execute(
        """INSERT OR REPLACE INTO project_facts
           (project_id, summary, outcome, outcome_reason, authorship, authorship_reason, facts, rejected,
            model, input_hash, extracted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (pack.project_id, redact(clean["summary"]), clean["outcome"]["value"], clean["outcome"]["reason"],
         clean["authorship"]["value"], clean["authorship"]["reason"], json.dumps(clean), json.dumps(rejected),
         model, input_hash, db.now()))
    conn.commit()
    return n


def candidate_projects(conn, min_evidence: int = 2, names: list[str] | None = None) -> list[int]:
    rows = conn.execute(
        """SELECT p.id, p.name, COUNT(e.id) n FROM projects p JOIN evidence e ON e.project_id=p.id
           GROUP BY p.id HAVING n >= ? ORDER BY MAX(e.created_at) DESC""", (min_evidence,)).fetchall()
    if names:
        wanted = {n.lower() for n in names}
        rows = [r for r in rows if r["name"].lower() in wanted]
    return [r["id"] for r in rows]


def extract_project(conn, llm: LLM, project_id: int, use_cache: bool = True) -> dict:
    pack = build_pack(conn, project_id)
    if pack is None:
        return {"project": project_id, "skipped": "no evidence"}
    facts = llm.complete_json(SYSTEM, pack.prompt, ProjectFacts, purpose=PURPOSE, use_cache=use_cache)
    clean, rejected = validate(facts, pack.evidence_ids)
    h = LLM.cache_key(llm.model, redact(SYSTEM), redact(pack.prompt), {})
    n = store(conn, pack, clean, rejected, llm.model, h)
    return {"project": pack.name, "skills_stored": n, "technologies": len(clean["technologies"]),
            "skills": len(clean["skills"]), "rejected": len(rejected), "outcome": clean["outcome"]["value"],
            "authorship": clean["authorship"]["value"]}
