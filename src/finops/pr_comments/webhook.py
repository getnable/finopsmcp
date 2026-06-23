"""
Webhook server for PR cost comments.

Receives GitHub PR events, fetches the diff, runs cost estimation,
and posts a comment if the estimated impact exceeds the threshold.

Run:
  finops-pr-webhook

The server listens on PORT (default 8080). Point your GitHub webhook
to: http://your-host:8080/webhook/github
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx

from .parser import parse_diff
from .estimator import estimate_changes, format_pr_comment

log = logging.getLogger(__name__)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
COST_THRESHOLD = float(os.getenv("PR_COST_THRESHOLD_USD", "10"))
COMMENT_TAG = "<!-- nable-cost-comment -->"
# A GitHub full_name is owner/repo, each segment limited to [A-Za-z0-9._-]. Used to
# validate the repo before it is interpolated into any api.github.com URL.
_GH_REPO = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def _verify_github_signature(payload: bytes, sig_header: str) -> bool:
    if not WEBHOOK_SECRET:
        log.error(
            "GITHUB_WEBHOOK_SECRET is not set — rejecting all webhook requests. "
            "Set this env var to enable the PR cost webhook."
        )
        return False  # fail closed: never skip verification
    expected = "sha256=" + hmac.HMAC(
        WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header or "")


def _github_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_pr_files(repo: str, pr_number: int) -> list[dict]:
    """Fetch list of changed files in a PR."""
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files"
    files = []
    page = 1
    while True:
        resp = httpx.get(url, headers=_github_headers(), params={"per_page": 100, "page": page})
        if not resp.is_success:
            log.warning("Failed to fetch PR files: %s", resp.text)
            break
        batch = resp.json()
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return files


def _post_or_update_comment(repo: str, pr_number: int, body: str) -> None:
    """Post a new comment or update the existing nable comment on the PR."""
    # Find existing comment
    comments_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    resp = httpx.get(comments_url, headers=_github_headers())
    existing_id: int | None = None
    if resp.is_success:
        for comment in resp.json():
            if COMMENT_TAG in comment.get("body", ""):
                existing_id = comment["id"]
                break

    full_body = f"{COMMENT_TAG}\n{body}"

    if existing_id:
        httpx.patch(
            f"https://api.github.com/repos/{repo}/issues/comments/{existing_id}",
            headers=_github_headers(),
            json={"body": full_body},
        )
        log.info("Updated nable comment on PR #%d", pr_number)
    else:
        httpx.post(
            comments_url,
            headers=_github_headers(),
            json={"body": full_body},
        )
        log.info("Posted nable comment on PR #%d", pr_number)


def _handle_pr_event(payload: dict) -> None:
    action = payload.get("action")
    if action not in ("opened", "synchronize", "reopened"):
        return

    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {}).get("full_name", "")
    pr_number = pr.get("number")

    if not repo or not pr_number:
        return

    # Validate path segments before they reach any api.github.com URL: full_name is
    # owner/repo (both [A-Za-z0-9._-]) and the PR number must be an int. Rejects
    # nothing real, neutralizes path injection (CodeQL py/partial-ssrf), and a
    # validated repo carries no CR/LF, so the log below cannot be forged either.
    if not _GH_REPO.match(repo):
        log.warning("Rejected webhook: invalid repository name")
        return
    try:
        pr_number = int(pr_number)
    except (TypeError, ValueError):
        return

    log.info("Processing PR #%d in %s", pr_number, repo)

    # Fetch changed files
    files = _get_pr_files(repo, pr_number)

    # Detect infra files and parse diffs
    from .parser import parse_diff as _parse
    all_changes = []
    for f in files:
        filename = f.get("filename", "")
        patch = f.get("patch", "")
        if not patch:
            continue
        # Only process infrastructure files
        if not any(filename.endswith(ext) for ext in (".tf", ".yaml", ".yml", ".json", ".ts", ".py")):
            continue
        if any(kw in filename.lower() for kw in ("terraform", "cloudformation", "cdk", "helm", "values", "k8s", "kubernetes", "infra", "deploy")):
            changes = _parse(patch, filename)
            all_changes.extend(changes)

    if not all_changes:
        log.info("No infrastructure changes detected in PR #%d", pr_number)
        return

    # Estimate costs
    estimates = estimate_changes(all_changes)
    comment = format_pr_comment(estimates, threshold_usd=COST_THRESHOLD)

    if not comment:
        log.info("Cost impact below threshold ($%.0f) for PR #%d", COST_THRESHOLD, pr_number)
        return

    _post_or_update_comment(repo, pr_number, comment)


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/webhook/github":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        payload_bytes = self.rfile.read(length)

        sig = self.headers.get("X-Hub-Signature-256", "")
        if not _verify_github_signature(payload_bytes, sig):
            log.warning("Invalid webhook signature")
            self.send_response(401)
            self.end_headers()
            return

        self.send_response(202)
        self.end_headers()

        event_type = self.headers.get("X-GitHub-Event", "")
        if event_type != "pull_request":
            return

        try:
            payload = json.loads(payload_bytes)
            _handle_pr_event(payload)
        except Exception as e:
            log.error("PR event handling failed: %s", e, exc_info=True)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info(fmt, *args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if not GITHUB_TOKEN:
        print("Error: GITHUB_TOKEN not set. PR comments won't be posted.")

    port = int(os.getenv("PR_WEBHOOK_PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    print(f"nable PR webhook listening on port {port}")
    print(f"  GitHub webhook URL: http://your-host:{port}/webhook/github")
    print(f"  Cost threshold: ${COST_THRESHOLD}/month")
    server.serve_forever()


if __name__ == "__main__":
    main()
