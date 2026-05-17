"""
FinOps MCP Server
-----------------
Exposes cloud + SaaS cost data as MCP tools.
Run via:  finops-mcp  or  python -m finops.server
"""

from __future__ import annotations

import logging
import os
import statistics
from datetime import date, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load vault credentials into os.environ before anything else reads env vars
from .security.env import load_vault_to_env
load_vault_to_env()

from .license import _UPGRADE_URL, get_status, require_pro
from .auth.rbac import (
    resolve_identity_from_env, set_current_identity,
    require_role, current_identity, enforce_team_scope, enforce_provider_scope,
    create_key, list_keys, revoke_key, audit,
)

from .connectors.aws import AWSConnector
from .connectors.azure import AzureConnector
from .connectors.base import CostSummary
from .connectors.gcp import GCPConnector
from .connectors.saas.cloudflare import CloudflareConnector
from .connectors.saas.datadog import DatadogConnector
from .connectors.saas.github import GitHubConnector
from .connectors.saas.mongodb_atlas import MongoDBAtlasConnector
from .connectors.saas.new_relic import NewRelicConnector
from .connectors.saas.pagerduty import PagerDutyConnector
from .connectors.saas.snowflake import SnowflakeConnector
from .connectors.saas.stripe import StripeConnector
from .connectors.saas.twilio import TwilioConnector
from .connectors.saas.vercel import VercelConnector

load_dotenv()

mcp = FastMCP("finops")

# ── connector registry ───────────────────────────────────────────────────────

_CLOUD_CONNECTORS: dict[str, Any] = {
    "aws": AWSConnector(),
    "azure": AzureConnector(),
    "gcp": GCPConnector(),
}

_SAAS_CONNECTORS: dict[str, Any] = {
    "datadog": DatadogConnector(),
    "snowflake": SnowflakeConnector(),
    "github": GitHubConnector(),
    "stripe": StripeConnector(),
    "mongodb_atlas": MongoDBAtlasConnector(),
    "vercel": VercelConnector(),
    "cloudflare": CloudflareConnector(),
    "pagerduty": PagerDutyConnector(),
    "twilio": TwilioConnector(),
    "new_relic": NewRelicConnector(),
}

_ALL_CONNECTORS: dict[str, Any] = {**_CLOUD_CONNECTORS, **_SAAS_CONNECTORS}


async def _active(subset: dict | None = None) -> dict[str, Any]:
    pool = subset or _ALL_CONNECTORS
    result = {}
    for name, connector in pool.items():
        if await connector.is_configured():
            result[name] = connector
    return result


def _default_dates() -> tuple[date, date]:
    lookback = int(os.getenv("DEFAULT_LOOKBACK_DAYS", "30"))
    end = date.today()
    return end - timedelta(days=lookback), end


def _fmt_usd(amount: float) -> str:
    return f"${amount:,.2f}"


def _summary_to_dict(summary: CostSummary) -> dict:
    return {
        "provider": summary.provider,
        "period": {"start": summary.start_date.isoformat(), "end": summary.end_date.isoformat()},
        "total_usd": round(summary.total_usd, 4),
        "total_formatted": _fmt_usd(summary.total_usd),
        "by_service": {
            k: round(v, 4) for k, v in sorted(summary.by_service.items(), key=lambda x: -x[1])
        },
        "by_account": {k: round(v, 4) for k, v in summary.by_account.items()},
        "by_region": {
            k: round(v, 4) for k, v in sorted(summary.by_region.items(), key=lambda x: -x[1])
        },
    }


async def _gather_costs(
    targets: dict[str, Any],
    start: date,
    end: date,
    granularity: str = "MONTHLY",
    service_filter: str | None = None,
) -> tuple[float, dict[str, dict], dict[str, float]]:
    """Run cost queries across targets, return (grand_total, by_provider, grand_by_service)."""
    grand_total = 0.0
    by_provider: dict[str, dict] = {}
    grand_by_service: dict[str, float] = {}

    for name, connector in targets.items():
        try:
            summary = await connector.get_costs(start, end, granularity=granularity)
            by_provider[name] = _summary_to_dict(summary)
            grand_total += summary.total_usd
            for svc, amt in summary.by_service.items():
                if service_filter and service_filter.lower() not in svc.lower():
                    continue
                grand_by_service[svc] = grand_by_service.get(svc, 0.0) + amt
        except Exception as exc:
            log.error(
                "Connector fetch failed: connector=%s error_type=%s error=%s timestamp=%s",
                name, type(exc).__name__, exc, datetime.utcnow().isoformat(),
            )
            by_provider[name] = {"error": str(exc)}

    return grand_total, by_provider, grand_by_service


# ── MCP tools ────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_connected_providers() -> dict:
    """
    List all configured cloud and SaaS providers with their connection status.
    Shows which connectors are active and which need credentials.
    """
    result: dict[str, dict] = {}
    for category, pool in [("cloud", _CLOUD_CONNECTORS), ("saas", _SAAS_CONNECTORS)]:
        for name, connector in pool.items():
            configured = await connector.is_configured()
            result[name] = {
                "category": category,
                "configured": configured,
                "status": "connected" if configured else "not configured — check .env",
            }
    return result


@mcp.tool()
async def get_cost_summary(
    provider: str | None = None,
    category: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    granularity: str = "MONTHLY",
) -> dict:
    """
    Get total spend summarized by service, account, and region.

    Args:
        provider: Specific provider name (e.g. "aws", "datadog", "snowflake"). None = all.
        category: "cloud" or "saas" to filter by type. None = all.
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.
        granularity: "DAILY" or "MONTHLY".

    Examples:
        - "How much did we spend total last month?"
        - "What did we spend on SaaS tools this quarter?"
        - "Give me an AWS cost summary for January"
    """
    sd, ed = _default_dates()
    if start_date:
        sd = date.fromisoformat(start_date)
    if end_date:
        ed = date.fromisoformat(end_date)

    if provider:
        pool = {provider: _ALL_CONNECTORS[provider]} if provider in _ALL_CONNECTORS else {}
    elif category == "cloud":
        pool = _CLOUD_CONNECTORS
    elif category == "saas":
        pool = _SAAS_CONNECTORS
    else:
        pool = _ALL_CONNECTORS

    targets = await _active(pool)
    if not targets:
        return {"error": "No providers configured. Set credentials in .env"}

    grand_total, by_provider, grand_by_service = await _gather_costs(targets, sd, ed, granularity)

    return {
        "period": {"start": sd.isoformat(), "end": ed.isoformat()},
        "grand_total_usd": round(grand_total, 4),
        "grand_total_formatted": _fmt_usd(grand_total),
        "by_provider": by_provider,
        "grand_by_service": {
            k: round(v, 4)
            for k, v in sorted(grand_by_service.items(), key=lambda x: -x[1])
        },
    }


@mcp.tool()
async def get_costs_by_service(
    service_filter: str | None = None,
    provider: str | None = None,
    category: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Cost breakdown by service, optionally filtered to a keyword.

    Args:
        service_filter: Case-insensitive substring to match service names (e.g. "compute", "storage", "logs").
        provider: Specific provider. None = all.
        category: "cloud" or "saas". None = all.
        start_date: ISO date. Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.

    Examples:
        - "How much did compute cost us across all clouds?"
        - "What did we spend on storage?"
        - "How much are we paying for GitHub Actions?"
        - "Show me all Datadog product costs"
    """
    sd, ed = _default_dates()
    if start_date:
        sd = date.fromisoformat(start_date)
    if end_date:
        ed = date.fromisoformat(end_date)

    if provider:
        pool = {provider: _ALL_CONNECTORS[provider]} if provider in _ALL_CONNECTORS else {}
    elif category == "cloud":
        pool = _CLOUD_CONNECTORS
    elif category == "saas":
        pool = _SAAS_CONNECTORS
    else:
        pool = _ALL_CONNECTORS

    targets = await _active(pool)
    if not targets:
        return {"error": "No providers configured."}

    combined: dict[str, dict[str, float]] = {}
    errors: dict[str, str] = {}

    for name, connector in targets.items():
        try:
            summary = await connector.get_costs(sd, ed)
            for svc, amt in summary.by_service.items():
                if service_filter and service_filter.lower() not in svc.lower():
                    continue
                if svc not in combined:
                    combined[svc] = {}
                combined[svc][name] = combined[svc].get(name, 0.0) + amt
        except Exception as exc:
            errors[name] = str(exc)

    ranked = sorted(
        [
            {
                "service": svc,
                "total_usd": round(sum(by_prov.values()), 4),
                "total_formatted": _fmt_usd(sum(by_prov.values())),
                "by_provider": {k: round(v, 4) for k, v in by_prov.items()},
            }
            for svc, by_prov in combined.items()
        ],
        key=lambda x: -x["total_usd"],
    )

    result: dict[str, Any] = {
        "period": {"start": sd.isoformat(), "end": ed.isoformat()},
        "filter": service_filter,
        "services": ranked,
        "total_usd": round(sum(s["total_usd"] for s in ranked), 4),
    }
    if errors:
        result["errors"] = errors
    return result


@mcp.tool()
async def get_top_cost_drivers(
    limit: int = 10,
    provider: str | None = None,
    category: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Return the top N most expensive services across all configured providers.

    Args:
        limit: Number of top services to return (default 10).
        provider: Specific provider. None = all.
        category: "cloud" or "saas". None = all.
        start_date: ISO date. Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.

    Examples:
        - "What are our biggest cost drivers this month?"
        - "Top 5 most expensive things in AWS"
        - "What SaaS tools are costing us the most?"
    """
    result = await get_costs_by_service(
        service_filter=None,
        provider=provider,
        category=category,
        start_date=start_date,
        end_date=end_date,
    )
    if "error" in result:
        return result

    grand = result.get("total_usd", 0.0)
    top = result["services"][:limit]
    for svc in top:
        svc["pct_of_total"] = round(svc["total_usd"] / grand * 100, 1) if grand else 0

    return {
        "period": result["period"],
        "top_services": top,
        "grand_total_usd": grand,
        "grand_total_formatted": _fmt_usd(grand),
    }


@mcp.tool()
async def compare_providers(
    category: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Side-by-side cost comparison across all configured providers.

    Args:
        category: "cloud" or "saas". None = all.
        start_date: ISO date. Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.

    Examples:
        - "Which cloud are we spending the most on?"
        - "Compare our SaaS tool spending"
        - "How does AWS compare to Azure and GCP?"
    """
    sd, ed = _default_dates()
    if start_date:
        sd = date.fromisoformat(start_date)
    if end_date:
        ed = date.fromisoformat(end_date)

    pool = _CLOUD_CONNECTORS if category == "cloud" else _SAAS_CONNECTORS if category == "saas" else _ALL_CONNECTORS
    targets = await _active(pool)
    if not targets:
        return {"error": "No providers configured."}

    provider_totals: list[dict] = []
    grand_total = 0.0

    for name, connector in targets.items():
        try:
            summary = await connector.get_costs(sd, ed)
            provider_totals.append({
                "provider": name,
                "category": "cloud" if name in _CLOUD_CONNECTORS else "saas",
                "total_usd": round(summary.total_usd, 4),
                "total_formatted": _fmt_usd(summary.total_usd),
                "top_services": [
                    {"service": k, "amount_usd": round(v, 4)}
                    for k, v in sorted(summary.by_service.items(), key=lambda x: -x[1])[:5]
                ],
            })
            grand_total += summary.total_usd
        except Exception as exc:
            provider_totals.append({"provider": name, "error": str(exc)})

    for p in provider_totals:
        if "total_usd" in p:
            p["pct_of_total"] = round(p["total_usd"] / grand_total * 100, 1) if grand_total else 0

    provider_totals.sort(key=lambda x: -x.get("total_usd", 0))

    return {
        "period": {"start": sd.isoformat(), "end": ed.isoformat()},
        "grand_total_usd": round(grand_total, 4),
        "grand_total_formatted": _fmt_usd(grand_total),
        "providers": provider_totals,
    }


@mcp.tool()
async def get_cost_trends(
    provider: str | None = None,
    category: str | None = None,
    days: int = 30,
    granularity: str = "DAILY",
) -> dict:
    """
    Cost trends over time broken down by day or month.

    Args:
        provider: Specific provider. None = all.
        category: "cloud" or "saas". None = all.
        days: Look-back window in days (default 30).
        granularity: "DAILY" or "MONTHLY".

    Examples:
        - "Is our AWS spend trending up or down?"
        - "Show daily cloud costs for the last 2 weeks"
        - "What did we spend each month this quarter?"
    """
    end = date.today()
    start = end - timedelta(days=days)

    pool = _CLOUD_CONNECTORS if category == "cloud" else _SAAS_CONNECTORS if category == "saas" else _ALL_CONNECTORS
    if provider and provider in pool:
        pool = {provider: pool[provider]}

    targets = await _active(pool)
    if not targets:
        return {"error": "No providers configured."}

    grand_total, by_provider, _ = await _gather_costs(targets, start, end, granularity)

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(), "granularity": granularity},
        "grand_total_usd": round(grand_total, 4),
        "grand_total_formatted": _fmt_usd(grand_total),
        "by_provider": by_provider,
        "note": "For full time-series granularity, configure BigQuery exports (GCP) or Cost and Usage Reports (AWS).",
    }


@mcp.tool()
async def list_accounts(provider: str | None = None) -> dict:
    """
    List all cloud accounts, subscriptions, and SaaS org IDs that are accessible.

    Args:
        provider: Specific provider. None = all.
    """
    pool = {provider: _ALL_CONNECTORS[provider]} if provider and provider in _ALL_CONNECTORS else _ALL_CONNECTORS
    targets = await _active(pool)
    result: dict[str, list] = {}
    for name, connector in targets.items():
        try:
            result[name] = await connector.list_accounts()
        except Exception as exc:
            result[name] = [{"error": str(exc)}]
    return result


@mcp.tool()
async def get_saas_spend_summary(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Dedicated summary of all SaaS tool spending (Datadog, Snowflake, GitHub, etc.).
    Useful for understanding your software vendor bill separate from cloud infrastructure.

    Examples:
        - "How much are we spending on SaaS tools?"
        - "What's our total software vendor spend?"
        - "Break down our SaaS costs by tool"
    """
    return await get_cost_summary(category="saas", start_date=start_date, end_date=end_date)


@mcp.tool()
async def get_total_spend_all_sources(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Grand total across ALL connected sources — cloud infrastructure + SaaS tools combined.
    The true "total technology spend" number.

    Examples:
        - "What is our total tech spend this month?"
        - "How much are we spending on everything combined?"
        - "Give me our full cloud + software cost picture"
    """
    sd, ed = _default_dates()
    if start_date:
        sd = date.fromisoformat(start_date)
    if end_date:
        ed = date.fromisoformat(end_date)

    targets = await _active(_ALL_CONNECTORS)
    if not targets:
        return {"error": "No providers configured."}

    grand_total, by_provider, grand_by_service = await _gather_costs(targets, sd, ed)

    cloud_total = sum(
        by_provider[p]["total_usd"]
        for p in _CLOUD_CONNECTORS
        if p in by_provider and "total_usd" in by_provider[p]
    )
    saas_total = sum(
        by_provider[p]["total_usd"]
        for p in _SAAS_CONNECTORS
        if p in by_provider and "total_usd" in by_provider[p]
    )

    return {
        "period": {"start": sd.isoformat(), "end": ed.isoformat()},
        "grand_total_usd": round(grand_total, 4),
        "grand_total_formatted": _fmt_usd(grand_total),
        "cloud_total_usd": round(cloud_total, 4),
        "cloud_total_formatted": _fmt_usd(cloud_total),
        "saas_total_usd": round(saas_total, 4),
        "saas_total_formatted": _fmt_usd(saas_total),
        "cloud_pct": round(cloud_total / grand_total * 100, 1) if grand_total else 0,
        "saas_pct": round(saas_total / grand_total * 100, 1) if grand_total else 0,
        "by_provider": by_provider,
        "top_services": [
            {"service": k, "amount_usd": round(v, 4), "formatted": _fmt_usd(v)}
            for k, v in sorted(grand_by_service.items(), key=lambda x: -x[1])[:10]
        ],
    }


# ── Anomaly tools ────────────────────────────────────────────────────────────


@mcp.tool()
async def get_anomalies(
    provider: str | None = None,
    severity: str | None = None,
    limit: int = 20,
) -> dict:
    """
    Return active (unacknowledged) cost anomalies detected from historical baselines.

    Args:
        provider: Filter to a specific provider. None = all.
        severity: "high", "medium", or "low". None = all severities.
        limit: Max anomalies to return (default 20).

    Examples:
        - "Are there any cost anomalies I should know about?"
        - "Show me high-severity cost spikes"
        - "What spiked in AWS this week?"

    Note: Anomalies require at least 7 days of snapshot history.
          Run 'finops snapshot' or wait for the daily job to accumulate data.
    """

    from .anomaly.detector import get_active_anomalies

    rows = get_active_anomalies(provider=provider, severity=severity, limit=limit)
    if not rows:
        return {
            "anomalies": [],
            "message": "No active anomalies." if rows is not None else "No snapshot history yet — run daily snapshots first.",
        }

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

    return {
        "count": len(formatted),
        "anomalies": formatted,
        "tip": "Use acknowledge_anomaly(id) to dismiss resolved anomalies.",
    }


@mcp.tool()
async def acknowledge_anomaly(anomaly_id: int) -> dict:
    """
    Mark an anomaly as acknowledged (dismissed). It will no longer appear in active anomalies.

    Args:
        anomaly_id: The ID from get_anomalies().

    Examples:
        - "Dismiss anomaly 42 — it was a planned migration"
        - "Acknowledge that spike, it was expected"
    """
    if err := require_role("analyst"):
        return err

    from .anomaly.detector import acknowledge_anomaly as _ack
    ok = _ack(anomaly_id)
    return {"acknowledged": ok, "id": anomaly_id}


@mcp.tool()
async def get_cost_history(
    provider: str,
    service: str,
    account_id: str,
    days: int = 30,
) -> dict:
    """
    Return historical daily cost data for a specific provider + service.
    Used for trend analysis and understanding anomaly context.

    Args:
        provider: e.g. "aws"
        service: e.g. "Amazon EC2"
        account_id: The account/subscription ID
        days: Look-back window in days (default 30)

    Examples:
        - "Show me 30 days of history for AWS EC2"
        - "What did Datadog cost each day this month?"
    """
    from .storage.snapshots import get_history

    rows = get_history(provider, service, account_id, days=days)
    if not rows:
        return {
            "data": [],
            "message": "No history found. Ensure daily snapshots are running.",
        }

    amounts = [r["amount_usd"] for r in rows]
    import statistics
    return {
        "provider": provider,
        "service": service,
        "account_id": account_id,
        "days_of_data": len(rows),
        "mean_usd": round(statistics.mean(amounts), 4) if amounts else 0,
        "max_usd": round(max(amounts), 4) if amounts else 0,
        "min_usd": round(min(amounts), 4) if amounts else 0,
        "data": [
            {"date": r["snapshot_date"], "amount_usd": round(r["amount_usd"], 4)}
            for r in rows
        ],
    }


@mcp.tool()
async def take_snapshot_now() -> dict:
    """
    Manually trigger a cost snapshot right now (fetches yesterday's costs from all providers).
    Normally this runs automatically at 01:00 UTC daily.

    Examples:
        - "Take a cost snapshot now"
        - "Update the cost history with today's data"
    """
    from .scheduler.jobs import run_snapshot_now
    results = await run_snapshot_now()
    return {"status": "complete", "results": results}


# ── Attribution tools ─────────────────────────────────────────────────────────


@mcp.tool()
async def get_costs_by_team(
    start_date: str | None = None,
    end_date: str | None = None,
    provider: str | None = None,
) -> dict:
    """
    Return cloud costs broken down by engineering team, using tag attribution rules.

    Requires:
    - Tag rules configured in ~/.finops/tag_rules.yaml (run 'finops setup' → tags)
    - Cloud providers that support tag-based cost grouping (AWS, Azure, GCP)

    Args:
        start_date: ISO date. Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.
        provider: Filter to a specific provider.

    Examples:
        - "How much is the data team spending?"
        - "Show me cloud costs by team this month"
        - "Which team has the highest AWS bill?"
    """

    from .storage.snapshots import get_costs_by_team as _get

    sd, ed = _default_dates()
    if start_date:
        sd = date.fromisoformat(start_date)
    if end_date:
        ed = date.fromisoformat(end_date)

    rows = _get(sd, ed, provider=provider)
    if not rows:
        return {
            "data": [],
            "message": (
                "No attributed cost data found. "
                "Ensure tag_rules.yaml is configured and run 'take_snapshot_now' to populate data."
            ),
        }

    by_team: dict[str, float] = {}
    for r in rows:
        team = r["team"] or "unattributed"
        by_team[team] = by_team.get(team, 0.0) + float(r["total_usd"])

    grand = sum(by_team.values())
    ranked = sorted(
        [{"team": t, "total_usd": round(v, 4), "total_formatted": _fmt_usd(v), "pct": round(v / grand * 100, 1) if grand else 0}
         for t, v in by_team.items()],
        key=lambda x: -x["total_usd"],
    )

    return {
        "period": {"start": sd.isoformat(), "end": ed.isoformat()},
        "grand_total_usd": round(grand, 4),
        "grand_total_formatted": _fmt_usd(grand),
        "by_team": ranked,
    }


@mcp.tool()
async def run_attribution_now(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Fetch tagged cost data from AWS/Azure/GCP and store team attributions.
    Run this after setting up tag_rules.yaml to populate team cost data.

    Args:
        start_date: ISO date. Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.

    Examples:
        - "Run tag attribution now"
        - "Update team cost data"
    """

    from .attribution.fetcher import fetch_aws_tagged_costs
    from .attribution.mapper import _load_rules
    from .storage.snapshots import store_attributed_cost

    sd, ed = _default_dates()
    if start_date:
        sd = date.fromisoformat(start_date)
    if end_date:
        ed = date.fromisoformat(end_date)

    cfg = _load_rules()
    tag_keys = list({r.get("tag_key", "") for r in cfg.get("rules", []) if r.get("tag_key")})

    total_stored = 0
    errors: dict[str, str] = {}

    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ROLE_ARNS"):
        try:
            role_arns = [a.strip() for a in os.environ.get("AWS_ROLE_ARNS", "").split(",") if a.strip()]
            rows = fetch_aws_tagged_costs(sd, ed, tag_keys, role_arns or None)
            for row in rows:
                attr = row["attribution"]
                store_attributed_cost(
                    provider="aws",
                    service=row["service"],
                    account_id=row["account_id"],
                    team=attr.get("team", "unattributed"),
                    environment=attr.get("environment", ""),
                    snapshot_date=sd,
                    amount_usd=row["amount_usd"],
                )
                total_stored += 1
        except Exception as e:
            errors["aws"] = str(e)

    return {
        "status": "complete",
        "records_stored": total_stored,
        "errors": errors,
        "period": {"start": sd.isoformat(), "end": ed.isoformat()},
        "tip": "If data is empty, check that ~/.finops/tag_rules.yaml is configured with your tag keys.",
    }


# ── Notification tools ────────────────────────────────────────────────────────


@mcp.tool()
async def send_digest_now() -> dict:
    """
    Manually trigger a cost digest to Slack and/or Teams right now.
    Normally this sends automatically at 09:00 UTC daily.

    Examples:
        - "Send the daily cost digest to Slack"
        - "Push the current cost summary to Teams"
    """
    if err := require_role("analyst"):
        return err

    from .scheduler.jobs import run_digest_now
    sent = await run_digest_now()
    return {
        "sent": sent,
        "message": "Digest sent." if sent else "No notification channels configured. Run 'finops setup slack' or 'finops setup teams'.",
    }


@mcp.tool()
async def check_notification_config() -> dict:
    """
    Check which notification channels (Slack, Teams) are configured and active.

    Examples:
        - "Is Slack configured for alerts?"
        - "Where are cost alerts being sent?"
    """
    from .notifications import slack, teams

    return {
        "slack": {
            "configured": slack.is_configured(),
            "method": "webhook" if os.environ.get("SLACK_WEBHOOK_URL") else "bot_token" if os.environ.get("SLACK_BOT_TOKEN") else "none",
            "channel": os.environ.get("SLACK_CHANNEL", "#finops-alerts"),
        },
        "teams": {
            "configured": teams.is_configured(),
        },
        "schedule": {
            "snapshot": os.environ.get("FINOPS_SNAPSHOT_CRON", "0 1 * * * (01:00 UTC)"),
            "anomaly_check": os.environ.get("FINOPS_ANOMALY_CRON", "0 2 * * * (02:00 UTC)"),
            "daily_digest": os.environ.get("FINOPS_DIGEST_CRON", "0 9 * * * (09:00 UTC)"),
        },
    }


# ── Vault tools (read-only — never expose values) ─────────────────────────────


@mcp.tool()
async def list_vault_credentials() -> dict:
    """
    List the names of credentials stored in the encrypted vault (never the values).

    Examples:
        - "What credentials are stored in the vault?"
        - "Which providers have been configured via setup?"
    """
    try:
        from .security.vault import Vault
        vault = Vault.default()
        keys = [k for k in vault.list_keys() if not k.startswith("_")]  # hide internal keys
        return {
            "count": len(keys),
            "credentials": keys,
            "note": "Values are never exposed. Use 'finops setup' CLI to add or update credentials.",
        }
    except Exception as e:
        return {"error": str(e)}


# ── Rightsizing & commitment tools ────────────────────────────────────────────

@mcp.tool()
async def get_rightsizing_recommendations(
    avg_cpu_threshold: float = 20.0,
    max_cpu_threshold: float = 50.0,
) -> dict:
    """
    Analyze EC2 instances with low CPU utilization over the past 14 days and
    return rightsizing recommendations with projected monthly savings.

    Args:
        avg_cpu_threshold: Flag instances with average CPU below this % (default 20%)
        max_cpu_threshold: Flag instances whose peak CPU never exceeded this % (default 50%)

    Examples:
        - "Which EC2 instances are over-provisioned?"
        - "How much could we save by rightsizing?"
        - "Find underutilized instances we should downsize"
    """

    try:
        from .recommendations.rightsizing import analyze_rightsizing, rightsizing_summary
        recs = analyze_rightsizing(
            avg_cpu_threshold=avg_cpu_threshold,
            max_cpu_threshold=max_cpu_threshold,
        )
        return rightsizing_summary(recs)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def get_commitment_analysis() -> dict:
    """
    Analyze Reserved Instance and Savings Plan coverage, utilization, and waste.
    Coverage %, utilization, and waste figures are free.
    Purchase recommendations with $ amounts require Pro (commitment_recommendations).

    Examples:
        - "How well are we using our Reserved Instances?"
        - "Should we buy more Savings Plans?"
        - "How much are we wasting on unused RIs?"
        - "What's our RI/SP coverage?"
    """
    try:
        from .recommendations.commitments import analyze_commitments, commitment_summary
        analysis = analyze_commitments()
        if analysis is None:
            return {"error": "AWS not configured. Run: finops setup aws"}
        result = commitment_summary(analysis)
        # Strip purchase recommendations on free tier — coverage/utilization/waste stays free
        if require_pro("commitment_recommendations") is not None:
            result["recommendations"] = [
                r for r in result.get("recommendations", []) if r.get("type") == "warning"
            ]
            result["recommendations_note"] = (
                "Purchase recommendations ($ amounts, ROI) require Pro. "
                f"Upgrade at {_UPGRADE_URL}"
            )
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def create_anomaly_tickets(limit: int = 20) -> dict:
    """
    Create tickets in Jira, Linear, or GitHub Issues for all active high/medium
    anomalies that don't already have a ticket. Uses the first configured
    ticketing provider.

    Args:
        limit: Max number of anomalies to process (default 20)

    Examples:
        - "Create Jira tickets for all cost anomalies"
        - "File GitHub issues for the anomalies"
        - "Open Linear tasks for cost spikes"
    """
    if err := require_pro("ticket_creation"):
        return err

    try:
        from .integrations.ticketing import create_tickets_for_unnotified
        urls = create_tickets_for_unnotified(limit=limit)
        return {
            "tickets_created": len(urls),
            "ticket_urls": urls,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def check_notification_config() -> dict:
    """
    Check which ticketing providers (Jira, Linear, GitHub Issues) are
    configured via environment variables.

    Examples:
        - "Is Jira connected?"
        - "Which ticket providers are set up?"
        - "Check my notification config"
    """
    try:
        from .integrations.ticketing import list_configured_providers
        providers = list_configured_providers()
        return {
            "configured_providers": providers,
            "active_provider": providers[0] if providers else None,
            "setup_instructions": {
                "jira": "Set JIRA_BASE_URL, JIRA_API_TOKEN, JIRA_USER_EMAIL, JIRA_PROJECT_KEY",
                "linear": "Set LINEAR_API_KEY, LINEAR_TEAM_ID",
                "github": "Set GITHUB_TOKEN, GITHUB_FINOPS_REPO (e.g. myorg/finops-alerts)",
            } if not providers else {},
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def create_rightsizing_tickets(
    min_monthly_savings: float = 100.0,
    provider: str = "aws",
) -> dict:
    """
    Create tickets for rightsizing recommendations — over-provisioned EC2, RDS,
    and other resources that could be downsized to save money.

    Args:
        min_monthly_savings: Only ticket recommendations above this threshold (default $100/mo)
        provider: Cloud provider to pull recommendations from (default: aws)

    Examples:
        - "Create Jira tickets for all rightsizing opportunities"
        - "File issues for EC2 instances we should downsize"
        - "Open Linear tasks for $500+ monthly rightsizing savings"
    """
    if err := require_pro("ticket_creation"):
        return err

    try:
        from .integrations.ticketing import create_rightsizing_ticket
        from .recommendations.rightsizing import get_rightsizing_recommendations

        recs = get_rightsizing_recommendations(provider=provider)
        if not recs:
            return {"message": "No rightsizing recommendations found", "tickets_created": 0}

        urls = []
        skipped = 0
        for rec in recs:
            savings = rec.get("monthly_savings_usd", 0)
            if savings < min_monthly_savings:
                skipped += 1
                continue
            url = create_rightsizing_ticket(rec)
            if url:
                urls.append({"resource": rec.get("resource_id"), "savings": savings, "url": url})

        return {
            "tickets_created": len(urls),
            "skipped_below_threshold": skipped,
            "threshold_usd": min_monthly_savings,
            "tickets": urls,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def create_kubernetes_waste_tickets(
    min_monthly_waste: float = 50.0,
) -> dict:
    """
    Create tickets for Kubernetes waste findings: idle nodes, over-provisioned
    workloads, and orphaned Helm releases.

    Args:
        min_monthly_waste: Only ticket findings above this threshold (default $50/mo)

    Examples:
        - "Create tickets for all Kubernetes waste"
        - "File Jira issues for idle K8s nodes"
        - "Open issues for orphaned Helm releases"
    """
    if err := require_pro("ticket_creation"):
        return err

    try:
        from .connectors.kubernetes import KubernetesConnector
        from .connectors.helm import discover_helm_releases
        from .integrations.ticketing import create_kubernetes_waste_ticket

        urls = []
        k8s_conn = KubernetesConnector()

        # Idle nodes and over-provisioned workloads
        # report is a ClusterReport dataclass; node_utilization is list[dict]
        reports = k8s_conn.analyze_all_clusters()
        for report in reports:
            # Idle nodes — idle_nodes is list[str] of node names
            for node in report.node_utilization:
                if node["node"] in report.idle_nodes and node["monthly_cost"] >= min_monthly_waste:
                    finding = {
                        "kind": "idle_node",
                        "cluster": report.cluster,
                        "name": node["node"],
                        "monthly_waste_usd": node["monthly_cost"],
                        "detail": (
                            f"CPU: {node.get('cpu_requested_pct', 0):.0f}%, "
                            f"Mem: {node.get('mem_requested_pct', 0):.0f}% utilized"
                        ),
                    }
                    url = create_kubernetes_waste_ticket(finding)
                    if url:
                        urls.append({"type": "idle_node", "name": node["node"], "url": url})

            # Over-provisioned workloads — rightsizing_opportunities is list[dict]
            for opp in report.rightsizing_opportunities:
                waste = opp.get("potential_savings_usd", 0)
                if waste >= min_monthly_waste:
                    finding = {
                        "kind": "over_requested",
                        "cluster": report.cluster,
                        "namespace": opp.get("namespace", ""),
                        "name": opp.get("workload", ""),
                        "monthly_waste_usd": waste,
                        "detail": "; ".join(opp.get("issues", [])),
                    }
                    url = create_kubernetes_waste_ticket(finding)
                    if url:
                        urls.append({"type": "over_provisioned", "name": opp.get("workload"), "url": url})

        # Orphaned Helm releases — discover_helm_releases requires a k8s client
        try:
            k8s_client = k8s_conn._load_client()
            releases = discover_helm_releases(k8s_client)
            for rel in releases:
                if rel.is_orphaned and rel.monthly_cost >= min_monthly_waste:
                    finding = {
                        "kind": "orphaned_helm",
                        "cluster": "default",
                        "namespace": rel.namespace,
                        "name": rel.name,
                        "monthly_waste_usd": rel.monthly_cost,
                        "detail": (
                            f"Chart: {rel.chart}, deployed "
                            f"{rel.deployed_at[:10] if rel.deployed_at else 'unknown'}, "
                            f"0 running pods"
                        ),
                    }
                    url = create_kubernetes_waste_ticket(finding)
                    if url:
                        urls.append({"type": "orphaned_helm", "name": rel.name, "url": url})
        except Exception:
            pass  # Helm optional

        return {
            "tickets_created": len(urls),
            "tickets": urls,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def create_scorecard_tickets(
    score_threshold: int = 50,
    team: str = "",
) -> dict:
    """
    Create tickets for scorecard dimensions scoring below a threshold.
    Helps teams track and remediate FinOps efficiency gaps.

    Args:
        score_threshold: Create tickets for dimensions below this score (default 50)
        team: Scope to a specific team tag (optional)

    Examples:
        - "Create tickets for all failing scorecard dimensions"
        - "File issues for the platform team's low scores"
        - "Open Jira tasks for scorecard dimensions below 40"
    """
    if err := require_pro("ticket_creation"):
        return err

    try:
        from .scoring.scorecard import build_scorecard
        from .integrations.ticketing import create_scorecard_ticket

        tag_filter = {"team": team} if team else None
        scorecard = build_scorecard(tag_filter=tag_filter)

        if not scorecard:
            return {"error": "Could not build scorecard"}

        urls = []
        for dim in scorecard.as_dict().get("dimensions", []):
            if dim.get("score", 100) < score_threshold:
                url = create_scorecard_ticket(dim, team=team)
                if url:
                    urls.append({
                        "dimension": dim["dimension"],
                        "score": dim["score"],
                        "grade": dim["grade"],
                        "url": url,
                    })

        return {
            "tickets_created": len(urls),
            "overall_score": scorecard.as_dict().get("overall_score"),
            "overall_grade": scorecard.as_dict().get("grade"),
            "tickets": urls,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def fetch_invoice_emails() -> dict:
    """
    Fetch unread invoice emails from the configured IMAP mailbox, extract
    amounts, and store them as cost entries. Solves the billing API gap for
    vendors like PagerDuty, New Relic, and GitHub Enterprise.

    Examples:
        - "Parse our billing inbox for new invoices"
        - "How much did PagerDuty charge us this month? (after forwarding invoice)"
        - "Fetch and store any new vendor invoices"
    """
    try:
        from .connectors.invoice.parser import fetch_and_store_invoices
        stored = fetch_and_store_invoices()
        if not stored:
            host = os.environ.get("FINOPS_INVOICE_IMAP_HOST", "")
            if not host:
                return {
                    "invoices_stored": 0,
                    "message": "No IMAP mailbox configured. Run: finops setup invoice",
                }
        return {
            "invoices_stored": len(stored),
            "invoices": stored,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def send_weekly_digest_now() -> dict:
    """
    Immediately send the weekly email digest to the configured recipient.
    Includes spend summary, anomalies, and top rightsizing recommendations.
    Works without Claude — pure standalone email.

    Examples:
        - "Send the weekly cost digest now"
        - "Trigger the weekly email report"
    """
    if err := require_pro("scheduled_email_digests"):
        return err

    try:
        from .scheduler.jobs import job_weekly_email_digest
        job_weekly_email_digest()
        to = os.environ.get("FINOPS_DIGEST_TO", "")
        return {
            "sent": True,
            "recipient": to or "configured address",
            "note": "Check FINOPS_DIGEST_TO / FINOPS_SMTP_* env vars if not received.",
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def list_idle_resources(
    resource_types: list[str] | None = None,
    regions: list[str] | None = None,
    min_idle_days: int = 7,
) -> dict:
    """
    Scan for idle/wasted AWS resources that are costing money but doing nothing.

    Finds: unattached EBS volumes, unused Elastic IPs, old snapshots with no AMI
    dependency, stopped EC2 instances (still paying for EBS), load balancers
    with no healthy targets.

    Results are sorted by monthly waste descending. Protected resources
    (tagged env=prod, protected=true, etc.) are flagged but never acted on.

    Examples:
        - "Find idle resources wasting money in AWS"
        - "List any unattached EBS volumes older than 90 days"
        - "What stopped EC2 instances are we still paying for?"
    """
    try:
        from .cleanup.idle import scan_idle_resources, idle_resources_summary
        resources = scan_idle_resources(
            resource_types=resource_types,
            regions=regions,
            min_idle_days=min_idle_days,
        )
        return idle_resources_summary(resources)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def cleanup_idle_resources(
    resource_ids: list[str] | None = None,
    resource_types: list[str] | None = None,
    regions: list[str] | None = None,
    min_idle_days: int = 7,
    dry_run: bool = True,
) -> dict:
    """
    Delete or release idle AWS resources. ALWAYS confirm with the user before
    setting dry_run=False. Protected resources are never touched.

    Requires FINOPS_CLEANUP_ENABLED=true in the environment (opt-in).
    Every action is written to ~/.finops-mcp/cleanup_audit.jsonl.

    dry_run=True (default): shows what WOULD be deleted, nothing is changed.
    dry_run=False: actually deletes. Only set this after explicit user confirmation.

    Examples:
        - "Show me what would happen if I cleaned up unattached EBS volumes"
        - "Delete the EBS volumes we just listed" (then confirm → dry_run=False)
        - "Clean up all unused Elastic IPs in us-east-1"
    """
    if err := require_role("admin"):
        return err
    try:
        from .cleanup.actions import cleanup_resources
        return cleanup_resources(
            resource_ids=resource_ids or [],
            dry_run=dry_run,
            resource_types=resource_types,
            regions=regions,
            min_idle_days=min_idle_days,
        )
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def get_effective_rate_profile() -> dict:
    """
    Auto-detect the account's effective private rates by comparing actual
    billed amounts against public on-demand prices.

    Captures EDP discounts, MOSA/negotiated rates, and private pricing
    automatically from Cost Explorer or CUR — no manual input needed.

    Used internally by the commitment optimizer and PR cost estimator.
    Useful for understanding how large your negotiated discount actually is.

    Examples:
        - "What's our effective AWS discount?"
        - "Do we have private pricing on AWS?"
        - "How does our actual rate compare to on-demand list prices?"
    """
    try:
        from .recommendations.rate_detector import detect_effective_rates
        profile = detect_effective_rates()
        result: dict = {
            "source": profile.source,
            "confidence": profile.confidence,
            "has_private_pricing": profile.has_private_pricing,
            "overall_discount_pct": round(profile.overall_discount_pct * 100, 1),
            "note": (
                f"Your effective rate is {profile.overall_discount_pct*100:.1f}% below public "
                f"on-demand prices (detected from {profile.source}, confidence: {profile.confidence})."
            ) if profile.has_private_pricing else (
                "No significant private pricing detected. Public on-demand rates apply."
            ),
        }
        if profile.per_service_discount:
            top = sorted(profile.per_service_discount.items(), key=lambda x: x[1], reverse=True)[:8]
            result["top_service_discounts"] = [
                {"service": k, "discount_pct": round(v * 100, 1)} for k, v in top
            ]
        result["metadata"] = profile.metadata
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def get_kubernetes_costs(
    context: str | None = None,
    namespace: str | None = None,
) -> dict:
    """
    Full Kubernetes cost breakdown — node costs attributed to namespaces,
    workloads, and labels. Detects wasted spend and rightsizing opportunities.

    Requires: pip install finops-mcp[kubernetes]
    Optional: metrics-server in-cluster for actual CPU/memory usage data.

    Examples:
        - "How much does our Kubernetes cluster cost?"
        - "Which namespace is spending the most?"
        - "Show me wasted Kubernetes spend"
        - "Which pods are over-provisioned?"
        - "What's our cluster CPU efficiency?"
    """
    try:
        from .connectors.kubernetes import KubernetesConnector
    except ImportError:
        return {"error": "kubernetes package not installed. Run: pip install finops-mcp[kubernetes]"}

    try:
        connector = KubernetesConnector()
        if not await connector.is_configured():
            return {"error": "No kubeconfig found. Set KUBECONFIG or ensure ~/.kube/config exists."}

        report = connector.analyze_cluster(context)

        # Persist to DB for trend analysis
        try:
            connector.persist_to_db(report)
        except Exception as e:
            log.warning("Failed to persist k8s data: %s", e)

        # Filter to namespace if requested
        workloads = report.workloads
        if namespace:
            workloads = [w for w in workloads if w.namespace == namespace]

        result: dict = {
            "cluster": report.cluster,
            "provider": report.provider,
            "node_count": report.node_count,
            "pod_count": report.pod_count,
            "total_monthly_cost_usd": report.total_monthly_cost,
            "pvc_storage_cost_usd": report.pvc_monthly_cost,
            "wasted_monthly_cost_usd": report.wasted_monthly_cost,
            "waste_pct": round(report.wasted_monthly_cost / report.total_monthly_cost * 100, 1)
                         if report.total_monthly_cost > 0 else 0,
        }

        if report.overall_cpu_efficiency is not None:
            result["cpu_efficiency_pct"] = report.overall_cpu_efficiency
            result["mem_efficiency_pct"] = report.overall_mem_efficiency

        if report.idle_nodes:
            result["idle_nodes"] = report.idle_nodes
            idle_cost = sum(
                n["monthly_cost"] for n in report.node_utilization
                if n["node"] in report.idle_nodes
            )
            result["idle_node_cost_usd"] = round(idle_cost, 2)

        # Cost by namespace
        ns_costs: dict[str, float] = {}
        for w in report.workloads:
            ns_costs[w.namespace] = ns_costs.get(w.namespace, 0) + w.monthly_cost
        result["cost_by_namespace"] = dict(
            sorted(ns_costs.items(), key=lambda x: x[1], reverse=True)
        )

        # Top workloads
        result["top_workloads"] = [
            {
                "namespace": w.namespace,
                "workload": f"{w.workload_kind}/{w.workload_name}",
                "pods": w.pod_count,
                "monthly_cost_usd": w.monthly_cost,
                "wasted_usd": w.wasted_usd,
                "cpu_efficiency_pct": w.cpu_efficiency_pct,
                "mem_efficiency_pct": w.mem_efficiency_pct,
                "labels": w.labels,
            }
            for w in workloads[:20]
        ]

        # Rightsizing opportunities
        if report.rightsizing_opportunities:
            result["rightsizing_opportunities"] = report.rightsizing_opportunities[:10]
            result["total_recoverable_usd"] = round(
                sum(r["potential_savings_usd"] for r in report.rightsizing_opportunities), 2
            )

        # Node utilization summary
        result["node_utilization"] = report.node_utilization

        # Human-readable summary
        lines = [
            f"Cluster: {report.cluster} ({report.provider.upper()}, {report.node_count} nodes)",
            f"Total cost: ${report.total_monthly_cost:,.0f}/month",
        ]
        if report.wasted_monthly_cost > 10:
            lines.append(
                f"Estimated waste: ${report.wasted_monthly_cost:,.0f}/month "
                f"({result['waste_pct']:.0f}% of cluster cost)"
            )
        if report.overall_cpu_efficiency is not None:
            lines.append(
                f"Efficiency: {report.overall_cpu_efficiency:.0f}% CPU, "
                f"{report.overall_mem_efficiency:.0f}% memory"
            )
        if report.idle_nodes:
            lines.append(
                f"{len(report.idle_nodes)} idle node(s) detected "
                f"(${result.get('idle_node_cost_usd', 0):,.0f}/month)"
            )
        top3_ns = list(result["cost_by_namespace"].items())[:3]
        if top3_ns:
            ns_str = ", ".join(f"{ns}: ${c:,.0f}" for ns, c in top3_ns)
            lines.append(f"Top namespaces: {ns_str}")
        result["summary"] = " | ".join(lines)

        return result

    except Exception as e:
        log.exception("Kubernetes cost analysis failed")
        return {"error": str(e)}


@mcp.tool()
async def get_kubernetes_namespace_breakdown(namespace: str) -> dict:
    """
    Deep-dive cost breakdown for a single Kubernetes namespace.
    Shows every workload, pod count, CPU/memory efficiency, and waste.

    Examples:
        - "Break down costs in the production namespace"
        - "Which services in 'data-platform' are most expensive?"
        - "Show me waste in the staging namespace"
    """
    return await get_kubernetes_costs(namespace=namespace)


@mcp.tool()
async def get_efficiency_scorecard(
    scope: str = "overall",
    team: str | None = None,
    environment: str | None = None,
    provider: str | None = None,
) -> dict:
    """
    FinOps efficiency scorecard — a 0–100 score with letter grade across
    5 dimensions: compute efficiency, waste reduction, commitment coverage,
    tag hygiene, and anomaly response. Tracked over time so you can see
    if you're improving.

    Scope options:
      - "overall"         — everything combined (default)
      - team=platform     — filter by team tag
      - environment=prod  — filter by environment tag
      - provider=aws      — single provider view

    Examples:
        - "What's our FinOps score?"
        - "Show me the efficiency scorecard for the platform team"
        - "How is our AWS efficiency rated?"
        - "What's our worst performing dimension?"
        - "Are we improving or getting worse on cloud efficiency?"
    """
    from .scoring.scorecard import build_scorecard

    # Build scope identifier and label
    if team:
        scope = f"team:{team}"
        label = f"{team.title()} team"
    elif environment:
        scope = f"env:{environment}"
        label = f"{environment.title()} environment"
    elif provider:
        scope = f"provider:{provider}"
        label = f"{provider.upper()}"
    else:
        scope = "overall"
        label = "Overall"

    try:
        # Gather available data for scoring
        k8s_reports = None
        idle_res     = None
        commitment   = None

        # Try Kubernetes
        try:
            from .connectors.kubernetes import KubernetesConnector
            conn = KubernetesConnector()
            if await conn.is_configured():
                k8s_reports = conn.analyze_all_clusters()
        except Exception:
            pass

        # Try idle resources from DB
        try:
            from .storage.db import get_engine, resource_inventory
            from sqlalchemy import select
            with get_engine().connect() as db:
                rows = db.execute(
                    select(resource_inventory).where(
                        resource_inventory.c.is_active == True,
                        resource_inventory.c.monthly_cost_usd == 0.0,
                    ).limit(100)
                ).fetchall()
                idle_res = [dict(r._mapping) for r in rows] if rows else None
        except Exception:
            pass

        # Try commitment data — scoped by tag when filtering by team/env
        tag_filter: dict | None = None
        if team:
            tag_filter = {"team": team}
        elif environment:
            tag_filter = {"env": environment}

        try:
            from .recommendations.commitments import analyze_commitments
            raw_commits = analyze_commitments(tag_filter=tag_filter)
            if raw_commits:
                commitment = {
                    "coverage_pct": (
                        raw_commits.savings_plan_coverage_pct +
                        raw_commits.ri_coverage_pct
                    ) / 2,
                    "on_demand_usd": raw_commits.uncovered_on_demand_usd,
                    "potential_savings_usd": sum(
                        r.get("monthly_savings", 0)
                        for r in raw_commits.recommendations
                        if r.get("type") != "warning"
                    ),
                }
        except Exception:
            pass

        # Get total spend from DB snapshots
        total_spend = 0.0
        try:
            from .storage.db import cost_snapshots, get_engine
            from sqlalchemy import select, func
            cutoff = (date.today() - timedelta(days=30)).isoformat()
            with get_engine().connect() as db:
                row = db.execute(
                    select(func.sum(cost_snapshots.c.amount_usd)).where(
                        cost_snapshots.c.snapshot_date >= cutoff
                    )
                ).scalar()
                total_spend = float(row or 0)
        except Exception:
            pass

        # Try tag coverage from attributed vs total costs
        untagged_spend = 0.0
        try:
            from .storage.db import attributed_costs, cost_snapshots, get_engine
            from sqlalchemy import select, func
            cutoff = (date.today() - timedelta(days=30)).isoformat()
            with get_engine().connect() as db:
                attributed = db.execute(
                    select(func.sum(attributed_costs.c.amount_usd)).where(
                        attributed_costs.c.snapshot_date >= cutoff,
                        attributed_costs.c.team != "unattributed",
                    )
                ).scalar() or 0
                untagged_spend = max(0.0, total_spend - float(attributed))
        except Exception:
            pass

        scorecard = build_scorecard(
            scope=scope,
            label=label,
            k8s_reports=k8s_reports,
            idle_resources=idle_res,
            commitment_data=commitment,
            untagged_spend_usd=untagged_spend,
            total_monthly_spend=total_spend,
            tag_filter=tag_filter,
        )

        return scorecard.as_dict()

    except Exception as e:
        log.exception("Scorecard generation failed")
        return {"error": str(e)}


@mcp.tool()
async def get_team_scorecards() -> dict:
    """
    Efficiency scorecard for every team, side by side.
    Teams are discovered from your cost attribution tags (team=X).
    Shows which teams are leading and which need help.

    Examples:
        - "Show me efficiency scores for all teams"
        - "Which team has the worst FinOps score?"
        - "Compare cloud efficiency across teams"
        - "Who is leading on waste reduction?"
    """
    from .scoring.scorecard import build_scorecard
    from datetime import timedelta

    try:
        # Discover teams from attribution data
        teams: list[str] = []
        try:
            from .storage.db import attributed_costs, get_engine
            from sqlalchemy import select, distinct
            cutoff = (date.today() - timedelta(days=30)).isoformat()
            with get_engine().connect() as db:
                rows = db.execute(
                    select(distinct(attributed_costs.c.team)).where(
                        attributed_costs.c.snapshot_date >= cutoff,
                        attributed_costs.c.team != "unattributed",
                        attributed_costs.c.team != "",
                    )
                ).fetchall()
                teams = [r[0] for r in rows]
        except Exception:
            pass

        if not teams:
            return {
                "error": "No team attribution data found. "
                         "Run `run_attribution_now` first to tag spend by team, "
                         "or ensure resources have a 'team' tag."
            }

        scorecards = []
        for team in teams[:10]:  # cap at 10 teams
            sc = build_scorecard(scope=f"team:{team}", label=f"{team} team")
            scorecards.append({
                "team": team,
                "score": sc.total_score,
                "grade": sc.grade,
                "trend": sc.trend,
                "trend_delta": sc.trend_delta,
                "potential_savings_usd": sc.potential_savings_usd,
                "dimensions": {d.name: round(d.raw_score, 1) for d in sc.dimensions},
                "top_win": sc.top_wins[0] if sc.top_wins else None,
            })

        scorecards.sort(key=lambda s: s["score"])

        leader    = max(scorecards, key=lambda s: s["score"])
        laggard   = min(scorecards, key=lambda s: s["score"])
        avg_score = statistics.mean(s["score"] for s in scorecards)

        return {
            "team_count": len(scorecards),
            "average_score": round(avg_score, 1),
            "leader": leader["team"],
            "needs_most_help": laggard["team"],
            "teams": scorecards,
            "summary": (
                f"{len(scorecards)} teams scored. "
                f"Avg: {avg_score:.0f}/100. "
                f"Leader: {leader['team']} ({leader['grade']}, {leader['score']:.0f}pts). "
                f"Most opportunity: {laggard['team']} ({laggard['grade']}, {laggard['score']:.0f}pts)."
            ),
        }

    except Exception as e:
        log.exception("Team scorecards failed")
        return {"error": str(e)}


@mcp.tool()
async def get_commitment_coverage_by_tag(
    tag_key: str,
    tag_value: str,
    tag_coverage_pct: float = 100.0,
) -> dict:
    """
    Estimate RI/SP commitment coverage for a specific tag slice,
    even when tagging is incomplete.

    At 70% tag coverage we measure the tagged resources directly via
    Cost Explorer, then solve algebraically for the untagged 30% using
    account totals — producing a full-domain estimate with confidence rating.

    Args:
        tag_key:          Tag key to filter on (e.g. "domain", "team", "service")
        tag_value:        Tag value (e.g. "payments", "platform", "checkout-api")
        tag_coverage_pct: How complete the tagging is for this domain (0–100).
                          If unknown, leave at 100 and interpret results as
                          lower bounds only.

    Examples:
        - "What's the RI coverage for the payments domain? Tags are about 70% complete"
        - "How covered is team=platform under Savings Plans?"
        - "Estimate commitment coverage for env=prod with 85% tag coverage"
    """
    try:
        from .recommendations.commitments import estimate_coverage_for_partial_tag

        result = estimate_coverage_for_partial_tag(
            tag_key=tag_key,
            tag_value=tag_value,
            tag_coverage_pct=tag_coverage_pct,
        )

        if not result:
            return {"error": "Could not fetch coverage data. Ensure AWS Cost Explorer is enabled."}

        is_partial = tag_coverage_pct < 95

        out: dict = {
            "tag": f"{tag_key}={tag_value}",
            "tag_coverage_pct": tag_coverage_pct,
            "confidence": result.confidence,
            "confidence_note": result.confidence_note,

            # What we can measure directly
            "directly_measured": {
                "tagged_spend_usd": result.tagged_spend_usd,
                "sp_coverage_pct": result.tagged_sp_coverage_pct,
                "ri_coverage_pct": result.tagged_ri_coverage_pct,
                "note": f"Covers {tag_coverage_pct:.0f}% of resources with {tag_key}={tag_value}",
            },
        }

        if is_partial:
            # Surface the residual inference
            out["inferred_untagged"] = {
                "untagged_spend_usd": result.untagged_spend_usd,
                "inferred_sp_coverage_pct": result.inferred_untagged_sp_coverage_pct,
                "inferred_ri_coverage_pct": result.inferred_untagged_ri_coverage_pct,
                "note": (
                    f"Inferred from account totals for the {100 - tag_coverage_pct:.0f}% "
                    f"of resources without the {tag_key} tag"
                ),
            }
            out["full_domain_estimate"] = {
                "sp_coverage_pct": result.estimated_sp_coverage_pct,
                "ri_coverage_pct": result.estimated_ri_coverage_pct,
                "combined_coverage_pct": result.estimated_combined_coverage_pct,
                "note": "Weighted blend of measured + inferred",
            }

        coverage = result.estimated_combined_coverage_pct if is_partial else (
            (result.tagged_sp_coverage_pct + result.tagged_ri_coverage_pct) / 2
        )

        if coverage < 30:
            assessment = f"Low coverage — ${result.tagged_spend_usd:,.0f}/month largely at on-demand rates"
        elif coverage < 60:
            assessment = "Moderate coverage — meaningful SP/RI opportunity remains"
        else:
            assessment = "Good coverage"

        out["summary"] = (
            f"{tag_key}={tag_value}: ~{coverage:.0f}% commitment coverage "
            f"({result.confidence} confidence). {assessment}. "
            + (f"Tagging is {tag_coverage_pct:.0f}% complete — "
               f"improving to 90%+ will give a high-confidence number."
               if tag_coverage_pct < 90 else "")
        )

        return out

    except Exception as e:
        log.exception("Commitment coverage by tag failed")
        return {"error": str(e)}


@mcp.tool()
async def get_helm_release_costs(
    context: str | None = None,
    namespace: str | None = None,
) -> dict:
    """
    Cost breakdown by Helm release — shows what each release actually costs
    rather than raw deployment names. Detects orphaned releases wasting money.

    Works without the helm CLI — reads release state directly from cluster secrets.

    Examples:
        - "How much does our Prometheus stack cost?"
        - "Which Helm releases are most expensive?"
        - "Do we have any orphaned Helm releases?"
        - "Show me waste broken down by Helm chart"
        - "How much is our ingress controller costing us?"
    """
    try:
        from .connectors.kubernetes import KubernetesConnector
        from .connectors.helm import discover_helm_releases, attribute_costs_to_releases
    except ImportError:
        return {"error": "kubernetes package not installed. Run: pip install finops-mcp[kubernetes]"}

    try:
        connector = KubernetesConnector()
        if not await connector.is_configured():
            return {"error": "No kubeconfig found. Set KUBECONFIG or ensure ~/.kube/config exists."}

        k8s_client = connector._load_client(context)

        # Get workload costs first
        report = connector.analyze_cluster(context)
        workloads = report.workloads
        if namespace:
            workloads = [w for w in workloads if w.namespace == namespace]

        # Discover Helm releases and attribute costs
        releases = discover_helm_releases(k8s_client)
        if namespace:
            releases = [r for r in releases if r.namespace == namespace]

        releases, unmanaged_cost = attribute_costs_to_releases(releases, workloads, k8s_client)

        # Cost by chart (across all releases of same chart)
        by_chart: dict[str, float] = {}
        for r in releases:
            by_chart[r.chart_name] = by_chart.get(r.chart_name, 0) + r.monthly_cost

        orphaned = [r for r in releases if r.is_orphaned]
        orphaned_cost = sum(r.monthly_cost for r in orphaned)

        result = {
            "release_count": len(releases),
            "total_managed_cost_usd": round(sum(r.monthly_cost for r in releases), 2),
            "unmanaged_workload_cost_usd": round(unmanaged_cost, 2),
            "orphaned_release_count": len(orphaned),
            "orphaned_cost_usd": round(orphaned_cost, 2),
            "cost_by_chart": dict(sorted(by_chart.items(), key=lambda x: x[1], reverse=True)),
            "releases": [
                {
                    "name": r.name,
                    "namespace": r.namespace,
                    "chart": r.chart,
                    "chart_name": r.chart_name,
                    "chart_version": r.chart_version,
                    "app_version": r.app_version,
                    "status": r.status,
                    "revision": r.revision,
                    "deployed_at": r.deployed_at,
                    "monthly_cost_usd": r.monthly_cost,
                    "wasted_usd": r.wasted_usd,
                    "pod_count": r.pod_count,
                    "cpu_efficiency_pct": r.cpu_efficiency_pct,
                    "workloads": r.workload_names,
                    "orphaned": r.is_orphaned,
                }
                for r in releases
            ],
        }

        if orphaned:
            result["orphaned_releases"] = [
                {
                    "name": r.name,
                    "namespace": r.namespace,
                    "chart": r.chart,
                    "status": r.status,
                    "deployed_at": r.deployed_at,
                    "monthly_cost_usd": r.monthly_cost,
                }
                for r in orphaned
            ]

        lines = [f"{len(releases)} Helm releases — ${result['total_managed_cost_usd']:,.0f}/month managed"]
        if unmanaged_cost > 10:
            lines.append(f"${unmanaged_cost:,.0f}/month in workloads not managed by Helm")
        if orphaned:
            lines.append(f"⚠️ {len(orphaned)} orphaned release(s) costing ${orphaned_cost:,.0f}/month")
        top3 = sorted(releases, key=lambda r: r.monthly_cost, reverse=True)[:3]
        if top3:
            lines.append("Top: " + ", ".join(f"{r.name} ${r.monthly_cost:,.0f}" for r in top3))
        result["summary"] = " | ".join(lines)

        return result

    except Exception as e:
        log.exception("Helm cost analysis failed")
        return {"error": str(e)}


@mcp.tool()
async def estimate_helm_diff_cost(
    diff_text: str,
    release_name: str = "unknown",
    current_replicas: int = 1,
    current_cpu_request: str = "100m",
    current_memory_request: str = "128Mi",
) -> dict:
    """
    Estimate the monthly cost impact of a helm diff or values.yaml change.
    Handles replicaCount, CPU/memory requests, instanceType, and nodeCount changes.

    Paste the output of `helm diff upgrade` or a values.yaml git diff.

    Examples:
        - "How much will this helm diff cost?"
        - "What's the cost impact of scaling from 3 to 10 replicas?"
        - "Estimate cost of upgrading this node pool instance type"
    """
    try:
        from .connectors.helm import estimate_helm_diff, format_helm_diff_comment
        diff = estimate_helm_diff(
            diff_text=diff_text,
            release_name=release_name,
            current_replica_count=current_replicas,
            current_cpu_request=current_cpu_request,
            current_mem_request=current_memory_request,
        )

        result: dict = {
            "release_name": diff.release_name,
            "delta_monthly_usd": diff.delta_monthly_usd,
            "confidence": diff.confidence,
            "changes": diff.changes,
        }

        if diff.changes:
            direction = "increase" if diff.delta_monthly_usd > 0 else "decrease" if diff.delta_monthly_usd < 0 else "no change"
            result["summary"] = (
                f"Estimated {direction} of ${abs(diff.delta_monthly_usd):,.0f}/month "
                f"for release '{release_name}' (confidence: {diff.confidence})"
            )
            comment = format_helm_diff_comment(diff)
            if comment:
                result["pr_comment"] = comment
        else:
            result["summary"] = "No cost-affecting changes detected in this diff."

        return result

    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def compare_kubernetes_clusters() -> dict:
    """
    Compare costs and efficiency across all configured Kubernetes clusters.
    Useful for multi-cluster setups (prod vs staging, region vs region).

    Set K8S_CONTEXTS=prod-cluster,staging-cluster to configure.

    Examples:
        - "Compare our Kubernetes clusters"
        - "Which cluster is most efficient?"
        - "Show me spend across all clusters"
    """
    try:
        from .connectors.kubernetes import KubernetesConnector
    except ImportError:
        return {"error": "kubernetes package not installed. Run: pip install finops-mcp[kubernetes]"}

    try:
        connector = KubernetesConnector()
        reports = connector.analyze_all_clusters()

        if not reports:
            return {"error": "No clusters found. Check K8S_CONTEXTS or KUBECONFIG."}

        comparison = []
        for r in reports:
            comparison.append({
                "cluster": r.cluster,
                "provider": r.provider,
                "nodes": r.node_count,
                "pods": r.pod_count,
                "monthly_cost_usd": r.total_monthly_cost,
                "wasted_usd": r.wasted_monthly_cost,
                "waste_pct": round(r.wasted_monthly_cost / r.total_monthly_cost * 100, 1)
                             if r.total_monthly_cost > 0 else 0,
                "cpu_efficiency_pct": r.overall_cpu_efficiency,
                "namespace_count": len(r.namespaces),
                "idle_nodes": len(r.idle_nodes),
            })

        comparison.sort(key=lambda c: c["monthly_cost_usd"], reverse=True)
        total = sum(c["monthly_cost_usd"] for c in comparison)
        total_waste = sum(c["wasted_usd"] for c in comparison)

        return {
            "clusters": comparison,
            "total_monthly_cost_usd": round(total, 2),
            "total_wasted_usd": round(total_waste, 2),
            "summary": (
                f"{len(reports)} cluster(s) — ${total:,.0f}/month total, "
                f"${total_waste:,.0f}/month estimated waste"
            ),
        }

    except Exception as e:
        return {"error": str(e)}


# ── entry point ──────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════════════
# SCHEDULED REPORTS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def subscribe_to_report(
    name: str,
    sections: list[str],
    frequency: str = "weekly",
    slack_channels: list[str] | None = None,
    email_addresses: list[str] | None = None,
    team: str = "",
    provider: str = "",
    lookback_days: int = 7,
    cron: str = "",
) -> dict:
    """
    Create a scheduled report subscription. Reports are delivered automatically
    to Slack channels and/or email addresses on the configured schedule.

    Args:
        name: Report name (e.g. "Platform Team Weekly")
        sections: List of sections to include. Options:
                  spend, anomalies, scorecard, k8s, commitments, rightsizing, budgets, teams
        frequency: "daily", "weekday", "weekly", "monthly" (or use cron for custom)
        slack_channels: List of Slack channel IDs or names (e.g. ["#finops-alerts"])
        email_addresses: List of email recipients
        team: Scope report to a specific team tag value
        provider: Scope report to a specific cloud provider (aws, azure, gcp)
        lookback_days: How many days of history to include (default 7)
        cron: Custom cron expression — overrides frequency (e.g. "0 8 * * 1-5")

    Examples:
        - "Send me a daily Slack report with spend and anomalies to #finops"
        - "Set up a weekly report for the platform team every Monday"
        - "Create a monthly rightsizing report emailed to cfo@company.com"
        - "Subscribe to a daily digest in #cost-alerts with spend, anomalies, and budgets"
    """
    if err := require_role("analyst"):
        return err
    try:
        from .notifications.reports import create_subscription, VALID_SECTIONS
        invalid = [s for s in sections if s not in VALID_SECTIONS]
        if invalid:
            return {
                "error": f"Invalid sections: {invalid}",
                "valid_sections": VALID_SECTIONS,
            }

        # Email delivery is Pro-only — warn at subscription time, don't block creation
        email_note = None
        if email_addresses and require_pro("scheduled_email_digests") is not None:
            email_note = (
                f"Email delivery requires Pro. The subscription will be created with Slack "
                f"delivery only. Upgrade at {_UPGRADE_URL} to enable email delivery."
            )
            email_addresses = []  # clear emails on free tier

        filters = {}
        if team:
            filters["team"] = team
        if provider:
            filters["provider"] = provider

        sub = create_subscription(
            name=name,
            sections=sections,
            frequency=frequency,
            slack_channels=slack_channels or [],
            email_addresses=email_addresses or [],
            filters=filters,
            lookback_days=lookback_days,
            cron=cron or None,
        )
        result = {
            "created": True,
            "subscription": sub,
            "message": f"Report '{name}' scheduled (cron: {sub['cron']}). Slack delivery is active.",
            "note": "Reports check every 5 minutes, or trigger manually with send_report_now.",
        }
        if email_note:
            result["pro_required"] = email_note
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def list_report_subscriptions() -> dict:
    """
    List all active report subscriptions — their names, schedules, sections, and delivery channels.

    Examples:
        - "What reports are scheduled?"
        - "Show me all active report subscriptions"
        - "List my scheduled reports"
    """
    try:
        from .notifications.reports import list_subscriptions
        subs = list_subscriptions()
        return {
            "count": len(subs),
            "subscriptions": [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "cron": s["cron"],
                    "sections": s["sections"],
                    "slack_channels": s["slack_channels"],
                    "email_addresses": s["email_addresses"],
                    "filters": s["filters"],
                    "lookback_days": s.get("lookback_days", 7),
                    "last_sent_at": str(s.get("last_sent_at") or "never"),
                }
                for s in subs
            ],
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def send_report_now(subscription_id: int) -> dict:
    """
    Trigger a report subscription immediately, regardless of its schedule.

    Args:
        subscription_id: ID of the subscription to run (from list_report_subscriptions)

    Examples:
        - "Send report #3 now"
        - "Run the platform team report immediately"
        - "Trigger report subscription 1"
    """
    if err := require_role("analyst"):
        return err
    try:
        from .notifications.reports import run_subscription
        result = await run_subscription(subscription_id)
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def cancel_report_subscription(subscription_id: int) -> dict:
    """
    Cancel (deactivate) a scheduled report subscription.

    Args:
        subscription_id: ID of the subscription to cancel

    Examples:
        - "Cancel report #2"
        - "Stop the weekly platform report"
        - "Disable subscription 3"
    """
    if err := require_role("analyst"):
        return err
    try:
        from .notifications.reports import cancel_subscription
        ok = cancel_subscription(subscription_id)
        return {"cancelled": ok, "subscription_id": subscription_id}
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# BUDGETS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def set_budget(
    name: str,
    limit_usd: float,
    scope_type: str = "total",
    scope_value: str = "*",
    period: str = "monthly",
    alert_at_pct: float = 80.0,
    block_at_pct: float = 100.0,
) -> dict:
    """
    Create or update a spending budget. Budgets fire Slack alerts when spend
    crosses alert_at_pct, and fail CI checks when it crosses block_at_pct.

    Args:
        name: Budget name (e.g. "Platform Team Monthly")
        limit_usd: Spending limit in USD
        scope_type: What to watch — "total", "provider", "team", "service"
        scope_value: The specific value (e.g. "aws", "platform", "EC2")
                     Use "*" for total account budget
        period: "monthly" or "weekly"
        alert_at_pct: Send warning alert at this % of limit (default 80)
        block_at_pct: Fail CI gate at this % of limit (default 100)

    Examples:
        - "Set a $50,000 monthly budget for AWS"
        - "Create a $15,000 monthly budget for the platform team"
        - "Set a $20,000 budget for EC2 with warnings at 75%"
        - "Add a total monthly budget of $100,000"
    """
    if err := require_role("analyst"):
        return err
    try:
        from .budget.enforcer import create_budget
        b = create_budget(
            name=name,
            scope_type=scope_type,
            scope_value=scope_value,
            period=period,
            limit_usd=limit_usd,
            alert_at_pct=alert_at_pct,
            block_at_pct=block_at_pct,
        )
        return {"created": True, "budget": b}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def check_budget_status(budget_name: str = "") -> dict:
    """
    Check current spend against budgets. Shows how much has been spent,
    what's remaining, and whether any budgets are in warning or exceeded status.

    Args:
        budget_name: Filter to a specific budget name (optional — shows all if empty)

    Examples:
        - "Check all budgets"
        - "How are we doing against budget?"
        - "Is the platform team over budget?"
        - "Show budget status for AWS"
    """
    try:
        from .budget.enforcer import check_all_budgets, list_budgets, check_budget
        results = check_all_budgets()
        if budget_name:
            results = [r for r in results if budget_name.lower() in r["name"].lower()]

        exceeded = [r for r in results if r["status"] == "exceeded"]
        warnings  = [r for r in results if r["status"] == "warning"]
        ok_budgets = [r for r in results if r["status"] == "ok"]

        return {
            "summary": {
                "total_budgets": len(results),
                "exceeded": len(exceeded),
                "warnings": len(warnings),
                "on_track": len(ok_budgets),
            },
            "budgets": results,
            "alert": (
                f"🔴 {len(exceeded)} budget(s) exceeded! Immediate action required."
                if exceeded else
                f"🟡 {len(warnings)} budget(s) approaching limit."
                if warnings else
                "✅ All budgets on track."
            ),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def list_budgets() -> dict:
    """
    List all configured budgets with their limits and scopes.

    Examples:
        - "What budgets do we have?"
        - "Show me all spending limits"
        - "List configured budgets"
    """
    try:
        from .budget.enforcer import list_budgets as _list
        budgets = _list()
        return {"count": len(budgets), "budgets": budgets}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def delete_budget(budget_id: int) -> dict:
    """
    Delete (deactivate) a budget by ID.

    Args:
        budget_id: Budget ID from list_budgets

    Examples:
        - "Delete budget #3"
        - "Remove the platform team budget"
    """
    if err := require_role("analyst"):
        return err
    try:
        from .budget.enforcer import delete_budget as _del
        ok = _del(budget_id)
        return {"deleted": ok, "budget_id": budget_id}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def sync_budgets_from_yaml(yaml_path: str) -> dict:
    """
    Import budgets from a budget.yml file. Idempotent — running twice
    is safe. Use this to version-control your spending limits alongside
    your infrastructure code.

    budget.yml format:
        budgets:
          - name: Platform Team Monthly
            scope_type: team
            scope_value: platform
            period: monthly
            limit_usd: 15000
            alert_at_pct: 80
            block_at_pct: 100

    Args:
        yaml_path: Path to the budget.yml file

    Examples:
        - "Load budgets from ./budget.yml"
        - "Sync budgets from /path/to/budget.yml"
        - "Import the budget configuration file"
    """
    if err := require_role("analyst"):
        return err
    try:
        from .budget.enforcer import sync_from_yaml
        return sync_from_yaml(yaml_path)
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# ORG / MULTI-ACCOUNT
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_accounts() -> dict:
    """
    List all AWS Organization member accounts, discovering them via the
    AWS Organizations API. Syncs account metadata to local DB for future queries.
    Account listing is free — detailed cost rollup across accounts requires Pro.

    Requires: AWS credentials with organizations:ListAccounts permission
    (management account or delegated admin).

    Examples:
        - "List all accounts in the org"
        - "Show me all AWS accounts"
        - "How many accounts do we have?"
    """
    try:
        from .connectors.aws_org import list_org_accounts
        accounts = list_org_accounts(sync_to_db=True)
        if not accounts:
            return {
                "message": "No accounts found. Ensure AWS credentials have organizations:ListAccounts permission.",
                "accounts": [],
            }
        mgmt = [a for a in accounts if a.get("is_management_account")]
        members = [a for a in accounts if not a.get("is_management_account")]
        return {
            "total_accounts": len(accounts),
            "management_account": mgmt[0] if mgmt else None,
            "member_accounts": members,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def get_org_cost_summary(days_back: int = 30) -> dict:
    """
    Get a cost rollup across all AWS Organization accounts — total spend,
    per-account breakdown sorted by spend, and top services per account.
    Requires Pro (org_reports).

    Args:
        days_back: Look-back period in days (default 30)

    Examples:
        - "Show me org-wide cloud costs"
        - "Which account is spending the most?"
        - "Give me a breakdown of costs across all accounts"
        - "What's our total AWS spend across the whole org?"
    """
    if err := require_pro("org_reports"):
        return err
    try:
        from .connectors.aws_org import org_cost_summary
        return org_cost_summary(days_back=days_back)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def get_top_spending_accounts(limit: int = 10, days_back: int = 30) -> dict:
    """
    Show the highest-spending AWS accounts in the organization.
    Requires Pro (org_reports).

    Args:
        limit: Number of top accounts to return (default 10)
        days_back: Look-back period in days (default 30)

    Examples:
        - "Which 5 accounts are spending the most?"
        - "Show top spending accounts this month"
        - "Which teams are the biggest AWS spenders?"
    """
    if err := require_pro("org_reports"):
        return err
    try:
        from .connectors.aws_org import top_spending_accounts
        accounts = top_spending_accounts(limit=limit, days_back=days_back)
        return {"top_accounts": accounts, "days_back": days_back}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def get_account_anomalies(days_back: int = 30) -> dict:
    """
    Detect accounts with unusual spend changes versus their prior period —
    accounts that significantly spiked or dropped in cost.
    Requires Pro (org_reports).

    Args:
        days_back: Look-back period to compare (default 30 vs prior 30)

    Examples:
        - "Which accounts had unusual spend changes?"
        - "Are any accounts spiking this month?"
        - "Show me account-level anomalies"
    """
    if err := require_pro("org_reports"):
        return err
    try:
        from .connectors.aws_org import account_anomalies
        anomalies = account_anomalies(days_back=days_back)
        spikes = [a for a in anomalies if a["direction"] == "spike"]
        drops  = [a for a in anomalies if a["direction"] == "drop"]
        return {
            "total_anomalies": len(anomalies),
            "spikes": len(spikes),
            "drops": len(drops),
            "anomalies": anomalies,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def get_ou_cost_breakdown(days_back: int = 30) -> dict:
    """
    Break costs down by AWS Organizational Unit (OU). When OUs map to
    departments or teams, this gives you a clean chargeback report.
    Requires Pro (org_reports).

    Args:
        days_back: Look-back period in days (default 30)

    Examples:
        - "Break down costs by business unit"
        - "Show OU-level cost breakdown"
        - "How much is each department spending in AWS?"
    """
    if err := require_pro("org_reports"):
        return err
    try:
        from .connectors.aws_org import ou_cost_breakdown
        breakdown = ou_cost_breakdown(days_back=days_back)
        return {"ous": breakdown, "days_back": days_back}
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# STORAGE MODE
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_storage_info() -> dict:
    """
    Show the current storage backend (SQLite local or Postgres shared).
    Helps teams understand whether they're in single-engineer or shared mode.

    Examples:
        - "What database is nable using?"
        - "Are we in shared mode?"
        - "Show storage configuration"
    """
    try:
        from .storage.db import storage_mode
        info = storage_mode()
        if info["mode"] == "sqlite":
            info["upgrade_note"] = (
                "To share data across your team, set DATABASE_URL=postgresql://user:pass@host/finops "
                "in your environment. All engineers with this URL will share one database."
            )
        else:
            info["note"] = "Running in shared Postgres mode. All team members with DATABASE_URL access the same data."
        return info
    except Exception as e:
        return {"error": str(e)}


# ── RBAC tools ───────────────────────────────────────────────────────────────

@mcp.tool()
def create_api_key(
    name: str,
    role: str = "viewer",
    email: str = "",
    scope_team: str | None = None,
    scope_provider: str | None = None,
) -> dict:
    """
    Create a new API key for a team member. Requires admin role in shared mode.

    Roles:
      viewer   — read-only cost queries, optionally scoped to one team/provider
      analyst  — viewer + attribution writes, budget management, snapshot triggers
      admin    — full access, can manage keys and connectors

    The raw key (nbl_...) is shown ONCE — it is not stored. Save it immediately.

    Examples:
        - "Create a viewer key for Alice scoped to the platform team"
        - "Give Bob an analyst key"
        - "Create an admin key for the CI system"
    """
    if err := require_role("admin"):
        return err
    result = create_key(
        name=name, role=role, email=email,
        scope_team=scope_team, scope_provider=scope_provider,
        created_by=current_identity().name if current_identity() else "admin",
    )
    audit("key_create", name, f"role={role} scope_team={scope_team}")
    return result


@mcp.tool()
def list_api_keys() -> list[dict]:
    """
    List all active API keys (names, roles, scopes). Raw keys are never shown.
    Requires admin role in shared mode.

    Examples:
        - "Who has access to finops?"
        - "List all API keys"
        - "Show team member access levels"
    """
    if err := require_role("admin"):
        return [err]
    return list_keys()


@mcp.tool()
def revoke_api_key(key_id: int) -> dict:
    """
    Revoke an API key by ID. The key is soft-deleted — it stops working immediately.
    Requires admin role. Use list_api_keys to find the key ID first.

    Examples:
        - "Revoke Alice's key"
        - "Remove access for key ID 3"
    """
    if err := require_role("admin"):
        return err
    ok = revoke_key(key_id)
    if ok:
        audit("key_revoke", f"id={key_id}", None)
    return {"revoked": ok, "key_id": key_id}


@mcp.tool()
def whoami() -> dict:
    """
    Show the current identity and access level. Works in both permissive and
    shared auth mode.

    Examples:
        - "Who am I logged in as?"
        - "What's my role?"
        - "Do I have analyst access?"
    """
    ident = current_identity()
    if ident is None:
        from .storage.db import storage_mode
        mode = storage_mode()
        return {
            "mode": "permissive",
            "role": "admin",
            "note": (
                "Running in single-user mode — no authentication required. "
                "Set FINOPS_REQUIRE_AUTH=1 and issue API keys to enforce RBAC."
            ),
            "storage": mode,
        }
    return {
        "mode": "authenticated",
        **ident.as_dict(),
    }


# ── Terraform tagging tools ───────────────────────────────────────────────────


@mcp.tool()
async def audit_terraform_tags(
    tf_dir: str,
    state_path: str | None = None,
) -> dict:
    """
    Scan Terraform state for resources missing required tags.
    Runs `terraform show -json` in tf_dir (or reads state_path directly).
    Required tags configured via FINOPS_REQUIRED_TAGS env var (comma-separated,
    default: team,environment,service).

    Args:
        tf_dir: Path to the Terraform working directory (must be initialized).
        state_path: Optional path to a .tfstate file. Skips terraform CLI if provided.

    Examples:
        - "Audit tags in our infra repo"
        - "Which resources are missing the team tag?"
    """
    if err := require_role("analyst"):
        return err

    from .connectors.terraform import audit_tags, persist_violations, _required_tags

    try:
        violations = audit_tags(tf_dir, state_path)
    except Exception as exc:
        return {"error": str(exc), "tf_dir": tf_dir}

    stored = persist_violations(tf_dir, violations)

    return {
        "tf_dir": tf_dir,
        "required_tags": _required_tags(),
        "violations_found": len(violations),
        "stored_in_db": stored,
        "violations": violations,
    }


@mcp.tool()
async def generate_terraform_tag_fixes(
    tf_dir: str,
) -> dict:
    """
    Generate HCL patches for all open tag violations in tf_dir.
    Shows a unified diff per .tf file — does NOT write to disk.
    Run audit_terraform_tags first to populate violations.

    Args:
        tf_dir: Same directory passed to audit_terraform_tags.

    Examples:
        - "Show me the tag fixes needed"
        - "What HCL changes are required to fix our tagging?"
    """
    if err := require_role("analyst"):
        return err

    import json as _json
    from sqlalchemy import select
    from .storage.db import terraform_tag_audits, get_engine
    from .tagging.hcl_patcher import generate_all_fixes

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(terraform_tag_audits).where(
                terraform_tag_audits.c.tf_dir == tf_dir,
                terraform_tag_audits.c.status == "open",
            )
        ).fetchall()

    if not rows:
        return {
            "message": "No open violations found. Run audit_terraform_tags first.",
            "diffs": {},
        }

    violations = [
        {
            "address": r.resource_address,
            "type": r.resource_type,
            "name": r.resource_name,
            "current_tags": _json.loads(r.current_tags),
            "missing_tags": _json.loads(r.missing_tags),
            "file_path": r.file_path or "",
        }
        for r in rows
    ]

    try:
        diffs = generate_all_fixes(tf_dir, violations)
    except Exception as exc:
        return {"error": str(exc)}

    return {
        "violations_count": len(violations),
        "files_to_patch": len(diffs),
        "diffs": diffs,
    }


@mcp.tool()
async def open_terraform_tag_pr(
    tf_dir: str,
    github_repo: str,
    branch: str = "fix/add-required-tags",
    base_branch: str = "main",
    pr_title: str = "fix: add required tags to Terraform resources",
) -> dict:
    """
    Apply tag fixes to .tf files and open a GitHub PR.
    Requires GITHUB_TOKEN env var and a git remote configured for github_repo.

    Args:
        tf_dir: Path to the Terraform working directory (must be a git repo).
        github_repo: GitHub repo in "owner/repo" format.
        branch: Branch name to create. Defaults to "fix/add-required-tags".
        base_branch: Target branch for the PR. Defaults to "main".
        pr_title: PR title.

    Examples:
        - "Open a PR to fix the tagging gaps"
        - "Create the tag fix PR against main"
    """
    if err := require_role("analyst"):
        return err

    import json as _json
    import subprocess as _sp
    from sqlalchemy import select
    from .storage.db import terraform_tag_audits, get_engine
    from .tagging.hcl_patcher import apply_fixes
    from .integrations.ticketing import create_github_pr

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(terraform_tag_audits).where(
                terraform_tag_audits.c.tf_dir == tf_dir,
                terraform_tag_audits.c.status == "open",
            )
        ).fetchall()

    if not rows:
        return {
            "message": "No open violations. Run audit_terraform_tags first.",
            "pr_url": None,
        }

    violations = [
        {
            "address": r.resource_address,
            "type": r.resource_type,
            "name": r.resource_name,
            "current_tags": _json.loads(r.current_tags),
            "missing_tags": _json.loads(r.missing_tags),
            "file_path": r.file_path or "",
        }
        for r in rows
    ]

    # 1. Apply fixes to disk
    try:
        modified_files = apply_fixes(tf_dir, violations)
    except Exception as exc:
        return {"error": f"Failed to apply fixes: {exc}"}

    if not modified_files:
        return {
            "message": (
                "No .tf files were modified. Violations may not be locatable in source — "
                "ensure tf_dir contains .tf files with matching resource declarations."
            ),
            "pr_url": None,
        }

    # 2. Git: checkout branch, stage, commit, push
    def _git(*args: str) -> str:
        result = _sp.run(
            ["git", *args], cwd=tf_dir, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    try:
        _git("checkout", "-b", branch)
        _git("add", "--", *modified_files)
        _git(
            "commit", "-m",
            f"fix: add required tags to Terraform resources\n\n"
            f"Fixed {len(violations)} missing tag violations across "
            f"{len(modified_files)} file(s).\n\n"
            f"Co-Authored-By: nable FinOps MCP <noreply@nable.dev>",
        )
        _git("push", "-u", "origin", branch)
    except Exception as exc:
        return {"error": f"Git operation failed: {exc}", "branch": branch}

    # 3. Open GitHub PR
    violation_lines = "\n".join(
        f"- `{v['address']}` — missing: {', '.join(v['missing_tags'])}"
        for v in violations[:30]
    )
    if len(violations) > 30:
        violation_lines += f"\n\n_...and {len(violations) - 30} more_"

    pr_body = (
        f"## Summary\n\n"
        f"Adds missing required tags to {len(violations)} Terraform resource(s) "
        f"across {len(modified_files)} file(s).\n\n"
        f"### Resources fixed\n\n"
        f"{violation_lines}\n\n"
        f"---\n"
        f"🤖 Generated by [nable FinOps MCP](https://github.com/nable-finops/nable)"
    )

    try:
        pr_resp = create_github_pr(
            repo=github_repo,
            title=pr_title,
            body=pr_body,
            head=branch,
            base=base_branch,
        )
        pr_url = pr_resp.get("html_url", "")
    except Exception as exc:
        return {"error": f"PR creation failed: {exc}", "branch": branch}

    # 4. Mark violations as fixed in DB
    ids = [r.id for r in rows]
    with engine.begin() as conn:
        conn.execute(
            terraform_tag_audits.update()
            .where(terraform_tag_audits.c.id.in_(ids))
            .values(status="fixed", pr_url=pr_url)
        )

    return {
        "pr_url": pr_url,
        "branch": branch,
        "violations_fixed": len(violations),
        "files_modified": modified_files,
    }


def main() -> None:
    import logging
    logging.basicConfig(level=logging.INFO)

    # Resolve and cache the calling user's identity at startup.
    # In single-user mode this is a no-op. In shared mode it validates
    # FINOPS_API_KEY and attaches the Identity to the main thread.
    ident = resolve_identity_from_env()
    set_current_identity(ident)

    status = get_status()
    border = "=" * 56
    if status.mode == "pro":
        print(f"\n{border}")
        print(f"  nable  ✦  Pro  —  {status.email}")
        print(f"{border}\n")
    elif status.mode == "trial":
        print(f"\n{border}")
        print(f"  nable  —  Free trial  ({status.days_remaining} days remaining)")
        print("  All Pro features unlocked.")
        print(f"  Subscribe → {_UPGRADE_URL}")
        print(f"{border}\n")
    else:
        print(f"\n{border}")
        print("  nable  —  Trial expired")
        print(f"  Subscribe to restore full access → {_UPGRADE_URL}")
        print(f"{border}\n")

    # Warn if running in Postgres mode without auth enforcement
    if os.getenv("DATABASE_URL") and os.getenv("FINOPS_REQUIRE_AUTH") != "1":
        log.warning(
            "WARNING: Running in shared/Postgres mode without FINOPS_REQUIRE_AUTH=1. "
            "All users have full access. Set FINOPS_REQUIRE_AUTH=1 to enforce RBAC."
        )

    from .scheduler.jobs import start_scheduler
    start_scheduler()
    mcp.run()


# ── AI / LLM cost tools ───────────────────────────────────────────────────────

@mcp.tool()
async def get_llm_costs(
    days: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Aggregate AI/LLM spend across all configured providers — OpenAI, Anthropic,
    AWS Bedrock, Azure OpenAI, and Vertex AI.

    Shows total spend, breakdown by provider, breakdown by model, daily trend,
    and model-switching recommendations to reduce costs.

    Args:
        days: Lookback window in days (default 30). Ignored if start_date set.
        start_date: ISO date string (YYYY-MM-DD). Optional.
        end_date: ISO date string (YYYY-MM-DD). Defaults to today.

    Examples:
        - "How much have we spent on AI APIs this month?"
        - "What's our total LLM spend across OpenAI and Bedrock?"
        - "Show AI cost breakdown by model for the last 7 days"
        - "Which AI models are we spending the most on?"
    """
    try:
        from datetime import date as _date
        sd = _date.fromisoformat(start_date) if start_date else None
        ed = _date.fromisoformat(end_date) if end_date else _date.today()
        from .connectors.llm_costs import get_all_llm_costs
        return get_all_llm_costs(start_date=sd, end_date=ed, days=days)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def get_llm_cost_by_model(
    days: int = 30,
    provider: str | None = None,
) -> dict:
    """
    Break down AI/LLM costs by individual model with efficiency metrics.

    Shows cost per model, estimated tokens consumed, cost per 1M tokens,
    and which models have cheaper alternatives for the same task class.

    Args:
        days: Lookback window in days (default 30).
        provider: Filter to a specific provider — "openai", "anthropic", "bedrock".
                  Leave blank to see all providers.

    Examples:
        - "Which of our AI models costs the most?"
        - "Show me OpenAI model cost breakdown"
        - "How much are we spending on GPT-4o vs GPT-4o-mini?"
        - "What would we save switching from Claude Opus to Sonnet?"
    """
    try:
        from datetime import date as _date, timedelta
        ed = _date.today()
        sd = ed - timedelta(days=days)
        from .connectors.llm_costs import get_all_llm_costs
        result = get_all_llm_costs(start_date=sd, end_date=ed)

        if provider:
            # Filter to specific provider
            prov_cost = result["by_provider"].get(provider, 0.0)
            return {
                "provider":    provider,
                "total_usd":   prov_cost,
                "by_model":    {k: v for k, v in result["by_model"].items()},
                "period":      result["period"],
                "recommendations": result.get("recommendations", []),
            }

        return {
            "period":          result["period"],
            "total_usd":       result["total_usd"],
            "by_provider":     result["by_provider"],
            "by_model":        result["by_model"],
            "top_spenders":    result["top_spenders"],
            "recommendations": result.get("recommendations", []),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def get_llm_unit_economics(
    metric_name: str = "request",
    metric_count: float | None = None,
    days: int = 30,
) -> dict:
    """
    Calculate cost per unit of business value from AI APIs.

    Divides total LLM spend by a business metric to give you cost-per-X:
    cost per API request, cost per user, cost per document processed, etc.

    Args:
        metric_name:  What you're dividing by — "request", "user", "document",
                      "transaction", or any label. Default: "request".
        metric_count: How many units occurred in the period. If omitted, returns
                      total spend only and asks for the metric count.
        days:         Lookback window (default 30).

    Examples:
        - "What's our cost per API request for AI features?"
        - "We processed 50000 documents this month. What's our cost per doc?"
        - "Cost per active user for our AI features last 30 days — we had 1200 users"
    """
    try:
        from datetime import date as _date, timedelta
        ed = _date.today()
        sd = ed - timedelta(days=days)
        from .connectors.llm_costs import get_all_llm_costs
        result = get_all_llm_costs(start_date=sd, end_date=ed)
        total = result["total_usd"]

        out: dict = {
            "period":           result["period"],
            "total_llm_usd":    total,
            "by_provider":      result["by_provider"],
        }

        if metric_count and metric_count > 0:
            out["metric"]           = metric_name
            out["metric_count"]     = metric_count
            out[f"cost_per_{metric_name}"] = round(total / metric_count, 6)
            out["monthly_projection"] = round(total / days * 30, 2)

            # Contextual benchmarks
            cpm = round(total / metric_count * 1000, 4)
            out["cost_per_1000"] = cpm
            if cpm < 0.10:
                out["benchmark"] = "Excellent — under $0.10 per 1,000 units"
            elif cpm < 0.50:
                out["benchmark"] = "Good — under $0.50 per 1,000 units"
            elif cpm < 2.00:
                out["benchmark"] = "Moderate — consider model optimisation"
            else:
                out["benchmark"] = "High — review model selection and prompt efficiency"
        else:
            out["next_step"] = (
                f"Provide metric_count (how many {metric_name}s in this period) "
                f"to calculate cost per {metric_name}."
            )

        return out
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def get_kubernetes_costs(
    cluster_name: str = "",
    context: str | None = None,
) -> dict:
    """
    Break down Kubernetes cluster costs to pod → deployment → namespace → team.

    AWS charges a single EC2 line for EKS node groups. This tool allocates that
    cost proportionally by pod CPU/memory requests, giving you per-team and
    per-namespace cost attribution that AWS doesn't provide natively.

    Requires: kubernetes Python package + valid kubeconfig
    Optional: NABLE_K8S_TEAM_LABEL env var for team attribution (default: "team")

    Returns pod-level breakdown, idle node cost, team totals, and recommendations.
    """
    try:
        from .connectors.kubernetes_costs import allocate_to_dict
        return allocate_to_dict(cluster_name=cluster_name, context=context)
    except ImportError:
        return {
            "error": "kubernetes package not installed.",
            "fix": "pip install 'finops-mcp[kubernetes]'",
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def benchmark_costs(
    account_id: str,
    vertical: str = "default",
    days: int = 30,
) -> dict:
    """
    Compare this account's spend profile against anonymised peer group medians.

    Shows where you're above or below the median for companies in your industry
    vertical across metrics like: EC2%, RDS%, savings plan coverage, idle
    resource %, LLM spend %, data transfer %, and rightsizing opportunity %.

    Args:
        account_id: AWS account ID to analyse
        vertical:   industry peer group — saas, ecommerce, fintech, media, ai_ml, default
        days:       lookback period for metric calculation

    Returns per-metric comparisons with assessments (better/similar/worse) and insights.
    """
    try:
        from .analytics.benchmarks import compare
        return compare(account_id=account_id, vertical=vertical, days=days)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def forecast_costs(
    account_id: str,
    service: str | None = None,
    horizon_days: int = 30,
    history_days: int = 90,
) -> dict:
    """
    Forecast future cloud spend using Holt-Winters time-series modelling.

    Automatically tunes forecast parameters (alpha/beta/gamma) to your account's
    historical spend patterns and returns a daily point forecast with 80%
    prediction intervals.

    Args:
        account_id:   AWS account ID or provider account identifier
        service:      specific service to forecast (e.g. "EC2", "RDS") — omit for total
        horizon_days: number of days to forecast (default 30)
        history_days: days of history to fit the model (default 90, need ≥14)

    Returns forecast including method used, MAPE accuracy %, monthly projection,
    and day-by-day point/lower/upper estimates.
    """
    try:
        from .ml.forecasting import Forecaster
        f = Forecaster.for_account(account_id, service=service, days=history_days)
        if not f._series:
            return {
                "error": "No historical data found for this account/service.",
                "hint": "Run `take_snapshot_now` first to populate cost history.",
            }
        return f.predict_dict(horizon_days)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def scan_waste_patterns(
    account_id: str,
    min_monthly_waste: float = 20.0,
    categories: str | None = None,
) -> dict:
    """
    Scan for cloud cost waste patterns using nable's proprietary pattern library.

    Runs 13 waste fingerprints across compute, storage, database, network, AI,
    and governance categories. Each finding includes confidence score, monthly
    waste estimate, and specific remediation steps.

    Args:
        account_id:        AWS account ID to scan
        min_monthly_waste: only return findings above this monthly USD threshold
        categories:        comma-separated filter e.g. "compute,storage" (omit for all)

    Returns structured findings sorted by monthly waste descending, with
    total_monthly_waste and total_annual_waste summary.
    """
    try:
        from .ml.patterns import PatternContext, scan_dict
        from .storage.db import get_engine
        from sqlalchemy import text as sql_text

        engine = get_engine()
        cat_list = [c.strip() for c in categories.split(",")] if categories else None

        # Pull daily cost series per service (last 90 days)
        with engine.connect() as conn:
            rows = conn.execute(sql_text("""
                SELECT service, snapshot_date, SUM(amount_usd) as total
                FROM cost_snapshots
                WHERE account_id = :aid
                  AND snapshot_date >= date('now', '-90 days')
                GROUP BY service, snapshot_date
                ORDER BY service, snapshot_date
            """), {"aid": account_id}).fetchall()

        daily_costs: dict[str, list[float]] = {}
        for service, _date, total in rows:
            daily_costs.setdefault(service, []).append(float(total))

        ctx = PatternContext(
            daily_costs=daily_costs,
            by_resource=[],
            snapshots=[],
            account_id=account_id,
        )

        result = scan_dict(ctx, min_monthly_waste=min_monthly_waste, categories=cat_list)
        result["account_id"] = account_id
        result["note"] = (
            "Findings based on cost time-series only. "
            "Connect EC2/RDS/Lambda metadata for higher-confidence results."
        )
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def estimate_terraform_cost(
    plan_json: str | None = None,
    plan_file: str | None = None,
    tf_dir: str | None = None,
) -> dict:
    """
    Estimate the monthly AWS cost change from a Terraform plan BEFORE applying it.

    Provide one of:
      - plan_json: raw JSON string from `terraform show -json plan.tfplan`
      - plan_file: path to a saved plan JSON file
      - tf_dir:    directory to run `terraform plan` in automatically

    Returns a cost delta breakdown per resource with adds, changes, and removes.
    Prices: AWS on-demand us-east-1. Supports EC2, RDS, Aurora, ElastiCache,
    EKS, NAT Gateways, ALB/NLB, ECS Fargate, Lambda, EBS, OpenSearch, MSK, Redshift.
    """
    try:
        from .connectors.terraform_estimate import estimate_plan, estimate_from_file, estimate_from_dir
        import json as _json

        if plan_json:
            data = _json.loads(plan_json)
            result = estimate_plan(data)
        elif plan_file:
            result = estimate_from_file(plan_file)
        elif tf_dir:
            result = estimate_from_dir(tf_dir)
        else:
            return {
                "error": "Provide plan_json, plan_file, or tf_dir.",
                "usage": (
                    "Run: terraform plan -out=plan.tfplan && "
                    "terraform show -json plan.tfplan > plan.json, "
                    "then pass the file path as plan_file."
                ),
            }

        return result
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    main()
