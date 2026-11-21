from typer.testing import CliRunner

from resumeforge import cli
from resumeforge.ingest.github import GitHubClient

from .conftest import USER

runner = CliRunner()


def test_ingest_then_list(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(cli, "make_client", lambda: GitHubClient(transport=fake, sleep=lambda s: None))
    dbp = str(tmp_path / "c.db")
    r = runner.invoke(cli.app, ["--db", dbp, "ingest", "github", USER])
    assert r.exit_code == 0, r.output
    assert '"imported": 4' in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "projects"])
    assert r.exit_code == 0
    assert "alpha" in r.output and "https://alpha.dev" in r.output and "4 project(s)" in r.output
    assert "forked" not in r.output
    r = runner.invoke(cli.app, ["--db", dbp, "projects", "--json"])
    assert '"commit_count": 3' in r.output


def test_projects_empty_db(tmp_path):
    r = runner.invoke(cli.app, ["--db", str(tmp_path / "e.db"), "projects"])
    assert "No projects" in r.output
