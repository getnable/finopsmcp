"""
Bridge the MCP server's tool registry into the Slack bot's Claude loop.

The Slack bot used to carry its own hand-copied set of 6 tool schemas, so it
could answer about 4% of what the MCP server can. This module enumerates the
server's FastMCP registry directly: one source of truth, no schema drift.

Curation rather than blanket exposure:
  - ALLOWED_TOOLS maps tool name to the minimum RBAC role that may call it.
  - Anything not listed is invisible to the Slack loop. That excludes setup,
    vault, API key management, file exports, dashboard servers, and anything
    destructive (cleanup_idle_resources). PR opening is deliberately absent:
    remediation goes through the pending-approval flow in remediation.py,
    never straight from model output.
  - Role gating happens twice: schemas are filtered before Claude sees them,
    and execute_bridge_tool() re-checks at call time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# tool name -> minimum role ("viewer" < "analyst" < "admin")
ALLOWED_TOOLS: dict[str, str] = {
    # Connection + identity
    "list_connected_providers": "viewer",
    "check_connector_health": "viewer",
    "whoami": "viewer",
    "what_can_nable_do": "viewer",
    # Core cost queries
    "slice_costs": "viewer",
    "pin_view": "viewer",
    "list_pinned_views": "viewer",
    "get_pinned_view": "viewer",
    "unpin_view": "viewer",
    "get_cost_summary": "viewer",
    "get_costs_by_service": "viewer",
    "get_top_cost_drivers": "viewer",
    "compare_providers": "viewer",
    "get_cost_trends": "viewer",
    "get_cost_history": "viewer",
    "list_accounts": "viewer",
    "get_cost_summary_all_accounts": "viewer",
    "get_saas_spend_summary": "viewer",
    "get_total_spend_all_sources": "viewer",
    "list_active_services": "viewer",
    "get_service_cost": "viewer",
    "forecast_costs": "viewer",
    "benchmark_costs": "viewer",
    # RCA
    "explain_recent_cost_drivers": "viewer",
    "explain_cost_change": "viewer",
    "get_unit_economics": "viewer",
    # Anomalies + budgets
    "get_anomalies": "viewer",
    "acknowledge_anomaly": "analyst",
    "check_budget_status": "viewer",
    "list_budgets": "viewer",
    "set_budget": "analyst",
    # Teams + attribution
    "get_costs_by_team": "viewer",
    "run_attribution_now": "analyst",
    "get_team_scorecards": "viewer",
    "get_efficiency_scorecard": "viewer",
    # Savings + recommendations
    "get_rightsizing_recommendations": "viewer",
    "get_savings_summary": "viewer",
    "list_savings_recommendations": "viewer",
    "get_savings_ledger": "viewer",
    "mark_recommendation_acted_on": "analyst",
    "dismiss_recommendation": "analyst",
    "get_nable_roi": "viewer",
    "get_recommendation_learning": "viewer",
    # Commitments
    "get_commitment_analysis": "viewer",
    "get_commitment_coverage_by_tag": "viewer",
    "get_effective_rate_profile": "viewer",
    # Waste + audits (read-only scans)
    "list_idle_resources": "viewer",
    "audit_aws_waste": "viewer",
    "scan_waste_patterns": "viewer",
    "get_idle_rds_instances": "viewer",
    "get_idle_load_balancers": "viewer",
    "get_data_transfer_costs": "viewer",
    "get_resource_cost_breakdown_aws": "viewer",
    # Kubernetes
    "get_kubernetes_costs": "viewer",
    "get_kubernetes_namespace_breakdown": "viewer",
    "get_cluster_efficiency": "viewer",
    "get_workload_costs": "viewer",
    "get_kubernetes_cost_trends": "viewer",
    # AI spend
    "get_llm_costs": "viewer",
    "get_llm_cost_by_model": "viewer",
    "get_ai_kpis": "viewer",
    "optimize_ai_spend": "viewer",
    "get_bedrock_costs": "viewer",
    "recommend_bedrock_model_routing": "viewer",
    # Org
    "get_org_cost_summary": "viewer",
    "get_top_spending_accounts": "viewer",
    "get_account_anomalies": "viewer",
    # Actions (analyst+)
    "take_snapshot_now": "analyst",
    "create_ticket": "analyst",
    "create_anomaly_tickets": "analyst",
    "create_rightsizing_tickets": "analyst",
    # Admin
    "send_digest_now": "admin",
    "send_weekly_digest_now": "admin",
}

_MAX_RESULT_CHARS = int(os.getenv("FINOPS_SLACK_MAX_RESULT_CHARS", "24000"))
_MAX_DESC_CHARS = 1500

_tool_manager: Any = None
_schema_cache: dict[str, list[dict]] = {}


def _get_tool_manager() -> Any:
    """Import the MCP server lazily (heavy) and cache its tool manager."""
    global _tool_manager
    if _tool_manager is None:
        from finops import server  # noqa: PLC0415 — intentional lazy import

        _tool_manager = server.mcp._tool_manager
    return _tool_manager


def _role_level(role: str) -> int:
    from ..auth.rbac import ROLE_LEVEL

    return ROLE_LEVEL.get(role, 0)


def warm() -> int:
    """Load the server registry at bot startup so the first question is fast.

    Returns the number of bridged tools, for the startup log line.
    """
    return len(get_bridge_tools("admin"))


def get_bridge_tools(role: str = "admin") -> list[dict]:
    """Return Anthropic tool schemas for every allowed tool this role can use."""
    if role in _schema_cache:
        return _schema_cache[role]

    level = _role_level(role)
    tools: list[dict] = []
    for tool in _get_tool_manager().list_tools():
        min_role = ALLOWED_TOOLS.get(tool.name)
        if min_role is None or level < _role_level(min_role):
            continue
        desc = (tool.description or "").strip()
        if len(desc) > _MAX_DESC_CHARS:
            desc = desc[: _MAX_DESC_CHARS - 3] + "..."
        tools.append(
            {
                "name": tool.name,
                "description": desc or tool.name,
                "input_schema": tool.parameters,
            }
        )

    missing = set(ALLOWED_TOOLS) - {t.name for t in _get_tool_manager().list_tools()}
    if missing:
        log.warning("Bridge allowlist names not in server registry: %s", sorted(missing))

    _schema_cache[role] = tools
    return tools


def execute_bridge_tool(name: str, arguments: dict[str, Any] | None, role: str = "admin") -> str:
    """Execute a bridged MCP tool and return a JSON string for the tool_result."""
    min_role = ALLOWED_TOOLS.get(name)
    if min_role is None:
        return json.dumps({"error": f"Tool '{name}' is not available from Slack."})
    if _role_level(role) < _role_level(min_role):
        return json.dumps(
            {"error": f"Your role ({role}) cannot run '{name}'. Requires {min_role} or above."}
        )

    # Demo safety: in demo mode no agent tool call may reach real credentials.
    # demo_bridge_result returns demo data (or a safe placeholder) for anything
    # not already demo-safe, and None to let the self-demo/local tools run.
    try:
        from ..demo_data import is_demo, demo_bridge_result
        if is_demo():
            demo = demo_bridge_result(name, arguments or {})
            if demo is not None:
                payload = json.dumps(demo, default=str)
                if len(payload) > _MAX_RESULT_CHARS:
                    payload = payload[:_MAX_RESULT_CHARS] + '"... [truncated]'
                return payload
    except Exception as e:  # never let the demo guard break a real call
        log.debug("demo bridge guard skipped for %s: %s", name, e)

    try:
        result = asyncio.run(_get_tool_manager().call_tool(name, arguments or {}))
    except Exception as e:  # noqa: BLE001 — tool errors go back to the model as data
        log.error("Bridged tool %s failed: %s", name, e, exc_info=True)
        return json.dumps({"error": str(e)})

    payload = json.dumps(result, default=str)
    if len(payload) > _MAX_RESULT_CHARS:
        payload = payload[:_MAX_RESULT_CHARS] + '"... [truncated: result too large for chat]'
    return payload
