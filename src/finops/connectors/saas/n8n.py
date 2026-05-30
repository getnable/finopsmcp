"""
n8n webhook connector.

Sends nable events to n8n workflows via webhook trigger.

In n8n: add a Webhook node, copy the URL, set N8N_WEBHOOK_URL env var.
nable will POST structured JSON to that URL when cost events occur.

Event types pushed to n8n:
- anomaly_detected: spike above threshold
- audit_complete: full cost audit finished, top opportunities
- budget_exceeded: spend over budget limit
- recommendation_ready: high-confidence savings opportunity found
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

_SOURCE = "nable"


class N8nConnector:
    """
    Sends nable events to n8n workflows via webhook trigger.

    In n8n: add a Webhook node, copy the URL, set N8N_WEBHOOK_URL env var.
    nable will POST structured JSON to that URL when cost events occur.

    Event types pushed to n8n:
    - anomaly_detected: spike above threshold
    - audit_complete: full cost audit finished, top opportunities
    - budget_exceeded: spend over budget limit
    - recommendation_ready: high-confidence savings opportunity found
    """

    def __init__(self) -> None:
        self._webhook_url = os.environ.get("N8N_WEBHOOK_URL", "")

    async def is_configured(self) -> bool:
        """Returns True when N8N_WEBHOOK_URL is set."""
        return bool(self._webhook_url or os.environ.get("N8N_WEBHOOK_URL", ""))

    async def send_event(self, event_type: str, payload: dict[str, Any]) -> bool:
        """
        POST a structured event to the n8n webhook URL.

        Returns True on HTTP 2xx, False on any error. Never raises.
        """
        url = self._webhook_url or os.environ.get("N8N_WEBHOOK_URL", "")
        if not url:
            return False

        body = {
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": _SOURCE,
            "data": payload,
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, json=body)
                if r.status_code < 300:
                    log.debug("n8n event %r delivered (HTTP %d)", event_type, r.status_code)
                    return True
                log.warning(
                    "n8n webhook returned HTTP %d for event %r", r.status_code, event_type
                )
                return False
        except Exception as exc:
            log.warning("n8n webhook failed for event %r: %s", event_type, exc)
            return False

    async def send_anomaly(self, anomaly: dict[str, Any]) -> bool:
        """
        Format and send an anomaly_detected event.

        anomaly is the dict stored by the anomaly detector and scheduler:
        keys: provider, service, account_id, severity, direction,
              pct_change, z_score, baseline_mean, current_amount, detected_at
        """
        delta_usd = round(
            anomaly.get("current_amount", 0) - anomaly.get("baseline_mean", 0), 2
        )
        spike_pct = round(abs(anomaly.get("pct_change", 0)))

        action = _recommended_action(anomaly)

        payload: dict[str, Any] = {
            "service": anomaly.get("service", ""),
            "provider": anomaly.get("provider", ""),
            "spike_pct": spike_pct,
            "delta_usd": delta_usd,
            "current_usd": round(anomaly.get("current_amount", 0), 2),
            "baseline_mean_usd": round(anomaly.get("baseline_mean", 0), 2),
            "account": anomaly.get("account_id", ""),
            "severity": anomaly.get("severity", ""),
            "direction": anomaly.get("direction", "spike"),
            "z_score": anomaly.get("z_score", 0),
            "recommended_action": action,
        }
        if anomaly.get("detected_at"):
            payload["detected_at"] = str(anomaly["detected_at"])

        return await self.send_event("anomaly_detected", payload)

    async def send_audit_summary(
        self,
        findings: list[dict[str, Any]],
        total_savings: float,
        account: str = "",
        scan_duration_s: float = 0,
    ) -> bool:
        """
        Format and send an audit_complete event.

        findings: list of waste findings from run_deep_audit.
        total_savings: total estimated monthly savings in USD.
        """
        annual_savings = round(total_savings * 12, 2)
        monthly_savings = round(total_savings, 2)

        top_opportunities = []
        for rank, finding in enumerate(findings[:10], start=1):
            top_opportunities.append({
                "rank": rank,
                "title": finding.get("title", finding.get("description", "")),
                "category": finding.get("category", finding.get("check", "")),
                "monthly_savings": round(
                    finding.get("estimated_monthly_savings", finding.get("monthly_savings", 0)), 2
                ),
            })

        payload: dict[str, Any] = {
            "total_monthly_savings": monthly_savings,
            "total_annual_savings": annual_savings,
            "top_opportunities": top_opportunities,
            "account": account,
            "scan_duration_s": round(scan_duration_s, 1),
        }

        return await self.send_event("audit_complete", payload)


def _recommended_action(anomaly: dict[str, Any]) -> str:
    """
    Produce a short recommended action string based on the anomaly fields.
    Not an exhaustive ruleset, just a helpful default.
    """
    service = anomaly.get("service", "")
    direction = anomaly.get("direction", "spike")
    severity = anomaly.get("severity", "low")

    if direction == "drop":
        return f"Investigate why {service} spend dropped; may indicate a broken pipeline."

    if severity == "high":
        return (
            f"High-severity spike in {service}. Check for runaway jobs, "
            "misconfigured autoscaling, or data transfer charges."
        )

    return f"Review recent {service} usage for non-prod or unexpected activity."
