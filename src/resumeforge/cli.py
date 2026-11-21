"""`resume` CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from . import db, extract as ex, llm as llm_mod
from .skills import key as skill_key
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
def extract(
    ctx: typer.Context,
    project: list[str] = typer.Option(None, "--project", "-p", help="Only these project names (repeatable)"),
    limit: int = typer.Option(0, help="Stop after N projects (0 = all)"),
    min_evidence: int = typer.Option(2, help="Skip projects with fewer evidence rows"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Build prompts and estimate cost; no LLM calls"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Ignore cached LLM results"),
    show_prompt: bool = typer.Option(False, help="With --dry-run, print each prompt"),
):
    """Extract summary, technologies, skills, outcome and authorship per project with the LLM."""
    conn = db.connect(ctx.obj["db"])
    ids = ex.candidate_projects(conn, min_evidence, project or None)
    if limit:
        ids = ids[:limit]
    if not ids:
        typer.echo("No projects with enough evidence. Run the ingest commands first.")
        raise typer.Exit(1)
    if dry_run:
        price = llm_mod.PRICES.get(llm_mod.MODEL, (0, 0, 0))
        total_in = 0
        for pid in ids:
            pack = ex.build_pack(conn, pid)
            est = (len(ex.SYSTEM) + len(pack.prompt)) // 3  # rough: ~3 chars per token for mixed code/prose
            total_in += est
            typer.echo(f"{pack.name[:34]:<34} {len(pack.evidence_ids):>4} evidence  ~{est:>6} input tokens")
            if show_prompt:
                typer.echo(pack.prompt + "\n" + "-" * 80)
        cost = (total_in * price[0] + len(ids) * 1500 * price[1]) / 1e6
        typer.echo(f"{len(ids)} projects, ~{total_in} input tokens, est. ${cost:.2f} with {llm_mod.MODEL} "
                   f"(assumes ~1500 output tokens each)")
        return
    client = llm_mod.LLM(conn)
    run_start = conn.execute("SELECT COALESCE(MAX(id), 0) FROM llm_calls").fetchone()[0]
    failed = 0
    for pid in ids:
        try:
            r = ex.extract_project(conn, client, pid, use_cache=not no_cache)
        except llm_mod.LLMError as e:
            if "not set" in str(e):
                typer.echo(f"error: {e} Use --dry-run to check the prompts without calling the API.", err=True)
                raise typer.Exit(1)
            failed += 1
            typer.echo(f"  {pid}: failed: {e}", err=True)
            continue
        typer.echo(f"  {r['project'][:34]:<34} tech {r['technologies']:>2}  skills {r['skills']:>2}  "
                   f"rejected {r['rejected']:>2}  {r['outcome'] or '-':<10} {r['authorship']}")
    u = conn.execute("""SELECT COUNT(*) calls, SUM(cached) hits, SUM(input_tokens) i, SUM(output_tokens) o,
                               SUM(cost_usd) c FROM llm_calls WHERE id > ?""", (run_start,)).fetchone()
    typer.echo(f"{len(ids) - failed} extracted, {failed} failed. LLM: {u['calls']} calls, {u['hits'] or 0} cached, "
               f"{u['i'] or 0} in / {u['o'] or 0} out tokens, ${u['c'] or 0:.4f}")


@app.command()
def skills(
    ctx: typer.Context,
    min_projects: int = typer.Option(1, help="Hide skills seen in fewer projects"),
    kind: str = typer.Option(None, help="language, technology or skill"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Skills with their count of projects and evidence rows."""
    conn = db.connect(ctx.obj["db"])
    rows = conn.execute("SELECT skill, project_id, evidence_id, kind, origin FROM skills_observed"
                        + (" WHERE kind=?" if kind else ""), (kind,) if kind else ()).fetchall()
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(skill_key(r["skill"]), {"skill": r["skill"], "projects": set(), "evidence": set(),
                                                      "kinds": set(), "origins": set()})
        g["projects"].add(r["project_id"])
        g["evidence"].add(r["evidence_id"])
        g["kinds"].add(r["kind"])
        g["origins"].add(r["origin"])
    out = sorted(({"skill": g["skill"], "projects": len(g["projects"]), "evidence": len(g["evidence"]),
                   "kinds": sorted(g["kinds"]), "origins": sorted(g["origins"])}
                  for g in groups.values() if len(g["projects"]) >= min_projects),
                 key=lambda x: (-x["projects"], -x["evidence"], x["skill"].lower()))
    if as_json:
        typer.echo(json.dumps(out, indent=2))
        return
    if not out:
        typer.echo("No skills yet. Run: resume ingest github <user>, then resume extract")
        return
    typer.echo(f"{'skill':<30} {'projects':>8} {'evidence':>8}  kind")
    for x in out:
        typer.echo(f"{x['skill'][:30]:<30} {x['projects']:>8} {x['evidence']:>8}  {','.join(x['kinds'])}")
    typer.echo(f"{len(out)} skill(s)")


@app.command("llm-usage")
def llm_usage(ctx: typer.Context):
    """Token and cost totals from the LLM call log."""
    conn = db.connect(ctx.obj["db"])
    rows = conn.execute("""SELECT purpose, model, COUNT(*) calls, SUM(cached) cached, SUM(1-ok) failed,
                                  SUM(input_tokens) i, SUM(output_tokens) o, SUM(cost_usd) c
                           FROM llm_calls GROUP BY purpose, model""").fetchall()
    if not rows:
        typer.echo("No LLM calls logged.")
        return
    for r in rows:
        typer.echo(f"{r['purpose'] or '-':<18} {r['model']:<18} calls {r['calls']:>4}  cached {r['cached']:>4}  "
                   f"failed {r['failed']:>3}  in {r['i']:>8}  out {r['o']:>7}  ${r['c'] or 0:.4f}")


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
