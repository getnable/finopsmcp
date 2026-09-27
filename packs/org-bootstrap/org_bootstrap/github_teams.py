# SPDX-License-Identifier: Apache-2.0
"""adapter github-teams: teams, members and repo ownership from GitHub.

Reads, with the pack's own GITHUB_TOKEN (a fine-grained token with read
access to the organization's members, or a classic token with read:org), for
the organization in GITHUB_ORG:

    GET /orgs/{org}/teams                    every team (slug, name, parent)
    GET /orgs/{org}/teams/{slug}/members     each team's members
    GET /orgs/{org}/teams/{slug}/repos       the repos it has access to, and
                                             its permission on each

GITHUB_API_URL points it at GitHub Enterprise Server (https://<host>/api/v3);
that host must be in the pack's declared network too. Only api.github.com is
declared as shipped.

What it proposes (every one a proposal a person confirms):

  team:<slug>                     name, parent team and members (as @login)
  repo_path:<host>/<org>/<repo>//.
                                  owned by the team with admin on it (0.6),
                                  else maintain (0.5); several teams at the
                                  top permission lower it by 0.15 and the
                                  rest are named in co_owners. Write, triage
                                  and read access propose nothing: access is
                                  not ownership. Archived repos are skipped.

The token goes in the Authorization header and nowhere else. It never
reaches a fact, a source or the log.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

from . import web

DEFAULT_API = "https://api.github.com"
API_VERSION_HEADER = {"X-GitHub-Api-Version": "2022-11-28"}
ACCEPT = "application/vnd.github+json"
MAX_PAGES = 20
MAX_TEAMS = 300
_ORG = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")


def _pages(url: str, token: str, log) -> list[Any]:
    out: list[Any] = []
    base = url
    for _ in range(MAX_PAGES):
        data, headers = web.get_json(url, token, accept=ACCEPT, extra_headers=API_VERSION_HEADER)
        if not isinstance(data, list):
            raise web.HttpError(f"{urllib.parse.urlsplit(url).path}: expected a list")
        out.extend(data)
        nxt = web.next_link(headers, base)
        if not nxt:
            return out
        url = nxt
    log(f"github-teams: stopped after {MAX_PAGES} pages of {urllib.parse.urlsplit(base).path}")
    return out


def web_host(api_base: str) -> str:
    """The host repo slugs use: github.com for api.github.com, else the
    Enterprise Server's own host."""
    host = (urllib.parse.urlsplit(api_base).hostname or "").lower()
    return "github.com" if host == "api.github.com" else host


def repo_owner_facts(repo_teams: dict[str, list[tuple[int, str]]], host: str, org: str
                     ) -> list[dict[str, Any]]:
    out = []
    for full, entries in sorted(repo_teams.items()):
        best = max(rank for rank, _ in entries)
        top = sorted({slug for rank, slug in entries if rank == best})
        rest = sorted({slug for rank, slug in entries if rank < best} - set(top))
        value: dict[str, Any] = {"team": top[0]}
        if top[1:] or rest:
            value["co_owners"] = top[1:] + rest
        conf = (0.6 if best == 2 else 0.5) - (0.15 if len(top) > 1 else 0.0)
        perm = "admin" if best == 2 else "maintain"
        out.append({"fact": "owner", "subject": f"repo_path:{host}/{full}//.", "value": value,
                    "source": f"github-teams:{org}/{top[0]}:{perm}", "confidence": conf})
    return out


def propose(ctx: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
    token = ctx.secret("GITHUB_TOKEN")
    org = (ctx.secret("GITHUB_ORG") or "").strip()
    if not token or not org:
        ctx.log("github-teams: GITHUB_TOKEN and GITHUB_ORG are not both set (nable pack secret "
                "set io.github.getnable/org-bootstrap GITHUB_TOKEN), so GitHub was not read")
        return []
    if not _ORG.match(org):
        ctx.log("github-teams: GITHUB_ORG is not a GitHub organization name; GitHub was not read")
        return []
    base = (ctx.secret("GITHUB_API_URL") or DEFAULT_API).strip().rstrip("/")
    why = web.url_problem(ctx, base)
    if why:
        ctx.log(f"github-teams: the GitHub API URL {why}; GitHub was not read")
        return []
    host = web_host(base)
    facts: list[dict[str, Any]] = []
    repo_teams: dict[str, list[tuple[int, str]]] = {}
    teams = _pages(f"{base}/orgs/{org}/teams?per_page=100", token, ctx.log)
    for team in teams[:MAX_TEAMS]:
        slug = team.get("slug") if isinstance(team, dict) else None
        if not isinstance(slug, str) or not _SLUG.match(slug):
            continue
        members = _pages(f"{base}/orgs/{org}/teams/{slug}/members?per_page=100", token, ctx.log)
        people = sorted({f"@{m['login']}" for m in members if isinstance(m, dict)
                         and isinstance(m.get("login"), str) and _LOGIN.match(m["login"])})
        value: dict[str, Any] = {"name": str(team.get("name") or slug)[:200]}
        parent = team.get("parent")
        if isinstance(parent, dict) and isinstance(parent.get("slug"), str) \
                and _SLUG.match(parent["slug"]):
            value["parent"] = parent["slug"]
        if people:
            value["people"] = people
        facts.append({"fact": "team", "subject": {"kind": "team", "id": slug}, "value": value,
                      "source": f"github-teams:{org}/{slug}", "confidence": 0.7})
        for repo in _pages(f"{base}/orgs/{org}/teams/{slug}/repos?per_page=100", token,
                           ctx.log):
            if not isinstance(repo, dict) or repo.get("archived"):
                continue
            full = str(repo.get("full_name") or "").lower()
            if not _REPO.match(full):
                continue
            perms = repo.get("permissions") if isinstance(repo.get("permissions"), dict) else {}
            role = repo.get("role_name")
            rank = 2 if perms.get("admin") or role == "admin" else \
                1 if perms.get("maintain") or role == "maintain" else 0
            if rank:
                repo_teams.setdefault(full, []).append((rank, slug))
    if len(teams) > MAX_TEAMS:
        ctx.log(f"github-teams: read the first {MAX_TEAMS} of {len(teams)} teams")
    return facts + repo_owner_facts(repo_teams, host, org)
