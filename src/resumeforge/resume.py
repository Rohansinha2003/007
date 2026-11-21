"""Generate a resume where every bullet and skill cites evidence ids."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from pydantic import BaseModel, Field

from . import db, evaluate, llm
from .extract import format_evidence


class Bullet(BaseModel):
    text: str = Field(description="One resume bullet, past tense, starts with a strong verb, max ~30 words")
    evidence_ids: list[int] = Field(description="Ids of the evidence items that prove this bullet")


class ProjectBullets(BaseModel):
    title: str = Field(description="Short project title for the resume")
    bullets: list[Bullet]


SYSTEM = """You write resume bullets for a software developer from evidence of their real work.
Rules:
- Every bullet must be directly supported by the cited evidence items ([E#] numbers). Cite all that apply.
- Do not invent numbers, users, companies, or outcomes. Only use metrics that appear in the evidence.
- Describe what the developer built or did, not what an AI assistant suggested.
- 2 to 4 bullets. Concrete technologies over adjectives.
- If a job description is given, emphasize evidence relevant to it, but never claim unrelated skills."""


def _bullets_llm(conn, p: dict, jd: str | None) -> tuple[str, list[tuple[str, list[int]]]]:
    rows = db.evidence_for_project(conn, p["project_key"])
    summary = conn.execute("SELECT summary FROM projects WHERE project_key=?", (p["project_key"],)).fetchone()[0]
    user = (f"Project: {p['name']}\nSummary: {summary}\nSkills evidenced: {', '.join(p['skills'])}\n"
            + (f"\nTarget job description:\n{jd[:6000]}\n" if jd else "")
            + f"\nEvidence:\n{format_evidence(rows)}")
    res = llm.complete_json(SYSTEM, user, ProjectBullets)
    return res.title, [(b.text, b.evidence_ids) for b in res.bullets]


def _bullets_offline(conn, p: dict, jd: str | None) -> tuple[str, list[tuple[str, list[int]]]]:
    """Template bullets. Each bullet cites the evidence behind every fact it states."""
    key = p["project_key"]
    rows = db.evidence_for_project(conn, key)
    bullets = []
    summary = conn.execute("SELECT summary FROM projects WHERE project_key=?", (key,)).fetchone()[0]
    src = [r["id"] for r in rows if summary and summary[:40] in r["content"]] or [rows[0]["id"]]
    bullets.append((summary.rstrip(".") + ".", src[:2]))
    top = conn.execute(
        """SELECT s.display, MIN(e.id) eid FROM skills s JOIN skill_evidence se ON se.skill_id=s.id
           JOIN evidence e ON e.id=se.evidence_id
           WHERE e.project_key=? AND s.include=1 GROUP BY s.id ORDER BY s.score DESC LIMIT 6""", (key,)).fetchall()
    if top:
        bullets.append(("Used " + ", ".join(t["display"] for t in top) + ".", [t["eid"] for t in top]))
    commits = [r for r in rows if r["kind"] == "commit"]
    if commits:
        recent = commits[-3:]
        bullets.append(("Recent commits: " + "; ".join(c["content"].splitlines()[0][:80].rstrip(". ") for c in recent)
                        + ".", [c["id"] for c in recent]))
    return p["name"], bullets


def generate(conn, *, profile: dict, jd: str | None = None, offline: bool = False,
             n_projects: int = 4, out_dir: Path = Path("out")) -> dict:
    resume_id = uuid.uuid4().hex[:8]
    ranked = evaluate.rank_projects(conn, jd)[:n_projects]
    sections, dropped = [], 0
    for p in ranked:
        title, raw = (_bullets_offline if offline else _bullets_llm)(conn, p, jd)
        kept = []
        for text, ids in raw:
            if not text.strip():
                continue
            try:
                bid = db.add_bullet(conn, resume_id=resume_id, project_key_=p["project_key"],
                                    text=text.strip(), evidence_ids=ids)
                kept.append(bid)
            except db.NoEvidenceError:
                dropped += 1  # no evidence, no claim
        if kept:
            sections.append((title, p, kept))
    conn.commit()
    paths = render(conn, resume_id, profile, sections, out_dir)
    return {"resume_id": resume_id, "projects": len(sections), "dropped_bullets": dropped, **paths}


CATEGORY_LABELS = {"language": "Languages", "frontend": "Frontend", "backend": "Backend", "data": "Data",
                   "ml": "AI / ML", "infra": "Cloud & DevOps", "practice": "Practices", "other": "Other"}


def _refs(ids) -> str:
    return " " + " ".join(f"[E{i}]" for i in sorted(ids))


def render(conn, resume_id, profile, sections, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    cited: set[int] = set()
    lines = [f"# {profile['name']}", "", f"GitHub: {profile['url']}"
             + (f" · {profile['location']}" if profile.get("location") else ""), ""]
    skills = conn.execute("SELECT * FROM skills WHERE include=1 ORDER BY category, score DESC").fetchall()
    if skills:
        lines += ["## Skills", ""]
        by_cat: dict[str, list] = {}
        for s in skills:
            by_cat.setdefault(s["category"], []).append(s)
        for cat, ss in by_cat.items():
            parts = []
            for s in ss:
                ids = [r[0] for r in conn.execute(
                    "SELECT evidence_id FROM skill_evidence WHERE skill_id=? ORDER BY evidence_id LIMIT 3", (s["id"],))]
                cited.update(ids)
                parts.append(s["display"] + _refs(ids))
            lines.append(f"- **{CATEGORY_LABELS.get(cat, cat.title())}:** " + ", ".join(parts))
        lines.append("")
    lines += ["## Projects", ""]
    for title, p, bullet_ids in sections:
        when = (p.get("last_ts") or "")[:7]
        lines.append(f"### {title}" + (f" ({when})" if when else ""))
        for bid in bullet_ids:
            text = conn.execute("SELECT text FROM bullets WHERE id=?", (bid,)).fetchone()[0]
            ids = [r[0] for r in conn.execute("SELECT evidence_id FROM bullet_evidence WHERE bullet_id=?", (bid,))]
            cited.update(ids)
            lines.append(f"- {text}{_refs(ids)}")
        lines.append("")
    resume_md = "\n".join(lines)

    ev_lines = [f"# Evidence for resume {resume_id}", "",
                "Every [E#] marker in the resume points to one of these rows.", ""]
    for i in sorted(cited):
        e = conn.execute("SELECT * FROM evidence WHERE id=?", (i,)).fetchone()
        snippet = " ".join(e["content"].split())[:200]
        ev_lines.append(f"- **E{i}** · {e['source']}/{e['kind']} · {(e['ts'] or '')[:10]} · `{e['ref']}`\n  > {snippet}")
    clean = re.sub(r" \[E\d+\]", "", resume_md)
    paths = {"resume": out_dir / "resume.md", "clean": out_dir / "resume.clean.md",
             "evidence": out_dir / "resume.evidence.md"}
    paths["resume"].write_text(resume_md)
    paths["clean"].write_text(clean)
    paths["evidence"].write_text("\n".join(ev_lines) + "\n")
    return {k: str(v) for k, v in paths.items()}
