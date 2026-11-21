"""Market demand from job postings you collect yourself (no scraping).

  read_inputs()      text file, folder, CSV, stdin or pasted text -> PostingIn list
  add_postings()     store under a role label (redacted, idempotent by content hash)
  extract_postings() required / preferred skills per posting via an Extractor
  demand()           per role: frequency across postings, required vs nice-to-have, demand score
  gap()              your evidenced skills vs the role's demand

Every extracted skill must quote words that appear in the posting; anything else is rejected,
the same way extraction rejects claims without evidence ids.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from . import db
from .llm import LLM, LLMError
from .redact import redact
from .skills import _LOOKUP, canonical, key as skill_key

PURPOSE = "extract_posting"
TEXT_SUFFIXES = {".txt", ".md", ".text"}
REQUIREMENTS = ("required", "preferred")


class MarketError(ValueError):
    pass


# ---------- input ----------

@dataclass
class PostingIn:
    description: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    source: str | None = None


def norm_role(role: str) -> str:
    r = " ".join((role or "").lower().split())
    if not r:
        raise MarketError("role label is empty")
    return r


def _text_posting(text: str, source: str) -> PostingIn | None:
    text = text.strip()
    if not text:
        return None
    first = next(l for l in text.splitlines() if l.strip())
    return PostingIn(description=text, title=first.strip().lstrip("#* ").strip()[:120], source=source)


def read_csv(text: str, source: str) -> tuple[list[PostingIn], list[str]]:
    reader = csv.DictReader(io.StringIO(text))
    cols = {(c or "").strip().lower(): c for c in reader.fieldnames or []}
    if "description" not in cols:
        raise MarketError(f"{source}: CSV needs a description column (have: {', '.join(cols) or 'none'})")
    out, skipped = [], []
    for i, row in enumerate(reader, start=2):  # header is line 1
        get = lambda c: (row.get(cols[c]) or "").strip() if c in cols else ""
        if not get("description"):
            skipped.append(f"{source}:{i} has no description")
            continue
        out.append(PostingIn(description=get("description"), title=get("title") or None, company=get("company") or None,
                             location=get("location") or None, source=f"{source}:{i}"))
    return out, skipped


def read_inputs(arg: str, stdin_text: str | None = None) -> tuple[list[PostingIn], list[str]]:
    """Path to a .txt/.md/.csv file or a folder of them, '-' for stdin, or the posting text itself."""
    if arg == "-":
        p = _text_posting(stdin_text or "", "paste")
        return ([p] if p else []), ([] if p else ["stdin was empty"])
    path = Path(arg).expanduser()
    try:
        exists = path.exists()
    except OSError:  # name too long: it is pasted text
        exists = False
    if not exists:
        if "\n" in arg or len(arg) >= 200:
            p = _text_posting(arg, "paste")
            return ([p] if p else []), []
        raise MarketError(f"{arg}: no such file or folder (pass '-' to paste on stdin)")
    files = sorted(f for f in path.iterdir() if f.is_file() and not f.name.startswith(".")) if path.is_dir() else [path]
    out, skipped = [], []
    for f in files:
        suffix = f.suffix.lower()
        if suffix == ".csv":
            rows, skip = read_csv(f.read_text(encoding="utf-8-sig", errors="replace"), str(f))
            out += rows
            skipped += skip
        elif suffix in TEXT_SUFFIXES or not path.is_dir():
            p = _text_posting(f.read_text(errors="replace"), str(f))
            if p:
                out.append(p)
            else:
                skipped.append(f"{f} is empty")
        else:
            skipped.append(f"{f} skipped (not .txt, .md or .csv)")
    return out, skipped


def content_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()


def add_postings(conn, role: str, postings: list[PostingIn]) -> tuple[list[int], int]:
    """Returns (ids of newly added postings, number already present)."""
    role = norm_role(role)
    added, dupes = [], 0
    for p in postings:
        desc = redact(p.description)
        cur = conn.execute(
            """INSERT OR IGNORE INTO postings (role, title, company, location, description, source, content_hash, added_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (role, redact(p.title) if p.title else None, redact(p.company) if p.company else None,
             redact(p.location) if p.location else None, desc, p.source, content_hash(p.description), db.now()))
        if cur.rowcount:
            added.append(cur.lastrowid)
        else:
            dupes += 1
    conn.commit()
    return added, dupes


def roles(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT role, COUNT(*) postings, SUM(extracted_at IS NOT NULL) extracted,
                  GROUP_CONCAT(DISTINCT extractor) extractors
           FROM postings GROUP BY role ORDER BY role""")]


def resolve_role(conn, role: str | None) -> str | None:
    """Explicit role, else the only role on file, else None. Raises if the role is unknown or ambiguous."""
    known = [r["role"] for r in roles(conn)]
    if role:
        r = norm_role(role)
        if r not in known:
            raise MarketError(f"no postings for role {r!r}" + (f"; known roles: {', '.join(known)}" if known else
                                                                "; add some with `resume market add`"))
        return r
    if len(known) == 1:
        return known[0]
    if len(known) > 1:
        raise MarketError(f"several roles on file ({', '.join(known)}): pass --role")
    return None


# ---------- extraction ----------

class PostingSkill(BaseModel):
    name: str = Field(description="Skill or technology, short canonical name, e.g. 'React', 'PostgreSQL', 'REST APIs'")
    requirement: Literal["required", "preferred"] = Field(
        description="required: listed as required, must-have or minimum, or central to the job; "
                    "preferred: nice to have, bonus, plus, preferred")
    quote: str = Field(description="The exact words from the posting where it appears, at most 12 words")


class PostingExtraction(BaseModel):
    skills: list[PostingSkill]


class Extractor(Protocol):
    name: str

    def extract(self, text: str) -> PostingExtraction: ...


class LLMExtractor:
    name = "llm"
    SYSTEM = """You extract the technical skills a job posting asks for.
Return each programming language, framework, library, database, platform, tool, or technical practice
(e.g. 'REST APIs', 'CI/CD', 'RAG', 'System design'). Skip soft skills, degrees, years of experience and perks.
requirement: "required" if it is listed as required, must-have or minimum, or the job is clearly built on it;
"preferred" if it is nice to have, a bonus, a plus, or preferred.
quote: copy the exact words from the posting where the skill appears (at most 12 words). Do not paraphrase."""

    def __init__(self, llm: LLM):
        self.llm = llm

    def extract(self, text: str) -> PostingExtraction:
        return self.llm.complete_json(self.SYSTEM, f"Job posting:\n\n{text}", PostingExtraction,
                                      purpose=PURPOSE, max_tokens=4000)


# Single words that are skills only when written this way (otherwise ordinary English or too short).
CASE_SENSITIVE = {"go": "Go", "rest": "REST", "react": "React", "express": "Express", "node": "Node", "spark": "Spark",
                  "swift": "Swift", "rust": "Rust", "excel": "Excel", "snowflake": "Snowflake", "rag": "RAG",
                  "ml": "ML", "chroma": "Chroma", "elt": "ELT"}
NEVER_SCAN = {"next", "nest", "cv", "dl", "sh", "pg", "hf", "py", "ts", "js", "auth", "experimentation", "unix",
              "embedding", "agents", "prompting"}
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+#/\-]*[A-Za-z0-9+#]|[A-Za-z0-9]")
_PREFERRED_HEAD = re.compile(r"\b(nice[\s-]to[\s-]haves?|preferred|bonus|pluses|plus points|good[\s-]to[\s-]have|"
                             r"desirable|extra credit|ideally)\b", re.I)
_REQUIRED_HEAD = re.compile(r"\b(requirements?|required|must[\s-]haves?|qualifications|what you('ll)? (need|bring)|"
                            r"you have|who you are|skills|tech stack|our stack)\b", re.I)
_PREFERRED_LINE = re.compile(r"\b(preferred|a plus|is a plus|nice to have|bonus|ideally|desirable)\b", re.I)


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 60 or re.match(r"([-*•·]|\d+[.)])\s", s):  # bullets are list items, not headings
        return False
    return (s.endswith(":") or s.startswith("#") or (s.startswith("**") and s.endswith("**"))
            or (s.isupper() and " " in s))  # e.g. "NICE TO HAVE"


def scan(line: str) -> list[tuple[str, str]]:
    """(canonical skill, matched text) for alias-table skills mentioned in a line, longest match first."""
    toks = list(_TOKEN.finditer(line))
    out, i = [], 0
    while i < len(toks):
        for n in (3, 2, 1):
            if i + n > len(toks):
                continue
            words = toks[i:i + n]
            gap = line[words[0].start():words[-1].end()]
            if n > 1 and not re.fullmatch(r"[\w.+#/\-]+(?:[ \t]+[\w.+#/\-]+)*", gap):
                continue
            k = skill_key(gap)
            if k not in _LOOKUP or (n == 1 and k in NEVER_SCAN):
                continue
            if n == 1 and k in CASE_SENSITIVE:
                if gap not in (CASE_SENSITIVE[k], CASE_SENSITIVE[k].upper()):
                    continue
                before, after = line[:words[0].start()], line[words[0].end():]
                if re.search(r"(^|[.!?])\s*$", before) and re.match(r"\s+[a-z]", after):
                    continue  # "Express interest ...", "Go to ...": a sentence starting with the English word
            out.append((_LOOKUP[k], gap))
            i += n
            break
        else:
            i += 1
    return out


class KeywordExtractor:
    """Offline extractor: alias-table matches, required vs preferred from section headings and line cues.

    Mentions under a 'nice to have' / 'preferred' heading or on a line that says 'a plus' are preferred.
    If the posting has a requirements heading, mentions outside it (intro, responsibilities) count as
    preferred; with no headings at all, every mention counts as required."""
    name = "keywords"

    def extract(self, text: str) -> PostingExtraction:
        lines = text.splitlines()
        has_required_head = any(_is_heading(l) and _REQUIRED_HEAD.search(l) and not _PREFERRED_HEAD.search(l)
                                for l in lines)
        section = None
        found: dict[str, PostingSkill] = {}
        for line in lines:
            if _is_heading(line):
                section = ("preferred" if _PREFERRED_HEAD.search(line) else
                           "required" if _REQUIRED_HEAD.search(line) else "other")
            if section == "preferred" or _PREFERRED_LINE.search(line):
                req = "preferred"
            elif section == "required" or not has_required_head:
                req = "required"
            else:
                req = "preferred"
            for name, quote in scan(line):
                k = skill_key(name)
                if k not in found or (req == "required" and found[k].requirement == "preferred"):
                    found[k] = PostingSkill(name=name, requirement=req, quote=quote)
        return PostingExtraction(skills=list(found.values()))


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def validate(x: PostingExtraction, text: str) -> tuple[list[dict], list[dict]]:
    """Keep skills whose quote appears in the posting. Canonical names, one row per skill, required wins."""
    hay = _norm(text)
    clean: dict[str, dict] = {}
    rejected = []
    for s in x.skills:
        name = canonical(s.name)
        k = skill_key(name)
        quote = " ".join(s.quote.split())[:200]
        if not k or not quote or _norm(quote) not in hay:
            rejected.append({"name": s.name, "requirement": s.requirement, "quote": quote,
                             "why": "quote not found in posting" if k else "empty name"})
            continue
        if k not in clean or (s.requirement == "required" and clean[k]["requirement"] == "preferred"):
            clean[k] = {"skill": name, "skill_key": k, "requirement": s.requirement, "quote": quote}
    return list(clean.values()), rejected


def extract_postings(conn, extractor: Extractor, role: str | None = None, *, force: bool = False,
                     fallback: Extractor | None = None, on_posting=None) -> dict:
    fallback = fallback or KeywordExtractor()
    q = "SELECT id, role, title, description FROM postings WHERE 1=1"
    args: list = []
    if role:
        q += " AND role=?"
        args.append(norm_role(role))
    if not force:
        q += " AND extracted_at IS NULL"
    stats = {"postings": 0, "skills": 0, "rejected": 0, "fallback": 0}
    for p in conn.execute(q + " ORDER BY id", args).fetchall():
        used = extractor.name
        try:
            x = extractor.extract(p["description"])
        except LLMError as e:
            x = fallback.extract(p["description"])
            used = f"{fallback.name} (fallback: {str(e)[:80]})"
            stats["fallback"] += 1
        clean, rejected = validate(x, p["description"])
        with conn:
            conn.execute("DELETE FROM posting_skills WHERE posting_id=?", (p["id"],))
            conn.executemany("INSERT INTO posting_skills VALUES (?,?,?,?,?)",
                             [(p["id"], c["skill"], c["skill_key"], c["requirement"], c["quote"]) for c in clean])
            conn.execute("UPDATE postings SET extractor=?, rejected=?, extracted_at=? WHERE id=?",
                         (used, json.dumps(rejected), db.now(), p["id"]))
        stats["postings"] += 1
        stats["skills"] += len(clean)
        stats["rejected"] += len(rejected)
        if on_posting:
            on_posting(p, clean, rejected, used)
    return stats


# ---------- demand ----------

@dataclass
class Demand:
    skill: str
    key: str
    postings: int          # postings that mention it
    total: int             # postings for the role
    required: int
    preferred: int
    score: float           # (required * w_req + preferred * w_pref) / total, in [0, 1]

    @property
    def frequency(self) -> float:
        return self.postings / self.total if self.total else 0.0

    @property
    def flag(self) -> str:
        return "required" if self.required >= self.preferred else "nice-to-have"

    def as_dict(self) -> dict:
        return {"skill": self.skill, "key": self.key, "postings": self.postings, "total": self.total,
                "frequency": round(self.frequency, 3), "required": self.required, "preferred": self.preferred,
                "flag": self.flag, "score": round(self.score, 3)}


DEFAULT_WEIGHTS = {"required_weight": 1.0, "preferred_weight": 0.5}


def demand(conn, role: str, cfg: dict | None = None) -> dict[str, Demand]:
    """Demand per skill key for a role. Postings not yet extracted count in the total."""
    cfg = {**DEFAULT_WEIGHTS, **(cfg or {})}
    role = norm_role(role)
    total = conn.execute("SELECT COUNT(*) FROM postings WHERE role=?", (role,)).fetchone()[0]
    out: dict[str, Demand] = {}
    names: dict[str, dict[str, int]] = {}
    for r in conn.execute("""SELECT ps.skill, ps.skill_key, ps.requirement FROM posting_skills ps
                             JOIN postings p ON p.id = ps.posting_id WHERE p.role=?""", (role,)):
        d = out.setdefault(r["skill_key"], Demand(r["skill"], r["skill_key"], 0, total, 0, 0, 0.0))
        d.postings += 1
        d.required += r["requirement"] == "required"
        d.preferred += r["requirement"] == "preferred"
        names.setdefault(r["skill_key"], {}).setdefault(r["skill"], 0)
        names[r["skill_key"]][r["skill"]] += 1
    for k, d in out.items():
        d.skill = max(names[k].items(), key=lambda kv: (kv[1], kv[0]))[0]
        d.score = min(1.0, (d.required * cfg["required_weight"] + d.preferred * cfg["preferred_weight"]) / total)
    return dict(sorted(out.items(), key=lambda kv: (-kv[1].score, -kv[1].postings, kv[1].skill.lower())))


# ---------- gap ----------

def my_skills(conn) -> dict[str, dict]:
    """Skills with evidence in your history: key -> {skill, projects, evidence, kinds}."""
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT skill, project_id, evidence_id, kind FROM skills_observed"):
        name = canonical(r["skill"])  # rows stored before an alias was added
        k = skill_key(name)
        g = out.setdefault(k, {"skill": name, "projects": set(), "evidence": set(), "kinds": set()})
        g["projects"].add(r["project_id"])
        g["evidence"].add(r["evidence_id"])
        g["kinds"].add(r["kind"])
    return out


def gap(conn, role: str, cfg: dict | None = None) -> dict:
    role = norm_role(role)
    dem = demand(conn, role, cfg)
    mine = my_skills(conn)
    evals = {r["item_id"]: dict(r) for r in conn.execute(
        "SELECT item_id, decision FROM evaluations WHERE item_type='skill'")}
    overrides = {r["item_id"]: r["decision"] for r in conn.execute("SELECT item_id, decision FROM overrides")}
    claimed = set()
    for (rej,) in conn.execute("SELECT rejected FROM project_facts"):
        claimed |= {skill_key(canonical(c["name"])) for c in json.loads(rej or "[]") if c.get("name")}

    def evaluation(k):
        iid = f"s:{k}"
        return overrides.get(iid) or (evals.get(iid) or {}).get("decision")

    have, missing, unused = [], [], []
    for k, d in dem.items():
        if k in mine:
            m = mine[k]
            have.append({**d.as_dict(), "projects": len(m["projects"]), "evidence": len(m["evidence"]),
                         "evaluation": evaluation(k)})
        else:
            missing.append({**d.as_dict(), "note": "claimed in extraction but no valid evidence" if k in claimed else ""})
    for k, m in sorted(mine.items(), key=lambda kv: (-len(kv[1]["projects"]), kv[1]["skill"].lower())):
        if k not in dem:
            unused.append({"skill": m["skill"], "key": k, "projects": len(m["projects"]), "evidence": len(m["evidence"]),
                           "kinds": sorted(m["kinds"]), "evaluation": evaluation(k)})
    info = next((r for r in roles(conn) if r["role"] == role), {"postings": 0, "extracted": 0, "extractors": None})
    total_demand = sum(d.score for d in dem.values())
    covered = sum(d.score for k, d in dem.items() if k in mine)
    return {"role": role, "postings": info["postings"], "extracted": info["extracted"], "extractors": info["extractors"],
            "coverage": round(covered / total_demand, 3) if total_demand else 0.0,
            "have": have, "missing": missing, "unused": unused}
