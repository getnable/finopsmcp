# SPDX-License-Identifier: Apache-2.0
"""anomalies MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
def get_anomalies(
    provider: str | None = None,
    severity: str | None = None,
    limit: int = 20,
    account: str | None = None,
    root_cause: bool = False,
) -> dict:
    """
    Return active (unacknowledged) cost anomalies detected from historical baselines.

    Args:
        provider: Filter to a specific provider. None = all.
        severity: "high", "medium", or "low". None = all severities.
        limit: Max anomalies to return (default 20).
        account: Named AWS account from accounts.yaml to filter results.
        root_cause: For up to 3 AWS spikes, also find the usage types and
            resources behind each one and the change that started it (from
            CloudTrail: who, when, via console/cli/terraform, and the guard's
            verdict), labelled "confirmed (resource id match)" or "likely".
            Off by default: it makes billed Cost Explorer requests (about
            $0.01 each, 2 per spike) and the answer says how many. Set it when
            the user asks why something spiked, which resource, or who changed
            it, not for a plain "any anomalies?".

    Examples:
        - "Are there any cost anomalies I should know about?"
        - "Show me high-severity cost spikes"
        - "What spiked in AWS this week?"
        - "Any anomalies in the production account?"
        - "Why did EC2 spike, and who launched it?" (root_cause=True)

    Note: Anomalies require at least 7 days of snapshot history. Before then,
          explain_recent_cost_drivers reads cost data directly and shows what moved.
    """
    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("get_anomalies", {
            "provider": provider, "severity": severity, "limit": limit}) or {}

    from ..anomaly.detector import (
        get_active_anomalies, has_enough_history, history_is_stale, latest_snapshot_date,
    )

    # Resolve account_id filter when a named account is requested. A name that
    # does not exist, or an account with no account_id to filter on, is an
    # error: answering with the default account, or with every account, would
    # label somebody else's anomalies (or their absence) as this account's.
    account_id_filter: str | None = None
    if account:
        from ..accounts import resolve_named_account
        acct_cfg, acct_err = resolve_named_account(account)
        if acct_err:
            return acct_err
        if not acct_cfg.account_id:
            return {"error": (f"Account '{account}' has no account_id in accounts.yaml, "
                              "so its anomalies cannot be told apart from other "
                              "accounts'. Add its account_id and ask again.")}
        account_id_filter = acct_cfg.account_id

    rows = get_active_anomalies(provider=provider, severity=severity, limit=limit,
                                account_id=account_id_filter)
    if not rows:
        # An empty result means one of three very different things. With enough
        # recent days of snapshots behind us it is a real all-clear. On a fresh
        # account it just means we cannot judge yet, and with history that
        # stopped days ago nothing recent has been checked. Saying "all clear"
        # in either of those is a false reassurance, so check the history first.
        last = latest_snapshot_date(provider, account_id_filter)
        # Day one should not end at "wait a week". explain_recent_cost_drivers
        # reads Cost Explorer directly and compares two windows, which answers
        # "did anything spike" before the snapshot baseline exists.
        meanwhile = (
            " Meanwhile, explain_recent_cost_drivers reads your cost data directly "
            "and compares the last 7 days with the 7 before, so it can show what "
            "moved right now."
        )
        next_tool = None
        if has_enough_history(provider, account_id_filter):
            message = "No active anomalies."
        elif last is not None and history_is_stale(provider, account_id_filter):
            message = (
                f"Cost history is stale: the newest snapshot is from {last.isoformat()}, "
                "so recent spend has not been checked for anomalies. Take a cost "
                "snapshot (take_snapshot_now) and ask again." + meanwhile
            )
            next_tool = "explain_recent_cost_drivers"
        else:
            message = (
                "Not enough history yet to detect anomalies, so nothing has been "
                "checked: this is not an all-clear. Anomaly detection needs about 7 "
                "days of daily snapshots to build a baseline (take_snapshot_now "
                "records today's)." + meanwhile
            )
            next_tool = "explain_recent_cost_drivers"
        empty: dict = {
            "anomalies": [],
            "message": message,
        }
        if next_tool:
            empty["next_tool"] = next_tool
            empty["next_tool_args"] = {"days": 7}
        if account:
            empty["account"] = account
        return empty

    formatted = []
    for r in rows:
        pct = abs(r["pct_change"])
        sign = "+" if r["direction"] == "spike" else "-"
        formatted.append({
            "id": r["id"],
            "provider": r["provider"],
            "service": r["service"],
            "account_id": r["account_id"],
            "severity": r["severity"],
            "direction": r["direction"],
            "change": f"{sign}{pct:.0f}%",
            "today": f"${r['current_amount']:,.2f}",
            "baseline_avg": f"${r['baseline_mean']:,.2f}",
            "z_score": r["z_score"],
            "detected": r["detected_at"],
            "snapshot_date": r["snapshot_date"],
        })

    # Apply custom alert policies (mutes, custom thresholds)
    policies = _srv._load_alert_policies()
    before_count = len(formatted)
    formatted = _srv._apply_alert_policies(formatted, policies)
    muted_count = before_count - len(formatted)

    # Spikes and drops are different events: a spike is the problem the user is
    # hunting; a drop is usually good news (a fix landed, something was cleaned up)
    # or, rarely, a sign that something stopped running. Splitting the counts keeps
    # "5 anomalies!" from meaning "1 real spike and 4 pieces of good news".
    spike_count = sum(1 for a in formatted if a.get("direction") == "spike")
    drop_count = len(formatted) - spike_count
    result: dict = {
        "count": len(formatted),
        "spikes": spike_count,
        "drops": drop_count,
        "anomalies": formatted,
        "tip": "Use acknowledge_anomaly(id) to dismiss resolved anomalies. Use set_alert_policy() to mute noisy services.",
    }
    if account:
        result["account"] = account
    if drop_count:
        result["drops_note"] = (
            "Drops are usually good news (a fix landed or a resource was removed). "
            "Treat a large sudden drop as a check that nothing stopped running "
            "unintentionally, not as a problem."
        )
    if muted_count > 0:
        result["muted_by_policy"] = muted_count
    if root_cause:
        result["root_cause"] = _root_causes(formatted)

    # Nudge free users toward Slack alerts -- most useful next step after seeing
    # anomalies. Lead with spikes; counting good-news drops as alarm inflates the
    # pitch and reads as noise the moment the user looks at the list.
    high_spikes = sum(1 for a in formatted
                      if a.get("severity") == "high" and a.get("direction") == "spike")
    if spike_count:
        nudge_msg = (
            f"You have {spike_count} cost spike{'s' if spike_count != 1 else ''}"
            + (f" ({high_spikes} high-severity)" if high_spikes else "")
            + ". To open a Jira, Linear, or GitHub ticket for each so a spike has an owner,"
            + " upgrade to Pro. (Alerts sent on a schedule, without asking, are nable Cloud.)"
        )
    else:
        nudge_msg = (
            "To open a Jira, Linear, or GitHub ticket from a cost spike so it has an owner,"
            " upgrade to Pro. (Alerts sent on a schedule, without asking, are nable Cloud.)"
        )
    nudge = _srv._team_nudge(nudge_msg, context="anomalies")
    if nudge:
        result["_upgrade"] = nudge

    return result


_ROOT_CAUSE_MAX = 3


def _root_causes(formatted: list[dict]) -> dict:
    """The drill-down for the first few AWS spikes, attached to each and
    combined for the answer. Billed Cost Explorer requests: counted and said."""
    from ..anomaly.impact import root_cause as _one
    from ..anomaly.root_cause import combine, compact

    spikes = [a for a in formatted
              if a.get("provider") == "aws" and a.get("direction") == "spike"]
    results = []
    for a in spikes[:_ROOT_CAUSE_MAX]:
        found = _one(a)
        if found is not None:
            a["root_cause"] = compact(found)
            results.append(found)
    if not results:
        return {"lines": [], "note": ("Root cause reads AWS spikes only (Cost Explorer and "
                                      "CloudTrail), and none are in this list.")}
    block = combine(results)
    block.pop("services", None)            # each anomaly carries its own
    if len(spikes) > _ROOT_CAUSE_MAX:
        block["not_drilled"] = (f"{len(spikes) - _ROOT_CAUSE_MAX} more AWS spike(s) were not "
                                "drilled into; filter by severity or provider to pick them.")
    return block


@_srv.mcp.tool()
def acknowledge_anomaly(anomaly_id: int) -> dict:
    """
    Mark an anomaly as acknowledged (dismissed). It will no longer appear in active anomalies.

    Args:
        anomaly_id: The ID from get_anomalies().

    Examples:
        - "Dismiss anomaly 42, it was a planned migration"
        - "Acknowledge that spike, it was expected"
    """
    if err := _srv.require_role("analyst"):
        return err

    from ..anomaly.detector import acknowledge_anomaly as _ack
    ok = _ack(anomaly_id)
    return {"acknowledged": ok, "id": anomaly_id}


@_srv.mcp.tool()
def get_account_anomalies(days_back: int = 30) -> dict:
    """
    Detect accounts with unusual spend changes versus their prior period.
    Returns accounts that significantly spiked or dropped in cost.
    Requires a Pro plan (org_reports).

    Args:
        days_back: Look-back period to compare (default 30 vs prior 30)

    Examples:
        - "Which accounts had unusual spend changes?"
        - "Are any accounts spiking this month?"
        - "Show me account-level anomalies"
    """
    if err := _srv.require_pro("org_reports"):
        return err
    try:
        from ..connectors.aws_org import account_anomalies
        anomalies = account_anomalies(days_back=days_back)
        spikes = [a for a in anomalies if a["direction"] == "spike"]
        drops  = [a for a in anomalies if a["direction"] == "drop"]
        total_current = round(sum(a.get("current_usd", 0) for a in anomalies), 2)
        total_previous = round(sum(a.get("previous_usd", 0) for a in anomalies), 2)
        # Sort by absolute dollar swing (real money moved), most-important-first, then cap.
        ranked = sorted(
            anomalies,
            key=lambda a: abs(a.get("current_usd", 0) - a.get("previous_usd", 0)),
            reverse=True,
        )
        kept, omitted = _srv.fit_to_budget(ranked, max_tokens=6000)
        result = {
            "total_anomalies": len(anomalies),
            "spikes": len(spikes),
            "drops": len(drops),
            "total_current_usd": total_current,
            "total_previous_usd": total_previous,
            "anomalies": kept,
        }
        if omitted > 0:
            result["anomalies_truncated"] = omitted
            result["hint"] = (
                f"showing top {len(kept)} of {len(anomalies)} account anomalies by dollar "
                f"swing; query a specific account for full detail"
            )
        return result
    except Exception as e:
        return {"error": str(e)}
