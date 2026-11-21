import json
from datetime import datetime, timezone

import pytest
from docx import Document
from typer.testing import CliRunner

from resumeforge import cli, db, evaluator as ev, generate as gen, market as mk, render
from resumeforge.ingest import claude_code, common, github
from resumeforge.llm import LLM, LLMError, Usage

from . import fixtures as fx
from .conftest import USER

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
RUBRIC = ev.load_rubric()
JD = """Backend Engineer (fictional)
Requirements:
- Python
- FastAPI
- PostgreSQL
Nice to have:
- TypeScript
"""


@pytest.fixture
def loaded(conn, client, tmp_path):
    github.ingest(conn, client, USER)
    common.store(conn, claude_code.parse(fx.claude_code(tmp_path)))
    conn.commit()
    ev.evaluate(conn, ev.HeuristicJudge(), RUBRIC, NOW)
    return conn


def jd():
    return gen.jd_demand(JD, mk.KeywordExtractor())[0]


def pid(conn, name):
    return conn.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()[0]


def build(conn, **kw):
    args = dict(writer=gen.TemplateWriter(), checkers=[gen.LexicalChecker()], extractor=mk.KeywordExtractor(),
                rubric=RUBRIC, header=gen.header(conn))
    args.update(kw)
    return gen.build(conn, JD, **args)


# ---------- selection ----------

def test_jd_demand_required_and_preferred():
    d = jd()
    assert {k: (v.required, v.preferred, v.score) for k, v in d.items()} == {
        "python": (1, 0, 1.0), "fastapi": (1, 0, 1.0), "postgresql": (1, 0, 1.0), "typescript": (0, 1, 0.5)}
    assert gen.jd_title(JD) == "Backend Engineer (fictional)"


def test_select_uses_decisions_overrides_and_fit(loaded):
    decisions = {r["item_id"]: r["decision"] for r in loaded.execute("SELECT item_id, decision FROM evaluations")}
    projects, skills = gen.select(loaded, jd(), RUBRIC)
    names = [p.name for p in projects]
    assert "empty" not in names and "gamma" not in names       # dropped by rules
    assert all(decisions[f"p{p.project_id}"] in ("keep", "rewrite") for p in projects)
    alpha = next(p for p in projects if p.name == "alpha")
    assert alpha.jd_matches == ["Python"] and alpha.fit > 0 and alpha.stack == ["Python", "Shell"]  # Shell is 10%, above min share
    ev.set_override(loaded, "alpha", "drop")
    ev.set_override(loaded, "gamma", "keep")
    names = [p.name for p in gen.select(loaded, jd(), RUBRIC)[0]]
    assert "alpha" not in names and "gamma" in names
    for s in skills:
        assert s.evidence_ids, s  # no evidence, no claim
    assert skills[0].in_jd


def test_dropped_and_unsupported_skills_never_listed(loaded):
    loaded.execute("INSERT INTO project_facts (project_id, facts, rejected, input_hash, extracted_at) VALUES (?,?,?,?,?)",
                   (pid(loaded, "alpha"), "{}", json.dumps([{"type": "technology", "name": "PostgreSQL"}] * 2), "h", "now"))
    ev.evaluate(loaded, ev.HeuristicJudge(), RUBRIC, NOW)
    assert loaded.execute("SELECT decision FROM evaluations WHERE item_id='s:postgresql'").fetchone()[0] == "needs_evidence"
    _, skills = gen.select(loaded, jd(), RUBRIC)
    assert "PostgreSQL" not in {s.name for s in skills}


# ---------- template writer and lexical check ----------

def test_first_sentence():
    assert gen.first_sentence("# X\n[![b](u)](l)\n\nAcme is a tool that syncs calendars across teams quickly.") == \
        "a tool that syncs calendars across teams quickly"
    assert gen.first_sentence("# React + TypeScript + Vite\n\nThis template provides a minimal setup for apps.") is None
    long = "Single-page portfolio with numbered chapters: " + "hero, " * 40
    assert gen.first_sentence(long) == "Single-page portfolio with numbered chapters"


def test_template_bullets_cite_own_evidence_and_pass_check(loaded):
    r = build(loaded)
    assert r.projects
    for p in r.projects:
        own = {i for (i,) in loaded.execute("SELECT id FROM evidence WHERE project_id=?", (p.project_id,))}
        for b in p.bullets:
            assert b.evidence_ids and set(b.evidence_ids) <= own
            assert b.status == "verified", (b.text, b.reason)
    alpha = next(p for p in r.projects if p.name == "alpha")
    texts = [b.text for b in alpha.bullets]
    assert texts[0] == "Built alpha: alpha description, using Python and Shell."
    assert any("https://alpha.dev" in t for t in texts)


def bo(text, ids):
    return gen.BulletOut(text, ids)


def test_lexical_checker_catches_invented_details(loaded):
    a = pid(loaded, "alpha")
    proj = next(p for p in gen.select(loaded, jd(), RUBRIC)[0] if p.project_id == a)
    rows = {r["kind"]: r["id"] for r in loaded.execute("SELECT id, kind FROM evidence WHERE project_id=?", (a,))}
    bullets = [bo("Built alpha, a FastAPI service.", [rows["readme"]]),
               bo("Cut latency by 40% with FastAPI.", [rows["readme"]]),
               bo("Deployed alpha on Kubernetes.", [rows["readme"]]),
               bo("Shipped alpha at https://alpha.dev.", [rows["deployment"]]),
               bo("Shipped alpha at https://other.dev.", [rows["deployment"]]),
               bo("Made 3 commits.", [rows["commits"]])]
    v = gen.LexicalChecker().check(loaded, proj, bullets)
    assert [x.verdict for x in v] == ["supported", "unsupported", "unsupported", "supported", "unsupported", "supported"]
    assert "40%" in v[1].reason and "Kubernetes" in v[2].reason and "other.dev" in v[4].reason


# ---------- LLM writer and checker (mocked) ----------

def test_llm_writer_and_checker_remove_and_flag(loaded):
    a, b = pid(loaded, "alpha"), pid(loaded, "beta")
    a_ev = {r["kind"]: r["id"] for r in loaded.execute("SELECT id, kind FROM evidence WHERE project_id=?", (a,))}
    b_ev = loaded.execute("SELECT id FROM evidence WHERE project_id=? LIMIT 1", (b,)).fetchone()[0]

    def backend(system, user, schema, max_tokens, model):
        if "verify resume bullets" in system:
            n = user.count("\nBullet ")
            verdicts = [{"bullet": 1, "verdict": "supported", "reason": "README says so."}]
            if n >= 2:
                verdicts.append({"bullet": 2, "verdict": "partial", "reason": "'production' is not shown."})
            # bullet 3, if any, gets no verdict
            return json.dumps({"verdicts": verdicts}), Usage(model, 400, 60)
        if "Project: alpha" not in user:
            return json.dumps({"bullets": []}), Usage(model, 100, 10)
        return json.dumps({"bullets": [
            {"text": "Built a FastAPI service for alpha.", "evidence_ids": [a_ev["readme"]]},
            {"text": "Shipped alpha to production at https://alpha.dev.", "evidence_ids": [a_ev["deployment"], 999999]},
            {"text": "Wrote three commits.", "evidence_ids": [a_ev["commits"]]},
            {"text": "Designed beta's site.", "evidence_ids": [b_ev]},                    # other project's evidence
            {"text": "Scaled alpha to 10k users.", "evidence_ids": [a_ev["readme"]]},     # invented metric
        ]}), Usage(model, 900, 120)

    client = LLM(loaded, backend=backend)
    r = build(loaded, writer=gen.LLMWriter(client), checkers=[gen.LexicalChecker(), gen.LLMChecker(client)])
    alpha = next(p for p in r.projects if p.name == "alpha")
    by_text = {x.text: x for x in alpha.bullets}
    assert by_text["Built a FastAPI service for alpha."].status == "verified"
    shipped = by_text["Shipped alpha to production at https://alpha.dev."]
    assert shipped.status == "flagged" and shipped.evidence_ids == [a_ev["deployment"]]  # bad id dropped
    assert by_text["Wrote three commits."].status == "removed" and "no verdict" in by_text["Wrote three commits."].reason
    assert by_text["Designed beta's site."].status == "removed" and "no valid evidence" in by_text["Designed beta's site."].reason
    assert by_text["Scaled alpha to 10k users."].status == "removed" and "10k" in by_text["Scaled alpha to 10k users."].reason
    assert [b.text for b in alpha.kept] == ["Built a FastAPI service for alpha.", "Shipped alpha to production at https://alpha.dev."]
    assert all(p.name == "alpha" for p in r.projects)  # projects without supported bullets are left off
    loaded.execute("DELETE FROM llm_cache")
    strict = build(loaded, writer=gen.LLMWriter(client), checkers=[gen.LLMChecker(client)], strict=True)
    s_alpha = next(p for p in strict.projects if p.name == "alpha")
    assert all(b.status == "verified" for b in s_alpha.kept)


def test_writer_failure_falls_back_to_template(loaded):
    def backend(*a):
        raise LLMError("API error 529: overloaded")

    r = build(loaded, writer=gen.LLMWriter(LLM(loaded, backend=backend)))
    assert r.projects and all(p.writer.startswith("template (fallback") for p in r.projects)


def test_bullet_limit(loaded):
    r = build(loaded, max_bullets=1)
    assert all(len(p.kept) == 1 for p in r.projects)


# ---------- rendering ----------

def visible_and_hidden(path):
    doc = Document(path)
    vis, hid = [], []
    for p in doc.paragraphs:
        vis.append("".join(r.text for r in p.runs if not r.font.hidden))
        hid.append("".join(r.text for r in p.runs if r.font.hidden))
    return doc, vis, hid


def test_docx_pdf_and_evidence_md(loaded, tmp_path):
    db.set_setting(loaded, "profile", {"email": "me@example.com"})
    r = build(loaded, header=gen.header(loaded, {"phone": "555-0100"}))
    assert r.header["name"] == "Dev Example" and r.header["email"] == "me@example.com"
    out = render.to_docx(r, tmp_path / "resume.docx")
    doc, vis, hid = visible_and_hidden(out)
    assert len(doc.tables) == 0 and len(doc.sections) == 1 and not doc.inline_shapes  # ATS-safe
    assert vis[0] == "Dev Example" and "me@example.com | 555-0100" in vis[1] and "SKILLS" in vis and "PROJECTS" in vis
    assert not any("[evidence" in v for v in vis)
    bullets = [(v, h) for p, v, h in zip(doc.paragraphs, vis, hid) if p.style.name == "List Bullet"]
    kept = [b for p in r.projects for b in p.kept]
    assert [v for v, _ in bullets] == [b.text for b in kept]
    assert all(h.strip() == render.ids_label(b.evidence_ids) for (_, h), b in zip(bullets, kept))
    plain = render.to_docx(r, tmp_path / "plain.docx", hidden_ids=False)
    assert not any(visible_and_hidden(plain)[2])

    pdf = render.to_pdf(r, tmp_path / "resume.pdf", compress=False)
    raw = pdf.read_bytes()
    assert raw.startswith(b"%PDF") and b"PROJECTS" in raw and b"[evidence" not in raw
    assert kept[0].text[:20].encode() in raw

    md = render.to_evidence_md(r, loaded, tmp_path / "evidence.md").read_text()
    for b in kept:
        assert b.text in md
        for i in b.evidence_ids:
            assert f"E{i} ·" in md
    assert "https://github.com/dev/alpha" in md and "## Removed or flagged bullets" in md
    assert "Asked for, no evidence, so not claimed: FastAPI (required), PostgreSQL (required)" in md


def test_pdf_handles_unicode(loaded, tmp_path):
    r = build(loaded)
    r.projects[0].bullets[0].text = "Built “smart” tool — fast → résumé ✓"
    raw = render.to_pdf(r, tmp_path / "u.pdf", compress=False).read_bytes()
    assert b'Built "smart" tool - fast -> r' in raw


# ---------- CLI ----------

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path, fake, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(cli, "make_client", lambda: github.GitHubClient(transport=fake, sleep=lambda s: None))
    (tmp_path / "jd.txt").write_text(JD)
    return tmp_path


def test_cli_generate(cli_env):
    dbp, t = str(cli_env / "c.db"), cli_env
    r = runner.invoke(cli.app, ["--db", dbp, "generate", "--jd", str(t / "jd.txt")])
    assert r.exit_code == 1 and "no projects" in r.output
    runner.invoke(cli.app, ["--db", dbp, "ingest", "github", USER])
    r = runner.invoke(cli.app, ["--db", dbp, "generate", "--jd", str(t / "jd.txt"), "--out", str(t / "o" / "cv.docx"),
                                "--name", "Dev Q. Example", "--email", "dev@example.com"])
    assert r.exit_code == 0, r.output
    assert "running `resume evaluate` first" in r.output and "Selected projects" in r.output and "Claim check" in r.output
    assert "JD skills not claimed (no evidence): FastAPI, PostgreSQL" in r.output
    for f in ("cv.docx", "cv.pdf", "evidence.md"):
        assert (t / "o" / f).exists()
    assert visible_and_hidden(t / "o" / "cv.docx")[1][0] == "Dev Q. Example"
    r = runner.invoke(cli.app, ["--db", dbp, "generate", "--jd", str(t / "jd.txt"), "--out", str(t / "o2" / "cv.docx"),
                                "--no-pdf"])
    assert not (t / "o2" / "cv.pdf").exists()
    assert visible_and_hidden(t / "o2" / "cv.docx")[1][0] == "Dev Q. Example"  # name was saved
    r = runner.invoke(cli.app, ["--db", dbp, "generate", "--jd", str(t / "jd.txt"), "--writer", "llm"])
    assert r.exit_code == 1 and "ANTHROPIC_API_KEY" in r.output


def test_cli_run_all(cli_env):
    dbp, t = str(cli_env / "c.db"), cli_env
    r = runner.invoke(cli.app, ["--db", dbp, "run-all", "--github", USER, "--jd", str(t / "jd.txt"),
                                "--out", str(t / "out" / "resume.docx"), "--no-logs"])
    assert r.exit_code == 0, r.output
    for step in ("1/5 GitHub", "2/5", "3/5 Extraction", "4/5 Evaluation", "5/5 Resume", "skipped: no ANTHROPIC_API_KEY"):
        assert step in r.output
    for f in ("resume.docx", "resume.pdf", "evidence.md"):
        assert (t / "out" / f).exists()
    assert "PROJECTS (" not in r.output  # evaluate prints counts only inside run-all
