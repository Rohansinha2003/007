"""Resume generation: select -> write bullets -> claim check. Rendering lives in render.py.

  jd_demand()    skills the job description asks for (same extractors and validation as market postings)
  select()       projects and skills from evaluator decisions (overrides win) plus fit with the JD
  Writer         writes bullets with evidence ids: LLMWriter, or TemplateWriter offline
  Checker        verifies each bullet against only its cited evidence: LLMChecker, LexicalChecker offline
  build()        runs the pipeline and returns a Resume

No evidence, no claim: a bullet without a valid evidence id of its own project never reaches the resume,
and a bullet the checker cannot support is removed (or flagged when only a detail is unsupported).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from . import db, evaluator as ev, extract as ex, market as mk
from .llm import LLM, LLMError
from .skills import canonical, key as skill_key

STATUSES = ("verified", "flagged", "removed")


# ---------- data ----------

@dataclass
class BulletOut:
    text: str
    evidence_ids: list[int]
    status: str = "verified"     # verified | flagged | removed
    reason: str = ""             # checker's reason
    checker: str = ""


@dataclass
class ProjectOut:
    project_id: int
    name: str
    url: str | None
    deployed_url: str | None
    stack: list[str]
    stack_evidence: list[int]
    dates: str
    decision: str
    score: float
    fit: float
    jd_matches: list[str]
    bullets: list[BulletOut] = field(default_factory=list)
    writer: str = ""

    @property
    def kept(self) -> list[BulletOut]:
        return [b for b in self.bullets if b.status != "removed"]


@dataclass
class SkillOut:
    name: str
    kind: str                    # language | technology | skill
    evidence_ids: list[int]
    in_jd: bool
    decision: str


@dataclass
class Resume:
    header: dict
    skills: list[SkillOut]
    projects: list[ProjectOut]
    jd_title: str
    jd_skills: list[dict]        # {skill, requirement, have}
    meta: dict

    def removed(self) -> list[tuple[str, BulletOut]]:
        return [(p.name, b) for p in self.projects for b in p.bullets if b.status != "verified"]


# ---------- job description ----------

def jd_demand(text: str, extractor: mk.Extractor, fallback: mk.Extractor | None = None) -> tuple[dict, str]:
    """{skill key: Demand} for one posting (required 1.0, preferred 0.5) and the extractor used."""
    used = extractor.name
    try:
        x = extractor.extract(text)
    except LLMError as e:
        x = (fallback or mk.KeywordExtractor()).extract(text)
        used = f"keywords (fallback: {str(e)[:60]})"
    clean, _ = mk.validate(x, text)
    out = {}
    for c in clean:
        req = c["requirement"] == "required"
        out[c["skill_key"]] = mk.Demand(c["skill"], c["skill_key"], 1, 1, int(req), int(not req), 1.0 if req else 0.5)
    return out, used


def jd_title(text: str) -> str:
    first = next((l.strip().lstrip("#* ").strip() for l in text.splitlines() if l.strip()), "")
    return first[:80]


# ---------- selection ----------

def _month(ts: str | None) -> str:
    t = ev.parse_ts(ts)
    return t.strftime("%b %Y") if t else ""


def _dates(p) -> str:
    a, b = _month(p["first_commit_at"]), _month(p["last_commit_at"])
    return a if a == b else f"{a} - {b}" if a and b else a or b


def _effective(conn) -> list[dict]:
    overrides = ev.get_overrides(conn)
    out = []
    for r in conn.execute("SELECT * FROM evaluations"):
        res = ev.Result(r["item_id"], r["item_type"], r["name"], r["score"], r["decision"], {}, r["reason"])
        d, _ = ev.effective(res, overrides.get(r["item_id"]))
        out.append({**dict(r), "effective": d})
    return out


def select(conn, jd: dict, rubric: dict, n_projects: int = 4, n_skills: int = 16, fit_weight: float = 0.4,
           keep_bonus: float = 0.1) -> tuple[list[ProjectOut], list[SkillOut]]:
    """Rank kept and rewrite-worthy projects by (1 - fit_weight) * evaluator score + fit_weight * JD fit,
    with a bonus for keep. Dropped items never appear. Skills: kept or rewrite skills with evidence, JD matches first."""
    rows = _effective(conn)
    top_k = rubric["market"]["project_top_k"]
    ranked = []
    for r in rows:
        if r["item_type"] != "project" or r["effective"] not in ("keep", "rewrite"):
            continue
        pid = int(r["item_id"][1:])
        pin = ev.project_inputs(conn, pid, rubric["market"]["min_language_share"])
        fit, matches = ev.project_demand(pin.skills, jd, top_k) if jd else (0.0, [])
        rank = (1 - fit_weight) * r["score"] + fit_weight * fit + (keep_bonus if r["effective"] == "keep" else 0)
        ranked.append((rank, pid, r, fit, matches, pin))
    ranked.sort(key=lambda x: -x[0])
    projects = []
    for rank, pid, r, fit, matches, pin in ranked[:n_projects]:
        p = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        stack, stack_ev = _stack(conn, pid, pin, jd)
        projects.append(ProjectOut(pid, p["name"], p["url"], p["deployed_url"], stack, stack_ev, _dates(p),
                                   r["effective"], r["score"], round(fit, 3), matches))

    evidence_by_skill: dict[str, list[int]] = {}
    kinds: dict[str, str] = {}
    github_languages = {skill_key(l) for (langs,) in conn.execute("SELECT languages FROM projects")
                        for l in json.loads(langs or "{}")}
    for s in conn.execute("SELECT skill, evidence_id, kind FROM skills_observed ORDER BY evidence_id"):
        name = canonical(s["skill"])
        k = skill_key(name)
        evidence_by_skill.setdefault(k, []).append(s["evidence_id"])
        # a GitHub "language" the alias table maps to a tool (Dockerfile -> Docker) is listed as a technology
        kind = "technology" if s["kind"] == "language" and k not in github_languages else s["kind"]
        if kinds.get(k) != "language":
            kinds[k] = kind
    skills = []
    for r in rows:
        if r["item_type"] != "skill" or r["effective"] not in ("keep", "rewrite"):
            continue
        k = r["item_id"][2:]
        if k not in evidence_by_skill:
            continue  # no evidence, no claim
        if r["effective"] == "rewrite" and k not in jd:
            continue  # borderline skills only when the job asks for them
        skills.append(SkillOut(r["name"], kinds[k], sorted(set(evidence_by_skill[k]))[:8], k in jd, r["effective"]))
    score = {r["item_id"]: r["score"] for r in rows}
    skills.sort(key=lambda s: (not s.in_jd, s.decision != "keep", -score[f"s:{skill_key(s.name)}"]))
    return projects, skills[:n_skills]


def _stack(conn, pid: int, pin: ev.ProjectInput, jd: dict) -> tuple[list[str], list[int]]:
    """Up to 5 technologies for the project line, JD matches first, with the evidence that shows each."""
    ev_ids: dict[str, list[int]] = {}
    names: dict[str, str] = {}
    for r in conn.execute("SELECT skill, evidence_id, kind FROM skills_observed WHERE project_id=? AND kind != 'skill'",
                          (pid,)):
        k = skill_key(canonical(r["skill"]))
        if k not in pin.skills:
            continue  # trace languages
        names[k] = canonical(r["skill"])
        ev_ids.setdefault(k, []).append(r["evidence_id"])
    share = {skill_key(canonical(l)): b for l, b in pin.languages.items()}
    order = sorted(names, key=lambda k: (k not in jd, -share.get(k, 0), names[k].lower()))[:5]
    return [names[k] for k in order], sorted({i for k in order for i in ev_ids[k]})


# ---------- writers ----------

class Bullet(BaseModel):
    text: str = Field(description="One resume bullet: action verb + what was built + how + result. At most 28 words.")
    evidence_ids: list[int] = Field(description="Ids of the [E#] evidence items that support every part of the bullet")


class ProjectBullets(BaseModel):
    bullets: list[Bullet]


class Writer(Protocol):
    name: str

    def write(self, conn, project: ProjectOut, jd_skills: list[str]) -> list[Bullet]: ...


class LLMWriter:
    name = "llm"
    SYSTEM = """You write resume bullets for one software project, using only the evidence given.
- Write 2 to 4 bullets. Each starts with a past-tense action verb and follows the form
  action + what was built + how (technology) + result. The result must be stated in the evidence:
  shipped, deployed, adopted, faster, automated, or the concrete capability delivered.
- At most 28 words each. No first person. Never invent numbers, users, metrics or outcomes.
  If the evidence has no metric, describe the delivered capability instead.
- Prefer facts relevant to the target job skills listed, but never claim a skill the evidence does not show.
- Cite in evidence_ids every [E#] item that supports the bullet. A bullet without valid ids is discarded."""

    def __init__(self, llm: LLM):
        self.llm = llm

    def write(self, conn, project: ProjectOut, jd_skills: list[str]) -> list[Bullet]:
        pack = ex.build_pack(conn, project.project_id)
        f = conn.execute("SELECT summary, outcome FROM project_facts WHERE project_id=?", (project.project_id,)).fetchone()
        user = (pack.prompt + (f"\n\nExtracted summary: {f['summary']} (outcome: {f['outcome']})" if f else "")
                + "\n\nTarget job skills: " + (", ".join(jd_skills) or "(none given)"))
        return self.llm.complete_json(self.SYSTEM, user, ProjectBullets, purpose="write_bullets", max_tokens=4000).bullets


_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


_BOILERPLATE = re.compile(r"this template provides|bootstrapped with|getting started with create|"
                          r"this project was generated with|^#\s*(react|vue|next\.?js)\s*(\+|$)", re.I | re.M)


def first_sentence(readme: str) -> str | None:
    """First prose sentence of a README, as a noun phrase where possible.
    Skips headings, quotes, badges, HTML, code, tables and lists, and framework starter READMEs."""
    if _BOILERPLATE.search(readme[:600]):
        return None
    in_code = False
    for line in readme.splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not s or s[0] in "#!<|>[" or s.startswith(("---", "===", "- [", "* [")):
            continue
        s = _MD_LINK.sub(r"\1", s).replace("**", "").replace("`", "").lstrip("-* ").strip()
        if len(s) < 30:
            continue
        sentence = re.split(r"(?<=[.!?])\s", s)[0].rstrip(".!? ")
        if len(sentence) > 160:  # long: keep the first clause
            sentence = re.split(r"[:;,]\s|\s[-\u2014]\s", sentence)[0]
        sentence = re.sub(r"^[\w.\-]+(?:\s[\w.\-]+)?\s(?:is|are)\s(?=(?:a|an|the)\s)", "", sentence)
        if 25 <= len(sentence) <= 160:
            return sentence
    return None


def join_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def _clause(text: str) -> str:
    text = text.strip().rstrip(".")
    if re.match(r"(A|An|The)\s", text):
        text = text[0].lower() + text[1:]
    return text


class TemplateWriter:
    """Offline writer: fills fixed action + result sentences with facts read straight from the evidence.
    Plain, but every word comes from a cited row."""
    name = "template"

    def write(self, conn, project: ProjectOut, jd_skills: list[str]) -> list[Bullet]:
        rows = {r["kind"]: r for r in conn.execute(
            "SELECT id, kind, snippet, url_or_path FROM evidence WHERE project_id=? AND session_id IS NULL ORDER BY id",
            (project.project_id,))}
        p = conn.execute("SELECT * FROM projects WHERE id=?", (project.project_id,)).fetchone()
        f = conn.execute("SELECT summary, facts FROM project_facts WHERE project_id=?", (project.project_id,)).fetchone()
        stack = join_names(project.stack[:3])
        out = []
        what, what_ids = None, []
        if p["description"] and "repo" in rows:
            what, what_ids = _clause(p["description"]), [rows["repo"]["id"]]
        elif "readme" in rows and (s := first_sentence(rows["readme"]["snippet"])):
            what, what_ids = _clause(s), [rows["readme"]["id"]]
        if what:
            sep = ", " if re.match(r"(a|an|the)\s", what) else ": "
            text = f"Built {project.name}{sep}{what}" + (f", using {stack}" if stack else "") + "."
            out.append(Bullet(text=text, evidence_ids=what_ids + project.stack_evidence))
        elif stack:
            out.append(Bullet(text=f"Built {project.name} in {stack}.", evidence_ids=project.stack_evidence))
        if f:
            facts = json.loads(f["facts"])
            claims = facts.get("skills", []) + facts.get("technologies", [])
            jd_keys = {skill_key(canonical(s)) for s in jd_skills}
            picked = sorted(claims, key=lambda c: skill_key(canonical(c["name"])) not in jd_keys)[:3]
            if picked:
                names = [canonical(c["name"]) for c in picked]
                out.append(Bullet(text=f"Implemented {join_names(names)} in {project.name}.",
                                  evidence_ids=sorted({i for c in picked for i in c["evidence_ids"]})))
        if "deployment" in rows and project.deployed_url:
            out.append(Bullet(text=f"Shipped a live deployment of {project.name} at {project.deployed_url}.",
                              evidence_ids=[rows["deployment"]["id"]]))
        if "commits" in rows and (p["commit_count"] or 0) >= 5:
            span = f" from {_month(p['first_commit_at'])} to {_month(p['last_commit_at'])}" if "-" in project.dates else ""
            out.append(Bullet(text=f"Developed and iterated on {project.name} across {p['commit_count']} commits{span}.",
                              evidence_ids=[rows["commits"]["id"]]))
        return out[:4]


# ---------- checkers ----------

class Verdict(BaseModel):
    bullet: int = Field(description="Bullet number as given")
    verdict: Literal["supported", "partial", "unsupported"]
    reason: str = Field(description="One sentence; name any claim the cited evidence does not support")


class CheckResult(BaseModel):
    verdicts: list[Verdict]


class Checker(Protocol):
    name: str

    def check(self, conn, project: ProjectOut, bullets: list[BulletOut]) -> list[Verdict]: ...


def _evidence_text(conn, ids: list[int], limit: int = 3000) -> dict[int, str]:
    if not ids:
        return {}
    q = f"SELECT id, kind, url_or_path, snippet FROM evidence WHERE id IN ({','.join('?' * len(ids))})"
    return {r["id"]: f"{r['kind']} {r['url_or_path']}\n{r['snippet'][:limit]}" for r in conn.execute(q, ids)}


class LLMChecker:
    name = "llm"
    SYSTEM = """You verify resume bullets against evidence. For each bullet, use ONLY the evidence items cited for it.
supported: every factual claim (what was built, technologies, numbers, outcomes, scale) is stated in or directly
implied by the cited evidence.
partial: the main claim is supported but a detail (a number, technology or outcome) is not.
unsupported: the main claim is not supported by the cited evidence.
Give a one-sentence reason that names any unsupported detail. Be strict: plausible is not supported."""

    def __init__(self, llm: LLM):
        self.llm = llm

    def check(self, conn, project: ProjectOut, bullets: list[BulletOut]) -> list[Verdict]:
        texts = _evidence_text(conn, sorted({i for b in bullets for i in b.evidence_ids}))
        parts = [f"Project: {project.name}"]
        for n, b in enumerate(bullets, 1):
            parts.append(f"\nBullet {n}: {b.text}\nCited evidence:")
            parts += [f"[E{i}] {texts[i]}" for i in b.evidence_ids if i in texts]
        return self.llm.complete_json(self.SYSTEM, "\n".join(parts), CheckResult, purpose="check_bullets",
                                      max_tokens=4000).verdicts


_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?:%|x|k|m)?(?![\w])", re.I)
_URL = re.compile(r"https?://\S+?(?=[,;)]?(?:\s|$))|\b[\w-]+(?:\.[\w-]+)*\.(?:app|dev|io|com|org|net|ai)\b(?:/\S*)?")


class LexicalChecker:
    """Offline checker: every number, URL and known technology in a bullet must appear in its cited evidence."""
    name = "lexical"

    def check(self, conn, project: ProjectOut, bullets: list[BulletOut]) -> list[Verdict]:
        out = []
        for n, b in enumerate(bullets, 1):
            texts = _evidence_text(conn, b.evidence_ids, limit=20000)
            hay = "\n".join(texts.values())
            low = hay.lower()
            observed = {skill_key(canonical(r["skill"])) for r in conn.execute(
                f"SELECT skill FROM skills_observed WHERE evidence_id IN ({','.join('?' * len(b.evidence_ids))})",
                b.evidence_ids)} if b.evidence_ids else set()
            observed |= {skill_key(s) for line in hay.splitlines() for s, _ in mk.scan(line)}
            text = b.text.replace(project.name, " ")
            missing = []
            for u in _URL.findall(text):
                u = u.rstrip(".,")
                if u.lower().removeprefix("https://").removeprefix("http://") not in low:
                    missing.append(u)
                text = text.replace(u, " ")
            for num in _NUMBER.findall(text):
                if num.lower() not in low:
                    missing.append(num)
            for s, quote in mk.scan(text):
                if skill_key(s) not in observed and quote.lower() not in low:
                    missing.append(s)
            if not b.evidence_ids or not texts:
                out.append(Verdict(bullet=n, verdict="unsupported", reason="cites no evidence"))
            elif missing:
                out.append(Verdict(bullet=n, verdict="unsupported",
                                   reason="not in the cited evidence: " + ", ".join(dict.fromkeys(missing))))
            else:
                out.append(Verdict(bullet=n, verdict="supported", reason="numbers, links and technologies match the cited evidence"))
        return out


# ---------- pipeline ----------

def _apply(bullets: list[BulletOut], verdicts: list[Verdict], name: str) -> None:
    by_n = {v.bullet: v for v in verdicts}
    for n, b in enumerate(bullets, 1):
        if b.status == "removed":
            continue
        v = by_n.get(n)
        if v is None:
            b.status, b.reason, b.checker = "removed", f"{name} checker returned no verdict", name
        elif v.verdict == "unsupported":
            b.status, b.reason, b.checker = "removed", v.reason, name
        elif v.verdict == "partial":
            b.status, b.reason, b.checker = "flagged", v.reason, name
        elif b.status == "verified":
            b.reason, b.checker = (b.reason + "; " if b.reason else "") + v.reason, (b.checker + "+" if b.checker else "") + name


def build(conn, jd_text: str, *, writer: Writer, checkers: list[Checker], extractor: mk.Extractor,
          rubric: dict, header: dict, n_projects: int = 4, n_skills: int = 16, max_bullets: int = 4,
          strict: bool = False, on_project=None) -> Resume:
    jd, jd_used = jd_demand(jd_text, extractor)
    projects, skills = select(conn, jd, rubric, n_projects, n_skills)
    jd_names = [d.skill for d in jd.values()]
    fallback = TemplateWriter()
    for p in projects:
        own = {r[0] for r in conn.execute("SELECT id FROM evidence WHERE project_id=?", (p.project_id,))}
        wname = writer.name
        try:
            raw = writer.write(conn, p, jd_names)
        except LLMError as e:
            raw = fallback.write(conn, p, jd_names)
            wname = f"template (fallback: {str(e)[:60]})"
        for b in raw:
            ids = [i for i in dict.fromkeys(b.evidence_ids) if i in own]
            text = " ".join(b.text.split())
            if not ids:
                p.bullets.append(BulletOut(text, [], "removed", "no valid evidence id for this project", "writer"))
            else:
                p.bullets.append(BulletOut(text, ids))
        live = [b for b in p.bullets if b.status != "removed"]
        for c in checkers:
            try:
                _apply(live, c.check(conn, p, live), c.name)
            except LLMError as e:
                for b in live:
                    if b.status == "verified":
                        b.status, b.reason, b.checker = "flagged", f"{c.name} check failed: {str(e)[:80]}", c.name
        if strict:
            for b in p.bullets:
                if b.status == "flagged":
                    b.status = "removed"
                    b.reason = "strict: " + b.reason
        kept = [b for b in p.bullets if b.status != "removed"]
        for b in kept[max_bullets:]:
            b.status, b.reason = "removed", f"over the {max_bullets}-bullet limit"
        p.writer = wname
        if on_project:
            on_project(p)
    projects = [p for p in projects if p.kept]
    mine = {skill_key(s.name) for s in skills} | set(mk.my_skills(conn))
    jd_skills = [{"skill": d.skill, "requirement": "required" if d.required else "preferred", "have": k in mine}
                 for k, d in jd.items()]
    meta = {"writer": writer.name, "checkers": [c.name for c in checkers], "jd_extractor": jd_used,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "evaluated_role": db.get_setting(conn, "evaluated_role")}
    return Resume(header, skills, projects, jd_title(jd_text), jd_skills, meta)


def header(conn, overrides: dict | None = None) -> dict:
    """Resume header from the GitHub profile plus anything you saved with `resume generate --name ...`."""
    gh = db.get_setting(conn, "github_profile") or {}
    saved = db.get_setting(conn, "profile") or {}
    h = {"name": gh.get("name") or gh.get("login") or db.get_setting(conn, "github_username") or "Your Name",
         "email": None, "phone": None, "location": gh.get("location"),
         "links": [l for l in (gh.get("html_url"), gh.get("blog")) if l]}
    h.update({k: v for k, v in saved.items() if v})
    h.update({k: v for k, v in (overrides or {}).items() if v})
    return h
