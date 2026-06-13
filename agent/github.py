"""Fetch a GitHub issue and its comments using the REST API.

No PyGithub or `gh` CLI dependency — `requests` is enough for two endpoints.
A GITHUB_TOKEN raises the rate limit from 60/hr to 5000/hr but is optional.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import requests


ISSUE_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<num>\d+)/?$"
)


@dataclass
class Issue:
    owner: str
    repo: str
    number: int
    title: str
    body: str
    state: str
    url: str
    comments: list[dict[str, Any]]

    @property
    def repo_slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    def render(self) -> str:
        """Format issue + comments as a single string for the LLM."""
        out = [
            f"Repository: {self.repo_slug}",
            f"Issue #{self.number}: {self.title}",
            f"State: {self.state}",
            f"URL: {self.url}",
            "",
            "--- BODY ---",
            (self.body or "(empty)").strip(),
        ]
        for i, c in enumerate(self.comments, 1):
            user = (c.get("user") or {}).get("login", "?")
            body = (c.get("body") or "").strip()
            out += ["", f"--- COMMENT {i} by @{user} ---", body]
        return "\n".join(out)


def parse_issue_url(url: str) -> tuple[str, str, int]:
    m = ISSUE_URL_RE.match(url.strip())
    if not m:
        raise ValueError(f"not a GitHub issue URL: {url!r}")
    return m["owner"], m["repo"], int(m["num"])


def _headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "agentic-go-contributor/0.1",
    }
    tok = os.getenv("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def fetch_issue(url: str) -> Issue:
    owner, repo, num = parse_issue_url(url)
    api = f"https://api.github.com/repos/{owner}/{repo}/issues/{num}"

    r = requests.get(api, headers=_headers(), timeout=30)
    r.raise_for_status()
    issue = r.json()

    comments: list[dict[str, Any]] = []
    if issue.get("comments", 0):
        cr = requests.get(api + "/comments", headers=_headers(), timeout=30)
        cr.raise_for_status()
        comments = cr.json()

    return Issue(
        owner=owner,
        repo=repo,
        number=num,
        title=issue.get("title") or "",
        body=issue.get("body") or "",
        state=issue.get("state") or "",
        url=issue.get("html_url") or url,
        comments=comments,
    )
