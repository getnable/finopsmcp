"""Docstring-quality ratchet for MCP tools.

External MCP directories grade each tool's description, and the model itself
picks tools by docstring. The bar: a real description (25+ words), documented
args when the tool takes params, and natural-language examples. Existing weak
docstrings are grandfathered below; the list may only SHRINK. A new tool that
ships weak fails here.
"""
import ast
from pathlib import Path

SERVER = Path(__file__).parent.parent / "src" / "finops" / "server.py"

# Grandfathered as of 2026-07-05 (49 tools). Fix one -> delete it here.
GRANDFATHERED = {
    "audit_textract_environment_waste", "benchmark_costs", "check_action_policy",
    "cleanup_idle_resources", "create_api_key", "estimate_change_cost",
    "estimate_helm_diff_cost", "estimate_terraform_cost", "explain_recent_cost_drivers",
    "export_board_summary", "export_cost_report_csv", "forecast_costs",
    "generate_account_dashboard", "get_ai_engineering_report", "get_bedrock_costs",
    "get_cluster_efficiency", "get_databricks_cluster_efficiency", "get_databricks_costs",
    "get_databricks_dbu_breakdown", "get_databricks_job_costs", "get_documentdb_costs",
    "get_efficiency_scorecard", "get_focus_costs", "get_helm_release_costs",
    "get_kendra_costs", "get_kubernetes_costs", "get_kubernetes_namespace_breakdown",
    "get_label_costs", "get_marketplace_costs", "get_nable_roi",
    "get_recommendation_learning", "get_recommendation_quality", "get_saas_spend_summary",
    "get_savings_ledger", "get_tableau_connection_info", "get_textract_costs",
    "get_total_spend_all_sources", "list_idle_resources", "list_profiles", "pin_view",
    "publish_cost_report_to_notion", "push_to_n8n", "recommend_bedrock_model_routing",
    "revoke_api_key", "run_full_cost_audit", "scan_waste_patterns", "slice_costs",
    "start_dashboard_server", "what_can_nable_do",
}


def _tools():
    tree = ast.parse(SERVER.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            if any(getattr(getattr(d, "func", None), "attr", "") == "tool"
                   for d in node.decorator_list if isinstance(d, ast.Call)):
                yield node


def _weak(node) -> str | None:
    doc = ast.get_docstring(node) or ""
    params = [a.arg for a in node.args.args if a.arg != "self"]
    if len(doc.split()) < 25:
        return "description under 25 words"
    if params and "Args:" not in doc:
        return "takes params but has no Args: section"
    if "Example" not in doc:
        return "no Examples"
    return None


def test_no_new_weak_tool_docstrings():
    failures = []
    for node in _tools():
        reason = _weak(node)
        if reason and node.name not in GRANDFATHERED:
            failures.append(f"{node.name}: {reason}")
    assert not failures, (
        "New/changed tools must ship a full docstring (25+ words, Args:, Examples): "
        + "; ".join(failures)
    )


def test_grandfathered_list_only_shrinks():
    # Entries that no longer exist or are no longer weak should be removed.
    weak_now = {n.name for n in _tools() if _weak(n)}
    stale = GRANDFATHERED - weak_now
    assert not stale, f"These are fixed or gone; remove from GRANDFATHERED: {sorted(stale)}"
