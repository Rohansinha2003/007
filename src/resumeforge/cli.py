"""resumeforge CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import typer

from . import db, evaluate as ev, extract as ex, llm, resume as rs
from .ingest import claude_code, codex, github

app = typer.Typer(add_completion=False, help="Evidence-based resume builder. No evidence, no claim.")

DBOpt = typer.Option(Path(os.environ.get("RESUMEFORGE_DB", "resumeforge.db")), "--db", help="SQLite file")


def _jd(path: Optional[Path]) -> Optional[str]:
    return path.read_text() if path else None


@app.command()
def ingest(
    db_path: Path = DBOpt,
    use_github: bool = typer.Option(True, "--github/--no-github"),
    use_claude: bool = typer.Option(True, "--claude/--no-claude"),
    use_codex: bool = typer.Option(True, "--codex/--no-codex"),
    claude_root: Path = typer.Option(claude_code.DEFAULT_ROOT),
    codex_root: Path = typer.Option(codex.DEFAULT_ROOT),
):
    """Read work history into the evidence table (redacted)."""
    conn = db.connect(db_path)
    if use_github:
        try:
            typer.echo(f"github: {github.ingest(conn)} evidence rows")
            db.set_meta(conn, "profile", github.profile())
        except (RuntimeError, FileNotFoundError) as e:
            typer.echo(f"github: skipped ({e})", err=True)
    if use_claude and claude_root.exists():
        typer.echo(f"claude_code: {claude_code.ingest(conn, claude_root)} evidence rows")
    if use_codex and codex_root.exists():
        typer.echo(f"codex: {codex.ingest(conn, codex_root)} evidence rows")
    _status(conn)


@app.command()
def extract(
    db_path: Path = DBOpt,
    offline: bool = typer.Option(False, help="Keyword heuristics instead of the LLM"),
    project: list[str] = typer.Option(None, help="Limit to these projects"),
):
    """Extract projects and evidence-linked skills."""
    conn = db.connect(db_path)
    try:
        stats = ex.extract_all(conn, offline=offline, only=project or None)
    except llm.LLMError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    for k, n in stats.items():
        typer.echo(f"{k}: {n} skills")


@app.command()
def evaluate(db_path: Path = DBOpt, jd: Optional[Path] = typer.Option(None, help="Job description text file")):
    """Score skills against evidence, recency, market demand and an optional job description."""
    conn = db.connect(db_path)
    for r in ev.evaluate(conn, _jd(jd)):
        mark = "+" if r["include"] else "-"
        typer.echo(f"{mark} {r['score']:.2f}  {r['name']:<28} {r['rationale']}")


@app.command()
def generate(
    db_path: Path = DBOpt,
    jd: Optional[Path] = typer.Option(None, help="Job description text file"),
    offline: bool = typer.Option(False, help="Template bullets instead of the LLM"),
    projects: int = typer.Option(4, help="Number of projects"),
    out: Path = typer.Option(Path("out")),
    name: Optional[str] = typer.Option(None, help="Override display name"),
):
    """Write resume.md (with [E#] citations), resume.clean.md and resume.evidence.md."""
    conn = db.connect(db_path)
    jd_text = _jd(jd)
    ev.evaluate(conn, jd_text)
    profile = db.get_meta(conn, "profile", {"name": "Your Name", "url": "", "location": None})
    if name:
        profile["name"] = name
    try:
        res = rs.generate(conn, profile=profile, jd=jd_text, offline=offline, n_projects=projects, out_dir=out)
    except llm.LLMError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(res, indent=2))


def _status(conn):
    for r in conn.execute("SELECT source, kind, COUNT(*) n FROM evidence GROUP BY 1,2 ORDER BY 1,2"):
        typer.echo(f"  {r['source']:<12} {r['kind']:<14} {r['n']}")


@app.command()
def status(db_path: Path = DBOpt):
    """Show evidence, project and skill counts."""
    conn = db.connect(db_path)
    _status(conn)
    p = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    s = conn.execute("SELECT COUNT(*), SUM(include) FROM skills").fetchone()
    typer.echo(f"projects: {p}  skills: {s[0]} (included: {s[1] or 0})")


if __name__ == "__main__":
    app()
