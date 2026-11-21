import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from resumeforge import cli, evaluator as ev, market as mk
from resumeforge.ingest import github
from resumeforge.llm import LLM, LLMError, Usage

from .conftest import USER

SAMPLE = Path(__file__).parent / "data" / "postings_ai_engineer.csv"
NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
RUBRIC = ev.load_rubric()


def add_text(conn, role, *texts):
    ids, _ = mk.add_postings(conn, role, [mk.PostingIn(description=t, title=t.split("\n")[0]) for t in texts])
    return ids


def skills_of(x: mk.PostingExtraction) -> dict[str, str]:
    return {s.name: s.requirement for s in x.skills}


# ---------- input ----------

def test_sample_csv_has_ten_postings():
    rows, skipped = mk.read_inputs(str(SAMPLE))
    assert len(rows) == 10 and not skipped
    assert rows[0].title == "AI Engineer" and rows[0].company == "Fictional Labs" and "Requirements:" in rows[0].description
    assert rows[0].source.endswith("postings_ai_engineer.csv:2")


def test_csv_header_tolerance_and_bad_rows():
    rows, skipped = mk.read_csv(" Title ,DESCRIPTION\nA,needs Python\nB,\n", "x.csv")
    assert [r.title for r in rows] == ["A"] and skipped == ["x.csv:3 has no description"]
    with pytest.raises(mk.MarketError, match="description column"):
        mk.read_csv("title,body\nA,b\n", "y.csv")


def test_folder_stdin_and_paste(tmp_path):
    (tmp_path / "a.txt").write_text("Backend Engineer\nRequirements:\n- Go\n")
    (tmp_path / "b.md").write_text("# Data Engineer\nSpark and Airflow")
    (tmp_path / "c.csv").write_text("title,description\nML,PyTorch\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")
    (tmp_path / ".hidden.txt").write_text("x")
    rows, skipped = mk.read_inputs(str(tmp_path))
    assert [r.title for r in rows] == ["Backend Engineer", "Data Engineer", "ML"]
    assert len(skipped) == 1 and "logo.png" in skipped[0]
    rows, _ = mk.read_inputs("-", "Frontend dev\nReact and TypeScript")
    assert rows[0].title == "Frontend dev" and rows[0].source == "paste"
    pasted = "Platform engineer\n" + "Kubernetes. " * 30
    assert mk.read_inputs(pasted)[0][0].description.startswith("Platform engineer")
    with pytest.raises(mk.MarketError, match="no such file"):
        mk.read_inputs("nope.csv")


def test_add_is_idempotent_and_redacted(conn):
    rows, _ = mk.read_inputs(str(SAMPLE))
    added, dupes = mk.add_postings(conn, "  AI   Engineer ", rows)
    assert len(added) == 10 and dupes == 0
    added, dupes = mk.add_postings(conn, "ai engineer", rows)
    assert added == [] and dupes == 10
    added, _ = mk.add_postings(conn, "ml engineer", rows[:2])  # same posting, other role: kept separately
    assert len(added) == 2
    add_text(conn, "ai engineer", "Recruiter\nEmail jane.doe@example.com, key sk-ant-api03-" + "x" * 40)
    desc = conn.execute("SELECT description FROM postings WHERE title='Recruiter'").fetchone()[0]
    assert "jane.doe@example.com" not in desc and "sk-ant" not in desc
    assert {r["role"]: r["postings"] for r in mk.roles(conn)} == {"ai engineer": 11, "ml engineer": 2}


# ---------- keyword extractor ----------

def test_keywords_sections_and_cues():
    text = """Senior Engineer
We use Docker everywhere.
Requirements:
- Python and PostgreSQL
- REST APIs, React Native
- Kubernetes experience is a plus
Nice to have:
- GitHub Actions
- Python scripting"""
    got = skills_of(mk.KeywordExtractor().extract(text))
    assert got["Python"] == "required"          # required wins over the later nice-to-have mention
    assert got["PostgreSQL"] == got["REST APIs"] == got["React Native"] == "required"
    assert got["Kubernetes"] == "preferred"     # line cue
    assert got["GitHub Actions"] == "preferred"  # nice-to-have section
    assert got["Docker"] == "preferred"         # intro, outside the requirements section
    assert "React" not in got                   # 'React Native' matched as one skill


def test_keywords_no_headings_means_required_and_ambiguous_words():
    text = ("You will go to market fast, react quickly, and rest well. Express interest via the form. "
            "Stack: Go, React, Node.js, OpenAI API, C++, CI/CD, scikit-learn.")
    got = skills_of(mk.KeywordExtractor().extract(text))
    assert set(got) == {"Go", "React", "Node.js", "LLM APIs", "C++", "CI/CD", "scikit-learn"}
    assert set(got.values()) == {"required"}


def test_bullets_are_not_headings():
    got = skills_of(mk.KeywordExtractor().extract("Requirements:\n- Python\n- AWS\n- SQL\nNice to have:\n- Go"))
    assert got == {"Python": "required", "AWS": "required", "SQL": "required", "Go": "preferred"}


# ---------- validation and LLM extractor ----------

POSTING = "Requirements:\n- ReactJS and Postgres\n- Python\nNice to have: Kubernetes"


def llm_reply(*skills):
    return json.dumps({"skills": [dict(zip(("name", "requirement", "quote"), s)) for s in skills]})


def test_validate_requires_quote_from_posting():
    x = mk.PostingExtraction.model_validate_json(llm_reply(
        ("ReactJS", "required", "ReactJS and Postgres"),
        ("Postgres", "preferred", "postgres"),          # case-insensitive match; required below wins
        ("PostgreSQL", "required", "Postgres"),
        ("Rust", "required", "5 years of Rust"),        # not in the posting -> rejected
        ("Kubernetes", "preferred", "Nice to have:   Kubernetes")))
    clean, rejected = mk.validate(x, POSTING)
    assert {c["skill"]: c["requirement"] for c in clean} == {"React": "required", "PostgreSQL": "required",
                                                             "Kubernetes": "preferred"}
    assert [r["name"] for r in rejected] == ["Rust"] and rejected[0]["why"] == "quote not found in posting"


def test_llm_extractor_stores_validated_skills(conn):
    seen = []

    def backend(system, user, schema, max_tokens, model):
        seen.append(user)
        return llm_reply(("React.js", "required", "ReactJS"), ("Go", "required", "Golang")), Usage(model, 300, 40)

    (pid,) = add_text(conn, "web", POSTING)
    stats = mk.extract_postings(conn, mk.LLMExtractor(LLM(conn, backend=backend)), "web")
    assert stats == {"postings": 1, "skills": 1, "rejected": 1, "fallback": 0}
    rows = conn.execute("SELECT skill, skill_key, requirement FROM posting_skills WHERE posting_id=?", (pid,)).fetchall()
    assert [tuple(r) for r in rows] == [("React", "react", "required")]
    p = conn.execute("SELECT extractor, rejected FROM postings WHERE id=?", (pid,)).fetchone()
    assert p["extractor"] == "llm" and json.loads(p["rejected"])[0]["name"] == "Go"
    assert "ReactJS and Postgres" in seen[0]
    assert mk.extract_postings(conn, mk.LLMExtractor(LLM(conn, backend=backend)), "web")["postings"] == 0  # done already


def test_llm_failure_falls_back_to_keywords(conn):
    def backend(*a):
        raise LLMError("API error 529: overloaded")

    add_text(conn, "web", POSTING)
    stats = mk.extract_postings(conn, mk.LLMExtractor(LLM(conn, backend=backend)), "web")
    assert stats["fallback"] == 1 and stats["skills"] == 4
    assert conn.execute("SELECT extractor FROM postings").fetchone()[0].startswith("keywords (fallback")


# ---------- demand ----------

@pytest.fixture
def sample(conn):
    mk.add_postings(conn, "ai engineer", mk.read_inputs(str(SAMPLE))[0])
    mk.extract_postings(conn, mk.KeywordExtractor(), "ai engineer")
    return conn


def test_demand_frequency_and_flags(sample):
    d = mk.demand(sample, "AI Engineer")
    py, docker, k8s = d["python"], d["docker"], d["kubernetes"]
    assert (py.postings, py.total, py.required, py.frequency, py.score, py.flag) == (10, 10, 10, 1.0, 1.0, "required")
    assert (docker.required, docker.preferred, docker.flag) == (3, 2, "required")
    assert docker.score == pytest.approx((3 + 0.5 * 2) / 10)
    assert (k8s.required, k8s.preferred, k8s.flag) == (1, 2, "nice-to-have")
    assert list(d)[0] == "python"  # sorted by demand
    w = mk.demand(sample, "ai engineer", {"preferred_weight": 0})
    assert w["kubernetes"].score == pytest.approx(0.1)


def test_unextracted_postings_count_in_total(sample):
    add_text(sample, "ai engineer", "Another posting\nPython")
    assert mk.demand(sample, "ai engineer")["python"].total == 11


def test_resolve_role(conn):
    assert mk.resolve_role(conn, None) is None
    add_text(conn, "AI engineer", "Python")
    assert mk.resolve_role(conn, None) == "ai engineer" == mk.resolve_role(conn, "AI Engineer")
    with pytest.raises(mk.MarketError, match="no postings for role"):
        mk.resolve_role(conn, "chef")
    add_text(conn, "web", "React")
    with pytest.raises(mk.MarketError, match="pass --role"):
        mk.resolve_role(conn, None)


# ---------- gap ----------

@pytest.fixture
def mine(conn, client):
    github.ingest(conn, client, USER)  # evidence for Python, Shell (alpha), TypeScript (beta), Go (gamma)
    conn.commit()
    return conn


def test_gap_partitions_skills(mine):
    add_text(mine, "backend", "Requirements:\n- Python\n- Go\n- Kubernetes\nNice to have:\n- TypeScript\n- Redis",
             "Requirements:\n- Python\n- PostgreSQL")
    mk.extract_postings(mine, mk.KeywordExtractor())
    g = mk.gap(mine, "backend")
    have = {x["skill"]: x for x in g["have"]}
    assert set(have) == {"Python", "Go", "TypeScript"}
    assert have["Python"]["postings"] == 2 and have["Python"]["projects"] == 1 and have["TypeScript"]["flag"] == "nice-to-have"
    assert {x["skill"] for x in g["missing"]} == {"Kubernetes", "PostgreSQL", "Redis"}
    assert {x["skill"] for x in g["unused"]} == {"Shell"}
    # demand: Python 1.0, Go/Kubernetes/PostgreSQL 0.5, TypeScript/Redis 0.25 (preferred in 1 of 2)
    assert g["coverage"] == pytest.approx((1 + 0.5 + 0.25) / (1 + 0.5 * 3 + 0.25 * 2), abs=1e-3)


def test_gap_marks_claims_without_evidence(mine):
    pid = mine.execute("SELECT id FROM projects WHERE name='alpha'").fetchone()[0]
    mine.execute("INSERT INTO project_facts (project_id, facts, rejected, input_hash, extracted_at) VALUES (?,?,?,?,?)",
                 (pid, "{}", json.dumps([{"type": "technology", "name": "k8s", "evidence_ids": []}]), "h", "now"))
    add_text(mine, "backend", "Kubernetes and Python")
    mk.extract_postings(mine, mk.KeywordExtractor())
    missing = {x["skill"]: x for x in mk.gap(mine, "backend")["missing"]}
    assert "no valid evidence" in missing["Kubernetes"]["note"]


# ---------- demand in the evaluator ----------

def pinput(**kw):
    base = dict(id=1, name="proj", url=None, summary=None, outcome=None, authorship=None, has_deployment=False,
                deployed_url=None, commit_count=5, last_activity=NOW.isoformat(), languages={"Python": 1000},
                n_sessions=0, n_user_messages=0, files_edited=0, readme="", evidence=[{"id": 1, "kind": "readme", "text": "x"}],
                skills={"python": "Python"})
    base.update(kw)
    return ev.ProjectInput(**base)


J = ev.Judgment(depth=5, complexity=3, reason="Solid.", evidence_ids=[1])


def D(skill, score, total=10):
    return mk.Demand(skill, skill.lower(), round(score * total), total, round(score * total), 0, score)


def test_project_demand_is_share_of_top_k():
    dem = {"python": D("Python", 1.0), "rag": D("RAG", 0.5), "docker": D("Docker", 0.5), "go": D("Go", 0.1)}
    assert ev.project_demand({"python": "Python"}, dem, 3) == (pytest.approx(0.5), ["Python"])
    assert ev.project_demand({"python": "", "rag": "", "docker": "", "go": ""}, dem, 3)[0] == pytest.approx(1.0)
    assert ev.project_demand({"html": "HTML"}, dem, 3) == (0.0, [])
    assert ev.project_demand({"python": "Python"}, {}, 3) == (0.0, [])


def test_demand_is_a_weighted_criterion_that_flips_decisions():
    p = pinput()
    hot = ev.score_project(p, J, RUBRIC, NOW, demand={"python": D("Python", 1.0)}, role="ai")
    cold = ev.score_project(p, J, RUBRIC, NOW, demand={"go": D("Go", 1.0)}, role="ai")
    none = ev.score_project(p, J, RUBRIC, NOW)
    w = RUBRIC["projects"]["weights"]
    assert sum(w.values()) == pytest.approx(1.0) and w["demand"] == pytest.approx(0.2)
    assert hot.breakdown["demand"]["value"] == 1.0 and cold.breakdown["demand"]["value"] == 0.0
    assert hot.score - cold.score == pytest.approx(w["demand"], abs=2e-3)
    assert sum(hot.breakdown[c]["points"] for c in ev.PROJECT_CRITERIA) == pytest.approx(hot.score, abs=2e-3)
    assert (hot.decision, cold.decision) == ("keep", "rewrite")
    assert "demand" not in none.breakdown and "ai demand for Python" in hot.reason and "low ai demand" in cold.reason


def test_skill_demand_criterion():
    rows = [{"project_id": 1, "evidence_id": 1, "kind": "language", "created_at": NOW.isoformat()}]
    langs = {1: {"Python": 1000}}
    hot = ev.score_skill("Python", rows, {1}, langs, RUBRIC, NOW, demand={"python": D("Python", 0.8)}, role="ai")
    cold = ev.score_skill("Python", rows, {1}, langs, RUBRIC, NOW, demand={}, role="ai")
    assert hot.breakdown["demand"]["value"] == 0.8 and cold.breakdown["demand"]["value"] == 0.0
    assert hot.score - cold.score == pytest.approx(0.8 * RUBRIC["skills"]["weights"]["demand"], abs=2e-3)
    assert "In 8/10 ai postings (required)" in hot.reason and "No ai posting asks for it" in cold.reason


def test_evaluate_with_role_changes_scores(mine):
    add_text(mine, "py", "Requirements:\n- Python")
    add_text(mine, "ts", "Requirements:\n- TypeScript")
    mk.extract_postings(mine, mk.KeywordExtractor())
    judge = ev.HeuristicJudge()
    py = {r.item_id: r for r in ev.evaluate(mine, judge, RUBRIC, NOW, role="py")}
    ts = {r.item_id: r for r in ev.evaluate(mine, judge, RUBRIC, NOW, role="ts")}
    alpha, beta = "p1", "p2"   # alpha is Python, beta is TypeScript
    assert py[alpha].score > ts[alpha].score and ts[beta].score > py[beta].score
    assert py["s:python"].breakdown["inputs"]["demand"]["postings"] == 1
    assert py[alpha].breakdown["inputs"]["demand_skills"] == ["Python"]
    assert ts[alpha].breakdown["inputs"]["role"] == "ts"


def test_small_language_shares_do_not_count(mine):
    p = ev.project_inputs(mine, 1, min_language_share=0.15)  # alpha: Python 90%, Shell 10%
    assert set(p.skills) == {"python"}
    assert set(ev.project_inputs(mine, 1).skills) == {"python", "shell"}


# ---------- CLI ----------

runner = CliRunner()


def test_cli_market_gap_evaluate(tmp_path, fake, monkeypatch):
    dbp = str(tmp_path / "c.db")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(cli, "make_client", lambda: github.GitHubClient(transport=fake, sleep=lambda s: None))
    runner.invoke(cli.app, ["--db", dbp, "ingest", "github", USER])
    r = runner.invoke(cli.app, ["--db", dbp, "gap"])
    assert r.exit_code == 1 and "no postings" in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "market", "add", str(SAMPLE), "--role", "AI Engineer"])
    assert r.exit_code == 0, r.output
    assert "10 added" in r.output and "keyword" in r.output and '"postings": 10' in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "market", "add", str(SAMPLE), "--role", "ai engineer"])
    assert "0 added, 10 already stored" in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "market", "demand"])
    line = next(l for l in r.output.splitlines() if l.startswith("Python "))
    assert "100%" in line and "required" in line
    r = runner.invoke(cli.app, ["--db", dbp, "gap", "--role", "ai engineer"])
    assert r.exit_code == 0, r.output
    assert "(a) In demand, and you have evidence" in r.output and "(b) In demand, no evidence" in r.output
    assert "(c) You have evidence, no posting asks for it" in r.output
    a, b = r.output.split("(b)")[0], r.output.split("(b)")[1].split("(c)")[0]
    assert "Python" in a and "LLM APIs" in b
    r = runner.invoke(cli.app, ["--db", dbp, "gap", "--json"])
    assert json.loads(r.output)["role"] == "ai engineer"
    r = runner.invoke(cli.app, ["--db", dbp, "evaluate"])
    assert r.exit_code == 0 and "role ai engineer" in r.output and "dem" in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "explain", "s:python"])
    assert "demand" in r.output
    runner.invoke(cli.app, ["--db", dbp, "market", "add", "-", "--role", "web"], input="Web dev\nReact")
    r = runner.invoke(cli.app, ["--db", dbp, "evaluate"])  # remembers the last role
    assert "role ai engineer" in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "market", "list"])
    assert "ai engineer" in r.output and "web" in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "market", "remove", "--role", "web"])
    assert "removed 1" in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "market", "add", str(SAMPLE), "--role", "x", "--extractor", "llm"])
    assert r.exit_code == 1 and "ANTHROPIC_API_KEY" in r.output
