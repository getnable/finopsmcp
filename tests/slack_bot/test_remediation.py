"""Tests for the remediation approval flow: drafts, RBAC gate, lifecycle, expiry."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from finops.slack_bot import remediation


def _draft_ticket(requested_by="U1"):
    side_effects: list[dict] = []
    result = json.loads(
        remediation.execute_draft_tool(
            "draft_ticket",
            {"title": "Reduce RDS spend", "body": "db.r5.4xlarge is at 6% CPU", "priority": "high"},
            requested_by=requested_by,
            side_effects=side_effects,
        )
    )
    return result, side_effects


def test_role_can_draft():
    assert not remediation.role_can_draft("viewer")
    assert remediation.role_can_draft("analyst")
    assert remediation.role_can_draft("admin")


def test_draft_ticket_creates_pending_action(tmp_db):
    result, side_effects = _draft_ticket()
    assert result["status"] == "awaiting_approval"
    action_id = result["pending_action_id"]
    assert side_effects == [{"type": "approval_card", "action_id": action_id}]

    action = remediation.get_pending(action_id)
    assert action["kind"] == "ticket"
    assert action["status"] == "pending"
    assert action["requested_by"] == "U1"
    assert "Reduce RDS spend" in action["preview"]


def test_draft_ticket_requires_title_and_body(tmp_db):
    side_effects: list[dict] = []
    result = json.loads(
        remediation.execute_draft_tool(
            "draft_ticket", {"title": "", "body": ""}, requested_by="U1", side_effects=side_effects
        )
    )
    assert "error" in result
    assert side_effects == []


def test_viewer_cannot_approve(tmp_db):
    result, _ = _draft_ticket()
    outcome = remediation.approve_action(result["pending_action_id"], resolved_by="U2", role="viewer")
    assert "error" in outcome
    assert "viewer" in outcome["error"]
    # Action stays pending after the rejected attempt
    assert remediation.get_pending(result["pending_action_id"])["status"] == "pending"


def test_approve_executes_ticket_and_resolves(tmp_db):
    result, _ = _draft_ticket()
    action_id = result["pending_action_id"]
    with patch(
        "finops.integrations.ticketing.create_custom_ticket",
        return_value="https://linear.app/x/ABC-1",
    ) as create:
        outcome = remediation.approve_action(action_id, resolved_by="U2", role="analyst")
    assert outcome == {"ticket_url": "https://linear.app/x/ABC-1"}
    create.assert_called_once()

    action = remediation.get_pending(action_id)
    assert action["status"] == "approved"
    assert action["resolved_by"] == "U2"


def test_approve_twice_fails(tmp_db):
    result, _ = _draft_ticket()
    action_id = result["pending_action_id"]
    with patch("finops.integrations.ticketing.create_custom_ticket", return_value="https://x/1"):
        remediation.approve_action(action_id, resolved_by="U2", role="analyst")
        outcome = remediation.approve_action(action_id, resolved_by="U3", role="admin")
    assert "error" in outcome
    assert "already approved" in outcome["error"]


def test_cancel(tmp_db):
    result, _ = _draft_ticket()
    action_id = result["pending_action_id"]
    assert remediation.cancel_action(action_id, resolved_by="U1") == {"cancelled": True}
    assert remediation.get_pending(action_id)["status"] == "cancelled"
    # Cancelled actions cannot be approved
    outcome = remediation.approve_action(action_id, resolved_by="U2", role="admin")
    assert "error" in outcome


def test_expired_action_cannot_run(tmp_db):
    result, _ = _draft_ticket()
    action_id = result["pending_action_id"]

    from finops.storage.db import get_engine, pending_actions

    stale = datetime.utcnow() - timedelta(hours=remediation.APPROVAL_TTL_HOURS + 1)
    with get_engine().begin() as conn:
        conn.execute(
            pending_actions.update()
            .where(pending_actions.c.id == action_id)
            .values(created_at=stale)
        )
    outcome = remediation.approve_action(action_id, resolved_by="U2", role="admin")
    assert "error" in outcome
    assert "expired" in outcome["error"]
    assert remediation.get_pending(action_id)["status"] == "expired"


def test_failed_execution_marks_failed(tmp_db):
    result, _ = _draft_ticket()
    action_id = result["pending_action_id"]
    with patch("finops.integrations.ticketing.create_custom_ticket", return_value=None):
        outcome = remediation.approve_action(action_id, resolved_by="U2", role="analyst")
    assert "error" in outcome
    assert remediation.get_pending(action_id)["status"] == "failed"


def test_draft_pr_without_tf_dir_errors(tmp_db, monkeypatch):
    monkeypatch.delenv("FINOPS_TF_DIR", raising=False)
    side_effects: list[dict] = []
    result = json.loads(
        remediation.execute_draft_tool(
            "draft_rightsizing_pr", {}, requested_by="U1", side_effects=side_effects
        )
    )
    assert "error" in result
    assert "FINOPS_TF_DIR" in result["error"]
    assert side_effects == []


def test_draft_pr_dry_run_builds_preview(tmp_db, monkeypatch):
    monkeypatch.setenv("FINOPS_TF_DIR", "/tmp/tf")
    monkeypatch.setenv("GITHUB_FINOPS_TF_REPO", "acme/infra")
    dry_result = {
        "files_modified": ["ec2.tf", "rds.tf"],
        "recommendations_acted_on": [1, 2],
        "estimated_monthly_savings_usd": 1500.0,
        "skipped": [],
    }
    side_effects: list[dict] = []
    with patch(
        "finops.remediation.rightsizing_pr.open_rightsizing_pr", return_value=dry_result
    ) as opr:
        result = json.loads(
            remediation.execute_draft_tool(
                "draft_rightsizing_pr", {"branch": "fix/rs"}, requested_by="U1", side_effects=side_effects
            )
        )
    assert opr.call_args.kwargs["dry_run"] is True
    assert result["status"] == "awaiting_approval"
    assert side_effects[0]["type"] == "approval_card"

    action = remediation.get_pending(result["pending_action_id"])
    assert action["kind"] == "rightsizing_pr"
    assert "$1,500/mo" in action["preview"]
    payload = json.loads(action["payload"])
    assert payload["branch"] == "fix/rs"
    assert payload["github_repo"] == "acme/infra"


def test_approve_pr_runs_for_real(tmp_db, monkeypatch):
    monkeypatch.setenv("FINOPS_TF_DIR", "/tmp/tf")
    dry = {
        "files_modified": ["ec2.tf"],
        "recommendations_acted_on": [1],
        "estimated_monthly_savings_usd": 900.0,
        "skipped": [],
    }
    real = {
        "pr_url": "https://github.com/acme/infra/pull/7",
        "branch": "fix/rightsizing",
        "files_modified": ["ec2.tf"],
        "estimated_monthly_savings_usd": 900.0,
    }
    side_effects: list[dict] = []
    with patch("finops.remediation.rightsizing_pr.open_rightsizing_pr", return_value=dry):
        result = json.loads(
            remediation.execute_draft_tool(
                "draft_rightsizing_pr", {}, requested_by="U1", side_effects=side_effects
            )
        )
    with patch(
        "finops.remediation.rightsizing_pr.open_rightsizing_pr", return_value=real
    ) as opr:
        outcome = remediation.approve_action(
            result["pending_action_id"], resolved_by="U2", role="admin"
        )
    assert opr.call_args.kwargs["dry_run"] is False
    assert outcome["pr_url"] == "https://github.com/acme/infra/pull/7"


def test_approval_blocks_shape(tmp_db):
    result, _ = _draft_ticket()
    blocks = remediation.approval_blocks(result["pending_action_id"])
    types = [b["type"] for b in blocks]
    assert types == ["header", "section", "context", "actions", "divider"]
    buttons = {e["action_id"] for e in blocks[3]["elements"]}
    assert buttons == {"approve_action", "cancel_action"}
