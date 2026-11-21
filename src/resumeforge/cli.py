"""`resume` CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from . import db
from .ingest import antigravity, claude_code, codex, common, exports, github

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


@ingest_app.command("logs")
def ingest_logs(
    ctx: typer.Context,
    claude_root: Path = typer.Option(claude_code.DEFAULT_ROOT, help="Claude Code projects folder"),
    codex_root: Path = typer.Option(codex.DEFAULT_ROOT, help="Codex home folder"),
    antigravity_root: Path = typer.Option(antigravity.DEFAULT_ROOT, help="Antigravity data folder"),
):
    """Import AI coding-tool sessions (Claude Code, Codex, Antigravity). Safe to re-run."""
    conn = db.connect(ctx.obj["db"])
    plans = [
        ("claude_code", claude_root, lambda: [claude_code.parse(p) for p in claude_code.find(claude_root)]),
        ("codex", codex_root, lambda: [codex.parse(p, codex.titles(codex_root)) for p in codex.find(codex_root)]),
        ("antigravity", antigravity_root,
         lambda: [s for p in antigravity.find(antigravity_root) for s in antigravity.parse_db(p)]),
    ]
    for name, root, run in plans:
        if not Path(root).exists():
            typer.echo(f"{name:<12} not found at {root}")
            continue
        try:
            sessions = [x for x in run() if x]
        except Exception as e:  # one broken source must not stop the others
            typer.echo(f"{name:<12} failed: {type(e).__name__}: {e}", err=True)
            continue
        for x in sessions:
            common.store(conn, x)
        conn.commit()
        linked = sum(1 for x in sessions if conn.execute(
            "SELECT project_id FROM sessions WHERE source=? AND external_id=?", (x.source, x.external_id)).fetchone()[0])
        typer.echo(f"{name:<12} {len(sessions)} sessions from {root} ({linked} linked to projects)")


@ingest_app.command("export")
def ingest_export(ctx: typer.Context, file: Path = typer.Argument(..., exists=True, dir_okay=False)):
    """Import a ChatGPT or Claude.ai data export (.zip or conversations.json)."""
    conn = db.connect(ctx.obj["db"])
    try:
        fmt, sessions, skipped = exports.parse(file)
    except (exports.ExportError, ValueError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    for x in sessions:
        common.store(conn, x)
    conn.commit()
    msgs = sum(len(x.messages) for x in sessions)
    typer.echo(f"{fmt}: {len(sessions)} conversations, {msgs} messages" + (f", {skipped} skipped" if skipped else ""))


@app.command()
def sessions(ctx: typer.Context, stats: bool = typer.Option(False, "--stats", help="Counts per source"),
             source: str = typer.Option(None, help="Only this source"), limit: int = 30):
    """List imported sessions and chats, or show counts per source."""
    conn = db.connect(ctx.obj["db"])
    if stats:
        rows = conn.execute(
            """SELECT s.source, s.kind, COUNT(*) n, SUM(s.project_id IS NOT NULL) linked,
                      SUM(s.user_message_count) um, SUM(s.assistant_message_count) am,
                      MIN(s.started_at) first, MAX(COALESCE(s.ended_at, s.started_at)) last,
                      (SELECT COUNT(*) FROM evidence e WHERE e.source=s.source AND e.session_id IS NOT NULL) ev,
                      (SELECT COUNT(*) FROM evidence e WHERE e.source=s.source AND e.snippet LIKE '%[REDACTED%') red
               FROM sessions s GROUP BY s.source ORDER BY n DESC""").fetchall()
        if not rows:
            typer.echo("No sessions. Run: resume ingest logs   or   resume ingest export <file>")
            return
        typer.echo(f"{'source':<12} {'kind':<7} {'sessions':>8} {'linked':>6} {'user msgs':>9} {'asst msgs':>9} "
                   f"{'evidence':>8} {'redacted':>8}  {'first':<10}  {'last':<10}")
        for r in rows:
            typer.echo(f"{r['source']:<12} {r['kind']:<7} {r['n']:>8} {r['linked']:>6} {r['um']:>9} {r['am']:>9} "
                       f"{r['ev']:>8} {r['red']:>8}  {(r['first'] or '-')[:10]:<10}  {(r['last'] or '-')[:10]:<10}")
        return
    q = ("SELECT s.*, p.name project FROM sessions s LEFT JOIN projects p ON p.id=s.project_id "
         + ("WHERE s.source=? " if source else "") + "ORDER BY COALESCE(s.ended_at, s.started_at) DESC LIMIT ?")
    for r in conn.execute(q, (source, limit) if source else (limit,)):
        files = json.loads(r["files_touched"] or "{}")
        typer.echo(f"{(r['ended_at'] or r['started_at'] or '-')[:10]}  {r['source']:<11} "
                   f"{(r['title'] or '(untitled)')[:44]:<44} msgs {r['user_message_count']:>3}/{r['assistant_message_count']:<3} "
                   f"files {len(files.get('edited', [])):>3}  -> {r['project'] or '-'}")


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
