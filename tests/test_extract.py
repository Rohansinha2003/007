import json

import pytest
from typer.testing import CliRunner

from resumeforge import cli, db, extract
from resumeforge.extract import Authorship, Claim, Outcome, ProjectFacts
from resumeforge.ingest import claude_code, common, github
from resumeforge.llm import LLM, Usage
from resumeforge.skills import canonical

from . import fixtures as fx
from .conftest import USER


@pytest.mark.parametrize("names,expected", [
    (["React.js", "ReactJS", "react", "React"], "React"),
    (["NodeJS", "node.js", "Node"], "Node.js"),
    (["Postgres", "postgresql", "PostgreSQL"], "PostgreSQL"),
    (["next js", "NextJS", "Next.js"], "Next.js"),
    (["Tailwind", "tailwindcss"], "Tailwind CSS"),
    (["Hugging Face", "huggingface", "transformers"], "Hugging Face"),
])
def test_aliases(names, expected):
    assert {canonical(n) for n in names} == {expected}


def test_unknown_skill_keeps_spelling():
    assert canonical("  LangGraph   state machines ") == "LangGraph state machines"


def facts(**kw):
    base = dict(
        summary="A CRM.",
        technologies=[Claim(name="React.js", evidence_ids=[1, 2]), Claim(name="ReactJS", evidence_ids=[3]),
                      Claim(name="Kubernetes", evidence_ids=[999]), Claim(name="Redis", evidence_ids=[])],
        skills=[Claim(name="Authentication", evidence_ids=[2, 404]), Claim(name="react", evidence_ids=[1]),
                Claim(name="Scaling to 1M users", evidence_ids=[])],
        outcome=Outcome(value="shipped", reason="Deployed.", evidence_ids=[3]),
        authorship=Authorship(value="mostly_ai", reason="AI wrote most files.", evidence_ids=[2]),
    )
    base.update(kw)
    return ProjectFacts(**base)


def test_validate_rejects_unsupported_claims():
    clean, rejected = extract.validate(facts(), allowed={1, 2, 3})
    assert clean["technologies"] == [{"name": "React", "evidence_ids": [1, 2, 3]}]  # aliases merged
    assert clean["skills"] == [{"name": "Authentication", "evidence_ids": [2]}]       # bad id dropped; dup of tech removed
    assert {r["name"] for r in rejected} == {"Kubernetes", "Redis", "Scaling to 1M users"}
    assert clean["outcome"]["value"] == "shipped" and clean["authorship"]["value"] == "mostly_ai"


def test_validate_rejects_unsupported_outcome_and_authorship():
    f = facts(outcome=Outcome(value="shipped", reason="r", evidence_ids=[77]),
              authorship=Authorship(value="self_written", reason="r", evidence_ids=[]))
    clean, rejected = extract.validate(f, allowed={1, 2, 3})
    assert clean["outcome"]["value"] is None and clean["authorship"]["value"] == "unknown"
    assert {r["type"] for r in rejected} >= {"outcome", "authorship"}


def test_unknown_authorship_needs_no_evidence():
    clean, rejected = extract.validate(facts(authorship=Authorship(value="unknown", reason="no logs", evidence_ids=[])),
                                       allowed={1, 2, 3})
    assert clean["authorship"]["value"] == "unknown" and not any(r["type"] == "authorship" for r in rejected)


def test_ids_from_another_project_are_invalid():
    clean, rejected = extract.validate(facts(), allowed={3})
    assert clean["technologies"] == [{"name": "React", "evidence_ids": [3]}]
    assert clean["skills"] == [] and clean["authorship"]["value"] == "unknown"


@pytest.fixture
def loaded(conn, client, tmp_path):
    """GitHub fake data plus a Claude Code session linked to 'alpha'."""
    github.ingest(conn, client, USER)
    common.store(conn, claude_code.parse(fx.claude_code(tmp_path)))
    conn.commit()
    return conn


def test_pack_contains_github_and_session_evidence(loaded):
    pid = loaded.execute("SELECT id FROM projects WHERE name='alpha'").fetchone()[0]
    pack = extract.build_pack(loaded, pid)
    kinds = {r[0] for r in loaded.execute(f"SELECT kind FROM evidence WHERE id IN ({','.join(map(str, pack.evidence_ids))})")}
    assert {"readme", "languages", "commits", "user_message", "assistant_summary", "files_touched"} <= kinds
    assert "Linked AI coding sessions: 1" in pack.prompt and "[REDACTED" in pack.prompt
    for i in pack.evidence_ids:
        assert f"[E{i}]" in pack.prompt


def fake_extractor(bad_ids=(999,)):
    """Backend that cites the first real ids it sees, plus invented ones."""
    import re

    def backend(system, user, schema, max_tokens, model):
        ids = [int(x) for x in re.findall(r"\[E(\d+)\]", user)]
        return json.dumps({
            "summary": "Alpha is a FastAPI service.",
            "technologies": [{"name": "fastapi", "evidence_ids": ids[:2]},
                             {"name": "Kubernetes", "evidence_ids": list(bad_ids)}],
            "skills": [{"name": "Authentication", "evidence_ids": ids[-2:]},
                       {"name": "Team leadership", "evidence_ids": []}],
            "outcome": {"value": "shipped", "reason": "Has a homepage.", "evidence_ids": ids[:1]},
            "authorship": {"value": "ai_assisted", "reason": "Session shows directed work.", "evidence_ids": ids[-1:]},
        }), Usage(model, 2000, 300)
    return backend


def test_extract_stores_only_supported_skills(loaded):
    pid = loaded.execute("SELECT id FROM projects WHERE name='alpha'").fetchone()[0]
    r = extract.extract_project(loaded, LLM(loaded, backend=fake_extractor()), pid)
    assert r["rejected"] == 2 and r["outcome"] == "shipped" and r["authorship"] == "ai_assisted"
    llm_skills = {row[0] for row in loaded.execute("SELECT skill FROM skills_observed WHERE origin='llm'")}
    assert llm_skills == {"FastAPI", "Authentication"}
    # every stored skill points at an evidence row of the same project
    bad = loaded.execute("""SELECT COUNT(*) FROM skills_observed so JOIN evidence e ON e.id=so.evidence_id
                            WHERE so.project_id IS NOT e.project_id""").fetchone()[0]
    assert bad == 0
    pf = loaded.execute("SELECT * FROM project_facts WHERE project_id=?", (pid,)).fetchone()
    assert {r["name"] for r in json.loads(pf["rejected"])} == {"Kubernetes", "Team leadership"}


def test_reextract_replaces_llm_skills_but_keeps_languages(loaded):
    pid = loaded.execute("SELECT id FROM projects WHERE name='alpha'").fetchone()[0]
    client = LLM(loaded, backend=fake_extractor())
    extract.extract_project(loaded, client, pid)
    before = loaded.execute("SELECT COUNT(*) FROM skills_observed").fetchone()[0]
    extract.extract_project(loaded, client, pid)  # served from cache, stored again
    assert loaded.execute("SELECT COUNT(*) FROM skills_observed").fetchone()[0] == before
    assert loaded.execute("SELECT COUNT(*) FROM skills_observed WHERE kind='language'").fetchone()[0] >= 2
    assert loaded.execute("SELECT SUM(cached) FROM llm_calls").fetchone()[0] == 1


runner = CliRunner()


def test_cli_extract_and_skills(tmp_path, fake, monkeypatch):
    dbp = str(tmp_path / "c.db")
    monkeypatch.setattr(cli, "make_client", lambda: github.GitHubClient(transport=fake, sleep=lambda s: None))
    assert runner.invoke(cli.app, ["--db", dbp, "ingest", "github", USER]).exit_code == 0
    r = runner.invoke(cli.app, ["--db", dbp, "extract", "--dry-run"])
    assert r.exit_code == 0 and "input tokens" in r.output and "est. $" in r.output
    monkeypatch.setattr(cli.llm_mod, "anthropic_backend", fake_extractor())
    r = runner.invoke(cli.app, ["--db", dbp, "extract"])
    assert r.exit_code == 0, r.output
    assert "rejected  2" in r.output and "LLM:" in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "skills"])
    assert "FastAPI" in r.output and "Kubernetes" not in r.output and "Python" in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "llm-usage"])
    assert "extract_project" in r.output


def test_cli_extract_without_key_fails_clearly(tmp_path, fake, monkeypatch):
    dbp = str(tmp_path / "c.db")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(cli, "make_client", lambda: github.GitHubClient(transport=fake, sleep=lambda s: None))
    runner.invoke(cli.app, ["--db", dbp, "ingest", "github", USER])
    r = runner.invoke(cli.app, ["--db", dbp, "extract"])
    assert r.exit_code == 1 and "ANTHROPIC_API_KEY" in r.output
