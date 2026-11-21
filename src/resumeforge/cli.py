"""`resume` CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path

import typer

import sys

from . import db, evaluator as ev, extract as ex, llm as llm_mod, market as mk
from .skills import canonical, key as skill_key
from .ingest import antigravity, claude_code, codex, common, exports, github

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Evidence-based resume builder. No evidence, no claim.")
ingest_app = typer.Typer(no_args_is_help=True, help="Import work history into the database.")
app.add_typer(ingest_app, name="ingest")
market_app = typer.Typer(no_args_is_help=True, help="Job postings you collect, and the skill demand they show.")
app.add_typer(market_app, name="market")

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
        name = canonical(r["skill"])
        g = groups.setdefault(skill_key(name), {"skill": name, "projects": set(), "evidence": set(),
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


def _cells(b: dict, criteria) -> str:
    return " ".join(f"{b[c]['value']:>4.2f}" if c in b else f"{'-':>4}" for c in criteria)


def _saved_role(conn) -> str | None:
    """The role used last time, if postings for it still exist."""
    r = db.get_setting(conn, "target_role")
    return r if r and conn.execute("SELECT 1 FROM postings WHERE role=? LIMIT 1", (r,)).fetchone() else None


DECISION_MARK = {"keep": "KEEP", "rewrite": "REWRITE", "drop": "drop", "needs_evidence": "NEEDS EVIDENCE"}


def _row_decision(r, overrides):
    res = ev.Result(r["item_id"], r["item_type"], r["name"], r["score"], r["decision"], {}, r["reason"])
    d, note = ev.effective(res, overrides.get(r["item_id"]))
    return DECISION_MARK[d] + ("*" if note == "override" else ""), note


@app.command()
def evaluate(
    ctx: typer.Context,
    judge: str = typer.Option("auto", help="Depth judge: auto, llm or heuristic"),
    rubric: Path = typer.Option(None, help="Rubric YAML (default: the packaged rubric.yaml)"),
    show: str = typer.Option("all", help="all, projects or skills"),
    as_json: bool = typer.Option(False, "--json"),
    width: int = typer.Option(70, help="Reason column width"),
    role: str = typer.Option(None, help="Target role for the demand criterion (default: last used, or the only role)"),
):
    """Score projects and skills with the rubric and print keep / rewrite / drop decisions."""
    conn = db.connect(ctx.obj["db"])
    try:
        role = mk.resolve_role(conn, role or _saved_role(conn))
    except mk.MarketError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    if role is None:
        typer.echo("No job postings: scoring without the demand criterion (see `resume market add`).", err=True)
    try:
        rb = ev.load_rubric(rubric)
    except (ev.RubricError, KeyError, OSError) as e:
        typer.echo(f"error: rubric: {e}", err=True)
        raise typer.Exit(1)
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if judge == "llm" and not has_key:
        typer.echo("error: --judge llm needs ANTHROPIC_API_KEY", err=True)
        raise typer.Exit(1)
    if judge == "auto":
        judge = "llm" if has_key else "heuristic"
        if not has_key:
            typer.echo("No ANTHROPIC_API_KEY: depth is rated by the heuristic judge.", err=True)
    if judge not in ("llm", "heuristic"):
        typer.echo("error: --judge must be auto, llm or heuristic", err=True)
        raise typer.Exit(1)
    j = ev.LLMJudge(llm_mod.LLM(conn)) if judge == "llm" else ev.HeuristicJudge()
    if not conn.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
        typer.echo("No projects. Run: resume ingest github <username>")
        raise typer.Exit(1)
    ev.evaluate(conn, j, rb, role=role)
    if role:
        db.set_setting(conn, "target_role", role)
        conn.commit()
    overrides = ev.get_overrides(conn)
    rows = conn.execute("SELECT * FROM evaluations ORDER BY item_type, score DESC").fetchall()
    if as_json:
        out = []
        for r in rows:
            d, note = _row_decision(r, overrides)
            out.append({**dict(r), "breakdown": json.loads(r["breakdown"]), "effective": d.rstrip("*"), "override_note": note})
        typer.echo(json.dumps(out, indent=2))
        return
    if show in ("all", "projects"):
        typer.echo(f"PROJECTS  (rubric {rb['hash']}, judge {judge}, role {role or 'none'}; * = your override)")
        typer.echo(f"{'id':<5} {'project':<28} {'score':>5}  {'dep':>4} {'orig':>4} {'evid':>4} {'ship':>4} {'rec':>4} "
                   f"{'dem':>4}  {'decision':<9} reason")
        for r in rows:
            if r["item_type"] != "project":
                continue
            b = json.loads(r["breakdown"])
            d, _ = _row_decision(r, overrides)
            cells = _cells(b, ev.PROJECT_CRITERIA)
            typer.echo(f"{r['item_id']:<5} {r['name'][:28]:<28} {r['score']:>5.2f}  {cells}  {d:<9} {r['reason'][:width]}")
    if show in ("all", "skills"):
        typer.echo(f"\nSKILLS")
        typer.echo(f"{'id':<22} {'skill':<20} {'score':>5}  {'brd':>4} {'dep':>4} {'rec':>4} {'evid':>4} {'dem':>4}  "
                   f"{'decision':<9} reason")
        for r in rows:
            if r["item_type"] != "skill":
                continue
            b = json.loads(r["breakdown"])
            d, note = _row_decision(r, overrides)
            cells = _cells(b, ev.SKILL_CRITERIA)
            reason = r["reason"] + (f" ({note})" if note and note != "override" else "")
            typer.echo(f"{r['item_id'][:22]:<22} {r['name'][:20]:<20} {r['score']:>5.2f}  {cells}  {d:<9} {reason[:width]}")
    counts = {}
    for r in rows:
        d, _ = _row_decision(r, overrides)
        counts.setdefault(r["item_type"], {}).setdefault(d.rstrip("*"), 0)
        counts[r["item_type"]][d.rstrip("*")] += 1
    typer.echo("\n" + "  ".join(f"{t}s: " + ", ".join(f"{v} {k.lower()}" for k, v in sorted(c.items())) for t, c in counts.items()))


@app.command()
def override(
    ctx: typer.Context,
    item: str = typer.Argument(..., help="Id from `resume evaluate` (p12, s:react) or an exact name"),
    decision: str = typer.Argument(None, help="keep or drop"),
    clear: bool = typer.Option(False, "--clear", help="Remove your override"),
    note: str = typer.Option(None, help="Why (optional)"),
):
    """Override a decision. Overrides persist and later `evaluate` runs never change them."""
    conn = db.connect(ctx.obj["db"])
    try:
        if clear:
            typer.echo(f"cleared override for {ev.clear_override(conn, item)}")
            return
        if decision is None:
            raise ev.OverrideError("give keep or drop, or --clear")
        item_id = ev.set_override(conn, item, decision.lower(), note)
    except ev.OverrideError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{item_id}: {decision.lower()} (override saved)")


@app.command()
def explain(ctx: typer.Context, item: str = typer.Argument(..., help="Id from `resume evaluate` or an exact name")):
    """Show the full score breakdown for one project or skill."""
    conn = db.connect(ctx.obj["db"])
    try:
        item_id = ev.resolve_id(conn, item)
    except ev.OverrideError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    r = conn.execute("SELECT * FROM evaluations WHERE item_id=?", (item_id,)).fetchone()
    b = json.loads(r["breakdown"])
    d, note = _row_decision(r, ev.get_overrides(conn))
    typer.echo(f"{r['item_id']}  {r['name']}  ({r['item_type']})")
    typer.echo(f"decision: {d.rstrip('*')}" + (f"  [{note}; computed: {r['decision']}]" if note else "")
               + f"   score {r['score']:.3f}   rubric {r['rubric_hash']}" + (f"   judge {r['judge']}" if r["judge"] else ""))
    for c, v in b.items():
        if isinstance(v, dict) and "weight" in v:
            typer.echo(f"  {c:<12} {v['value']:.3f} x {v['weight']:.3f} = {v['points']:.3f}")
    if "rule" in b:
        typer.echo(f"  rule: {b['rule']}")
    typer.echo("inputs: " + json.dumps(b.get("inputs", {})))
    typer.echo(f"reason: {r['reason']}")


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


def _posting_extractor(conn, name: str):
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if name == "llm" and not has_key:
        typer.echo("error: --extractor llm needs ANTHROPIC_API_KEY", err=True)
        raise typer.Exit(1)
    if name == "auto":
        name = "llm" if has_key else "keywords"
        if not has_key:
            typer.echo("No ANTHROPIC_API_KEY: skills are extracted by keyword matching on the alias table.", err=True)
    if name not in ("llm", "keywords"):
        typer.echo("error: --extractor must be auto, llm or keywords", err=True)
        raise typer.Exit(1)
    return mk.LLMExtractor(llm_mod.LLM(conn)) if name == "llm" else mk.KeywordExtractor()


def _print_extracted(p, clean, rejected, used):
    req = [c["skill"] for c in clean if c["requirement"] == "required"]
    pref = [c["skill"] for c in clean if c["requirement"] == "preferred"]
    typer.echo(f"  #{p['id']} {(p['title'] or '')[:40]:<40} required {len(req):>2}, preferred {len(pref):>2}"
               + (f", rejected {len(rejected)}" if rejected else "") + (f"  [{used}]" if used not in ("llm", "keywords") else ""))


@market_app.command("add")
def market_add(
    ctx: typer.Context,
    source: str = typer.Argument(..., help="A .txt/.md/.csv file, a folder of them, '-' to paste on stdin, or the text itself"),
    role: str = typer.Option(..., "--role", help="Role label for these postings, e.g. 'ai engineer'"),
    extractor: str = typer.Option("auto", help="Skill extractor: auto, llm or keywords"),
    no_extract: bool = typer.Option(False, "--no-extract", help="Store only; extract later with `resume market extract`"),
):
    """Add job postings under a role label. CSV columns: title, company, location, description."""
    conn = db.connect(ctx.obj["db"])
    x = None if no_extract else _posting_extractor(conn, extractor)  # fail before storing anything
    try:
        postings, skipped = mk.read_inputs(source, sys.stdin.read() if source == "-" else None)
        added, dupes = mk.add_postings(conn, role, postings)
    except mk.MarketError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    for s_ in skipped:
        typer.echo(f"  skipped: {s_}", err=True)
    r = mk.norm_role(role)
    typer.echo(f"{r}: {len(added)} added, {dupes} already stored")
    if added and not no_extract:
        stats = mk.extract_postings(conn, x, r, on_posting=_print_extracted)
        typer.echo(json.dumps(stats))


@market_app.command("extract")
def market_extract(
    ctx: typer.Context,
    role: str = typer.Option(None, help="Only this role"),
    extractor: str = typer.Option("auto", help="auto, llm or keywords"),
    force: bool = typer.Option(False, "--force", help="Re-extract postings already done"),
):
    """Extract required and preferred skills from stored postings."""
    conn = db.connect(ctx.obj["db"])
    stats = mk.extract_postings(conn, _posting_extractor(conn, extractor), role, force=force, on_posting=_print_extracted)
    typer.echo(json.dumps(stats))


@market_app.command("list")
def market_list(ctx: typer.Context, role: str = typer.Option(None, help="Show this role's postings")):
    """Roles with posting counts, or one role's postings."""
    conn = db.connect(ctx.obj["db"])
    if not role:
        rows = mk.roles(conn)
        if not rows:
            typer.echo("No postings. Run: resume market add <file> --role <name>")
            return
        for r in rows:
            typer.echo(f"{r['role']:<30} {r['postings']:>4} postings  {r['extracted']:>4} extracted  {r['extractors'] or '-'}")
        return
    for p in conn.execute("""SELECT p.id, p.title, p.company, p.location, p.extractor, COUNT(ps.skill_key) n
                             FROM postings p LEFT JOIN posting_skills ps ON ps.posting_id = p.id
                             WHERE p.role=? GROUP BY p.id ORDER BY p.id""", (mk.norm_role(role),)):
        typer.echo(f"#{p['id']:<4} {(p['title'] or '-')[:36]:<36} {(p['company'] or '-')[:20]:<20} "
                   f"{(p['location'] or '-')[:16]:<16} {p['n']:>3} skills  {p['extractor'] or 'not extracted'}")


@market_app.command("remove")
def market_remove(ctx: typer.Context, posting_id: int = typer.Argument(None), role: str = typer.Option(None, help="Remove every posting for this role")):
    """Remove one posting by id, or all postings for a role."""
    conn = db.connect(ctx.obj["db"])
    if posting_id is None and not role:
        typer.echo("error: give a posting id or --role", err=True)
        raise typer.Exit(1)
    cur = (conn.execute("DELETE FROM postings WHERE id=?", (posting_id,)) if posting_id is not None
           else conn.execute("DELETE FROM postings WHERE role=?", (mk.norm_role(role),)))
    conn.commit()
    typer.echo(f"removed {cur.rowcount} posting(s)")


@market_app.command("demand")
def market_demand(
    ctx: typer.Context,
    role: str = typer.Option(None, help="Role (default: the only role on file)"),
    top: int = typer.Option(40, help="Rows to show"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Demand per skill for a role: share of postings, required vs nice-to-have."""
    conn = db.connect(ctx.obj["db"])
    r = _role_or_exit(conn, role)
    dem = mk.demand(conn, r, ev.load_rubric()["market"])
    if as_json:
        typer.echo(json.dumps([d.as_dict() for d in dem.values()], indent=2))
        return
    total = next(iter(dem.values())).total if dem else 0
    typer.echo(f"DEMAND  role {r}  ({total} postings)")
    typer.echo(f"{'skill':<24} {'postings':>8} {'freq':>5} {'req':>4} {'pref':>4}  {'flag':<12} {'demand':>6}")
    for d in list(dem.values())[:top]:
        typer.echo(f"{d.skill[:24]:<24} {d.postings:>8} {d.frequency:>5.0%} {d.required:>4} {d.preferred:>4}  "
                   f"{d.flag:<12} {d.score:>6.2f}")


def _role_or_exit(conn, role):
    try:
        r = mk.resolve_role(conn, role)
    except mk.MarketError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    if r is None:
        typer.echo("error: no postings. Run: resume market add <file> --role <name>", err=True)
        raise typer.Exit(1)
    return r


@app.command()
def gap(
    ctx: typer.Context,
    role: str = typer.Option(None, help="Role (default: the only role on file)"),
    top: int = typer.Option(25, help="Rows per section"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Compare your evidenced skills with a role's demand."""
    conn = db.connect(ctx.obj["db"])
    r = _role_or_exit(conn, role)
    g = mk.gap(conn, r, ev.load_rubric()["market"])
    if as_json:
        typer.echo(json.dumps(g, indent=2))
        return
    pending = g["postings"] - (g["extracted"] or 0)
    typer.echo(f"GAP REPORT  role {g['role']}  ({g['postings']} postings, extractor {g['extractors'] or '-'})"
               + (f"  [{pending} not extracted: run `resume market extract`]" if pending else ""))
    typer.echo(f"Your evidence covers {g['coverage']:.0%} of this role's demand.\n")
    def demand_cells(x):
        return (f"  {x['skill'][:24]:<24} {str(x['postings']) + '/' + str(x['total']):>8} {x['frequency']:>5.0%} "
                f"{x['required']:>4} {x['preferred']:>4}  {x['flag']:<12} {x['score']:>6.2f}")

    hdr = f"  {'skill':<24} {'postings':>8} {'freq':>5} {'req':>4} {'pref':>4}  {'flag':<12} {'demand':>6}"
    typer.echo(f"(a) In demand, and you have evidence  ({len(g['have'])})")
    typer.echo(hdr + f"  {'projects':>8} {'evidence':>8}  evaluation")
    for x in g["have"][:top]:
        typer.echo(demand_cells(x) + f"  {x['projects']:>8} {x['evidence']:>8}  {x['evaluation'] or '-'}")
    typer.echo(f"\n(b) In demand, no evidence yet  ({len(g['missing'])})")
    typer.echo(hdr + "  note")
    for x in g["missing"][:top]:
        typer.echo(demand_cells(x) + f"  {x['note']}")
    if len(g["missing"]) > top:
        typer.echo(f"  ... {len(g['missing']) - top} more (--top)")
    typer.echo(f"\n(c) You have evidence, no posting asks for it  ({len(g['unused'])})")
    typer.echo(f"  {'skill':<24} {'projects':>8} {'evidence':>8}  {'kind':<20} evaluation")
    for x in g["unused"][:top]:
        typer.echo(f"  {x['skill'][:24]:<24} {x['projects']:>8} {x['evidence']:>8}  {','.join(x['kinds'])[:20]:<20} "
                   f"{x['evaluation'] or '-'}")


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
