# SPDX-License-Identifier: Apache-2.0
"""budgets MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
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
        scope_type: What to watch, "total", "provider", "team", "service"
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
    if err := _srv.require_role("analyst"):
        return err
    try:
        from ..budget.enforcer import create_budget
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


@_srv.mcp.tool()
async def check_budget_status(budget_name: str = "") -> dict:
    """
    Check current spend against budgets. Shows how much has been spent,
    what's remaining, and whether any budgets are in warning or exceeded status.

    Args:
        budget_name: Filter to a specific budget name (optional, shows all if empty)

    Examples:
        - "Check all budgets"
        - "How are we doing against budget?"
        - "Is the platform team over budget?"
        - "Show budget status for AWS"
    """
    try:
        from ..budget.enforcer import check_all_budgets, list_budgets, check_budget
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
                f"🔴 {len(exceeded)} budget(s) exceeded. Immediate action required."
                if exceeded else
                f"🟡 {len(warnings)} budget(s) approaching limit."
                if warnings else
                "✅ All budgets on track."
            ),
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def list_budgets() -> dict:
    """
    List all configured budgets with their limits and scopes.

    Examples:
        - "What budgets do we have?"
        - "Show me all spending limits"
        - "List configured budgets"
    """
    try:
        from ..budget.enforcer import list_budgets as _list
        budgets = _list()
        return {"count": len(budgets), "budgets": budgets}
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def delete_budget(budget_id: int) -> dict:
    """
    Delete (deactivate) a budget by ID so it stops alerting and gating agent actions.

    Args:
        budget_id: Budget ID from list_budgets

    Examples:
        - "Delete budget #3"
        - "Remove the platform team budget"
    """
    if err := _srv.require_role("analyst"):
        return err
    try:
        from ..budget.enforcer import delete_budget as _del
        ok = _del(budget_id)
        return {"deleted": ok, "budget_id": budget_id}
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def sync_budgets_from_yaml(yaml_path: str) -> dict:
    """
    Import budgets from a budget.yml file. Idempotent, running twice
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
    if err := _srv.require_role("analyst"):
        return err
    try:
        from ..budget.enforcer import sync_from_yaml
        return sync_from_yaml(yaml_path)
    except Exception as e:
        return {"error": str(e)}
