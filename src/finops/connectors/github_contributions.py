"""GitHub AI engineering attribution.

Not what AI cost, but what it did. Pulls merged pull requests (or commits, for
teams that push straight to main with no PRs) from connected repos, attributes
each to the model or agent that wrote it, sizes the change, and
(joined with LLM spend by model) answers the question that makes AI spend legible:
"Opus 4.8 was 49% of AI spend and shipped 10 PRs: 3 high, 5 medium, 2 low,
$X per PR."

Attribution signals, best first:
- Co-author trailers in the commits or PR body. Claude Code writes the exact model
  ("Co-Authored-By: Claude Opus 4.8 ..."), so Claude work resolves to the model.
- "Generated with <tool>" markers.
- Bot author logins (Copilot, Codex, Cursor, Devin show up as GitHub bot accounts),
  which resolve to the tool, not a specific model.
Everything with no AI signal is Human.

Honest limit: a precise model-by-model split only exists where the tool names the
model. Claude does; most others report at the tool level. We show what is known
and bucket the rest by tool. Reuses the GitHub connector's GITHUB_TOKEN /
GITHUB_ORGS. Read-only.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import date, timedelta
from typing import Any

import httpx

_API = "https://api.github.com"
_HUMAN = "Human"
_MAX_PRS = 120          # hard cap so a busy org can't make this unbounded
_CONCURRENCY = 8        # bounded parallel PR detail fetches (CE/perf lesson)

# Co-author trailer and "generated with" markers carry the agent/model name.
_CO_TRAILER = re.compile(r"co-?authored-by:\s*([^<\n]+?)\s*(?:<|$)", re.IGNORECASE | re.MULTILINE)
_GEN_MARKER = re.compile(r"generated with\s+\[?([A-Za-z0-9 .+-]+?)\]?[\s)(\n]", re.IGNORECASE)

# GitHub bot/app author logins that are AI coding agents (login minus the [bot]).
_AI_BOT_LOGINS = {
    "copilot-swe-agent": "GitHub Copilot",
    "copilot": "GitHub Copilot",
    "github-copilot": "GitHub Copilot",
    "devin-ai-integration": "Devin",
    "cursor": "Cursor",
    "cursoragent": "Cursor",
    "sweep-ai": "Sweep",
    "openai-codex": "OpenAI Codex",
    "codex": "OpenAI Codex",
    "chatgpt-codex-connector": "OpenAI Codex",
    "google-labs-jules": "Jules",
}


def _clean_login(login: str) -> str:
    return (login or "").lower().removesuffix("[bot]").strip()


def _normalize_agent(name: str) -> str | None:
    """Map a co-author / marker name to a normalized model or agent label, or None
    if it is not a recognized AI agent."""
    n = (name or "").strip()
    low = n.lower()
    if not low:
        return None
    if low.startswith("claude"):
        # "Claude Opus 4.8 (1M context)" -> "Claude Opus 4.8"; bare "Claude" stays.
        return re.split(r"\s*\(", n, 1)[0].strip() or "Claude"
    if "copilot" in low:
        return "GitHub Copilot"
    if "codex" in low:
        return "OpenAI Codex"
    if "cursor" in low:
        return "Cursor"
    if "devin" in low:
        return "Devin"
    if low.startswith(("gpt", "openai")):
        return "OpenAI Codex"
    if "gemini" in low or low.startswith("jules"):
        return "Gemini"
    return None


def attribute(*, author_login: str, author_is_bot: bool, text: str) -> str:
    """Attribute one PR to an AI model/agent, or Human. text is the PR body plus
    its commit messages (where trailers and 'Generated with' markers live)."""
    body = text or ""
    # 1. Co-author trailers, the most reliable signal. Prefer a model-named label
    #    (one with a digit, e.g. "Claude Opus 4.8") over a bare tool name.
    best: str | None = None
    for m in _CO_TRAILER.finditer(body):
        label = _normalize_agent(m.group(1))
        if label and (best is None or any(ch.isdigit() for ch in label)):
            best = label
    if best:
        return best
    # 2. "Generated with <tool>" markers.
    for m in _GEN_MARKER.finditer(body):
        label = _normalize_agent(m.group(1))
        if label:
            return label
    # 3. Bot author login.
    login = _clean_login(author_login)
    if login in _AI_BOT_LOGINS:
        return _AI_BOT_LOGINS[login]
    if author_is_bot:
        return "AI agent"
    return _HUMAN


def magnitude(lines_changed: int) -> str:
    if lines_changed >= 300:
        return "high"
    if lines_changed >= 30:
        return "medium"
    return "low"


def summarize(prs: list[dict]) -> dict:
    """Aggregate attributed PRs by label, with magnitude counts and a few examples.
    prs items: {label, magnitude, lines, repo, title, url}."""
    by_label: dict[str, dict] = {}
    for pr in prs:
        lbl = pr["label"]
        b = by_label.setdefault(lbl, {
            "label": lbl, "pr_count": 0, "high": 0, "medium": 0, "low": 0,
            "lines_changed": 0, "examples": [],
        })
        b["pr_count"] += 1
        b[pr["magnitude"]] += 1
        b["lines_changed"] += int(pr.get("lines", 0) or 0)
        if len(b["examples"]) < 5:
            b["examples"].append({
                "title": pr.get("title", ""), "magnitude": pr["magnitude"],
                "lines": pr.get("lines", 0), "url": pr.get("url", ""),
                "repo": pr.get("repo", ""),
            })
    total_all = sum(b["pr_count"] for b in by_label.values())
    total_ai = sum(b["pr_count"] for lbl, b in by_label.items() if lbl != _HUMAN)
    ordered = dict(sorted(by_label.items(), key=lambda kv: kv[1]["pr_count"], reverse=True))
    return {
        "by_label": ordered,
        "ai_pr_count": total_ai,
        "human_pr_count": total_all - total_ai,
        "total_pr_count": total_all,
        "ai_share_pct": round(100.0 * total_ai / total_all, 1) if total_all else 0.0,
    }


def _match_spend(label: str, by_model: dict) -> float | None:
    """Match an attribution label to LLM spend by loose model-name comparison, e.g.
    "Claude Opus 4.8" against a by_model key "claude-opus-4-8". Returns None when
    the label is a tool without a known model (Copilot, Codex)."""
    if not by_model:
        return None
    tokens = [t for t in re.split(r"[\s.\-_]+", label.lower()) if t and t != "claude"]
    if not tokens:
        return None
    for model, spend in by_model.items():
        haystack = re.sub(r"[\-_.]+", " ", str(model).lower())
        if all(t in haystack for t in tokens):
            try:
                return float(spend)
            except (TypeError, ValueError):
                return None
    return None


def join_llm_spend(summary: dict, by_model: dict | None, total_llm_spend: float | None) -> dict:
    """Attach % of AI spend and cost-per-PR per label where the model matches LLM
    spend. Spend fields stay None when the label is a tool with no model-level
    spend, so we never invent a number."""
    for lbl, b in summary["by_label"].items():
        if lbl == _HUMAN:
            continue
        spend = _match_spend(lbl, by_model or {})
        b["llm_spend_usd"] = round(spend, 2) if spend is not None else None
        b["spend_share_pct"] = (
            round(100.0 * spend / total_llm_spend, 1)
            if (spend is not None and total_llm_spend) else None
        )
        b["cost_per_pr_usd"] = (
            round(spend / b["pr_count"], 2) if (spend is not None and b["pr_count"]) else None
        )
    summary["total_llm_spend_usd"] = round(total_llm_spend, 2) if total_llm_spend else None
    return summary


# ── GitHub I/O ────────────────────────────────────────────────────────────────

def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _get(client: httpx.AsyncClient, token: str, path: str, params: dict | None = None) -> Any:
    r = await client.get(f"{_API}{path}", headers=_headers(token), params=params or {})
    if r.status_code in (404, 422):
        return None
    r.raise_for_status()
    return r.json()


async def _search_merged_prs(client, token, query: str, cap: int) -> list[dict]:
    items: list[dict] = []
    page = 1
    while len(items) < cap and page <= 10:
        data = await _get(client, token, "/search/issues", {
            "q": query, "per_page": 100, "page": page, "sort": "updated", "order": "desc",
        })
        batch = (data or {}).get("items", []) if isinstance(data, dict) else []
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return items[:cap]


async def _attribute_item(client, token, item: dict) -> dict | None:
    """Fetch a PR's diff size and commit trailers, then attribute it."""
    repo_url = item.get("repository_url", "")  # .../repos/{owner}/{repo}
    m = re.search(r"/repos/([^/]+/[^/]+)$", repo_url)
    number = item.get("number")
    if not m or number is None:
        return None
    full = m.group(1)
    detail = await _get(client, token, f"/repos/{full}/pulls/{number}")
    if not isinstance(detail, dict):
        return None
    commits = await _get(client, token, f"/repos/{full}/pulls/{number}/commits")
    msgs = ""
    if isinstance(commits, list):
        msgs = "\n".join(
            (c.get("commit", {}) or {}).get("message", "") for c in commits if isinstance(c, dict)
        )
    user = detail.get("user", {}) or {}
    lines = int(detail.get("additions", 0) or 0) + int(detail.get("deletions", 0) or 0)
    label = attribute(
        author_login=user.get("login", ""),
        author_is_bot=(user.get("type") == "Bot"),
        text=(detail.get("body") or "") + "\n" + msgs,
    )
    return {
        "label": label,
        "magnitude": magnitude(lines),
        "lines": lines,
        "repo": full,
        "title": detail.get("title", ""),
        "url": detail.get("html_url", ""),
        "merged_at": detail.get("merged_at", ""),
    }


async def fetch_ai_contributions(*, days: int = 30, repos: list[str] | None = None,
                                 max_prs: int = _MAX_PRS) -> dict:
    """Pull merged PRs from the configured orgs (or explicit repos) and attribute
    each. Returns {configured, prs, window_days}. Never raises; returns a clear
    not-configured payload when GITHUB_TOKEN/GITHUB_ORGS are absent."""
    token = os.getenv("GITHUB_TOKEN", "")
    orgs = [o.strip() for o in os.getenv("GITHUB_ORGS", "").split(",") if o.strip()]
    scopes = [f"repo:{r.strip()}" for r in (repos or []) if r.strip()] or [f"org:{o}" for o in orgs]
    if not token or not scopes:
        return {"configured": False, "reason": "Set GITHUB_TOKEN and GITHUB_ORGS (or pass repos)."}

    since = (date.today() - timedelta(days=max(1, days))).isoformat()
    per_scope = max(20, max_prs // len(scopes))
    async with httpx.AsyncClient(timeout=30) as client:
        searches = await asyncio.gather(*[
            _search_merged_prs(client, token, f"is:pr is:merged merged:>={since} {scope}", per_scope)
            for scope in scopes
        ], return_exceptions=True)
        items: list[dict] = []
        for res in searches:
            if isinstance(res, list):
                items.extend(res)
        items = items[:max_prs]

        sem = asyncio.Semaphore(_CONCURRENCY)

        async def _one(it):
            async with sem:
                try:
                    return await _attribute_item(client, token, it)
                except Exception:
                    return None

        attributed = await asyncio.gather(*[_one(it) for it in items])
    prs = [p for p in attributed if p]
    return {"configured": True, "prs": prs, "window_days": days, "pr_count": len(prs)}


async def _list_owner_repos(client, token, owner: str, cap: int = 30) -> list[str]:
    """Repo full-names for an org or user, most-recently-pushed first. Tries the
    org endpoint then the user endpoint, so it works for both."""
    for path in (f"/orgs/{owner}/repos", f"/users/{owner}/repos"):
        data = await _get(client, token, path,
                          {"per_page": min(100, cap), "sort": "pushed", "direction": "desc"})
        if isinstance(data, list) and data:
            return [r["full_name"] for r in data
                    if isinstance(r, dict) and r.get("full_name")][:cap]
    return []


# Commit sizing at scale: one GraphQL call returns 100 commits with their message
# (where the model trailer lives), diff size, and author. The REST path needs a
# detail fetch per commit, so it can't accurately count a busy repo's commits, and
# an undercount would overstate cost-per-commit. GraphQL counts every commit.
_GQL = "https://api.github.com/graphql"

_HISTORY_Q = """
query($owner:String!,$name:String!,$since:GitTimestamp!,$cursor:String){
  repository(owner:$owner,name:$name){
    defaultBranchRef{ target{ ... on Commit {
      history(since:$since, first:100, after:$cursor){
        pageInfo{ hasNextPage endCursor }
        nodes{ oid message additions deletions committedDate author{ user{ login } } }
      }
    }}}
  }
}
"""


async def _gql(client: httpx.AsyncClient, token: str, query: str, variables: dict) -> dict | None:
    r = await client.post(_GQL, headers=_headers(token), json={"query": query, "variables": variables})
    if r.status_code >= 400:
        return None
    body = r.json()
    return body.get("data") if isinstance(body, dict) else None


async def _repo_commit_items(client, token, full: str, since_iso: str,
                             max_commits: int, max_pages: int = 30) -> list[dict]:
    """All default-branch commits for one repo over the window, attributed and
    sized, via the GraphQL history (paginated 100 at a time)."""
    try:
        owner, name = full.split("/", 1)
    except ValueError:
        return []
    items: list[dict] = []
    cursor: str | None = None
    for _ in range(max_pages):
        if len(items) >= max_commits:
            break
        data = await _gql(client, token, _HISTORY_Q,
                          {"owner": owner, "name": name, "since": since_iso, "cursor": cursor})
        target = ((((data or {}).get("repository") or {}).get("defaultBranchRef") or {}).get("target") or {})
        hist = target.get("history")
        if not isinstance(hist, dict):
            break
        for n in hist.get("nodes", []):
            if not isinstance(n, dict):
                continue
            msg = n.get("message", "") or ""
            login = (((n.get("author") or {}).get("user") or {}).get("login")) or ""
            lines = int(n.get("additions", 0) or 0) + int(n.get("deletions", 0) or 0)
            items.append({
                "label": attribute(
                    author_login=login,
                    author_is_bot=(_clean_login(login) in _AI_BOT_LOGINS or login.endswith("[bot]")),
                    text=msg,
                ),
                "magnitude": magnitude(lines),
                "lines": lines,
                "repo": full,
                "title": msg.splitlines()[0] if msg else "",
                "url": f"https://github.com/{full}/commit/{n.get('oid', '')}",
                "committed_at": n.get("committedDate", ""),
            })
        page = hist.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return items[:max_commits]


async def fetch_ai_commits(*, days: int = 30, repos: list[str] | None = None,
                           max_commits: int = 2000) -> dict:
    """Pull every default-branch commit over the window and attribute each, for
    teams that commit straight to main instead of opening PRs. Same attribution
    and sizing as the PR path, so cost-per-commit divides spend by the true commit
    count. Returns {configured, commits, window_days}. Never raises."""
    token = os.getenv("GITHUB_TOKEN", "")
    orgs = [o.strip() for o in os.getenv("GITHUB_ORGS", "").split(",") if o.strip()]
    explicit = [r.strip() for r in (repos or []) if r.strip()]
    if not token or not (explicit or orgs):
        return {"configured": False, "reason": "Set GITHUB_TOKEN and GITHUB_ORGS (or pass repos)."}

    since_iso = (date.today() - timedelta(days=max(1, days))).isoformat() + "T00:00:00Z"
    async with httpx.AsyncClient(timeout=30) as client:
        targets = list(explicit)
        if not targets:
            listed = await asyncio.gather(
                *[_list_owner_repos(client, token, o) for o in orgs], return_exceptions=True)
            for res in listed:
                if isinstance(res, list):
                    targets.extend(res)
        if not targets:
            return {"configured": False,
                    "reason": "No repositories found for the connected GitHub account."}

        per_repo = max(200, max_commits // max(1, len(targets)))
        sem = asyncio.Semaphore(_CONCURRENCY)

        async def _one(full: str) -> list[dict]:
            async with sem:
                try:
                    return await _repo_commit_items(client, token, full, since_iso, per_repo)
                except Exception:
                    return []

        gathered = await asyncio.gather(*[_one(r) for r in targets])
    commits: list[dict] = []
    for res in gathered:
        commits.extend(res)
    commits = commits[:max_commits]
    return {"configured": True, "commits": commits, "window_days": days, "commit_count": len(commits)}


async def build_report(*, days: int = 30, repos: list[str] | None = None,
                       unit: str = "auto") -> dict:
    """The full report: fetch + attribute + aggregate, joined with LLM spend by
    model so each AI model carries its share of spend and a cost per unit.

    unit selects the unit of work: "pr" (merged pull requests), "commit" (commits
    on the default branch), or "auto" (PRs if the repo has any in the window, else
    commits, so PR-shops and commit-to-main shops both work). The chosen unit is
    returned in report["unit"]."""
    unit = (unit or "auto").lower()
    if unit not in ("auto", "pr", "commit"):
        unit = "auto"

    items: list[dict] | None = None
    chosen: str | None = None

    # Try PRs first unless commits are explicitly requested.
    if unit in ("auto", "pr"):
        prf = await fetch_ai_contributions(days=days, repos=repos)
        if prf.get("configured"):
            prs = prf.get("prs", [])
            if prs or unit == "pr":          # use PRs if any exist, or if forced
                items, chosen = prs, "pr"
        elif unit == "pr":
            return {"configured": False, "reason": prf.get("reason", ""),
                    "window_days": days, "unit": "pr"}

    # Fall back to commits: auto found no PRs, or commits were requested.
    if items is None:
        cf = await fetch_ai_commits(days=days, repos=repos)
        if not cf.get("configured"):
            return {"configured": False, "reason": cf.get("reason", ""),
                    "window_days": days, "unit": "commit"}
        items, chosen = cf.get("commits", []), "commit"

    report = summarize(items)
    report["window_days"] = days
    report["unit"] = chosen

    # Join LLM spend by model where we have it. Best-effort: the GitHub side is the
    # point, the spend join is the bonus that turns it into unit economics.
    by_model: dict = {}
    total_spend: float | None = None
    try:
        from ..connectors.llm_costs import get_all_llm_costs
        llm = await asyncio.to_thread(get_all_llm_costs, days=days)
        if isinstance(llm, dict):
            by_model = llm.get("by_model", {}) or {}
            total_spend = llm.get("total_usd", llm.get("total", None))
    except Exception:
        pass
    join_llm_spend(report, by_model, total_spend)
    return report
