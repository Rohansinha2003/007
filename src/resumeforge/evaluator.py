"""Decide what stays on the resume: deterministic rubric scoring plus a pluggable depth judge.

Pipeline per project:
  1. gather inputs from the database (ProjectInput)
  2. a Judge rates depth and complexity (1-5) with a one-line reason and evidence ids
  3. score_project() combines all criteria with the rubric weights -> score, breakdown, decision
Skills are scored entirely in code (score_skill).
With job postings on file for the target role, both get a "demand" criterion from market.demand();
without postings that criterion is left out and the other weights are rescaled.

To plug in a local model later, implement the Judge protocol (a `name` and a `judge()` method that
returns a Judgment) and pass it to evaluate(). Nothing else changes.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

import yaml
from pydantic import BaseModel, Field

from . import db, market
from .llm import LLM, LLMError
from .skills import canonical, key as skill_key

DEFAULT_RUBRIC = Path(__file__).with_name("rubric.yaml")
PROJECT_CRITERIA = ("depth", "originality", "evidence", "shipped", "recency", "demand")
SKILL_CRITERIA = ("breadth", "depth", "recency", "evidence", "demand")
DECISIONS = ("keep", "rewrite", "drop", "needs_evidence")


# ---------- rubric ----------

class RubricError(ValueError):
    pass


def load_rubric(path: str | Path | None = None) -> dict:
    raw = Path(path or DEFAULT_RUBRIC).read_text()
    r = yaml.safe_load(raw)
    for group, crit in (("projects", PROJECT_CRITERIA), ("skills", SKILL_CRITERIA)):
        w = r[group]["weights"]
        missing = set(crit) - set(w)
        if missing:
            raise RubricError(f"{group}.weights missing {sorted(missing)}")
        if any(v < 0 for v in w.values()) or sum(w.values()) <= 0:
            raise RubricError(f"{group}.weights must be non-negative and not all zero")
        total = sum(w[c] for c in crit)
        r[group]["weights"] = {c: w[c] / total for c in crit}
        t = r[group]["thresholds"]
        if not 0 <= t["rewrite"] <= t["keep"] <= 1:
            raise RubricError(f"{group}.thresholds need 0 <= rewrite <= keep <= 1")
    r["market"] = {**market.DEFAULT_WEIGHTS, "project_top_k": 3, "min_language_share": 0.05, **(r.get("market") or {})}
    r["hash"] = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return r


# ---------- shared math ----------

def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def recency_score(ts: str | None, now: datetime, half_life_days: float) -> float:
    t = parse_ts(ts)
    if t is None:
        return 0.0
    age = max((now - t).total_seconds() / 86400, 0)
    return 0.5 ** (age / half_life_days)


def saturating(n: float, saturation: float) -> float:
    """log-scaled 0..1: 0 -> 0, saturation -> 1."""
    if n <= 0:
        return 0.0
    return min(1.0, math.log1p(n) / math.log1p(saturation))


def band(score: float, thresholds: dict) -> str:
    if score >= thresholds["keep"]:
        return "keep"
    if score >= thresholds["rewrite"]:
        return "rewrite"
    return "drop"


def active_weights(weights: dict[str, float], use_demand: bool) -> dict[str, float]:
    """Weights to apply: all of them, or without demand (rescaled to sum to 1) when there is no market data."""
    if use_demand:
        return weights
    rest = {c: w for c, w in weights.items() if c != "demand"}
    total = sum(rest.values())
    return {c: w / total for c, w in rest.items()} if total else rest


def project_demand(skills: dict[str, str], dem: dict, top_k: int) -> tuple[float, list[str]]:
    """Share of the role's top-k demand that the project's top-k skills cover, in [0, 1]."""
    mine = sorted(((dem[k].score, dem[k].skill) for k in skills if k in dem), reverse=True)[:top_k]
    best = sum(sorted((d.score for d in dem.values()), reverse=True)[:top_k])
    if not best:
        return 0.0, []
    return min(1.0, sum(v for v, _ in mine) / best), [n for v, n in mine if v > 0]


def combine(values: dict[str, float], weights: dict[str, float]) -> tuple[float, dict]:
    breakdown = {c: {"value": round(values[c], 3), "weight": round(weights[c], 3),
                     "points": round(values[c] * weights[c], 3)} for c in weights}
    return round(sum(b["points"] for b in breakdown.values()), 3), breakdown


# ---------- judge interface ----------

class Judgment(BaseModel):
    depth: Literal[1, 2, 3, 4, 5] = Field(description="Technical depth: 1 tutorial/toy, 3 real app with several parts, 5 substantial system")
    complexity: Literal[1, 2, 3, 4, 5] = Field(description="Engineering complexity: integrations, data, infra, algorithms")
    reason: str = Field(description="One line, at most 20 words, on why this project should or should not be on a resume")
    evidence_ids: list[int] = Field(description="Ids of the evidence items the judgment is based on")


@dataclass
class ProjectInput:
    id: int
    name: str
    url: str | None
    summary: str | None
    outcome: str | None          # from extraction, if run
    authorship: str | None       # from extraction, if run
    has_deployment: bool
    deployed_url: str | None
    commit_count: int
    last_activity: str | None
    languages: dict[str, int]
    n_sessions: int
    n_user_messages: int
    files_edited: int
    readme: str
    evidence: list[dict] = field(default_factory=list)   # {id, kind, text}
    skills: dict[str, str] = field(default_factory=dict)  # skill key -> name, for market demand

    @property
    def evidence_ids(self) -> set[int]:
        return {e["id"] for e in self.evidence}


class Judge(Protocol):
    name: str

    def judge(self, p: ProjectInput) -> Judgment: ...


class JudgmentRejected(ValueError):
    pass


class HeuristicJudge:
    """Deterministic stand-in for the LLM: rates depth from measurable signals."""
    name = "heuristic"
    _KEYWORDS = re.compile(r"\b(test|tests|architecture|deploy|docker|ci|auth|api|database|schema|pipeline|model)\b", re.I)

    def judge(self, p: ProjectInput) -> Judgment:
        code_kb = sum(p.languages.values()) / 1024
        topics = len({m.lower() for m in self._KEYWORDS.findall(p.readme)})
        depth = 1
        depth += len(p.readme) > 1500
        depth += p.files_edited >= 10 or p.commit_count >= 10
        depth += p.n_sessions >= 2 or p.n_user_messages >= 10
        depth += p.has_deployment or topics >= 4
        complexity = 1 + sum(code_kb >= t for t in (20, 100, 500, 2000))
        complexity = min(5, complexity + (len(p.languages) >= 4))
        used = [e["id"] for e in p.evidence if e["kind"] in ("readme", "languages", "commits", "files_touched", "deployment")]
        reason = (f"README {len(p.readme) / 1000:.1f}k chars, {p.commit_count} commits, {p.files_edited} files edited "
                  f"in {p.n_sessions} sessions, {code_kb:.0f} KB in {len(p.languages)} languages")
        return Judgment(depth=min(depth, 5), complexity=complexity, reason=reason, evidence_ids=used)


class LLMJudge:
    """Asks the LLM for depth, complexity and a one-line reason. Results are cached by input."""
    name = "llm"
    SYSTEM = """You judge whether a software project is worth a place on its author's resume.
Rate technical depth and engineering complexity from 1 to 5 using only the evidence given:
1 = tutorial, clone or toy; 2 = small single-feature app; 3 = real app with several working parts;
4 = substantial system with integrations, data or infrastructure; 5 = complex system with hard problems solved.
Write one line, at most 20 words, naming the concrete reason. Cite the evidence ids ([E#]) you relied on.
Do not reward length of text or ambition stated in plans; reward what was built."""

    def __init__(self, llm: LLM):
        self.llm = llm

    def prompt(self, p: ProjectInput) -> str:
        lines = [f"Project: {p.name}", f"Extracted summary: {p.summary or '(not extracted)'}",
                 f"Outcome: {p.outcome or ('deployed' if p.has_deployment else 'unknown')}",
                 f"Commits by author: {p.commit_count}; AI sessions: {p.n_sessions}", "", "Evidence:"]
        lines += [f"[E{e['id']}] {e['kind']}: {e['text']}" for e in p.evidence]
        return "\n".join(lines)

    def judge(self, p: ProjectInput) -> Judgment:
        j = self.llm.complete_json(self.SYSTEM, self.prompt(p), Judgment, purpose="judge_project", max_tokens=4000)
        valid = [i for i in j.evidence_ids if i in p.evidence_ids]
        if not valid:
            raise JudgmentRejected("judgment cited no valid evidence ids")
        return j.model_copy(update={"evidence_ids": valid, "reason": " ".join(j.reason.split())[:200]})


# ---------- inputs ----------

def project_inputs(conn, project_id: int, min_language_share: float = 0.0) -> ProjectInput:
    p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    f = conn.execute("SELECT * FROM project_facts WHERE project_id=?", (project_id,)).fetchone()
    sess = conn.execute("""SELECT COUNT(*) n, COALESCE(SUM(user_message_count), 0) um, MAX(ended_at) last
                           FROM sessions WHERE project_id=?""", (project_id,)).fetchone()
    edited = set()
    for (ft,) in conn.execute("SELECT files_touched FROM sessions WHERE project_id=?", (project_id,)):
        edited |= set(json.loads(ft or "{}").get("edited", []))
    rows = conn.execute("SELECT id, kind, snippet FROM evidence WHERE project_id=? ORDER BY session_id IS NOT NULL, id",
                        (project_id,)).fetchall()
    readme = next((r["snippet"] for r in rows if r["kind"] == "readme"), "")
    compact, summaries = [], 0
    for r in rows:
        if r["kind"] in ("repo", "languages", "commits", "deployment", "topics", "files_touched"):
            compact.append({"id": r["id"], "kind": r["kind"], "text": " ".join(r["snippet"].split())[:600]})
        elif r["kind"] == "readme":
            compact.append({"id": r["id"], "kind": r["kind"], "text": r["snippet"][:3000]})
        elif r["kind"] in ("assistant_summary", "assistant_message") and summaries < 6:
            summaries += 1
            compact.append({"id": r["id"], "kind": r["kind"], "text": " ".join(r["snippet"].split())[:400]})
    langs = json.loads(p["languages"] or "{}")
    total_bytes = sum(langs.values()) or 1
    skills = {}
    for r in conn.execute("SELECT skill, kind, origin FROM skills_observed WHERE project_id=?", (project_id,)):
        if r["kind"] == "language" and r["origin"] == "github":
            share = next((b for l, b in langs.items() if skill_key(canonical(l)) == skill_key(canonical(r["skill"]))), 0) / total_bytes
            if share < min_language_share:
                continue
        skills[skill_key(canonical(r["skill"]))] = canonical(r["skill"])
    last = max([t for t in (p["last_commit_at"], sess["last"]) if t], key=lambda t: parse_ts(t) or datetime.min.replace(tzinfo=timezone.utc), default=None)
    return ProjectInput(
        id=p["id"], name=p["name"], url=p["url"], summary=f["summary"] if f else None,
        outcome=f["outcome"] if f else None, authorship=f["authorship"] if f else None,
        has_deployment=bool(p["has_deployment"]), deployed_url=p["deployed_url"], commit_count=p["commit_count"] or 0,
        last_activity=last, languages=langs, n_sessions=sess["n"],
        n_user_messages=sess["um"], files_edited=len(edited), readme=readme, evidence=compact, skills=skills)


# ---------- scoring ----------

@dataclass
class Result:
    item_id: str
    item_type: str
    name: str
    score: float
    decision: str
    breakdown: dict
    reason: str
    judge: str | None = None


def score_project(p: ProjectInput, j: Judgment, rubric: dict, now: datetime, username: str | None = None,
                  judge_name: str = "", demand: dict | None = None, role: str | None = None) -> Result:
    """demand: market.demand() for the target role, or None to score without the demand criterion."""
    R = rubric["projects"]
    outcome = p.outcome or ("shipped" if p.has_deployment else "unknown")
    outcome_src = "extraction" if p.outcome else ("GitHub deployment" if p.has_deployment else "no data")
    authorship = p.authorship or "unknown"
    kinds = {e["kind"] for e in p.evidence} | ({"user_message"} if p.n_user_messages else set())
    ev = R["evidence"]
    n_evidence = len(p.evidence) + p.n_user_messages
    values = {
        "depth": ((j.depth + j.complexity) / 2 - 1) / 4,
        "originality": R["originality"].get(authorship, R["originality"]["unknown"]),
        "evidence": ev["volume_weight"] * saturating(n_evidence, ev["saturation"])
                    + (1 - ev["volume_weight"]) * len(kinds & set(ev["kinds"])) / len(ev["kinds"]),
        "shipped": R["shipped"].get(outcome, R["shipped"]["unknown"]),
        "recency": recency_score(p.last_activity, now, R["recency_half_life_days"]),
    }
    demand_skills: list[str] = []
    if demand is not None:
        values["demand"], demand_skills = project_demand(p.skills, demand, rubric["market"]["project_top_k"])
    weights = active_weights(R["weights"], demand is not None)
    score, breakdown = combine(values, weights)
    breakdown["inputs"] = {"role": role, "demand_skills": demand_skills, "depth": j.depth, "complexity": j.complexity, "authorship": authorship,
                           "outcome": outcome, "outcome_source": outcome_src, "evidence_rows": n_evidence,
                           "evidence_kinds": sorted(kinds), "last_activity": p.last_activity,
                           "judge_evidence_ids": j.evidence_ids}
    decision = band(score, R["thresholds"])
    rule = None
    if R["rules"].get("drop_profile_repo") and username and p.name.lower() == username.lower():
        rule = "profile README repository, not a project"
    elif R["rules"].get("drop_without_own_work") and p.commit_count == 0 and p.n_sessions == 0:
        rule = "no commits by you and no AI sessions linked"
    if rule:
        decision = "drop"
        breakdown["rule"] = rule
    return Result(f"p{p.id}", "project", p.name, score, decision, breakdown,
                  _project_reason(j.reason, values, weights, breakdown["inputs"], rule), judge_name)


def _project_reason(judge_reason: str, values: dict, weights: dict, inputs: dict, rule: str | None) -> str:
    if rule:
        return f"Rule: {rule}."
    labels = {"depth": f"depth {inputs['depth']}/5, complexity {inputs['complexity']}/5",
              "originality": f"authorship {inputs['authorship']}", "evidence": f"{inputs['evidence_rows']} evidence rows",
              "shipped": f"outcome {inputs['outcome']}",
              "recency": f"last active {(inputs['last_activity'] or 'unknown')[:10]}",
              "demand": _demand_label(values.get("demand", 0.0), inputs.get("role"), inputs.get("demand_skills") or [])}
    ranked = sorted(weights, key=lambda c: values[c])
    weak = [labels[c] for c in ranked if values[c] < 0.4][:2]
    strong = [labels[c] for c in reversed(ranked) if values[c] >= 0.7][:2]
    parts = []
    if strong:
        parts.append("Strong: " + ", ".join(strong) + ".")
    if weak:
        parts.append("Weak: " + ", ".join(weak) + ".")
    parts.append(judge_reason.rstrip(".") + ".")
    return " ".join(parts)


def _demand_label(value: float, role: str | None, skills: list[str]) -> str:
    named = "/".join(skills[:3])
    if value >= 0.4:
        return f"{role} demand for {named}"
    return f"low {role} demand" + (f" (only {named})" if named else " for its skills")


def score_skill(name: str, rows: list[dict], kept_projects: set[int], languages_by_project: dict[int, dict],
                rubric: dict, now: datetime, demand: dict | None = None, role: str | None = None) -> Result:
    """rows: skills_observed joined with evidence created_at: {project_id, evidence_id, kind, created_at}."""
    S = rubric["skills"]
    k = skill_key(name)
    in_kept = [r for r in rows if r["project_id"] in kept_projects]
    projects = {r["project_id"] for r in in_kept}
    evidence = {r["evidence_id"] for r in in_kept}
    # depth: byte share for languages, evidence density for extracted skills
    shares = []
    for pid in projects:
        langs = {skill_key(canonical(l)): b for l, b in languages_by_project.get(pid, {}).items()}
        total = sum(langs.values())
        if total and k in langs:
            shares.append(langs[k] / total)
    lang_depth = sum(sorted(shares, reverse=True)[:3]) / min(3, len(shares)) if shares else 0.0
    extracted = [r for r in in_kept if r["kind"] != "language"]
    per_project = len({r["evidence_id"] for r in extracted}) / max(1, len({r["project_id"] for r in extracted}))
    other_depth = min(1.0, per_project / S["depth_items_per_project"]) if extracted else 0.0
    latest = max((r["created_at"] for r in in_kept if r["created_at"]), key=lambda t: parse_ts(t) or datetime.min.replace(tzinfo=timezone.utc), default=None)
    values = {
        "breadth": min(1.0, len(projects) / S["breadth_saturation"]),
        "depth": max(lang_depth, other_depth),
        "recency": recency_score(latest, now, S["recency_half_life_days"]),
        "evidence": saturating(len(evidence), S["evidence_saturation"]),
    }
    d = demand.get(k) if demand is not None else None
    if demand is not None:
        values["demand"] = d.score if d else 0.0
    score, breakdown = combine(values, active_weights(S["weights"], demand is not None))
    all_projects = {r["project_id"] for r in rows}
    breakdown["inputs"] = {"role": role, "demand": d.as_dict() if d else None, "projects_kept": len(projects), "projects_total": len(all_projects),
                           "evidence_rows": len(evidence), "kinds": sorted({r["kind"] for r in rows}),
                           "last_seen": latest}
    if not projects:
        decision = "drop"
        reason = f"Only appears in {len(all_projects)} dropped project(s)."
    else:
        decision = band(score, S["thresholds"])
        reason = (f"Used in {len(projects)} kept project(s) with {len(evidence)} evidence rows; "
                  f"depth {values['depth']:.2f}; last seen {(latest or 'unknown')[:10]}.")
        if demand is not None:
            reason += (f" In {d.postings}/{d.total} {role} postings ({d.flag})." if d
                       else f" No {role} posting asks for it.")
        if decision == "keep" and values["depth"] < S.get("min_depth_for_keep", 0):
            decision = "rewrite"
            breakdown["rule"] = f"depth {values['depth']:.2f} below min_depth_for_keep {S['min_depth_for_keep']}"
            reason += " Only a trace of it in each project, so mention it in context rather than list it."
    return Result(f"s:{k}", "skill", name, score, decision, breakdown, reason)


def unsupported_skill(name: str, claims: int, rubric: dict, demand: dict | None = None) -> Result:
    """A skill the model claimed but that has no evidence. Only drop or needs_evidence are allowed."""
    decision = "needs_evidence" if claims >= rubric["skills"]["needs_evidence_min_claims"] else "drop"
    breakdown = {c: {"value": 0.0, "weight": round(rubric["skills"]["weights"][c], 3), "points": 0.0} for c in SKILL_CRITERIA}
    breakdown["inputs"] = {"evidence_rows": 0, "claims_without_evidence": claims}
    reason = f"Claimed {claims} time(s) during extraction with no valid evidence."
    d = (demand or {}).get(skill_key(name))
    if d:
        reason += f" In demand ({d.postings}/{d.total} postings): find or add evidence."
    return Result(f"s:{skill_key(name)}", "skill", name, 0.0, decision, breakdown, reason)


# ---------- run ----------

def evaluate(conn, judge: Judge, rubric: dict, now: datetime | None = None,
             fallback: Judge | None = None, on_project=None, role: str | None = None) -> list[Result]:
    """role: target role for the demand criterion (see market.resolve_role); None scores without demand."""
    now = now or datetime.now(timezone.utc)
    demand = market.demand(conn, role, rubric["market"]) if role else None
    fallback = fallback or HeuristicJudge()
    username = db.get_setting(conn, "github_username")
    results: list[Result] = []
    project_ids = [r[0] for r in conn.execute("SELECT id FROM projects ORDER BY id")]
    for pid in project_ids:
        p = project_inputs(conn, pid, rubric["market"]["min_language_share"])
        name = judge.name
        try:
            j = judge.judge(p)
        except (JudgmentRejected, LLMError) as e:
            j = fallback.judge(p)
            name = f"{fallback.name} (fallback: {str(e)[:80]})"
        r = score_project(p, j, rubric, now, username, name, demand, role)
        results.append(r)
        if on_project:
            on_project(r)
    overrides = get_overrides(conn)
    kept = {int(r.item_id[1:]) for r in results if effective(r, overrides.get(r.item_id))[0] != "drop"}

    langs = {r["id"]: json.loads(r["languages"] or "{}") for r in conn.execute("SELECT id, languages FROM projects")}
    by_skill: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    for r in conn.execute("""SELECT so.skill, so.project_id, so.evidence_id, so.kind, e.created_at
                             FROM skills_observed so JOIN evidence e ON e.id = so.evidence_id"""):
        k = skill_key(canonical(r["skill"]))  # canonical again: rows stored before an alias was added
        names.setdefault(k, canonical(r["skill"]))
        by_skill.setdefault(k, []).append(dict(r))
    for k, rows in by_skill.items():
        results.append(score_skill(names[k], rows, kept, langs, rubric, now, demand, role))
    claims: dict[str, list[str]] = {}
    for (rej,) in conn.execute("SELECT rejected FROM project_facts"):
        for c in json.loads(rej or "[]"):
            if c.get("type") in ("technology", "skill") and c.get("name"):
                claims.setdefault(skill_key(canonical(c["name"])), []).append(canonical(c["name"]))
    for k, ns in claims.items():
        if k not in by_skill:
            results.append(unsupported_skill(ns[0], len(ns), rubric, demand))
    db.set_setting(conn, "evaluated_role", role)
    _store(conn, results, rubric["hash"], now)
    return results


def _store(conn, results: list[Result], rubric_hash: str, now: datetime) -> None:
    conn.execute("DELETE FROM evaluations")  # overrides live in their own table and are untouched
    conn.executemany(
        "INSERT INTO evaluations VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(r.item_id, r.item_type, r.name, r.score, r.decision, json.dumps(r.breakdown), r.reason, r.judge,
          rubric_hash, now.isoformat(timespec="seconds")) for r in results])
    conn.commit()


# ---------- overrides ----------

class OverrideError(ValueError):
    pass


def get_overrides(conn) -> dict[str, dict]:
    return {r["item_id"]: dict(r) for r in conn.execute("SELECT * FROM overrides")}


def has_evidence(conn, item_id: str) -> bool:
    if item_id.startswith("p"):
        return conn.execute("SELECT 1 FROM evidence WHERE project_id=? LIMIT 1", (int(item_id[1:]),)).fetchone() is not None
    k = item_id[2:]
    return any(skill_key(canonical(s)) == k for (s,) in conn.execute("SELECT DISTINCT skill FROM skills_observed"))


def resolve_id(conn, ref: str) -> str:
    """Accept p12, s:react, or an exact project or skill name."""
    ref = ref.strip()
    if conn.execute("SELECT 1 FROM evaluations WHERE item_id=?", (ref,)).fetchone():
        return ref
    rows = conn.execute("SELECT item_id FROM evaluations WHERE lower(name)=lower(?)", (ref,)).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    k = "s:" + skill_key(canonical(ref))
    if conn.execute("SELECT 1 FROM evaluations WHERE item_id=?", (k,)).fetchone():
        return k
    raise OverrideError(f"no evaluated item {ref!r}; run `resume evaluate` and use an id from its table")


def set_override(conn, ref: str, decision: str, note: str | None = None) -> str:
    if decision not in ("keep", "drop"):
        raise OverrideError("decision must be keep or drop")
    item_id = resolve_id(conn, ref)
    if decision == "keep" and not has_evidence(conn, item_id):
        raise OverrideError(f"{item_id} has no evidence, so it cannot be kept (no evidence, no claim)")
    conn.execute("INSERT OR REPLACE INTO overrides VALUES (?,?,?,?)", (item_id, decision, note, db.now()))
    conn.commit()
    return item_id


def clear_override(conn, ref: str) -> str:
    item_id = resolve_id(conn, ref)
    conn.execute("DELETE FROM overrides WHERE item_id=?", (item_id,))
    conn.commit()
    return item_id


def effective(r: Result, override: dict | None) -> tuple[str, str | None]:
    """(decision, note). An override wins, except keep on an item without evidence."""
    if not override:
        return r.decision, None
    if override["decision"] == "keep" and r.decision == "needs_evidence":
        return r.decision, "override keep ignored: no evidence"
    return override["decision"], "override"
