"""Git blame/log helpers for cost-to-code blame.

Given a Terraform file and the line of a resource's sizing attribute, resolve the
commit that last changed that line, the sizing value at the parent commit (the
revert target), and — when a GitHub repo + token are configured — the pull request
behind that commit. All git access goes through remediation.rightsizing_pr._git
(argv list, no shell, guarded refs). This module never writes to the repo.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from ..remediation.rightsizing_pr import _git

log = logging.getLogger(__name__)

_NULL_SHA = "0" * 40


@dataclass
class CommitInfo:
    sha: str
    author: str
    author_email: str
    authored_date: str | None   # ISO-8601
    summary: str


def _epoch_to_iso(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


def _parse_porcelain(out: str) -> CommitInfo | None:
    """Parse `git blame --porcelain` output for a single line. Returns None when
    the line is uncommitted (all-zero sha) — a working-tree edit not yet committed."""
    lines = out.splitlines()
    if not lines:
        return None
    header = lines[0].split()
    if not header:
        return None
    sha = header[0]
    if sha == _NULL_SHA:
        return None
    fields: dict[str, str] = {}
    for ln in lines[1:]:
        if ln.startswith("\t"):
            break  # the source line; header block done
        key, _, value = ln.partition(" ")
        fields[key] = value
    return CommitInfo(
        sha=sha,
        author=fields.get("author", ""),
        author_email=fields.get("author-mail", "").strip("<>"),
        authored_date=_epoch_to_iso(fields.get("author-time")),
        summary=fields.get("summary", ""),
    )


def blame_sizing_commit(tf_dir: str, file_path: str, line: int) -> CommitInfo | None:
    """Return the commit that last changed `line` of `file_path`. None if the line
    is uncommitted. Raises RuntimeError (from _git) if tf_dir is not a git repo or
    the path is untracked."""
    rel = os.path.relpath(file_path, tf_dir)
    out = _git(tf_dir, "blame", "--porcelain", "-L", f"{line},{line}", "--", rel)
    return _parse_porcelain(out)


def previous_sizing_value(
    tf_dir: str, sha: str, file_path: str, resource_type: str, resource_name: str
) -> str | None:
    """Return the resource's sizing value at the commit BEFORE `sha` (the revert
    target), or None if the parent has no such file/block (the commit introduced
    the resource)."""
    from ..tagging.hcl_patcher import extract_sizing_value

    rel = os.path.relpath(file_path, tf_dir)
    try:
        content = _git(tf_dir, "show", f"{sha}^:./{rel}")
    except RuntimeError as exc:
        log.debug("no parent revision for %s at %s^: %s", rel, sha, exc)
        return None
    return extract_sizing_value(content, resource_type, resource_name)


def resolve_pr_for_commit(github_repo: str, sha: str, *, token: str | None = None) -> dict | None:
    """Resolve the PR associated with a commit via GitHub
    `GET /repos/{repo}/commits/{sha}/pulls`, which returns the PR for a squash- or
    rebase-merged commit as well as a merge commit. Returns dict with
    number/url/title/author/merged_at, or None when there is no token, no associated
    PR, or the call fails."""
    from ..integrations.ticketing import _env, _http_with_retry

    resolved_token = token or _env("GITHUB_TOKEN")
    if not resolved_token or not github_repo:
        return None
    try:
        r = _http_with_retry(
            "GET",
            f"https://api.github.com/repos/{github_repo}/commits/{sha}/pulls",
            headers={
                "Authorization": f"Bearer {resolved_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15,
        )
        data = r.json()
    except Exception as exc:
        log.debug("commit->PR lookup failed for %s: %s", sha, exc)
        return None
    if not data:
        return None
    pr = data[0]
    return {
        "number": pr.get("number"),
        "url": pr.get("html_url"),
        "title": pr.get("title"),
        "author": (pr.get("user") or {}).get("login"),
        "merged_at": pr.get("merged_at"),
    }
