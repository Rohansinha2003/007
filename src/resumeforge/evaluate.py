"""Score each skill for resume inclusion: evidence strength, recency, market demand, job fit.

Deterministic and explainable on purpose. Every score comes with a rationale string.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from . import market

UNKNOWN_DEMAND = 0.3
INCLUDE_THRESHOLD = 0.45


def _age_days(ts: str | None, now: datetime) -> float | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max((now - t).days, 0)


def evaluate(conn, jd_text: str | None = None, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    jd_skills = market.match(jd_text) if jd_text else set()
    jd_lower = (jd_text or "").lower()
    results = []
    for s in conn.execute("SELECT * FROM skills").fetchall():
        ev = conn.execute(
            """SELECT e.* FROM evidence e JOIN skill_evidence se ON se.evidence_id=e.id
               WHERE se.skill_id=?""", (s["id"],)).fetchall()
        n_ev = len(ev)
        n_proj = len({e["project_key"] for e in ev})
        hard = sum(1 for e in ev if e["kind"] in ("languages", "files_edited", "readme", "commit"))
        # Strength saturates: 3 projects or ~8 items is "strong".
        strength = min(1.0, 0.5 * min(n_proj / 3, 1) + 0.3 * min(n_ev / 8, 1) + 0.2 * min(hard / 3, 1))
        ages = [a for a in (_age_days(e["ts"], now) for e in ev) if a is not None]
        recency = math.exp(-min(ages) / 365) if ages else 0.5
        d = market.demand(s["name"])
        dem = d if d is not None else UNKNOWN_DEMAND
        in_jd = s["name"] in jd_skills or s["display"].lower() in jd_lower
        if jd_text:
            score = 0.35 * strength + 0.15 * recency + 0.2 * dem + 0.3 * (1.0 if in_jd else 0.0)
        else:
            score = 0.45 * strength + 0.2 * recency + 0.35 * dem
        include = score >= INCLUDE_THRESHOLD or (in_jd and n_ev >= 1)
        why = (f"{n_ev} evidence items across {n_proj} project(s), {hard} hard artifacts; "
               f"most recent {min(ages) if ages else '?'} days ago; demand "
               f"{dem:.2f}{'' if d is not None else ' (unknown, default)'}"
               f"{'; named in job description' if in_jd else ''}")
        conn.execute("UPDATE skills SET score=?, include=?, rationale=? WHERE id=?",
                     (round(score, 3), int(include), why, s["id"]))
        results.append({"name": s["display"], "category": s["category"], "score": round(score, 3),
                        "include": include, "rationale": why})
    conn.commit()
    return sorted(results, key=lambda r: -r["score"])


def rank_projects(conn, jd_text: str | None = None) -> list[dict]:
    """Projects ranked by the included skills they evidence, evidence volume and recency."""
    jd_skills = market.match(jd_text) if jd_text else set()
    out = []
    for p in conn.execute("SELECT * FROM projects WHERE summary != ''").fetchall():
        rows = conn.execute(
            """SELECT DISTINCT s.name, s.score FROM skills s
               JOIN skill_evidence se ON se.skill_id=s.id JOIN evidence e ON e.id=se.evidence_id
               WHERE e.project_key=? AND s.include=1""", (p["project_key"],)).fetchall()
        n_ev = conn.execute("SELECT COUNT(*) FROM evidence WHERE project_key=?", (p["project_key"],)).fetchone()[0]
        skill_score = sum(r["score"] for r in rows)
        jd_hits = sum(1 for r in rows if r["name"] in jd_skills)
        score = skill_score + 0.5 * math.log1p(n_ev) + 1.0 * jd_hits
        out.append({"project_key": p["project_key"], "name": p["name"], "score": round(score, 2),
                    "skills": [r["name"] for r in rows], "last_ts": p["last_ts"]})
    return sorted(out, key=lambda r: -r["score"])
