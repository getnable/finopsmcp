"""
Scheduled watchdog job: run the correlator continuously and push a one-click
approval for the worst waste. Follows the scheduler/jobs.py pattern
(job_ai_monitor / job_credit_check): an async check, a sync job_* wrapper, and
file-based dedup so the same finding alerts once.

STATUS: the correlator (detect) and the approval-card shaping (prepare) are real
and tested. The actual push to Slack/Teams is STUBBED here (_push_one_click_card)
so no real notification fires and nothing is wired to execution. Wiring the push
into the existing notifications path and registering the cron in start_scheduler
is a follow-up, deliberately left out so this ships without side effects.

PROPOSE-ONLY, ALWAYS. This job detects and prepares. It never applies a fix.
There is no execution path here and none may be added.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

log = logging.getLogger("finops.watchdog")

# How many findings to push per run. The card shows the worst waste first; a
# flood trains the owner to mute the channel, which kills the approval loop.
_MAX_CARDS_PER_RUN = 3


def _dedup_key(finding: Any) -> str:
    """One approval per (month, resource, fix). Re-alerting the same finding
    daily is how a team mutes the channel."""
    r = finding.remediation
    return f"{str(date.today())[:7]}:{finding.resource_id}:{r.kind}"


def _push_one_click_card(finding: Any) -> bool:
    """STUB. Shape the one-click approval card and (in a real build) push it over
    the existing notifications path (finops.notifications.slack / teams), the
    same path job_detect_and_alert uses.

    This stub logs the card and returns True. It performs no network call, opens
    no execution path, and mutates nothing. Approval, when wired, triggers the
    already-reviewed prepare step (open_rightsizing_pr for rightsizing; the human
    still merges and applies). It never auto-applies.
    """
    r = finding.remediation
    card = {
        "headline": f"${finding.monthly_waste_usd:,.0f}/mo waste: {finding.name or finding.resource_id}",
        "detail": finding.reason,
        "fix": r.title,
        "command": r.command,
        "requires_approval": True,  # the human tap is mandatory
        "approve_action": {
            # Describes what an approval WOULD trigger. Inert data, not a call.
            "prepare_via": r.prepare_via,
            "params": r.params,
        },
    }
    log.info(
        "Watchdog approval card prepared (STUB, not pushed): %s",
        card["headline"],
    )
    return True


async def _run_watchdog() -> dict:
    """Detect underutilized waste, prepare fixes, push (stubbed) approval cards
    for the worst, deduped once per month per resource+fix."""
    from .correlator import correlate_spend_and_utilization
    from ..scheduler.jobs import _alert_already_sent, _mark_alert_sent

    findings = correlate_spend_and_utilization()
    pushed: list[str] = []
    for finding in findings[:_MAX_CARDS_PER_RUN]:
        key = _dedup_key(finding)
        if _alert_already_sent("watchdog", key):
            continue
        if _push_one_click_card(finding):
            _mark_alert_sent("watchdog", key)
            pushed.append(finding.resource_id)

    return {
        "findings": len(findings),
        "pushed": pushed,
        "total_monthly_waste_usd": round(sum(f.monthly_waste_usd for f in findings), 2),
    }


def job_watchdog() -> None:
    """Sync wrapper for APScheduler. Register in start_scheduler like the other
    job_* callers, behind a FINOPS_WATCHDOG_CRON env var, once the push is wired.
    """
    try:
        from ..scheduler.jobs import _run
        result = _run(_run_watchdog())
        if result:
            log.info(
                "Watchdog: %d finding(s), %d card(s) prepared",
                result.get("findings", 0),
                len(result.get("pushed", [])),
            )
    except Exception:
        log.exception("Watchdog job failed")
