"""
FinOps MCP Server
-----------------
Exposes cloud + SaaS cost data as MCP tools.
Run via:  finops-mcp  or  python -m finops.server
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load vault credentials into os.environ before anything else reads env vars
from .security.env import load_vault_to_env
load_vault_to_env()

from .license import _UPGRADE_URL, get_status, require_pro

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
    if err := require_pro("anomaly detection"):
        return err

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
    if err := require_pro("anomaly management"):
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
    if err := require_pro("team cost attribution"):
        return err

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
    if err := require_pro("team cost attribution"):
        return err

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
    if err := require_pro("cost digests"):
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
    if err := require_pro("rightsizing recommendations"):
        return err

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
    Returns purchase recommendations with projected ROI.

    Examples:
        - "How well are we using our Reserved Instances?"
        - "Should we buy more Savings Plans?"
        - "How much are we wasting on unused RIs?"
        - "What's our RI/SP coverage?"
    """
    if err := require_pro("commitment analysis"):
        return err

    try:
        from .recommendations.commitments import analyze_commitments, commitment_summary
        analysis = analyze_commitments()
        if analysis is None:
            return {"error": "AWS not configured. Run: finops setup aws"}
        return commitment_summary(analysis)
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
    if err := require_pro("ticket creation"):
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
    if err := require_pro("weekly email digest"):
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
    if err := require_pro("idle resource detection"):
        return err
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
    if err := require_pro("resource cleanup"):
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
    if err := require_pro("rate profile"):
        return err
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


# ── entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    import logging
    logging.basicConfig(level=logging.INFO)

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

    from .scheduler.jobs import start_scheduler
    start_scheduler()
    mcp.run()


if __name__ == "__main__":
    main()
