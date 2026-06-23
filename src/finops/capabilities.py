"""
Capability catalog: turn nable's 160+ tools into a stack-tailored answer to
"what can you do?"

The problem this solves: nable has a lot of tools, and a user who has connected
only Claude + AWS has no idea most of them exist or which apply to them. This
module holds an outcome-oriented catalog (find savings, understand the bill, AI
cost, commitments, forecast, fix it, share, automate) where every group is gated
on what the user actually has connected. The renderer is pure and testable; the
MCP tool feeds it the detected surfaces.

Design goals:
  - Tailored: show what THIS stack unlocks, not a generic dump.
  - Honest credit: name the marquee tools and counts so the breadth is visible.
  - Activating: end with the single highest-value thing left to connect.
"""
from __future__ import annotations

from typing import Any

# Total read-only tools nable ships (matches the plugin marketplace description).
TOTAL_TOOLS = 160

# Surface tokens the renderer reasons over. The MCP tool maps connected provider
# names onto these.
_CLOUD = {"aws", "azure", "gcp"}
_LLM_PROVIDERS = {"openai", "anthropic", "bedrock", "vertex", "openrouter",
                  "litellm", "modal", "together", "replicate"}


def _has(connected: set[str], *tokens: str) -> bool:
    return any(t in connected for t in tokens)


def has_cloud(connected: set[str]) -> bool:
    return _has(connected, *_CLOUD)


def has_llm(connected: set[str]) -> bool:
    return "llm" in connected or _has(connected, *_LLM_PROVIDERS)


# ── the catalog ───────────────────────────────────────────────────────────────
# Each group: id, title, gate (a predicate over the connected set), one-line
# blurb, an approximate backing tool_count (for honest "credit"), example asks
# (the natural-language question + the concrete impact), and the marquee tool
# names surfaced in detailed mode.

CATALOG: list[dict[str, Any]] = [
    {
        "id": "understand",
        "title": "Understand your bill",
        "gate": has_cloud,
        "count": 18,
        "blurb": "Ask anything about where the money goes, across every connected provider.",
        "asks": [
            ("What's driving our spend this month?", "top cost drivers vs last month, with the why"),
            ("Break down our AWS spend by service", "last 30/60/90 days, any provider"),
            ("What do we spend by team or tag?", "attribution by your tag rules"),
            ("Compare our cloud providers", "AWS vs Azure vs GCP side by side"),
        ],
        "tools": ["get_cost_summary", "get_costs_by_service", "get_costs_by_team",
                  "get_top_cost_drivers", "explain_recent_cost_drivers", "get_cost_trends",
                  "compare_providers", "get_tag_cost_breakdown_cur"],
    },
    {
        "id": "savings",
        "title": "Find savings",
        "gate": has_cloud,
        "count": 30,
        "blurb": "Dozens of waste scanners and rightsizing engines, ranked by dollar impact.",
        "asks": [
            ("What are our biggest savings opportunities?", "runs every scanner, ranked by $/mo"),
            ("Show me rightsizing recommendations", "EC2, RDS, Lambda, ECS, EKS"),
            ("Any idle resources to clean up?", "idle load balancers, RDS, volumes, IPs"),
            ("What Graviton or spot can we move to?", "20-40% on compute, up to 90% on spot"),
        ],
        "tools": ["scan_waste_patterns", "run_full_cost_audit", "get_rightsizing_recommendations",
                  "list_idle_resources", "scan_graviton_migration_opportunities",
                  "recommend_spot_adoption", "audit_public_ipv4_addresses", "get_savings_summary"],
    },
    {
        "id": "commitments_cloud",
        "title": "Commitments (RIs & Savings Plans)",
        "gate": has_cloud,
        "count": 8,
        "blurb": "Where you are over- or under-committed, and what to buy.",
        "asks": [
            ("Show our commitment coverage", "RI and Savings Plan gaps by tag"),
            ("Are we wasting any reserved capacity?", "RI/SP utilization and waste detail"),
            ("What should we commit to?", "break-even-aware buy recommendations"),
        ],
        "tools": ["get_commitment_analysis", "get_commitment_coverage_by_tag",
                  "get_ri_waste_detail", "get_savings_plan_showback", "get_effective_rate_profile"],
    },
    {
        "id": "catch",
        "title": "Catch surprises",
        "gate": has_cloud,
        "count": 10,
        "blurb": "Anomaly detection and budgets so a spike never waits for the invoice.",
        "asks": [
            ("Why did our bill spike?", "anomaly detection with tag attribution"),
            ("Set a budget and alert me at 80%", "two-tier: warn at 80%, block at 100%"),
            ("What AI spend is AWS not watching?", "Bedrock/Marketplace blind spots"),
        ],
        "tools": ["get_anomalies", "get_account_anomalies", "set_budget", "check_budget_status",
                  "get_ai_billing_blind_spots"],
    },
    {
        "id": "forecast_cloud",
        "title": "Forecast",
        "gate": has_cloud,
        "count": 3,
        "blurb": "Per-account Holt-Winters projection so finance sees the curve early.",
        "asks": [
            ("Forecast our cloud spend next quarter", "trend + seasonality, with confidence band"),
            ("Are we on track to blow the budget?", "projected vs budget, by account"),
        ],
        "tools": ["forecast_costs", "forecast_azure_costs"],
    },
    {
        "id": "credits",
        "title": "Credits & the cash cliff",
        "gate": lambda c: "aws" in c,
        "count": 3,
        "blurb": "Track promo-credit burn and the day billing flips to real cash.",
        "asks": [
            ("Are our AWS credits about to run out?", "runway from observed burn, no API for balance"),
            ("When do credits flip to cash?", "the cliff alert AWS never sends"),
        ],
        "tools": ["get_credit_status", "get_ai_billing_blind_spots"],
    },
    {
        "id": "ai_cost",
        "title": "AI / LLM cost",
        "gate": lambda c: has_llm(c) or "aws" in c,  # Bedrock cost works for AWS too
        "count": 14,
        "blurb": "The token bill almost nobody watches, by model, with unit economics.",
        "asks": [
            ("What are we spending on tokens, by model?", "OpenAI, Anthropic, Bedrock, gateways"),
            ("What's our cost per customer or per request?", "AI unit economics tied to your metrics"),
            ("Where's the AI waste?", "cache hit rate, model sprawl, prompt efficiency"),
            ("Cut our AI spend", "model-routing and caching recommendations"),
        ],
        "tools": ["get_llm_costs", "get_llm_cost_by_model", "get_llm_unit_economics_full",
                  "get_ai_kpis", "optimize_ai_spend", "recommend_bedrock_model_routing",
                  "get_gpu_infra_costs", "get_bedrock_costs"],
    },
    {
        "id": "ai_commitments",
        "title": "AI commitments & contracts",
        "gate": has_llm,
        "count": 1,
        "blurb": "Reserved-Instance analysis for tokens: credits, PTUs, Provisioned Throughput, rate cards.",
        "asks": [
            ("Are we utilizing our Azure PTUs?", "utilization, effective $/Mtok vs on-demand"),
            ("Should we buy provisioned throughput?", "break-even on your stable token baseline"),
            ("Are we clearing our enterprise minimum?", "flags committed volume you paid for unused"),
        ],
        "tools": ["get_llm_commitment_analysis"],
    },
    {
        "id": "ai_forecast",
        "title": "AI forecast & monitor",
        "gate": has_llm,
        "count": 2,
        "blurb": "Project the token bill and get the credit/commitment exhaustion date.",
        "asks": [
            ("When will our credits run out at this rate?", "exhaustion date from the token forecast"),
            ("Is our token bill accelerating?", "projected spend + month-over-month growth"),
            ("Did our token spend spike?", "daily anomaly + commitment attention"),
        ],
        "tools": ["forecast_llm_costs", "get_ai_spend_monitor"],
    },
    {
        "id": "fix_aws",
        "title": "Fix it (close the loop)",
        "gate": lambda c: "aws" in c,
        "count": 6,
        "blurb": "Not just recommend, act: nable opens the PR that applies the fix.",
        "asks": [
            ("Open a PR for the top rightsizing rec", "reads tfstate, patches .tf, opens a GitHub PR"),
            ("Apply the missing tag fixes", "writes tags straight into your Terraform"),
            ("Estimate the cost of this Terraform plan", "diff the bill before you merge"),
        ],
        "tools": ["open_rightsizing_pr", "open_terraform_tag_pr", "generate_terraform_tag_fixes",
                  "estimate_terraform_cost", "estimate_helm_diff_cost"],
    },
    {
        "id": "aws_audits",
        "title": "Deep AWS audits",
        "gate": lambda c: "aws" in c,
        "count": 16,
        "blurb": "Sixteen targeted scanners for the line items everyone forgets.",
        "asks": [
            ("Audit our idle public IPv4 addresses", "$3.60/mo each since Feb 2024"),
            ("Check our NLB cross-zone data charges", "and EFS cross-AZ mounts"),
            ("Find orphaned CloudWatch alarms and EBS snapshots", "and S3 tiering opportunities"),
        ],
        "tools": ["audit_public_ipv4_addresses", "audit_nlb_cross_zone_costs",
                  "audit_ebs_snapshot_replication", "audit_cloudwatch_orphaned_alarms",
                  "audit_s3_intelligent_tiering", "audit_rds_manual_snapshots",
                  "scan_cloudwatch_waste", "scan_lambda_concurrency_waste"],
    },
    {
        "id": "gcp_audits",
        "title": "Deep GCP audits",
        "gate": lambda c: "gcp" in c,
        "count": 4,
        "blurb": "Resource-level GCP waste scans the billing export cannot see.",
        "asks": [
            ("Run a full GCP waste audit", "unattached disks, idle IPs, old snapshots, idle VMs"),
            ("Find unattached GCP persistent disks", "and reserved static IPs that are not in use"),
            ("Which GCP VMs are idle this month?", "CPU joined from Cloud Monitoring"),
        ],
        "tools": ["audit_gcp_waste"],
    },
    {
        "id": "azure",
        "title": "Azure deep dives",
        "gate": lambda c: "azure" in c,
        "count": 7,
        "blurb": "Advisor, VM rightsizing, and reservation utilization, native to Azure.",
        "asks": [
            ("Show Azure cost by dimension", "resource group, service, tag"),
            ("Get Azure Advisor recommendations", "plus VM rightsizing"),
            ("Are our Azure reservations utilized?", "reservation utilization detail"),
        ],
        "tools": ["get_azure_cost_by_dimension", "get_azure_advisor_recommendations",
                  "get_azure_vm_rightsizing", "get_azure_reservation_utilization",
                  "get_azure_budgets", "forecast_azure_costs"],
    },
    {
        "id": "kubernetes",
        "title": "Kubernetes",
        "gate": lambda c: "kubernetes" in c,
        "count": 8,
        "blurb": "Cluster and namespace cost, efficiency, and waste, no agent to install.",
        "asks": [
            ("Break down Kubernetes cost by namespace", "and by workload"),
            ("How efficient is our cluster?", "requested vs used, the waste gap"),
            ("Compare our clusters", "cost and efficiency side by side"),
        ],
        "tools": ["get_kubernetes_costs", "get_kubernetes_namespace_breakdown",
                  "get_cluster_efficiency", "compare_kubernetes_clusters",
                  "get_helm_release_costs", "create_kubernetes_waste_tickets"],
    },
    {
        "id": "saas",
        "title": "SaaS & data platforms",
        "gate": lambda c: _has(c, "datadog", "snowflake", "databricks"),
        "count": 10,
        "blurb": "Datadog, Snowflake, Databricks, and more, in the same cost view.",
        "asks": [
            ("What are our Databricks DBU costs?", "by job and cluster, with efficiency"),
            ("Summarize our SaaS spend", "every connected SaaS provider"),
        ],
        "tools": ["get_databricks_costs", "get_databricks_dbu_breakdown",
                  "get_saas_spend_summary", "get_marketplace_costs"],
    },
    {
        "id": "share",
        "title": "Share & automate",
        "gate": lambda c: True,
        "count": 14,
        "blurb": "Get findings out of chat: dashboards, exports, tickets, alerts, digests.",
        "asks": [
            ("Export this to CSV", "opens clean in Excel or Sheets"),
            ("Start the team dashboard", "browser dashboard, no Claude required"),
            ("File a Jira ticket for this anomaly", "or Linear or GitHub"),
            ("Send a weekly digest every Monday", "top drivers by email or Slack"),
        ],
        "tools": ["export_cost_report_csv", "start_dashboard_server", "create_ticket",
                  "create_anomaly_tickets", "send_weekly_digest_now", "publish_cost_report_to_notion",
                  "push_to_n8n", "subscribe_to_report"],
    },
]

# What connecting a not-yet-connected surface unlocks (the activation nudge).
UNLOCKS = {
    "llm": "your OpenAI/Anthropic key, to add token cost, AI unit economics, "
           "commitment analysis, and the credit-exhaustion forecast",
    "azure": "Azure, for Advisor, VM rightsizing, and reservation utilization",
    "gcp": "GCP, for label-level cost and committed-use analysis",
    "kubernetes": "your kubeconfig, for namespace cost and cluster efficiency (no agent)",
    "slack": "Slack, for anomaly and budget alerts where your team already is",
    "datadog": "Datadog, to fold observability spend into the same view",
}


def render_capabilities(
    connected: set[str],
    plan: str = "free",
    detailed: bool = False,
) -> str:
    """Render the capability map tailored to the connected surfaces.

    Args:
      connected: surface tokens that are connected (provider names plus the
        derived token "llm" when any LLM provider is configured, and
        "kubernetes" when a cluster is reachable).
      plan: "free" | "trial" | "pro" | "team", for the closing note.
      detailed: when True, list the backing tool names under each group.
    """
    # "Connected" only counts a real data source. A notification-only surface
    # (Slack/Notion) without any cloud, LLM, or SaaS spend has nothing to act on.
    has_data = has_cloud(connected) or has_llm(connected) or _has(
        connected, "datadog", "snowflake", "databricks")
    if not has_data:
        return "\n".join([
            "## What nable can do",
            "",
            "Nothing's connected yet, so there's no cost data to work with. Connect your "
            "first provider and ask again:",
            "- `uvx finops-mcp setup aws` (or azure, gcp)",
            "- Add an `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` to track token spend",
            "",
            f"nable ships {TOTAL_TOOLS}+ read-only tools. They light up as you connect.",
        ])

    shown = [g for g in CATALOG if g["gate"](connected)]
    relevant = sum(g["count"] for g in shown)

    # Name the stack in human terms.
    clouds = [c.upper() for c in ("aws", "azure", "gcp") if c in connected]
    stack_bits = ["Claude"]  # the MCP client asking is always the editor surface
    stack_bits += clouds
    if has_llm(connected):
        stack_bits.append("your LLM providers")
    if "kubernetes" in connected:
        stack_bits.append("Kubernetes")
    stack = ", ".join(stack_bits)

    lines: list[str] = []

    lines += [
        f"## What nable can do with {stack}",
        "",
        f"You've got **{stack}** connected. That lights up **{relevant}** of nable's "
        f"{TOTAL_TOOLS}+ read-only tools. Here's the map, grouped by what you'd actually ask:",
        "",
    ]

    for g in shown:
        n = g["count"]
        tool_label = "1 tool" if n == 1 else f"~{n} tools"
        lines.append(f"### {g['title']}  ·  {tool_label}")
        lines.append(g["blurb"])
        for ask, impact in g["asks"]:
            lines.append(f'- **"{ask}"**  ({impact})')
        if detailed:
            lines.append(f"  <sub>tools: {', '.join(g['tools'])}</sub>")
        lines.append("")

    # Activation nudge: the single highest-value thing left to connect.
    missing = [tok for tok in ("llm", "azure", "gcp", "kubernetes", "slack", "datadog")
               if tok not in connected and not (tok == "llm" and has_llm(connected))]
    if missing:
        lines.append("### Connect more to unlock")
        for tok in missing[:3]:
            lines.append(f"- Connect {UNLOCKS[tok]}.")
        lines.append("")

    lines.append("---")
    lines.append("Ask for any of these naturally, nable picks the right tool. "
                 "Everything is read-only and runs on your machine.")
    if plan in ("free",):
        lines.append("")
        lines.append("*Team plan adds ticket auto-creation, scheduled email digests, "
                     "commitment recommendations, and org rollups.*")
    return "\n".join(lines)
