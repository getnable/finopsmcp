"""One-shot batch fix: add missing Args:/Examples sections to the 49 grandfathered
tool docstrings in server.py. Content lives in the dicts below; insertion is
mechanical (before the docstring's closing quotes, bottom-up so line numbers hold).
Run once, verify with tests/test_tool_docstring_quality.py, then delete-or-keep.
"""
from __future__ import annotations

import ast

SERVER = "src/finops/server.py"

# Shared parameter descriptions. Per-tool overrides win.
COMMON = {
    "days": "Look-back window in days (default 30).",
    "account": "Named AWS account from accounts.yaml. Uses the default when omitted.",
    "account_id": "AWS account id to scope the analysis. Defaults to the connected account.",
    "start_date": "ISO date (YYYY-MM-DD). Defaults to 30 days ago.",
    "end_date": "ISO date (YYYY-MM-DD). Defaults to today.",
    "regions": "AWS regions to scan. Defaults to all enabled regions.",
    "top_n": "How many top results to return.",
    "context": "Kubernetes context name from list_kubernetes_contexts(). Default context when omitted.",
    "namespace": "Limit to one Kubernetes namespace. All namespaces when omitted.",
    "period_days": "Reporting period in days (default 30).",
    "horizon_days": "How many days ahead to forecast (default 30).",
    "history_days": "How much history to train on, in days (default 90).",
    "provider": "Limit to one provider (e.g. \"aws\"). None = all.",
    "port": "Local TCP port to serve on.",
}

# tool -> (param->desc overrides, [examples])
FIX: dict[str, tuple[dict, list[str]]] = {
    "get_saas_spend_summary": ({}, []),
    "get_total_spend_all_sources": ({}, []),
    "generate_account_dashboard": (
        {"account_id": "AWS account id to render the dashboard for.",
         "open_browser": "Open the generated dashboard in your browser (default True).",
         "push_to_notion": "Also publish the dashboard summary to Notion (needs NOTION_API_KEY)."},
        ['"Build me a dashboard for the prod account"', '"Generate an account cost dashboard and open it"']),
    "get_savings_ledger": ({}, ['"Show the savings ledger"', '"What savings has nable found and what happened to them?"']),
    "get_recommendation_quality": ({}, ['"How accurate have nable\'s recommendations been?"', '"Show recommendation quality stats"']),
    "get_recommendation_learning": ({}, ['"What has nable learned from my accepted and dismissed recommendations?"']),
    "list_profiles": ({}, ['"List my nable profiles"', '"Which cost profiles are configured?"']),
    "list_idle_resources": (
        {"resource_types": "Subset to scan, e.g. [\"ebs\", \"eip\", \"nat\"]. All types when omitted.",
         "min_idle_days": "Only report resources idle at least this many days."},
        []),
    "cleanup_idle_resources": (
        {"resource_ids": "Explicit resource ids to act on. Required unless scanning by type.",
         "resource_types": "Idle resource types to include, e.g. [\"ebs\", \"eip\"].",
         "min_idle_days": "Only include resources idle at least this many days.",
         "dry_run": "True (default) previews actions without executing anything."},
        []),
    "get_kubernetes_costs": ({}, []),
    "get_kubernetes_namespace_breakdown": ({}, []),
    "get_efficiency_scorecard": (
        {"scope": "\"org\" (default) or \"team\" for a single team's scorecard.",
         "team": "Team name from your attribution tags, when scope=\"team\".",
         "environment": "Limit to one environment (e.g. \"prod\").",
         "provider": COMMON["provider"]},
        []),
    "get_helm_release_costs": ({}, []),
    "estimate_helm_diff_cost": (
        {"diff_text": "Output of `helm diff upgrade ...` to price.",
         "release_name": "Helm release the diff belongs to.",
         "current_replicas": "Current replica count, for delta math.",
         "current_cpu_request": "Current CPU request (e.g. \"500m\").",
         "current_memory_request": "Current memory request (e.g. \"512Mi\")."},
        []),
    "get_cluster_efficiency": ({}, []),
    "get_label_costs": (
        {"label_key": "Kubernetes label key to group costs by (e.g. \"app\", \"team\")."},
        []),
    "create_api_key": (
        {"name": "Human-readable key name (e.g. \"ci-reporter\").",
         "role": "\"viewer\", \"analyst\", or \"admin\".",
         "email": "Owner email recorded for audit.",
         "scope_team": "Restrict the key to one team's data.",
         "scope_provider": "Restrict the key to one provider."},
        []),
    "revoke_api_key": ({"key_id": "The key id from list_api_keys()."}, []),
    "get_ai_engineering_report": (
        {"repos": "Git repos to include (owner/name). All configured repos when omitted.",
         "unit": "Business unit for cost-per-unit math (e.g. \"pr\", \"commit\")."},
        ['"What has AI coding shipped this month and what did it cost?"',
         '"AI engineering report for the last 14 days"']),
    "benchmark_costs": (
        {"vertical": "Industry vertical to benchmark against (e.g. \"saas\")."},
        ['"How does our cloud spend compare to similar companies?"', '"Benchmark our costs"']),
    "forecast_costs": (
        {"service": "Limit the forecast to one service (e.g. \"AmazonEC2\")."},
        ['"Forecast our AWS spend for next month"', '"Where will EC2 costs be in 60 days?"']),
    "scan_waste_patterns": (
        {"min_monthly_waste": "Ignore findings below this monthly dollar amount.",
         "categories": "Subset of waste categories to scan. All when omitted."},
        ['"Scan for waste patterns"', '"Any recurring waste in this account?"']),
    "estimate_terraform_cost": (
        {"plan_json": "Terraform plan JSON string (`terraform show -json`).",
         "plan_file": "Path to a terraform plan JSON file.",
         "tf_dir": "Terraform directory to plan and price."},
        ['"What will this terraform plan cost?"', '"Price the plan in ./infra"']),
    "estimate_change_cost": (
        {"terraform_plan_json": "Terraform plan JSON string to price.",
         "terraform_plan_file": "Path to a terraform plan JSON file.",
         "tf_dir": "Terraform directory to plan and price.",
         "helm_diff": "Helm diff text to price instead of terraform.",
         "monthly_delta_usd": "Known monthly delta, when you already have the number.",
         "budget_name": "Budget to check the delta against."},
        ['"What would this change cost per month?"', '"Preflight the cost of this terraform plan"']),
    "check_action_policy": (
        {"action_type": "The infra action being attempted (e.g. \"terraform_apply\").",
         "terraform_plan_json": "Terraform plan JSON string to evaluate.",
         "terraform_plan_file": "Path to a terraform plan JSON file.",
         "tf_dir": "Terraform directory to plan and evaluate.",
         "helm_diff": "Helm diff text to evaluate instead of terraform.",
         "monthly_delta_usd": "Known monthly delta, when you already have the number.",
         "budget_name": "Budget to evaluate the action against."},
        ['"Is this apply within policy?"', '"Check this change against our cost guardrails"']),
    "export_board_summary": ({}, []),
    "get_databricks_costs": ({}, ['"What are we spending on Databricks?"', '"Databricks costs this month"']),
    "get_databricks_dbu_breakdown": ({}, ['"Break down our Databricks DBU usage by SKU"']),
    "get_databricks_cluster_efficiency": ({}, ['"Which Databricks clusters are inefficient?"']),
    "get_databricks_job_costs": ({}, ['"What do our Databricks jobs cost?"', '"Top 10 most expensive Databricks jobs"']),
    "get_focus_costs": (
        {"group_by": "FOCUS field to group by (e.g. \"ServiceCategory\", \"ProviderName\")."},
        ['"Show costs in FOCUS format grouped by service category"']),
    "slice_costs": (
        {"dimensions": "Fields to group by (e.g. [\"service\", \"region\"]).",
         "filters": "Include-filters, {field: [values]}.",
         "exclusions": "Exclude-filters, {field: [values]}.",
         "metric": "\"cost\" (default) or another supported metric.",
         "granularity": "\"DAILY\" or \"MONTHLY\".",
         "order_by": "Sort field, defaults to the metric descending.",
         "limit": "Max rows to return.",
         "title": "Optional title for the resulting card.",
         "via": "Internal: how the slice was invoked."},
        []),
    "pin_view": (
        {"title": "Card title shown on the dashboard.",
         "dimensions": "Fields to group by (as in slice_costs).",
         "filters": "Include-filters, {field: [values]}.",
         "exclusions": "Exclude-filters, {field: [values]}.",
         "metric": "\"cost\" (default) or another supported metric.",
         "granularity": "\"DAILY\" or \"MONTHLY\".",
         "order_by": "Sort field, defaults to the metric descending.",
         "limit": "Max rows in the card.",
         "scope": "\"instance\" (default) pins for this machine."},
        ['"Pin this S3-by-region view to my dashboard"', '"Save that as a card"']),
    "get_bedrock_costs": ({}, ['"What is Bedrock costing us?"', '"Bedrock spend by model this month"']),
    "get_documentdb_costs": ({}, ['"DocumentDB costs for the last 30 days"']),
    "get_kendra_costs": ({}, ['"What is Amazon Kendra costing us?"']),
    "get_textract_costs": ({}, ['"Textract spend this month"', '"What are we paying for OCR?"']),
    "audit_textract_environment_waste": ({}, ['"Is non-prod Textract usage wasting money?"']),
    "recommend_bedrock_model_routing": ({}, ['"Could cheaper Bedrock models handle some of our load?"']),
    "get_marketplace_costs": ({}, ['"What AWS Marketplace subscriptions are we paying for?"']),
    "run_full_cost_audit": ({}, ['"Run a full cost audit"', '"Find everything we could save"']),
    "export_cost_report_csv": (
        {"output_path": "Full path for the CSV. Defaults to ~/Downloads/nable-report-<date>.csv."},
        ['"Export that audit to CSV"', '"Save the findings as a spreadsheet"']),
    "push_to_n8n": (
        {"event_type": "Which payload to send (e.g. \"cost_audit\")."},
        ['"Push the audit results to n8n"']),
    "publish_cost_report_to_notion": ({}, ['"Publish the cost report to Notion"', '"Share this audit with the team"']),
    "what_can_nable_do": (
        {"detailed": "True returns the full capability list instead of the summary."},
        ['"What can nable do?"', '"List your capabilities"']),
    "explain_recent_cost_drivers": ({}, ['"Why did costs go up this week?"', '"What drove spend recently?"']),
    "get_nable_roi": ({}, ['"What has nable saved us versus what it costs?"', '"Show nable ROI"']),
    "start_dashboard_server": (
        {"host": "Interface to bind (default 127.0.0.1, local only).",
         "expose": "True binds beyond localhost. Only on a trusted network."},
        ['"Start the dashboard"', '"Serve the web dashboard on port 9000"']),
    "get_tableau_connection_info": ({}, ['"How do I connect Tableau?"', '"Give me the Tableau connector URL"']),
}


def main() -> None:
    src = open(SERVER).read()
    lines = src.split("\n")
    tree = ast.parse(src)

    jobs = []  # (insert_line_idx0, indent, args_block, examples_block)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if node.name not in FIX:
            continue
        if not any(getattr(getattr(d, "func", None), "attr", "") == "tool"
                   for d in node.decorator_list if isinstance(d, ast.Call)):
            continue
        doc_node = node.body[0]
        doc = ast.get_docstring(node) or ""
        params = [a.arg for a in node.args.args if a.arg != "self"]
        overrides, examples = FIX[node.name]

        need_args = bool(params) and "Args:" not in doc
        need_ex = "Example" not in doc
        if not (need_args or need_ex):
            continue

        # docstring closing quotes live on doc_node.end_lineno (1-based)
        close_idx = doc_node.end_lineno - 1
        closing = lines[close_idx]
        if '"""' not in closing:
            raise SystemExit(f"{node.name}: unexpected docstring close at line {close_idx+1}")
        indent = " " * (len(closing) - len(closing.lstrip()))
        # one-line docstrings were already fixed; guard anyway
        if closing.strip() != '"""':
            raise SystemExit(f"{node.name}: docstring closes on a content line; fix manually")

        block: list[str] = []
        if need_args:
            block.append(f"{indent}Args:")
            for p in params:
                desc = overrides.get(p) or COMMON.get(p)
                if not desc:
                    raise SystemExit(f"{node.name}: no description for param {p!r}")
                block.append(f"{indent}    {p}: {desc}")
            block.append("")
        if need_ex:
            ex = examples or FIX[node.name][1]
            if not ex:
                raise SystemExit(f"{node.name}: needs examples but none provided")
            block.append(f"{indent}Examples:")
            for e in ex:
                block.append(f"{indent}    - {e}")
            block.append("")
        jobs.append((close_idx, block, node.name))

    # bottom-up so line indices stay valid
    for close_idx, block, name in sorted(jobs, reverse=True):
        lines[close_idx:close_idx] = block
        print(f"fixed {name}")

    open(SERVER, "w").write("\n".join(lines))
    print(f"\n{len(jobs)} docstrings updated")


if __name__ == "__main__":
    main()
