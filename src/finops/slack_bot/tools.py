"""
Tool definitions for the Slack bot's Claude API calls.

These wrap the same underlying logic as the MCP server tools but are
expressed as Anthropic tool_use dicts rather than MCP tool decorators.
Shared business logic lives in the connectors/recommendations modules —
nothing is duplicated here.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

# ─── tool schemas (passed to Claude API) ─────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "get_cost_summary",
        "description": "Get total cloud spend across all connected providers for a date range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_back": {"type": "integer", "description": "Number of days to look back (default 30)", "default": 30},
                "provider": {"type": "string", "description": "Filter to one provider (aws/azure/gcp) or omit for all"},
            },
        },
    },
    {
        "name": "get_top_cost_drivers",
        "description": "Return the top services or resources by spend.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_back": {"type": "integer", "default": 30},
                "limit": {"type": "integer", "default": 10},
                "provider": {"type": "string"},
            },
        },
    },
    {
        "name": "get_anomalies",
        "description": "List active spend anomalies and spikes detected this week.",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_severity": {"type": "string", "enum": ["low", "medium", "high"], "default": "medium"},
            },
        },
    },
    {
        "name": "get_cost_trends",
        "description": "Show week-over-week or month-over-month cost trends.",
        "input_schema": {
            "type": "object",
            "properties": {
                "weeks_back": {"type": "integer", "default": 4},
                "provider": {"type": "string"},
            },
        },
    },
    {
        "name": "list_idle_resources",
        "description": "Find idle/wasted AWS resources (unattached EBS, unused EIPs, old snapshots, stopped EC2).",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_idle_days": {"type": "integer", "default": 30},
                "resource_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["ebs_volume", "elastic_ip", "snapshot", "stopped_ec2", "load_balancer"]},
                },
            },
        },
    },
    {
        "name": "get_commitment_analysis",
        "description": "Show RI/Savings Plan coverage, utilization, and purchase recommendations.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


# ─── tool executors ───────────────────────────────────────────────────────────

def _run_get_cost_summary(inp: dict) -> dict:
    from finops.connectors.aws import AWSConnector
    from finops.connectors.azure import AzureConnector
    from finops.connectors.gcp import GCPConnector
    import asyncio

    days_back = inp.get("days_back", 30)
    provider_filter = inp.get("provider")
    end = date.today()
    start = end - timedelta(days=days_back)

    connectors = {"aws": AWSConnector(), "azure": AzureConnector(), "gcp": GCPConnector()}
    if provider_filter:
        connectors = {k: v for k, v in connectors.items() if k == provider_filter.lower()}

    results = {}
    total = 0.0

    async def _fetch():
        for name, conn in connectors.items():
            if not await conn.is_configured():
                continue
            try:
                summary = await conn.get_costs(start, end)
                results[name] = {
                    "total_usd": round(summary.total_usd, 2),
                    "top_services": sorted(
                        [{"service": k, "amount": round(v, 2)} for k, v in summary.by_service.items()],
                        key=lambda x: x["amount"], reverse=True
                    )[:5],
                }
                total_val = summary.total_usd
                results["__total__"] = results.get("__total__", 0.0) + total_val
            except Exception as e:
                results[name] = {"error": str(e)}

    asyncio.run(_fetch())
    grand_total = results.pop("__total__", 0.0)
    return {"period_days": days_back, "total_usd": round(grand_total, 2), "by_provider": results}


def _run_get_anomalies(inp: dict) -> dict:
    try:
        from finops.anomaly.detector import get_active_anomalies
        anomalies = get_active_anomalies()
        min_sev = inp.get("min_severity", "medium")
        sev_order = {"low": 0, "medium": 1, "high": 2}
        min_level = sev_order.get(min_sev, 1)
        filtered = [a for a in anomalies if sev_order.get(a.get("severity", "low"), 0) >= min_level]
        return {"anomalies": filtered, "count": len(filtered)}
    except Exception as e:
        return {"error": str(e)}


def _run_list_idle_resources(inp: dict) -> dict:
    from finops.cleanup.idle import scan_idle_resources, idle_resources_summary
    resources = scan_idle_resources(
        resource_types=inp.get("resource_types"),
        min_idle_days=inp.get("min_idle_days", 30),
    )
    return idle_resources_summary(resources)


def _run_get_commitment_analysis(_inp: dict) -> dict:
    from finops.recommendations.commitments import analyze_commitments, commitment_summary
    from finops.recommendations.rate_detector import detect_effective_rates
    analysis = analyze_commitments()
    if not analysis:
        return {"error": "AWS not configured or commitment data unavailable"}
    rates = detect_effective_rates()
    result = commitment_summary(analysis)
    result["rate_profile"] = {
        "source": rates.source,
        "overall_discount_pct": round(rates.overall_discount_pct * 100, 1),
        "has_private_pricing": rates.has_private_pricing,
        "confidence": rates.confidence,
    }
    return result


def _run_get_top_cost_drivers(inp: dict) -> dict:
    try:
        summary_result = _run_get_cost_summary({"days_back": inp.get("days_back", 30), "provider": inp.get("provider")})
        all_services: dict[str, float] = {}
        for provider_data in summary_result.get("by_provider", {}).values():
            for svc in provider_data.get("top_services", []):
                all_services[svc["service"]] = all_services.get(svc["service"], 0) + svc["amount"]
        top = sorted(all_services.items(), key=lambda x: x[1], reverse=True)[:inp.get("limit", 10)]
        return {"top_drivers": [{"service": k, "amount_usd": v} for k, v in top]}
    except Exception as e:
        return {"error": str(e)}


def _run_get_cost_trends(inp: dict) -> dict:
    weeks = inp.get("weeks_back", 4)
    today = date.today()
    weekly = []
    for i in range(weeks, 0, -1):
        week_end = today - timedelta(weeks=i - 1)
        week_start = week_end - timedelta(weeks=1)
        try:
            result = _run_get_cost_summary({"days_back": 7})
            weekly.append({"week_ending": week_end.isoformat(), "total_usd": result.get("total_usd", 0)})
        except Exception:
            weekly.append({"week_ending": week_end.isoformat(), "total_usd": None})
    return {"weekly_trend": weekly}


TOOL_EXECUTORS = {
    "get_cost_summary": _run_get_cost_summary,
    "get_top_cost_drivers": _run_get_top_cost_drivers,
    "get_anomalies": _run_get_anomalies,
    "get_cost_trends": _run_get_cost_trends,
    "list_idle_resources": _run_list_idle_resources,
    "get_commitment_analysis": _run_get_commitment_analysis,
}


def execute_tool(name: str, inputs: dict) -> str:
    """Execute a tool by name and return JSON string result."""
    executor = TOOL_EXECUTORS.get(name)
    if not executor:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        result = executor(inputs)
        return json.dumps(result, default=str)
    except Exception as e:
        log.error("Tool %s failed: %s", name, e, exc_info=True)
        return json.dumps({"error": str(e)})
