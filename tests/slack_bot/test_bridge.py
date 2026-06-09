"""Tests for the MCP-to-Slack tool bridge: allowlist curation and role gating."""
from __future__ import annotations

import json

import pytest

mcp_missing = False
try:  # the bridge imports the full MCP server
    import mcp  # noqa: F401
except ImportError:
    mcp_missing = True

pytestmark = pytest.mark.skipif(mcp_missing, reason="mcp package not installed")


from finops.slack_bot import bridge  # noqa: E402


def _names(role: str) -> set[str]:
    return {t["name"] for t in bridge.get_bridge_tools(role)}


def test_role_tiers_are_nested_and_growing():
    viewer, analyst, admin = _names("viewer"), _names("analyst"), _names("admin")
    assert viewer < analyst <= admin
    assert len(viewer) >= 40, "viewer should see the broad read-only surface"


def test_action_tools_hidden_from_viewer():
    viewer, analyst = _names("viewer"), _names("analyst")
    for action_tool in ("create_ticket", "acknowledge_anomaly", "set_budget"):
        assert action_tool not in viewer
        assert action_tool in analyst


def test_dangerous_tools_never_bridged():
    admin = _names("admin")
    for forbidden in (
        "cleanup_idle_resources",   # destructive
        "open_rightsizing_pr",      # must go through the approval flow
        "open_terraform_tag_pr",
        "create_api_key",           # credential management
        "revoke_api_key",
        "list_vault_credentials",
    ):
        assert forbidden not in admin, f"{forbidden} must not be reachable from Slack"


def test_rca_and_ai_tools_are_bridged():
    viewer = _names("viewer")
    for must_have in (
        "explain_recent_cost_drivers",
        "get_cost_summary",
        "optimize_ai_spend",
        "get_llm_costs",
        "get_kubernetes_costs",
    ):
        assert must_have in viewer


def test_allowlist_matches_registry():
    registry = {t.name for t in bridge._get_tool_manager().list_tools()}
    missing = set(bridge.ALLOWED_TOOLS) - registry
    assert not missing, f"Allowlist names absent from server registry: {sorted(missing)}"


def test_execute_unknown_tool_rejected():
    result = json.loads(bridge.execute_bridge_tool("not_a_real_tool", {}, role="admin"))
    assert "error" in result


def test_execute_respects_role_at_call_time():
    result = json.loads(bridge.execute_bridge_tool("create_ticket", {}, role="viewer"))
    assert "error" in result
    assert "viewer" in result["error"]


def test_schemas_are_anthropic_shaped():
    for tool in bridge.get_bridge_tools("admin"):
        assert set(tool) == {"name", "description", "input_schema"}
        assert tool["description"]
        assert isinstance(tool["input_schema"], dict)
