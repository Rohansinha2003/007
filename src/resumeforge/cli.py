"""`resume` CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from . import db
from .ingest import github

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Evidence-based resume builder. No evidence, no claim.")
ingest_app = typer.Typer(no_args_is_help=True, help="Import work history into the database.")
app.add_typer(ingest_app, name="ingest")

DB_PATH = Path(os.environ.get("RESUME_DB", "resume.db"))


@app.callback()
def main(ctx: typer.Context, db_path: Path = typer.Option(DB_PATH, "--db", help="SQLite file (env RESUME_DB)")):
    ctx.obj = {"db": db_path}


def make_client() -> github.GitHubClient:
    """Factory so tests can swap in a mocked transport."""
    return github.GitHubClient(
        token=os.environ.get("GITHUB_TOKEN") or None,
        on_wait=lambda s, why: typer.echo(f"  {why}: waiting {s:.0f}s", err=True),
    )


@ingest_app.command("github")
def ingest_github(ctx: typer.Context, username: str):
    """Import a user's non-fork public repos. Safe to re-run."""
    conn = db.connect(ctx.obj["db"])
    gh = make_client()
    if not gh.token:
        typer.echo("No GITHUB_TOKEN set: unauthenticated limit is 60 requests/hour.", err=True)
    try:
        stats = github.ingest(conn, gh, username, on_repo=lambda n: typer.echo(f"  {n}"))
    except github.RateLimitError as e:
        typer.echo(f"error: {e}\nRe-run later; finished repos are saved.", err=True)
        raise typer.Exit(2)
    except github.GitHubError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(stats))


@app.command()
def projects(ctx: typer.Context, as_json: bool = typer.Option(False, "--json")):
    """List imported projects."""
    rows = db.list_projects(db.connect(ctx.obj["db"]))
    if as_json:
        typer.echo(json.dumps([dict(r) for r in rows], indent=2))
        return
    if not rows:
        typer.echo("No projects. Run: resume ingest github <username>")
        return
    typer.echo(f"{'name':<34} {'lang':<12} {'★':>3} {'commits':>7}  {'first':<10}  {'last':<10}  {'ev':>3}  deployed")
    for r in rows:
        dep = r["deployed_url"] or ("yes" if r["has_deployment"] else "")
        typer.echo(f"{r['name'][:34]:<34} {(r['primary_language'] or '-')[:12]:<12} {r['stars']:>3} "
                   f"{r['commit_count'] or 0:>7}  {(r['first_commit_at'] or '-')[:10]:<10}  "
                   f"{(r['last_commit_at'] or '-')[:10]:<10}  {r['evidence_count']:>3}  {dep}")
    typer.echo(f"{len(rows)} project(s)")


if __name__ == "__main__":
    app()
