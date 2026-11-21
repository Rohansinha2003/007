import json
from datetime import datetime, timezone

import pytest

from resumeforge import db, evaluate, extract, llm, resume
from resumeforge.ingest import claude_code, github

from .conftest import fake_gh

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)


@pytest.fixture
def loaded(conn, claude_root):
    github.ingest(conn, api=fake_gh)
    claude_code.ingest(conn, claude_root)
    return conn


@pytest.fixture
def fake_llm(monkeypatch):
    calls = []

    def backend(system, user, schema, max_tokens):
        calls.append({"system": system, "user": user, "schema": schema})
        ids = [int(x) for x in __import__("re").findall(r"\[E(\d+)\]", user)]
        if "summary" in schema["properties"]:
            return json.dumps({"summary": "A WhatsApp CRM.", "substantive": True, "skills": [
                {"name": "Next.js", "category": "frontend", "evidence_ids": ids[:2]},
                {"name": "PostgreSQL", "category": "data", "evidence_ids": [ids[0], 99999]},
                {"name": "Kubernetes", "category": "infra", "evidence_ids": [99999]},  # hallucinated: dropped
            ]})
        return json.dumps({"title": "Small CRM", "bullets": [
            {"text": "Built a Next.js CRM.", "evidence_ids": ids[:2]},
            {"text": "Scaled to 1M users.", "evidence_ids": []},  # unsupported: dropped
        ]})

    monkeypatch.setattr(llm, "backend", backend)
    return calls


def test_strict_schema_shape():
    s = llm._strict_schema(extract.ProjectExtraction.model_json_schema())
    assert s["additionalProperties"] is False and set(s["required"]) == {"summary", "substantive", "skills"}
    item = s["$defs"]["SkillClaim"]
    assert item["additionalProperties"] is False and "title" not in item


def test_invalid_json_raises(monkeypatch):
    monkeypatch.setattr(llm, "backend", lambda *a: '{"summary": 1}')
    with pytest.raises(llm.LLMError):
        llm.complete_json("s", "u", extract.ProjectExtraction)


def test_llm_input_is_redacted(monkeypatch):
    seen = {}
    monkeypatch.setattr(llm, "backend", lambda s, u, sc, m: seen.setdefault("u", u) and '{"title":"t","bullets":[]}')
    llm.complete_json("s", "key ghp_" + "x" * 30, resume.ProjectBullets)
    assert "ghp_" not in seen["u"]


def test_llm_pipeline_drops_unsupported_claims(loaded, fake_llm, tmp_path):
    extract.extract_all(loaded)
    names = {r[0] for r in loaded.execute("SELECT name FROM skills")}
    assert names == {"next.js", "postgresql"}
    evaluate.evaluate(loaded, now=NOW)
    out = resume.generate(loaded, profile=github.profile(api=fake_gh), out_dir=tmp_path)
    assert out["dropped_bullets"] == 1
    md = open(out["resume"]).read()
    assert "Built a Next.js CRM." in md and "1M users" not in md
    assert "[E" in md and "[E" not in open(out["clean"]).read()
    # every cited id is listed in the evidence file
    ev = open(out["evidence"]).read()
    for i in set(__import__("re").findall(r"\[E(\d+)\]", md)):
        assert f"**E{i}**" in ev


def test_every_claim_has_evidence(loaded, fake_llm, tmp_path):
    extract.extract_all(loaded)
    evaluate.evaluate(loaded, now=NOW)
    resume.generate(loaded, profile={"name": "X", "url": ""}, out_dir=tmp_path)
    assert loaded.execute("SELECT COUNT(*) FROM skills s WHERE NOT EXISTS "
                          "(SELECT 1 FROM skill_evidence WHERE skill_id=s.id)").fetchone()[0] == 0
    assert loaded.execute("SELECT COUNT(*) FROM bullets b WHERE NOT EXISTS "
                          "(SELECT 1 FROM bullet_evidence WHERE bullet_id=b.id)").fetchone()[0] == 0


def test_offline_pipeline(loaded, tmp_path):
    extract.extract_all(loaded, offline=True)
    names = {r[0] for r in loaded.execute("SELECT name FROM skills")}
    assert {"typescript", "postgresql", "next.js"} <= names
    res = evaluate.evaluate(loaded, now=NOW)
    assert all(r["rationale"] for r in res)
    out = resume.generate(loaded, profile={"name": "X", "url": ""}, offline=True, out_dir=tmp_path)
    assert out["projects"] == 1 and "WhatsApp CRM" in open(out["resume"]).read()


def test_job_description_boosts_skill(loaded):
    extract.extract_all(loaded, offline=True)
    base = {r["name"]: r["score"] for r in evaluate.evaluate(loaded, now=NOW)}
    jd = {r["name"]: r["score"] for r in evaluate.evaluate(loaded, "We need PostgreSQL experience.", now=NOW)}
    assert jd["PostgreSQL"] > base["PostgreSQL"]


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="ANTHROPIC_API_KEY"):
        llm._call_anthropic("s", "u", {}, 10)
