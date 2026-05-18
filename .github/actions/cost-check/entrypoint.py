"""
nable GitHub Action entrypoint.

Fetches the PR diff from the GitHub API, runs cost estimation via the
finops.pr_comments module, and posts/updates a comment on the PR.
"""
from __future__ import annotations

import os
import sys
import httpx

# ── Environment ────────────────────────────────────────────────────────────────

GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
REPO          = os.environ.get("GH_REPO", "")
PR_NUMBER_STR = os.environ.get("GH_PR_NUMBER", "")
REGION        = os.environ.get("PRICING_REGION", "us-east-1")
THRESHOLD     = float(os.environ.get("PR_COST_THRESHOLD_USD", "10"))

if not REPO or not PR_NUMBER_STR:
    print("Not running inside a pull_request event — skipping cost check.")
    sys.exit(0)

PR_NUMBER = int(PR_NUMBER_STR)


# ── GitHub API helpers ─────────────────────────────────────────────────────────

def _gh_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_pr_files() -> list[dict]:
    files, page = [], 1
    while True:
        resp = httpx.get(
            f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}/files",
            headers=_gh_headers(),
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return files


COMMENT_TAG = "<!-- nable-cost-comment -->"


def post_or_update_comment(body: str) -> None:
    comments_url = f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments"
    resp = httpx.get(comments_url, headers=_gh_headers())
    resp.raise_for_status()

    existing_id: int | None = None
    for comment in resp.json():
        if COMMENT_TAG in comment.get("body", ""):
            existing_id = comment["id"]
            break

    full_body = f"{COMMENT_TAG}\n{body}"

    if existing_id:
        httpx.patch(
            f"https://api.github.com/repos/{REPO}/issues/comments/{existing_id}",
            headers=_gh_headers(),
            json={"body": full_body},
        ).raise_for_status()
        print(f"Updated nable cost comment on PR #{PR_NUMBER}")
    else:
        httpx.post(comments_url, headers=_gh_headers(), json={"body": full_body}).raise_for_status()
        print(f"Posted nable cost comment on PR #{PR_NUMBER}")


# ── Infra file detection ───────────────────────────────────────────────────────

_INFRA_EXTENSIONS = {".tf", ".yaml", ".yml", ".json", ".ts"}
_INFRA_KEYWORDS   = {
    "terraform", "cloudformation", "cdk", "helm", "values",
    "k8s", "kubernetes", "infra", "deploy", "stack",
}


def is_infra_file(filename: str) -> bool:
    lower = filename.lower()
    ext_ok  = any(lower.endswith(ext) for ext in _INFRA_EXTENSIONS)
    name_ok = any(kw in lower for kw in _INFRA_KEYWORDS)
    tf_ok   = lower.endswith(".tf")
    return tf_ok or (ext_ok and name_ok)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    from finops.pr_comments.parser    import parse_diff
    from finops.pr_comments.estimator import estimate_changes, format_pr_comment

    print(f"nable cost check — PR #{PR_NUMBER} in {REPO}")

    files = get_pr_files()
    print(f"  {len(files)} changed file(s) in PR")

    all_changes = []
    for f in files:
        filename = f.get("filename", "")
        patch    = f.get("patch", "")
        if not patch or not is_infra_file(filename):
            continue
        changes = parse_diff(patch, filename)
        if changes:
            print(f"  {filename}: {len(changes)} resource change(s)")
            all_changes.extend(changes)

    if not all_changes:
        print("No infrastructure resource changes detected — skipping comment.")
        return

    estimates = estimate_changes(all_changes, region=REGION)
    comment   = format_pr_comment(estimates, threshold_usd=THRESHOLD)

    if not comment:
        total = sum(e.monthly_usd for e in estimates)
        print(f"Cost impact ${abs(total):.0f}/mo is below ${THRESHOLD:.0f} threshold — skipping comment.")
        return

    if not GITHUB_TOKEN:
        print("No GITHUB_TOKEN — printing comment to stdout:\n")
        print(comment)
        return

    post_or_update_comment(comment)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"nable cost check error: {exc}", file=sys.stderr)
        # Don't fail the whole CI — cost check is advisory
        sys.exit(0)
