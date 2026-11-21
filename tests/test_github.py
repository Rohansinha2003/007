import json

import pytest

from resumeforge import db
from resumeforge.ingest import github
from resumeforge.ingest.github import Response

from .conftest import USER


def _projects(conn):
    return {r["name"]: r for r in db.list_projects(conn)}


def test_ingest_fills_projects(conn, client):
    stats = github.ingest(conn, client, USER)
    assert stats["imported"] == 4 and stats["forks_skipped"] == 1
    p = _projects(conn)
    assert set(p) == {"alpha", "beta", "empty", "gamma"}
    a = p["alpha"]
    assert a["commit_count"] == 3
    assert (a["first_commit_at"], a["last_commit_at"]) == ("2026-02-01T00:00:00Z", "2026-11-01T00:00:00Z")
    assert json.loads(a["languages"]) == {"Python": 900, "Shell": 100}
    assert json.loads(a["topics"]) == ["fastapi", "llm"] and a["stars"] == 5
    assert (a["has_deployment"], a["deployed_url"]) == (1, "https://alpha.dev")
    assert p["beta"]["commit_count"] == 1 and p["beta"]["first_commit_at"] == p["beta"]["last_commit_at"]
    assert p["beta"]["deployed_url"] == f"https://{USER}.github.io/beta/"
    assert p["gamma"]["deployed_url"] == "https://gamma.vercel.app" and p["gamma"]["commit_count"] == 0
    assert p["empty"]["commit_count"] == 0 and p["empty"]["has_deployment"] == 0
    assert db.get_setting(conn, "github_username") == USER


def test_evidence_rows_per_repo(conn, client):
    github.ingest(conn, client, USER)
    pid = _projects(conn)["alpha"]["id"]
    kinds = {r["kind"]: r for r in conn.execute("SELECT * FROM evidence WHERE project_id=?", (pid,))}
    assert set(kinds) == {"repo", "languages", "readme", "topics", "commits", "deployment"}
    assert "ghp_" not in kinds["readme"]["snippet"] and "FastAPI service" in kinds["readme"]["snippet"]
    assert kinds["readme"]["url_or_path"].endswith("/alpha/blob/main/README.md")
    skills = {(r["skill"], r["evidence_id"]) for r in conn.execute("SELECT * FROM skills_observed WHERE project_id=?", (pid,))}
    assert skills == {("Python", kinds["languages"]["id"]), ("Shell", kinds["languages"]["id"])}
    # beta has no topics; empty has no languages, commits or readme
    empty = _projects(conn)["empty"]["id"]
    assert [r[0] for r in conn.execute("SELECT kind FROM evidence WHERE project_id=?", (empty,))] == ["repo"]


def test_rerun_is_idempotent_and_updates(conn, client, fake):
    github.ingest(conn, client, USER)
    counts = lambda: tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
                           for t in ("projects", "evidence", "skills_observed"))
    before = counts()
    github.ingest(conn, client, USER)
    assert counts() == before
    # repo changes upstream: new star count, README deleted
    fake.repos[0]["stargazers_count"] = 42
    del fake.readmes["alpha"]
    github.ingest(conn, client, USER)
    a = _projects(conn)["alpha"]
    assert a["stars"] == 42
    assert conn.execute("SELECT COUNT(*) FROM evidence WHERE project_id=? AND kind='readme'", (a["id"],)).fetchone()[0] == 0
    assert counts()[0] == before[0]


def test_primary_rate_limit_waits_until_reset(client, fake):
    fake.queue = [Response(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"}, b"{}")]
    assert client.get(f"/repos/{USER}/alpha/languages").json() == {"Python": 900, "Shell": 100}
    assert client.sleeps == [31.0]


def test_secondary_rate_limit_uses_retry_after(client, fake):
    fake.queue = [Response(429, {"retry-after": "5"}, b"{}")]
    client.get(f"/repos/{USER}/alpha/languages")
    assert client.sleeps == [5.0]


def test_rate_limit_too_long_raises(client, fake):
    fake.queue = [Response(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "99999"}, b"{}")]
    with pytest.raises(github.RateLimitError):
        client.get(f"/repos/{USER}/alpha/languages")


def test_server_errors_retry_with_backoff(client, fake):
    fake.queue = [Response(502, {}, b""), Response(503, {}, b"")]
    client.get(f"/repos/{USER}/alpha/languages")
    assert client.sleeps == [1, 2]


def test_plain_403_is_not_treated_as_rate_limit(client, fake):
    fake.queue = [Response(403, {"x-ratelimit-remaining": "4000"}, b'{"message":"Forbidden"}')]
    with pytest.raises(github.GitHubError) as e:
        client.get(f"/repos/{USER}/alpha/languages")
    assert e.value.status == 403 and client.sleeps == []


def test_token_is_sent_when_set(fake):
    seen = {}
    gh = github.GitHubClient(token="t0k", transport=lambda u, h: seen.update(h) or fake(u, h))
    gh.get(f"/repos/{USER}/alpha/languages")
    assert seen["Authorization"] == "Bearer t0k"


def test_github_homepage_is_not_a_deployment(conn, client, fake):
    fake.repos[4]["homepage"] = "https://github.com/dev"
    fake.deployments = {}
    github.ingest(conn, client, USER)
    g = _projects(conn)["gamma"]
    assert (g["has_deployment"], g["deployed_url"]) == (0, None)


def test_projects_without_commits_sort_last(conn, client):
    github.ingest(conn, client, USER)
    names = [r["name"] for r in db.list_projects(conn)]
    assert names[0] == "alpha" and set(names[-2:]) == {"empty", "gamma"}
