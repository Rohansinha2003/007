import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from resumeforge import cli, db, evaluator as ev
from resumeforge.ingest import claude_code, common, github
from resumeforge.llm import LLM, Usage

from . import fixtures as fx
from .conftest import USER

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
RUBRIC = ev.load_rubric()


def ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def pinput(**kw):
    base = dict(id=1, name="proj", url=None, summary=None, outcome=None, authorship=None, has_deployment=False,
                deployed_url=None, commit_count=5, last_activity=ago(0), languages={"Python": 1000}, n_sessions=0,
                n_user_messages=0, files_edited=0, readme="", evidence=[{"id": 1, "kind": "readme", "text": "x"}])
    base.update(kw)
    return ev.ProjectInput(**base)


J = ev.Judgment(depth=5, complexity=3, reason="Solid.", evidence_ids=[1])


# ---------- math ----------

def test_recency_half_life():
    assert ev.recency_score(ago(0), NOW, 365) == pytest.approx(1.0)
    assert ev.recency_score(ago(365), NOW, 365) == pytest.approx(0.5, abs=1e-3)
    assert ev.recency_score(ago(730), NOW, 365) == pytest.approx(0.25, abs=1e-3)
    assert ev.recency_score(None, NOW, 365) == 0.0


def test_saturating():
    assert ev.saturating(0, 10) == 0 and ev.saturating(10, 10) == 1 and ev.saturating(50, 10) == 1
    assert ev.saturating(3, 10) == pytest.approx(math.log(4) / math.log(11))


def test_band_edges():
    t = {"keep": 0.6, "rewrite": 0.4}
    assert [ev.band(x, t) for x in (0.6, 0.5999, 0.4, 0.3999)] == ["keep", "rewrite", "rewrite", "drop"]


def test_project_score_is_weighted_sum():
    p = pinput(outcome="shipped", authorship="ai_assisted")
    r = ev.score_project(p, J, RUBRIC, NOW)
    w = RUBRIC["projects"]["weights"]
    ev_cfg = RUBRIC["projects"]["evidence"]
    expected_evidence = ev_cfg["volume_weight"] * ev.saturating(1, ev_cfg["saturation"]) + \
        (1 - ev_cfg["volume_weight"]) * 1 / len(ev_cfg["kinds"])
    expected = (w["depth"] * 0.75 + w["originality"] * 0.7 + w["evidence"] * expected_evidence
                + w["shipped"] * 1.0 + w["recency"] * 1.0)
    assert r.score == pytest.approx(expected, abs=1e-3)
    assert sum(r.breakdown[c]["points"] for c in ev.PROJECT_CRITERIA) == pytest.approx(r.score, abs=2e-3)
    assert r.breakdown["inputs"]["outcome_source"] == "extraction"


def test_weights_are_normalized_and_configurable(tmp_path):
    raw = yaml.safe_load(ev.DEFAULT_RUBRIC.read_text())
    raw["projects"]["weights"] = {"depth": 2, "originality": 0, "evidence": 0, "shipped": 0, "recency": 0}
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump(raw))
    rb = ev.load_rubric(path)
    assert rb["projects"]["weights"]["depth"] == 1.0
    r = ev.score_project(pinput(), J, rb, NOW)
    assert r.score == pytest.approx(0.75) and rb["hash"] != RUBRIC["hash"]


@pytest.mark.parametrize("patch,msg", [
    ({"weights": {"depth": 1}}, "missing"),
    ({"weights": {"depth": -1, "originality": 1, "evidence": 1, "shipped": 1, "recency": 1}}, "non-negative"),
    ({"thresholds": {"keep": 0.3, "rewrite": 0.5}}, "thresholds"),
])
def test_bad_rubric_rejected(tmp_path, patch, msg):
    raw = yaml.safe_load(ev.DEFAULT_RUBRIC.read_text())
    raw["projects"].update(patch)
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ev.RubricError, match=msg):
        ev.load_rubric(path)


def test_deployment_counts_as_shipped_without_extraction():
    r = ev.score_project(pinput(has_deployment=True), J, RUBRIC, NOW)
    assert r.breakdown["shipped"]["value"] == 1.0 and r.breakdown["inputs"]["outcome_source"] == "GitHub deployment"


def test_rules_drop_regardless_of_score():
    r = ev.score_project(pinput(commit_count=0, n_sessions=0, has_deployment=True), J, RUBRIC, NOW)
    assert r.decision == "drop" and "no commits" in r.reason
    r = ev.score_project(pinput(name="dev", has_deployment=True), J, RUBRIC, NOW, username="dev")
    assert r.decision == "drop" and "profile" in r.reason


def test_reason_names_strong_and_weak_criteria():
    r = ev.score_project(pinput(outcome="shipped", last_activity=ago(1500)), J, RUBRIC, NOW)
    assert r.reason.startswith("Strong: ") and "outcome shipped" in r.reason and "Weak: " in r.reason
    assert "last active" in r.reason and r.reason.endswith("Solid.")


def test_skill_scoring():
    rows = [{"project_id": 1, "evidence_id": 10, "kind": "language", "created_at": ago(0)},
            {"project_id": 2, "evidence_id": 11, "kind": "language", "created_at": ago(365)},
            {"project_id": 3, "evidence_id": 12, "kind": "language", "created_at": ago(10)}]
    langs = {1: {"Python": 900, "HTML": 100}, 2: {"Python": 500, "Go": 500}, 3: {"Python": 10}}
    r = ev.score_skill("Python", rows, kept_projects={1, 2}, languages_by_project=langs, rubric=RUBRIC, now=NOW)
    b = r.breakdown
    assert b["breadth"]["value"] == pytest.approx(2 / 4)       # project 3 is not kept
    assert b["depth"]["value"] == pytest.approx((0.9 + 0.5) / 2)
    assert b["recency"]["value"] == pytest.approx(1.0)
    assert b["evidence"]["value"] == pytest.approx(ev.saturating(2, 10), abs=1e-3)
    assert r.item_id == "s:python" and b["inputs"]["projects_total"] == 3


def test_skill_only_in_dropped_projects_is_dropped():
    rows = [{"project_id": 9, "evidence_id": 1, "kind": "language", "created_at": ago(0)}]
    r = ev.score_skill("Go", rows, set(), {9: {"Go": 1}}, RUBRIC, NOW)
    assert r.decision == "drop" and "dropped project" in r.reason


def test_trace_skill_capped_at_rewrite():
    rows = [{"project_id": i, "evidence_id": i, "kind": "language", "created_at": ago(0)} for i in range(1, 6)]
    langs = {i: {"Shell": 1, "Python": 999} for i in range(1, 6)}
    r = ev.score_skill("Shell", rows, set(range(1, 6)), langs, RUBRIC, NOW)
    assert r.score >= RUBRIC["skills"]["thresholds"]["keep"] and r.decision == "rewrite"


@pytest.mark.parametrize("claims,decision", [(1, "drop"), (2, "needs_evidence"), (5, "needs_evidence")])
def test_unsupported_skill_only_drop_or_needs_evidence(claims, decision):
    r = ev.unsupported_skill("Kubernetes", claims, RUBRIC)
    assert r.decision == decision and r.score == 0


def test_heuristic_judge_is_deterministic_and_cites_evidence():
    p = pinput(readme="x" * 2000 + " tests api docker auth", commit_count=12, has_deployment=True,
               languages={"Python": 600_000, "TS": 1, "CSS": 1, "HTML": 1})
    a, b = ev.HeuristicJudge().judge(p), ev.HeuristicJudge().judge(p)
    assert a == b and a.depth == 4 and a.complexity == 5 and a.evidence_ids == [1]


# ---------- judges plugged in through the interface ----------

class FixedJudge:
    name = "fixed"

    def __init__(self, depth):
        self.depth = depth

    def judge(self, p):
        return ev.Judgment(depth=self.depth, complexity=self.depth, reason="fixed", evidence_ids=sorted(p.evidence_ids)[:1])


@pytest.fixture
def loaded(conn, client, tmp_path):
    github.ingest(conn, client, USER)
    common.store(conn, claude_code.parse(fx.claude_code(tmp_path)))
    conn.commit()
    return conn


def test_any_judge_can_replace_the_llm(loaded):
    lo = {r.item_id: r.score for r in ev.evaluate(loaded, FixedJudge(1), RUBRIC, NOW)}
    hi = {r.item_id: r for r in ev.evaluate(loaded, FixedJudge(5), RUBRIC, NOW)}
    assert hi["p1"].score > lo["p1"] and hi["p1"].judge == "fixed"


def test_llm_judge_valid_and_rejected(loaded):
    def backend(bad):
        def f(system, user, schema, max_tokens, model):
            ids = [999999] if bad else [int(user.split("[E")[1].split("]")[0])]
            return json.dumps({"depth": 4, "complexity": 3, "reason": "Real auth flow.", "evidence_ids": ids}), Usage(model, 500, 50)
        return f
    good = {r.item_id: r for r in ev.evaluate(loaded, ev.LLMJudge(LLM(loaded, backend=backend(False))), RUBRIC, NOW)}
    assert good["p1"].judge == "llm" and good["p1"].reason.endswith("Real auth flow.")
    loaded.execute("DELETE FROM llm_cache")  # otherwise the good judgment above is served from cache
    bad = {r.item_id: r for r in ev.evaluate(loaded, ev.LLMJudge(LLM(loaded, backend=backend(True))), RUBRIC, NOW)}
    assert bad["p1"].judge.startswith("heuristic (fallback")  # unsupported judgment is not used


def test_rejected_extraction_claims_become_needs_evidence(loaded):
    pid = loaded.execute("SELECT id FROM projects WHERE name='alpha'").fetchone()[0]
    rejected = [{"type": "technology", "name": "k8s", "evidence_ids": []},
                {"type": "skill", "name": "Kubernetes", "evidence_ids": [5]}]
    loaded.execute("INSERT INTO project_facts (project_id, facts, rejected, input_hash, extracted_at) VALUES (?,?,?,?,?)",
                   (pid, "{}", json.dumps(rejected), "h", "now"))
    res = {r.item_id: r for r in ev.evaluate(loaded, FixedJudge(3), RUBRIC, NOW)}
    assert res["s:kubernetes"].decision == "needs_evidence"
    with pytest.raises(ev.OverrideError, match="no evidence"):
        ev.set_override(loaded, "s:kubernetes", "keep")
    assert ev.set_override(loaded, "Kubernetes", "drop") == "s:kubernetes"


# ---------- overrides ----------

def test_override_persists_across_runs(loaded):
    first = {r.item_id: r for r in ev.evaluate(loaded, FixedJudge(1), RUBRIC, NOW)}
    target = next(i for i, r in first.items() if r.item_type == "project" and r.decision == "drop")
    ev.set_override(loaded, target, "keep", note="client project")
    kept = next(i for i, r in first.items() if r.item_type == "project" and r.decision != "drop")
    ev.set_override(loaded, kept, "drop")
    ev.set_override(loaded, "s:python", "drop")
    for depth in (1, 5, 3):
        ev.evaluate(loaded, FixedJudge(depth), RUBRIC, NOW)
        ov = ev.get_overrides(loaded)
        assert ov[target]["decision"] == "keep" and ov[target]["note"] == "client project"
        assert ov["s:python"]["decision"] == "drop" and ov[kept]["decision"] == "drop"
    row = loaded.execute("SELECT * FROM evaluations WHERE item_id=?", (target,)).fetchone()
    res = ev.Result(row["item_id"], "project", row["name"], row["score"], row["decision"], {}, row["reason"])
    assert ev.effective(res, ev.get_overrides(loaded)[target]) == ("keep", "override")


def test_overridden_keep_project_counts_for_skills(loaded):
    res = {r.item_id: r for r in ev.evaluate(loaded, FixedJudge(1), RUBRIC, NOW)}
    gamma = loaded.execute("SELECT id FROM projects WHERE name='gamma'").fetchone()[0]
    assert res["s:go"].decision == "drop"  # Go only appears in gamma, which is dropped
    ev.set_override(loaded, f"p{gamma}", "keep")
    res = {r.item_id: r for r in ev.evaluate(loaded, FixedJudge(1), RUBRIC, NOW)}
    assert res["s:go"].breakdown["inputs"]["projects_kept"] == 1


def test_override_validation(loaded):
    ev.evaluate(loaded, FixedJudge(3), RUBRIC, NOW)
    with pytest.raises(ev.OverrideError, match="keep or drop"):
        ev.set_override(loaded, "p1", "maybe")
    with pytest.raises(ev.OverrideError, match="no evaluated item"):
        ev.set_override(loaded, "p9999", "keep")
    ev.set_override(loaded, "alpha", "drop")
    assert ev.clear_override(loaded, "alpha") == "p1" and ev.get_overrides(loaded) == {}


runner = CliRunner()


def test_cli_evaluate_override_explain(tmp_path, fake, monkeypatch):
    dbp = str(tmp_path / "c.db")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(cli, "make_client", lambda: github.GitHubClient(transport=fake, sleep=lambda s: None))
    runner.invoke(cli.app, ["--db", dbp, "ingest", "github", USER])
    r = runner.invoke(cli.app, ["--db", dbp, "evaluate"])
    assert r.exit_code == 0, r.output
    assert "PROJECTS" in r.output and "SKILLS" in r.output and "heuristic" in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "override", "p4", "keep", "--note", "mine"])
    assert r.exit_code == 0 and "override saved" in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "evaluate", "--show", "projects"])
    line = next(l for l in r.output.splitlines() if l.startswith("p4 "))
    assert "KEEP*" in line
    r = runner.invoke(cli.app, ["--db", dbp, "explain", "p4"])
    assert "override" in r.output and "depth" in r.output and "x" in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "override", "p4", "--clear"])
    assert "cleared" in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "evaluate", "--judge", "llm"])
    assert r.exit_code == 1 and "ANTHROPIC_API_KEY" in r.output
