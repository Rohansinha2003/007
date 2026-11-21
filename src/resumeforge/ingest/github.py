"""GitHub ingest via the locally authenticated `gh` CLI (read-only API calls)."""
from __future__ import annotations

import base64
import json
import subprocess
from typing import Callable

from ..db import add_evidence

Runner = Callable[[str], object]


def gh_api(path: str):
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def ingest(conn, api: Runner = gh_api, max_repos: int = 50, max_commits: int = 30) -> int:
    user = api("user")
    login = user["login"]
    repos = api(f"user/repos?per_page=100&affiliation=owner&sort=pushed")
    n = 0
    for r in [r for r in repos if not r.get("fork")][:max_repos]:
        name, url = r["name"], r["html_url"]
        if name.lower() == login.lower():  # profile README repo
            continue
        topics = ", ".join(r.get("topics") or [])
        desc = (f"Repository {name}. Description: {r.get('description') or '(none)'}. "
                f"Primary language: {r.get('language') or 'unknown'}. Topics: {topics or '(none)'}. "
                f"Created {r.get('created_at')}, last pushed {r.get('pushed_at')}.")
        add_evidence(conn, source="github", kind="repo", project_name=name, ref=url, content=desc,
                     ts=r.get("pushed_at"), meta={"stars": r.get("stargazers_count", 0)})
        n += 1
        try:
            langs = api(f"repos/{login}/{name}/languages")
            if langs:
                total = sum(langs.values()) or 1
                txt = "Languages by bytes: " + ", ".join(f"{k} {v * 100 / total:.0f}%" for k, v in langs.items())
                add_evidence(conn, source="github", kind="languages", project_name=name,
                             ref=f"{url}#languages", content=txt, ts=r.get("pushed_at"), meta=langs)
                n += 1
        except RuntimeError:
            pass
        try:
            rd = api(f"repos/{login}/{name}/readme")
            text = base64.b64decode(rd.get("content", "")).decode("utf-8", "replace")
            add_evidence(conn, source="github", kind="readme", project_name=name,
                         ref=rd.get("html_url") or f"{url}#readme", content=text[:4000], ts=r.get("pushed_at"))
            n += 1
        except RuntimeError:
            pass  # no README
        try:
            commits = api(f"repos/{login}/{name}/commits?author={login}&per_page={max_commits}")
        except RuntimeError:
            commits = []
        for c in commits or []:
            msg = c["commit"]["message"].strip()
            if not msg:
                continue
            add_evidence(conn, source="github", kind="commit", project_name=name, ref=c["html_url"],
                         content=msg[:1000], ts=c["commit"]["author"]["date"])
            n += 1
    conn.commit()
    return n


def profile(api: Runner = gh_api) -> dict:
    u = api("user")
    return {"name": u.get("name") or u["login"], "login": u["login"], "url": u["html_url"],
            "location": u.get("location"), "bio": u.get("bio")}
