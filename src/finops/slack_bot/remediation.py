"""
Remediation from Slack, with a human approval gate.

The model never opens a PR or files a ticket directly. The flow is:

  1. Claude calls draft_rightsizing_pr or draft_ticket during a conversation.
  2. We run a dry-run (for PRs), store a pending_action row, and hand the bot
     a side effect: "post this approval card".
  3. A human clicks Approve in Slack. The handler re-checks RBAC (analyst+),
     then executes for real and posts the PR/ticket URL.

Pending actions expire after APPROVAL_TTL_HOURS so a stale card from last
week cannot open a PR against drifted Terraform.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

log = logging.getLogger(__name__)

APPROVAL_TTL_HOURS = 24
_DRAFT_MIN_ROLE = "analyst"


def role_can_draft(role: str) -> bool:
    from ..auth.rbac import ROLE_LEVEL

    return ROLE_LEVEL.get(role, 0) >= ROLE_LEVEL.get(_DRAFT_MIN_ROLE, 999)


# ── Draft tools exposed to the Claude loop (analyst+ only) ───────────────────

def draft_tool_schemas() -> list[dict]:
    return [
        {
            "name": "draft_rightsizing_pr",
            "description": (
                "Draft a Terraform rightsizing PR from open rightsizing recommendations. "
                "Runs a dry-run patch against the configured Terraform directory and posts "
                "an approval card in Slack. Nothing is pushed or opened until a human "
                "clicks Approve. Requires FINOPS_TF_DIR (and a GitHub repo) configured on "
                "the bot host. Use after the user asks to fix or right-size something."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "recommendation_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Specific recommendation IDs to include. Omit for all open recs.",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch name for the PR (default fix/rightsizing).",
                    },
                },
            },
        },
        {
            "name": "draft_ticket",
            "description": (
                "Draft a ticket in the configured tracker (Jira, Linear, or GitHub Issues). "
                "Posts an approval card in Slack. The ticket is only filed after a human "
                "clicks Approve. Write a specific title and an actionable body with the "
                "dollar impact and the data behind it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Ticket title"},
                    "body": {"type": "string", "description": "Ticket body, markdown"},
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "Priority (default medium)",
                    },
                },
                "required": ["title", "body"],
            },
        },
    ]


def execute_draft_tool(
    name: str, args: dict, *, requested_by: str, side_effects: list[dict]
) -> str:
    """Run a draft tool from inside the agentic loop. Returns JSON for the tool_result."""
    try:
        if name == "draft_rightsizing_pr":
            result = _draft_rightsizing_pr(args, requested_by)
        elif name == "draft_ticket":
            result = _draft_ticket(args, requested_by)
        else:
            return json.dumps({"error": f"Unknown draft tool: {name}"})
    except Exception as e:  # noqa: BLE001
        log.error("Draft tool %s failed: %s", name, e, exc_info=True)
        return json.dumps({"error": str(e)})

    if "pending_action_id" in result:
        side_effects.append({"type": "approval_card", "action_id": result["pending_action_id"]})
    return json.dumps(result, default=str)


def _draft_rightsizing_pr(args: dict, requested_by: str) -> dict:
    tf_dir = os.getenv("FINOPS_TF_DIR", "").strip()
    if not tf_dir:
        return {
            "error": (
                "FINOPS_TF_DIR is not set on the bot host, so I can't locate the Terraform "
                "to patch. Set FINOPS_TF_DIR (and GITHUB_FINOPS_TF_REPO for the PR target)."
            )
        }
    repo = os.getenv("GITHUB_FINOPS_TF_REPO", "").strip() or None
    branch = (args.get("branch") or "fix/rightsizing").strip()
    rec_ids = args.get("recommendation_ids") or None

    from ..remediation.rightsizing_pr import open_rightsizing_pr

    dry = open_rightsizing_pr(
        tf_dir=tf_dir,
        github_repo=repo,
        recommendation_ids=rec_ids,
        branch=branch,
        dry_run=True,
    )
    if dry.get("error"):
        return {"error": dry["error"]}
    files = dry.get("files_modified") or []
    if not files:
        return {
            "result": "Dry run found nothing to patch.",
            "skipped": dry.get("skipped", []),
            "hint": "The open recommendations may not map to Terraform resources in FINOPS_TF_DIR.",
        }

    monthly = dry.get("estimated_monthly_savings_usd", 0)
    preview_lines = [
        f"*Terraform rightsizing PR* (branch `{branch}`)",
        f"Files: {', '.join(f'`{f}`' for f in files[:8])}" + (" and more" if len(files) > 8 else ""),
        f"Recommendations: {len(dry.get('recommendations_acted_on', []) or rec_ids or [])}",
        f"Estimated savings: *${monthly:,.0f}/mo* (${monthly * 12:,.0f}/yr)",
    ]
    if repo:
        preview_lines.append(f"PR target: `{repo}`")
    if dry.get("skipped"):
        preview_lines.append(f"Skipped (no Terraform mapping): {len(dry['skipped'])}")
    preview = "\n".join(preview_lines)

    action_id = _create_pending(
        kind="rightsizing_pr",
        payload={
            "tf_dir": tf_dir,
            "github_repo": repo,
            "recommendation_ids": rec_ids,
            "branch": branch,
        },
        preview=preview,
        requested_by=requested_by,
    )
    return {
        "pending_action_id": action_id,
        "status": "awaiting_approval",
        "preview": {
            "files_modified": files,
            "estimated_monthly_savings_usd": monthly,
            "branch": branch,
        },
        "note": "Approval card posted in Slack. Tell the user to review and click Approve.",
    }


def _draft_ticket(args: dict, requested_by: str) -> dict:
    title = (args.get("title") or "").strip()
    body = (args.get("body") or "").strip()
    priority = (args.get("priority") or "medium").strip().lower()
    if not title or not body:
        return {"error": "Both title and body are required."}

    body_preview = body if len(body) <= 600 else body[:597] + "..."
    preview = f"*Ticket draft* ({priority} priority)\n*{title}*\n{body_preview}"
    action_id = _create_pending(
        kind="ticket",
        payload={"title": title, "body": body, "priority": priority},
        preview=preview,
        requested_by=requested_by,
    )
    return {
        "pending_action_id": action_id,
        "status": "awaiting_approval",
        "note": "Approval card posted in Slack. Tell the user to review and click Approve.",
    }


# ── Pending action store ─────────────────────────────────────────────────────

def _create_pending(kind: str, payload: dict, preview: str, requested_by: str) -> int:
    from ..storage.db import get_engine, pending_actions

    with get_engine().begin() as conn:
        result = conn.execute(
            pending_actions.insert().values(
                kind=kind,
                payload=json.dumps(payload, default=str),
                preview=preview,
                status="pending",
                requested_by=requested_by,
                created_at=datetime.utcnow(),
            )
        )
        return int(result.inserted_primary_key[0])


def get_pending(action_id: int) -> dict | None:
    from ..storage.db import get_engine, pending_actions

    with get_engine().connect() as conn:
        row = conn.execute(
            select(pending_actions).where(pending_actions.c.id == action_id)
        ).fetchone()
    return dict(row._mapping) if row else None


def _resolve(action_id: int, status: str, resolved_by: str, result: dict | None = None) -> None:
    from ..storage.db import get_engine, pending_actions

    with get_engine().begin() as conn:
        conn.execute(
            pending_actions.update()
            .where(pending_actions.c.id == action_id)
            .values(
                status=status,
                resolved_by=resolved_by,
                resolved_at=datetime.utcnow(),
                result=json.dumps(result or {}, default=str),
            )
        )


# ── Approval handlers (called from Slack button actions) ─────────────────────

def approve_action(action_id: int, resolved_by: str, role: str) -> dict:
    """Execute a pending action for real. Re-checks RBAC and expiry."""
    if not role_can_draft(role):
        return {"error": f"Your role ({role}) cannot approve actions. Requires {_DRAFT_MIN_ROLE}+."}

    action = get_pending(action_id)
    if not action:
        return {"error": f"Action #{action_id} not found."}
    if action["status"] != "pending":
        return {"error": f"Action #{action_id} is already {action['status']}."}
    if action["created_at"] < datetime.utcnow() - timedelta(hours=APPROVAL_TTL_HOURS):
        _resolve(action_id, "expired", resolved_by)
        return {"error": f"Action #{action_id} expired (older than {APPROVAL_TTL_HOURS}h). Ask me to draft it again."}

    payload = json.loads(action["payload"] or "{}")
    try:
        if action["kind"] == "rightsizing_pr":
            outcome = _execute_rightsizing_pr(payload)
        elif action["kind"] == "ticket":
            outcome = _execute_ticket(payload)
        else:
            return {"error": f"Unknown action kind: {action['kind']}"}
    except Exception as e:  # noqa: BLE001
        log.error("Approved action #%s failed: %s", action_id, e, exc_info=True)
        _resolve(action_id, "failed", resolved_by, {"error": str(e)})
        return {"error": f"Execution failed: {e}"}

    if outcome.get("error"):
        _resolve(action_id, "failed", resolved_by, outcome)
        return outcome
    _resolve(action_id, "approved", resolved_by, outcome)
    return outcome


def cancel_action(action_id: int, resolved_by: str) -> dict:
    action = get_pending(action_id)
    if not action:
        return {"error": f"Action #{action_id} not found."}
    if action["status"] != "pending":
        return {"error": f"Action #{action_id} is already {action['status']}."}
    _resolve(action_id, "cancelled", resolved_by)
    return {"cancelled": True}


def _execute_rightsizing_pr(payload: dict) -> dict:
    from ..remediation.rightsizing_pr import open_rightsizing_pr

    result = open_rightsizing_pr(
        tf_dir=payload["tf_dir"],
        github_repo=payload.get("github_repo"),
        recommendation_ids=payload.get("recommendation_ids"),
        branch=payload.get("branch", "fix/rightsizing"),
        dry_run=False,
    )
    if result.get("error"):
        return {"error": result["error"]}
    return {
        "pr_url": result.get("pr_url"),
        "branch": result.get("branch"),
        "files_modified": result.get("files_modified", []),
        "estimated_monthly_savings_usd": result.get("estimated_monthly_savings_usd", 0),
    }


def _execute_ticket(payload: dict) -> dict:
    from ..integrations.ticketing import create_custom_ticket

    url = create_custom_ticket(
        title=payload["title"],
        body=payload["body"],
        priority=payload.get("priority", "medium"),
        labels=["finops", "slack"],
    )
    if not url:
        return {
            "error": (
                "No ticketing provider configured. Set Jira (JIRA_BASE_URL, JIRA_API_TOKEN, "
                "JIRA_USER_EMAIL, JIRA_PROJECT_KEY), Linear (LINEAR_API_KEY, LINEAR_TEAM_ID), "
                "or GitHub (GITHUB_TOKEN, GITHUB_FINOPS_REPO)."
            )
        }
    return {"ticket_url": url}


# ── Block Kit ────────────────────────────────────────────────────────────────

def approval_blocks(action_id: int) -> list[dict]:
    action = get_pending(action_id)
    if not action:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": f"Action #{action_id} not found."}}]
    kind_label = "Terraform PR" if action["kind"] == "rightsizing_pr" else "Ticket"
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"Approval needed: {kind_label}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": action["preview"]}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Action #{action_id} · requested by <@{action['requested_by']}> · "
                        f"expires in {APPROVAL_TTL_HOURS}h · approving requires the "
                        f"{_DRAFT_MIN_ROLE} role"
                    ),
                }
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "approve_action",
                    "value": str(action_id),
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Execute this action?"},
                        "text": {"type": "mrkdwn", "text": "This will run for real (open the PR / file the ticket)."},
                        "confirm": {"type": "plain_text", "text": "Approve"},
                        "deny": {"type": "plain_text", "text": "Back"},
                    },
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "style": "danger",
                    "action_id": "cancel_action",
                    "value": str(action_id),
                },
            ],
        },
        {"type": "divider"},
    ]
