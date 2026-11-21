"""GitHub importer over the REST API (stdlib HTTP, token optional via GITHUB_TOKEN).

Per non-fork public repo: one project row plus evidence rows for repo metadata, languages,
README, topics, commit stats by the user, and deployment. Re-running updates in place.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .. import db

API = "https://api.github.com"
SOURCE = "github"
README_CHARS = 8000


class GitHubError(RuntimeError):
    def __init__(self, status: int, url: str, msg: str = ""):
        super().__init__(f"GitHub {status} for {url} {msg}".strip())
        self.status = status


class NotFound(GitHubError):
    pass


class RateLimitError(GitHubError):
    pass


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        return json.loads(self.body or b"null")


Transport = Callable[[str, dict[str, str]], Response]


def urllib_transport(url: str, headers: dict[str, str]) -> Response:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b"")


def _links(header: str | None) -> dict[str, str]:
    return {m.group(2): m.group(1) for m in re.finditer(r'<([^>]+)>;\s*rel="([^"]+)"', header or "")}


@dataclass
class GitHubClient:
    token: str | None = None
    transport: Transport = urllib_transport
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    max_wait: float = 900.0          # longest rate-limit wait we accept before giving up
    retries: int = 4
    on_wait: Callable[[float, str], None] = lambda s, why: None
    calls: int = field(default=0, init=False)

    def _headers(self, accept: str) -> dict[str, str]:
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "resume-builder"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _rate_wait(self, r: Response) -> float | None:
        """Seconds to wait if this response is a rate limit, else None."""
        if r.status not in (403, 429):
            return None
        if "retry-after" in r.headers:
            return float(r.headers["retry-after"])
        if r.headers.get("x-ratelimit-remaining") == "0":
            return max(float(r.headers.get("x-ratelimit-reset", 0)) - self.clock(), 0) + 1
        if b"rate limit" in r.body.lower():
            return 60.0
        return None

    def get(self, path_or_url: str, params: dict | None = None,
            accept: str = "application/vnd.github+json") -> Response:
        url = path_or_url if path_or_url.startswith("http") else API + path_or_url
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        for attempt in range(self.retries + 1):
            self.calls += 1
            try:
                r = self.transport(url, self._headers(accept))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt == self.retries:
                    raise GitHubError(0, url, f"network error: {e}") from e
                self.sleep(2 ** attempt)
                continue
            wait = self._rate_wait(r)
            if wait is not None:
                if wait > self.max_wait or attempt == self.retries:
                    raise RateLimitError(r.status, url, f"rate limited; reset in {wait:.0f}s. "
                                         "Set GITHUB_TOKEN for 5000 requests/hour.")
                self.on_wait(wait, "rate limit")
                self.sleep(wait)
                continue
            if r.status >= 500 and attempt < self.retries:
                self.sleep(2 ** attempt)
                continue
            if r.status == 404:
                raise NotFound(404, url)
            if r.status >= 400:
                raise GitHubError(r.status, url)
            return r
        raise GitHubError(0, url, "retries exhausted")

    def paginate(self, path: str, params: dict | None = None) -> list:
        out, r = [], self.get(path, {"per_page": 100, **(params or {})})
        while True:
            out.extend(r.json())
            nxt = _links(r.headers.get("link")).get("next")
            if not nxt:
                return out
            r = self.get(nxt)


# ---------- per-repo facts ----------

def commit_stats(gh: GitHubClient, owner: str, repo: str, user: str) -> tuple[int, str | None, str | None]:
    """(count, first_at, last_at) of commits authored by `user` on the default branch.

    Uses per_page=1 so the `last` page number equals the count, then fetches that page for the oldest commit.
    """
    path = f"/repos/{owner}/{repo}/commits"
    try:
        r = gh.get(path, {"author": user, "per_page": 1})
    except GitHubError as e:
        if e.status == 409:  # empty repository
            return 0, None, None
        raise
    items = r.json() or []
    if not items:
        return 0, None, None
    last_at = items[0]["commit"]["author"]["date"]
    last_link = _links(r.headers.get("link")).get("last")
    if not last_link:
        return 1, last_at, last_at
    count = int(urllib.parse.parse_qs(urllib.parse.urlparse(last_link).query)["page"][0])
    oldest = gh.get(last_link).json()
    return count, oldest[0]["commit"]["author"]["date"], last_at


def deployment(gh: GitHubClient, owner: str, repo: dict) -> tuple[bool, str | None, str]:
    """(has_deployment, url, how). Checks homepage, GitHub Pages, then the deployments API."""
    home = (repo.get("homepage") or "").strip()
    if home and urllib.parse.urlparse(home if "//" in home else "//" + home).hostname not in ("github.com", "www.github.com"):
        return True, home, "homepage field"  # a link back to GitHub is not a deployment
    name = repo["name"]
    if repo.get("has_pages"):
        try:
            return True, gh.get(f"/repos/{owner}/{name}/pages").json().get("html_url"), "GitHub Pages"
        except NotFound:
            pass
    try:
        deps = gh.get(f"/repos/{owner}/{name}/deployments", {"per_page": 1}).json()
    except NotFound:
        deps = []
    if not deps:
        return False, None, ""
    url = None
    try:
        statuses = gh.get(deps[0]["statuses_url"], {"per_page": 1}).json()
        if statuses:
            url = statuses[0].get("environment_url") or None
    except GitHubError:
        pass
    return True, url, f"deployments API (environment: {deps[0].get('environment')})"


def readme(gh: GitHubClient, owner: str, name: str) -> tuple[str, str] | None:
    try:
        meta = gh.get(f"/repos/{owner}/{name}/readme").json()
        text = gh.get(f"/repos/{owner}/{name}/readme", accept="application/vnd.github.raw+json").body
    except NotFound:
        return None
    return text.decode("utf-8", "replace"), meta.get("html_url") or f"{API}/repos/{owner}/{name}/readme"


# ---------- import ----------

def import_repo(conn, gh: GitHubClient, user: str, repo: dict) -> int:
    owner, name, url = repo["owner"]["login"], repo["name"], repo["html_url"]
    languages = gh.get(f"/repos/{owner}/{name}/languages").json() or {}
    count, first_at, last_at = commit_stats(gh, owner, name, user)
    has_dep, dep_url, dep_how = deployment(gh, owner, repo)
    rd = readme(gh, owner, name)
    topics = repo.get("topics") or []

    pid = db.upsert_project(
        conn, source=SOURCE, external_id=str(repo["id"]), name=name, url=url,
        description=repo.get("description"), primary_language=repo.get("language"),
        languages=languages, topics=topics, stars=repo.get("stargazers_count", 0),
        commit_count=count, first_commit_at=first_at, last_commit_at=last_at,
        deployed_url=dep_url, has_deployment=int(has_dep),
    )
    ev = lambda kind, ref, snippet, at=None: db.upsert_evidence(  # noqa: E731
        conn, project_id=pid, source=SOURCE, kind=kind, url_or_path=ref, snippet=snippet,
        created_at=at or repo.get("pushed_at"))

    kept = [ev("repo", url, f"{name}: {repo.get('description') or '(no description)'}. "
               f"Stars: {repo.get('stargazers_count', 0)}. Created {repo.get('created_at')}.",
               repo.get("created_at"))]
    if languages:
        total = sum(languages.values()) or 1
        lang_id = ev("languages", f"{url}#languages",
                     "Languages by bytes: " + ", ".join(f"{k} {v * 100 / total:.0f}%" for k, v in languages.items()))
        kept.append(lang_id)
        conn.execute("DELETE FROM skills_observed WHERE evidence_id=?", (lang_id,))
        for lang in languages:
            db.observe_skill(conn, skill=lang, evidence_id=lang_id, kind="language")
    if rd:
        kept.append(ev("readme", rd[1], rd[0][:README_CHARS]))
    if topics:
        kept.append(ev("topics", f"{url}#topics", "Topics: " + ", ".join(topics)))
    if count:
        kept.append(ev("commits", f"{url}/commits?author={user}",
                       f"{count} commit(s) by {user} on the default branch, {first_at} to {last_at}.", last_at))
    if has_dep:
        kept.append(ev("deployment", dep_url or f"{url}/deployments",
                       f"Deployed ({dep_how})" + (f" at {dep_url}" if dep_url else "") + "."))
    # Drop evidence this repo no longer has (e.g. README deleted), so re-runs mirror GitHub.
    conn.execute(f"DELETE FROM evidence WHERE project_id=? AND source=? AND id NOT IN ({','.join('?' * len(kept))})",
                 (pid, SOURCE, *kept))
    conn.commit()
    return pid


def ingest(conn, gh: GitHubClient, user: str, on_repo: Callable[[str], None] = lambda n: None) -> dict:
    repos = gh.paginate(f"/users/{user}/repos", {"type": "owner", "sort": "pushed"})
    own = [r for r in repos if not r.get("fork")]
    for r in own:
        on_repo(r["name"])
        import_repo(conn, gh, user, r)
    db.set_setting(conn, "github_username", user)
    db.set_setting(conn, "github_last_ingest_at", db.now())
    conn.commit()
    return {"repos_seen": len(repos), "imported": len(own), "forks_skipped": len(repos) - len(own),
            "api_calls": gh.calls}
