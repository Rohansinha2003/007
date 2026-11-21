import json
import urllib.parse

import pytest

from resumeforge import db
from resumeforge.ingest.github import API, GitHubClient, Response

USER = "dev"


def repo(id_, name, **kw):
    base = {"id": id_, "name": name, "owner": {"login": USER}, "html_url": f"https://github.com/{USER}/{name}",
            "description": f"{name} description", "language": "Python", "topics": [], "stargazers_count": 0,
            "fork": False, "homepage": None, "has_pages": False, "created_at": "2026-01-01T00:00:00Z",
            "pushed_at": "2026-11-01T00:00:00Z"}
    return {**base, **kw}


class FakeGitHub:
    """Routes request URLs to canned responses. `routes` maps path (no query) -> callable(query) -> Response."""

    def __init__(self):
        self.repos = [
            repo(1, "alpha", topics=["fastapi", "llm"], stargazers_count=5, homepage="https://alpha.dev"),
            repo(2, "beta", language="TypeScript", has_pages=True),
            repo(3, "forked", fork=True),
            repo(4, "empty"),
            repo(5, "gamma"),
        ]
        self.readmes = {"alpha": "# Alpha\nFastAPI service. token ghp_" + "a" * 36, "beta": "# Beta\nA site."}
        self.languages = {"alpha": {"Python": 900, "Shell": 100}, "beta": {"TypeScript": 1000},
                          "empty": {}, "gamma": {"Go": 10}}
        # commits by USER, newest first
        self.commits = {"alpha": ["2026-11-01T00:00:00Z", "2026-06-01T00:00:00Z", "2026-02-01T00:00:00Z"],
                        "beta": ["2026-10-01T00:00:00Z"], "gamma": []}
        self.deployments = {"gamma": [{"environment": "Production",
                                       "statuses_url": f"{API}/repos/{USER}/gamma/deployments/9/statuses"}]}
        self.log: list[str] = []
        self.queue: list[Response] = []   # responses served before routing (e.g. rate limits)

    def __call__(self, url, headers):
        self.log.append(url)
        if self.queue:
            return self.queue.pop(0)
        u = urllib.parse.urlparse(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        path = u.path
        parts = path.strip("/").split("/")
        ok = lambda body, h=None: Response(200, h or {}, json.dumps(body).encode())  # noqa: E731
        if path == f"/users/{USER}/repos":
            page = int(q.get("page", 1))
            if page == 1:  # two pages to exercise pagination
                return ok(self.repos[:3], {"link": f'<{API}{path}?page=2>; rel="next"'})
            return ok(self.repos[3:])
        name = parts[2] if len(parts) > 2 else None
        tail = "/".join(parts[3:])
        if tail == "languages":
            return ok(self.languages[name])
        if tail == "readme":
            if name not in self.readmes:
                return Response(404, {}, b'{"message":"Not Found"}')
            if "raw" in headers["Accept"]:
                return Response(200, {}, self.readmes[name].encode())
            return ok({"html_url": f"https://github.com/{USER}/{name}/blob/main/README.md"})
        if tail == "commits":
            if name == "empty":
                return Response(409, {}, b'{"message":"Git Repository is empty."}')
            dates = self.commits[name]
            page = int(q.get("page", 1))
            items = [{"commit": {"author": {"date": d}}} for d in dates[page - 1:page]]
            h = {}
            if len(dates) > 1 and page == 1:
                h["link"] = (f'<{API}{path}?author={USER}&per_page=1&page=2>; rel="next", '
                             f'<{API}{path}?author={USER}&per_page=1&page={len(dates)}>; rel="last"')
            return ok(items, h)
        if tail == "pages":
            return ok({"html_url": f"https://{USER}.github.io/{name}/"})
        if tail == "deployments":
            return ok(self.deployments.get(name, []))
        if tail.startswith("deployments/") and tail.endswith("statuses"):
            return ok([{"environment_url": "https://gamma.vercel.app"}])
        return Response(404, {}, b"{}")


@pytest.fixture
def fake():
    return FakeGitHub()


@pytest.fixture
def client(fake):
    sleeps = []
    gh = GitHubClient(transport=fake, sleep=sleeps.append, clock=lambda: 1000.0)
    gh.sleeps = sleeps
    return gh


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")
