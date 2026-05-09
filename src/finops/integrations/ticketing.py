"""
Ticketing integrations — auto-create issues on anomaly detection.
Supports Jira, Linear, and GitHub Issues.

When an anomaly is detected the scheduler calls `create_ticket()`, which
tries each configured provider in turn and returns the first ticket URL.

Setup via `finops setup jira / linear / github-issues`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date
from typing import Any

import httpx

log = logging.getLogger(__name__)


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _dedup_key(provider: str, service: str, snapshot_date: str) -> str:
    """Stable key used to avoid creating duplicate tickets for the same anomaly."""
    raw = f"{provider}:{service}:{snapshot_date}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _anomaly_title(anomaly: dict[str, Any]) -> str:
    direction = "↑" if anomaly.get("direction") == "spike" else "↓"
    pct = abs(anomaly.get("pct_change", 0))
    return (
        f"[FinOps] {anomaly['provider'].upper()} / {anomaly['service']} "
        f"cost {direction}{pct:.0f}% vs baseline"
    )


def _anomaly_body(anomaly: dict[str, Any]) -> str:
    current = anomaly.get("current_amount", 0)
    baseline = anomaly.get("baseline_mean", 0)
    z = anomaly.get("z_score", 0)
    pct = anomaly.get("pct_change", 0)
    sev = anomaly.get("severity", "medium").upper()
    direction_word = "spike" if anomaly.get("direction") == "spike" else "drop"

    return f"""## FinOps Anomaly — {sev}

**Provider:** {anomaly.get('provider', '').upper()}
**Service:** {anomaly.get('service', '')}
**Detected:** {anomaly.get('snapshot_date', date.today().isoformat())}

### What happened
Cost {direction_word} of **{abs(pct):.1f}%** vs 28-day baseline
- Current: **${current:,.2f}**
- Baseline avg: **${baseline:,.2f}**
- Z-score: {z:.2f}

### Next steps
- [ ] Identify root cause (new deployment? config change? data growth?)
- [ ] Confirm whether this is expected or unexpected
- [ ] If unexpected, mitigate and close this ticket
- [ ] If expected, update the baseline tag/label

---
*Created automatically by [FinOps MCP](https://finops-mcp.dev)*
"""


# ── Jira ──────────────────────────────────────────────────────────────────────

def _create_jira_ticket(anomaly: dict[str, Any]) -> str | None:
    base_url = _env("JIRA_BASE_URL").rstrip("/")
    token = _env("JIRA_API_TOKEN")
    email_addr = _env("JIRA_USER_EMAIL")
    project_key = _env("JIRA_PROJECT_KEY")

    if not all([base_url, token, email_addr, project_key]):
        return None

    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": _anomaly_title(anomaly),
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": _anomaly_body(anomaly)}],
                    }
                ],
            },
            "issuetype": {"name": _env("JIRA_ISSUE_TYPE", "Task")},
            "priority": {"name": "High" if anomaly.get("severity") == "high" else "Medium"},
            "labels": ["finops", "cost-anomaly"],
        }
    }

    assignee_id = _env("JIRA_ASSIGNEE_ID")
    if assignee_id:
        payload["fields"]["assignee"] = {"id": assignee_id}  # type: ignore[index]

    try:
        r = httpx.post(
            f"{base_url}/rest/api/3/issue",
            json=payload,
            auth=(email_addr, token),
            timeout=15,
        )
        r.raise_for_status()
        key = r.json()["key"]
        return f"{base_url}/browse/{key}"
    except Exception as e:
        log.error("Jira ticket creation failed: %s", e)
        return None


# ── Linear ────────────────────────────────────────────────────────────────────

_LINEAR_CREATE_ISSUE = """
mutation CreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id url }
  }
}
"""


def _create_linear_ticket(anomaly: dict[str, Any]) -> str | None:
    api_key = _env("LINEAR_API_KEY")
    team_id = _env("LINEAR_TEAM_ID")

    if not all([api_key, team_id]):
        return None

    priority_map = {"high": 1, "medium": 2, "low": 3}
    variables = {
        "input": {
            "teamId": team_id,
            "title": _anomaly_title(anomaly),
            "description": _anomaly_body(anomaly),
            "priority": priority_map.get(anomaly.get("severity", "medium"), 2),
            "labelIds": [],
        }
    }

    assignee_id = _env("LINEAR_ASSIGNEE_ID")
    if assignee_id:
        variables["input"]["assigneeId"] = assignee_id  # type: ignore[index]

    try:
        r = httpx.post(
            "https://api.linear.app/graphql",
            json={"query": _LINEAR_CREATE_ISSUE, "variables": variables},
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        return data["data"]["issueCreate"]["issue"]["url"]
    except Exception as e:
        log.error("Linear ticket creation failed: %s", e)
        return None


# ── GitHub Issues ─────────────────────────────────────────────────────────────

def _create_github_issue(anomaly: dict[str, Any]) -> str | None:
    token = _env("GITHUB_TOKEN")
    repo = _env("GITHUB_FINOPS_REPO")  # e.g. "myorg/finops-alerts"

    if not all([token, repo]):
        return None

    labels = ["finops", "cost-anomaly"]
    if anomaly.get("severity") == "high":
        labels.append("priority:high")

    payload: dict[str, Any] = {
        "title": _anomaly_title(anomaly),
        "body": _anomaly_body(anomaly),
        "labels": labels,
    }

    assignees_raw = _env("GITHUB_FINOPS_ASSIGNEES")
    if assignees_raw:
        payload["assignees"] = [a.strip() for a in assignees_raw.split(",")]

    try:
        r = httpx.post(
            f"https://api.github.com/repos/{repo}/issues",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["html_url"]
    except Exception as e:
        log.error("GitHub issue creation failed: %s", e)
        return None


# ── Public interface ──────────────────────────────────────────────────────────

def create_ticket(anomaly: dict[str, Any]) -> str | None:
    """
    Try each configured ticketing provider. Returns the first ticket URL
    created, or None if none are configured / all failed.
    """
    preferred = _env("FINOPS_TICKET_PROVIDER", "").lower()

    providers = {
        "jira": _create_jira_ticket,
        "linear": _create_linear_ticket,
        "github": _create_github_issue,
    }

    if preferred and preferred in providers:
        ordered = [(preferred, providers[preferred])] + [
            (k, v) for k, v in providers.items() if k != preferred
        ]
    else:
        ordered = list(providers.items())

    for name, fn in ordered:
        url = fn(anomaly)
        if url:
            log.info("Created %s ticket: %s", name, url)
            _persist_ticket(anomaly, name, url)
            return url

    return None


def _persist_ticket(anomaly: dict[str, Any], provider: str, url: str) -> None:
    """Store the ticket URL against the anomaly record for later reference."""
    try:
        from ..storage.db import anomalies, get_engine
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                anomalies.update()
                .where(anomalies.c.id == anomaly.get("id"))
                .values(metadata=json.dumps({"ticket_provider": provider, "ticket_url": url}))
            )
    except Exception as e:
        log.warning("Could not persist ticket URL: %s", e)


def create_tickets_for_unnotified(limit: int = 20) -> list[str]:
    """
    Called by scheduler after anomaly detection. Creates tickets for all
    high/medium anomalies that haven't been ticketed yet.
    """
    from ..anomaly.detector import get_active_anomalies
    anomaly_list = get_active_anomalies(limit=limit)
    urls: list[str] = []
    for a in anomaly_list:
        if a.get("severity") not in ("high", "medium"):
            continue
        # Skip if ticket already created (metadata has ticket_url)
        meta = a.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if meta.get("ticket_url"):
            continue
        url = create_ticket(a)
        if url:
            urls.append(url)
    return urls
