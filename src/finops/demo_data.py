"""
Demo / recording mode for nable.

Set FINOPS_DEMO_MODE=1 to make all cost tools return realistic-looking
fake data instead of hitting real cloud APIs.

Use this when:
  - Recording product demos / tutorial videos
  - Sales calls where you don't want to show real account numbers
  - Integration tests that don't need live credentials
  - Docs screenshots

The fake data is internally consistent: the same account IDs, service
names, and cost numbers appear across all tools so the demo flows naturally.
"""
from __future__ import annotations

import os
import time
from datetime import date, timedelta
from typing import Any

_TRUTHY = ("1", "true", "yes")
# Accept FINOPS_DEMO as an alias for FINOPS_DEMO_MODE (docs and the landing page
# refer to FINOPS_DEMO; both now work so users don't hit a silently-ignored var).
DEMO_MODE = (
    os.environ.get("FINOPS_DEMO_MODE", "").lower() in _TRUTHY
    or os.environ.get("FINOPS_DEMO", "").lower() in _TRUTHY
)


def _managed_instance() -> bool:
    """True when this process is a managed (control-plane) hosted instance, i.e.
    getnable.com control-plane login is configured (both the per-instance secret
    and the instance id are set). Such an instance serves a real paying customer."""
    return bool(
        os.environ.get("FINOPS_CONTROL_PLANE_SECRET", "").strip()
        and os.environ.get("FINOPS_INSTANCE_ID", "").strip()
    )


# Vault keys that mean a real cloud provider has been connected. setup stores AWS
# under AWS_ACCESS_KEY_ID / AWS_ROLE_ARNS, Azure and GCP under theirs. If any is
# present, the user connected a real account, so demo data must step aside.
_PROVIDER_CRED_KEYS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_ROLE_ARNS",
    "AZURE_TENANT_ID",
    "AZURE_SUBSCRIPTION_IDS",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "_GCP_SERVICE_ACCOUNT_JSON",
    "GCP_BILLING_ACCOUNT_IDS",
)

_real_provider_cache: "tuple[float, bool] | None" = None


def _real_provider_connected() -> bool:
    """True once a real cloud provider is connected. Cheap and cached briefly so
    is_demo stays free on the hot path. Fast path: a connect mirrors credentials
    into os.environ at startup, so a cred already in the env means a real account
    (covers the common connect-then-restart). Fallback: read the vault live, to
    catch a provider connected mid-session before the process restarted."""
    global _real_provider_cache
    now = time.monotonic()
    if _real_provider_cache is not None and _real_provider_cache[0] > now:
        return _real_provider_cache[1]
    found = any(os.environ.get(k) for k in _PROVIDER_CRED_KEYS)
    if not found:
        try:
            from .security.vault import Vault

            keys = set(Vault.default().list_keys())
            found = any(k in keys for k in _PROVIDER_CRED_KEYS)
        except Exception:
            found = False
    _real_provider_cache = (now + 30.0, found)
    return found


def is_demo() -> bool:
    # A managed hosted instance never serves demo data, even if FINOPS_DEMO is set
    # by a stray env. A paying customer must get real numbers and the real model,
    # never the canned demo_data stubs.
    if _managed_instance():
        return False
    if not DEMO_MODE:
        return False
    # Demo yields to real data: the moment a real provider is connected, show the
    # real numbers, not the canned demo. FINOPS_DEMO_FORCE=1 keeps demo on even
    # then, for recording a demo on a machine that has a real account connected.
    if os.environ.get("FINOPS_DEMO_FORCE", "").lower() in _TRUTHY:
        return True
    if _real_provider_connected():
        return False
    return True


# ── Shared demo constants (internally consistent across all tools) ────────────

_ACCOUNT_ID   = "123456789012"
_ACCOUNT_NAME = "acme-production"
_REGION       = "us-east-1"

_TODAY = date.today()
_MONTH_START = _TODAY.replace(day=1).isoformat()
_YESTERDAY   = (_TODAY - timedelta(days=1)).isoformat()


# ── Tool response stubs ───────────────────────────────────────────────────────

def cost_summary() -> dict[str, Any]:
    return {
        "period": f"{_MONTH_START} to {_YESTERDAY}",
        "total_usd": 12847.22,
        "vs_last_month_pct": 23.4,
        "by_service": {
            "Amazon EC2":                  7240.10,
            "Amazon RDS":                  2100.44,
            "AWS Data Transfer":           1890.33,
            "Amazon S3":                    822.15,
            "Amazon CloudWatch":            412.88,
            "AWS Lambda":                   201.44,
            "Amazon EKS":                   180.00,
        },
        "account_id":   _ACCOUNT_ID,
        "account_name": _ACCOUNT_NAME,
        "summary": (
            "Total AWS spend this month: $12,847 (+23% vs last month). "
            "EC2 is the top driver at $7,240, up $1,890 from last month."
        ),
    }


def anomalies() -> dict[str, Any]:
    return {
        "anomalies": [
            {
                "id":          "anom-001",
                "service":     "Amazon EC2",
                "account_id":  _ACCOUNT_ID,
                "severity":    "high",
                "detected_at": f"{(_TODAY - timedelta(days=3)).isoformat()}T14:22:00Z",
                "description": (
                    "EC2 spend in us-east-1 spiked $1,890 (+35%) between May 18-21. "
                    "A new m5.4xlarge was added to the data-platform node group."
                ),
                "daily_cost_before": 234.10,
                "daily_cost_after":  315.80,
                "projected_monthly_impact": 2481.00,
                "resource_ids":  ["i-0a1b2c3d4e5f67890"],
                "tags":          {"team": "data-platform", "env": "production"},
            },
            {
                "id":          "anom-002",
                "service":     "AWS Data Transfer",
                "account_id":  _ACCOUNT_ID,
                "severity":    "medium",
                "detected_at": f"{(_TODAY - timedelta(days=2)).isoformat()}T09:15:00Z",
                "description": (
                    "Data transfer out increased $640 (+51%) — likely correlated "
                    "with the EC2 node group change on May 18."
                ),
                "daily_cost_before":         42.10,
                "daily_cost_after":          63.40,
                "projected_monthly_impact":  640.00,
            },
        ],
        "total_anomalies": 2,
        "high_severity":   1,
        "summary": "2 cost anomalies detected. EC2 spike is the primary concern at +$1,890/mo.",
    }


def rightsizing() -> dict[str, Any]:
    return {
        "recommendations": [
            {
                "resource_id":   "i-0a1b2c3d4e5f67890",
                "resource_name": "data-platform-worker-01",
                "resource_type": "ec2",
                "current_type":  "m5.4xlarge",
                "recommended_type": "m5.2xlarge",
                "current_monthly_cost":    560.64,
                "recommended_monthly_cost": 280.32,
                "monthly_savings":         280.32,
                "cpu_avg_pct":  12.4,
                "mem_avg_pct":  31.2,
                "reason": "CPU averaging 12% over 14 days. m5.2xlarge has sufficient headroom.",
                "confidence": "high",
            },
            {
                "resource_id":   "db-prod-analytics-01",
                "resource_name": "prod-analytics",
                "resource_type": "rds",
                "current_type":  "db.r5.2xlarge",
                "recommended_type": "db.r5.xlarge",
                "current_monthly_cost":    1094.40,
                "recommended_monthly_cost":  547.20,
                "monthly_savings":           547.20,
                "cpu_avg_pct":  8.1,
                "mem_avg_pct": 42.3,
                "reason": "CPU at 8% avg, memory at 42%. db.r5.xlarge covers both comfortably.",
                "confidence": "medium",
            },
        ],
        "total_monthly_savings": 827.52,
        "summary": "2 rightsizing opportunities found. Total potential savings: $828/month.",
    }


def kubernetes_costs() -> dict[str, Any]:
    return {
        "cluster":               "prod-eks-cluster",
        "provider":              "aws",
        "node_count":            8,
        "pod_count":             47,
        "total_monthly_cost_usd": 4180.00,
        "wasted_monthly_cost_usd": 890.00,
        "waste_pct":             21.3,
        "cpu_efficiency_pct":    44.2,
        "mem_efficiency_pct":    61.8,
        "cost_by_namespace": {
            "data-platform":  1840.00,
            "api-services":   1120.00,
            "monitoring":      620.00,
            "kube-system":     380.00,
            "staging":         220.00,
        },
        "top_workloads": [
            {
                "namespace":          "data-platform",
                "workload":           "Deployment/spark-worker",
                "pods":               6,
                "monthly_cost_usd":   1240.00,
                "wasted_usd":         480.00,
                "cpu_efficiency_pct": 28.4,
                "mem_efficiency_pct": 52.1,
            },
            {
                "namespace":          "api-services",
                "workload":           "Deployment/payments-api",
                "pods":               4,
                "monthly_cost_usd":   560.00,
                "wasted_usd":         80.00,
                "cpu_efficiency_pct": 71.2,
                "mem_efficiency_pct": 68.4,
            },
        ],
        "idle_nodes":    ["ip-10-0-1-44.ec2.internal"],
        "idle_node_cost_usd": 280.32,
        "summary": (
            "Cluster 'prod-eks-cluster' (AWS, 8 nodes): $4,180/month. "
            "~$890/month wasted — spark-worker is 28% CPU efficient."
        ),
    }


def cluster_efficiency() -> dict[str, Any]:
    return {
        "cluster":  "prod-eks-cluster",
        "provider": "aws",
        "score":    58.4,
        "grade":    "C",
        "total_monthly_cost_usd":   4180.00,
        "wasted_monthly_cost_usd":   890.00,
        "has_metrics_server": True,
        "dimensions": {
            "cpu_efficiency_pct":  44.2,
            "cpu_score":           13.3,
            "mem_efficiency_pct":  61.8,
            "mem_score":           18.5,
            "idle_node_pct":       12.5,
            "idle_node_score":     15.0,
            "waste_pct":           21.3,
            "waste_score":         11.6,
        },
        "headline": (
            "Cluster 'prod-eks-cluster' scores 58/100 (Grade C) — "
            "$4,180/mo total, $890/mo estimated waste. "
            "Moderate waste. Tackle idle nodes and top rightsizing candidates first."
        ),
        "top_recommendations": [
            {
                "priority": "high",
                "category": "idle_nodes",
                "action":   "Drain ip-10-0-1-44 (idle node, <10% CPU/mem) — saving ~$252/mo.",
                "potential_savings_usd": 252.0,
            },
            {
                "priority": "medium",
                "category": "rightsizing",
                "action":   "Rightsize data-platform/spark-worker: CPU requests 24 cores, using 6.8 (28%) — reduce to 9 cores.",
                "potential_savings_usd": 336.0,
            },
        ],
    }


def cost_summary_cur() -> dict[str, Any]:
    """Demo response for CUR/Athena line-item query."""
    return {
        "source":  "AWS Cost and Usage Report (Athena)",
        "period":  f"{_MONTH_START} to {_YESTERDAY}",
        "total_usd": 12847.22,
        "top_resources": [
            {
                "resource_id":   "i-0a1b2c3d4e5f67890",
                "resource_name": "data-platform-worker-01",
                "service":       "Amazon EC2",
                "instance_type": "m5.4xlarge",
                "region":        "us-east-1",
                "monthly_cost":  560.64,
                "tags": {"team": "data-platform", "env": "production"},
            },
            {
                "resource_id":   "db-prod-analytics-01",
                "resource_name": "prod-analytics",
                "service":       "Amazon RDS",
                "instance_type": "db.r5.2xlarge",
                "region":        "us-east-1",
                "monthly_cost":  1094.40,
                "tags": {"team": "data", "env": "production"},
            },
        ],
        "by_tag_team": {
            "data-platform": 3840.10,
            "api":           2100.44,
            "data":          1890.33,
            "platform":       822.15,
            "untagged":      4194.20,
        },
        "untagged_pct": 32.6,
        "note": "32% of spend is untagged — add 'team' tags to reduce allocation blind spots.",
    }


# ── Registry: maps tool name → demo response function ─────────────────────────

def llm_costs() -> dict[str, Any]:
    # AI/LLM spend, consistent with the acme-production story: ~$4,120/mo,
    # which is ~32% of the $12,847 infra bill. gpt-4o dominates. The wedge:
    # show the money answer AND the switch that recovers it, with zero creds.
    daily = []
    for i in range(13, -1, -1):
        d = (_TODAY - timedelta(days=i)).isoformat()
        # gentle upward drift, ~$135/day average
        daily.append({"date": d, "total_usd": round(118 + (13 - i) * 2.6, 2)})
    return {
        "period": f"{_MONTH_START} to {_YESTERDAY}",
        "total_usd": 4120.00,
        "pct_of_total_cloud_spend": 32.1,
        "by_provider": {
            "openai":    2920.00,
            "anthropic":  870.00,
            "bedrock":    330.00,
        },
        "by_model": {
            "gpt-4o":                       2080.00,
            "claude-sonnet-4-5-20250929":    620.00,
            "o1":                            430.00,
            "gpt-4o-mini":                   410.00,
            "bedrock/anthropic.claude":      330.00,
            "claude-haiku-4-5-20251001":     250.00,
        },
        "model_count": 6,
        "top_spenders": [
            {"model": "gpt-4o",            "provider": "openai",    "cost_usd": 2080.00},
            {"model": "claude-sonnet-4-5", "provider": "anthropic", "cost_usd":  620.00},
            {"model": "o1",                "provider": "openai",    "cost_usd":  430.00},
        ],
        "daily": daily,
        "recommendations": [
            {
                "title": "Route short-context requests off gpt-4o",
                "detail": (
                    "gpt-4o is 71% of AI spend ($2,920/mo). About 60% of those requests "
                    "use under 4K context and don't need it. Routing them to gpt-4o-mini "
                    "saves an estimated $1,640/mo."
                ),
                "estimated_savings_usd": 1640.00,
                "effort": "medium",
            },
            {
                "title": "Turn on prompt caching",
                "detail": (
                    "Prompt cache hit rate is 8%. Caching system prompts and few-shot "
                    "examples could recover an estimated $740/mo at current volume."
                ),
                "estimated_savings_usd": 740.00,
                "effort": "low",
            },
        ],
        "sources": {"openai": "ok", "anthropic": "ok", "bedrock": "ok"},
        "summary": (
            "AI/LLM spend this month: $4,120 (32% of total cloud cost). gpt-4o drives "
            "71% of it. Two changes recover ~$2,380/mo: route short-context calls to "
            "gpt-4o-mini ($1,640) and enable prompt caching ($740)."
        ),
    }


def cost_drivers() -> dict[str, Any]:
    """Demo 'why did the bill change' answer, consistent with the $12,847
    acme-production story (up 23.4% vs the prior month)."""
    return {
        "period": f"{_MONTH_START} to {_TODAY}",
        "comparison_period": "prior 30 days",
        "total_current_usd": 12847.22,
        "total_previous_usd": 10410.00,
        "net_change_usd": 2437.22,
        "net_change_pct": 23.4,
        "top_increases": [
            {"key": "AWS Data Transfer", "current": 1890.33, "previous": 1180.00, "delta": 710.33, "delta_pct": 60.2, "direction": "increase"},
            {"key": "Amazon EC2", "current": 7240.10, "previous": 6100.00, "delta": 1140.10, "delta_pct": 18.7, "direction": "increase"},
            {"key": "Amazon RDS", "current": 2100.44, "previous": 1720.00, "delta": 380.44, "delta_pct": 22.1, "direction": "increase"},
        ],
        "top_decreases": [
            {"key": "Amazon S3", "current": 822.15, "previous": 917.00, "delta": -94.85, "delta_pct": -10.3, "direction": "decrease"},
        ],
        "all_drivers": [],
        "summary": (
            "Costs rose $2,437 (+23.4%) vs the prior 30 days. The standout is "
            "Data Transfer, up 60% ($710), which usually means a new cross-AZ or "
            "egress path went live. EC2 added $1,140 from on-demand growth, and RDS "
            "$380. S3 fell $95. Start with the Data Transfer jump: it is the fastest "
            "to trace to a single change."
        ),
    }


def bedrock_split() -> dict[str, Any]:
    """Demo Bedrock input/output/cache split, consistent with the ~$330 Bedrock
    line in llm_costs(): input-heavy and uncached, which is the signature
    caching finding. Lets optimize_ai_spend fire the prompt-caching lever with
    no credentials."""
    return {
        "input_cost": 294.0,       # ~89% of the $330 Bedrock bill
        "output_cost": 36.0,
        "cache_read_cost": 0.0,
        "cache_write_cost": 0.0,
        "input_share_pct": 89.0,
        "caching_active": False,
    }


def ai_engineering_report() -> dict:
    """Demo: what AI shipped this month, by model, joined to AI spend."""
    return {
        "configured": True,
        "window_days": 30,
        "total_pr_count": 23,
        "ai_pr_count": 18,
        "human_pr_count": 5,
        "ai_share_pct": 78.3,
        "total_llm_spend_usd": 1240.0,
        "by_label": {
            "Claude Opus 4.8": {
                "label": "Claude Opus 4.8", "pr_count": 10, "high": 3, "medium": 5, "low": 2,
                "lines_changed": 4200, "llm_spend_usd": 608.0, "spend_share_pct": 49.0,
                "cost_per_pr_usd": 60.8,
                "examples": [
                    {"title": "Parallelize the cost audit", "magnitude": "high", "lines": 540, "url": "", "repo": "acme/infra"},
                    {"title": "Add the managed-AI credit ledger", "magnitude": "high", "lines": 430, "url": "", "repo": "acme/platform"},
                ],
            },
            "Claude Sonnet 4.6": {
                "label": "Claude Sonnet 4.6", "pr_count": 6, "high": 0, "medium": 4, "low": 2,
                "lines_changed": 910, "llm_spend_usd": 372.0, "spend_share_pct": 30.0,
                "cost_per_pr_usd": 62.0,
                "examples": [
                    {"title": "Tighten the onboarding copy", "magnitude": "medium", "lines": 120, "url": "", "repo": "acme/web"},
                ],
            },
            "OpenAI Codex": {
                "label": "OpenAI Codex", "pr_count": 2, "high": 0, "medium": 1, "low": 1,
                "lines_changed": 180, "llm_spend_usd": None, "spend_share_pct": None,
                "cost_per_pr_usd": None, "examples": [],
            },
            "Human": {
                "label": "Human", "pr_count": 5, "high": 1, "medium": 2, "low": 2,
                "lines_changed": 1500, "examples": [],
            },
        },
        "_demo_mode": True,
    }


DEMO_RESPONSES: dict[str, Any] = {
    "get_cost_summary":             cost_summary,
    "get_anomalies":                anomalies,
    "get_rightsizing_recommendations": rightsizing,
    "get_kubernetes_costs":         kubernetes_costs,
    "get_cluster_efficiency":       cluster_efficiency,
    "get_tag_cost_breakdown_cur":   cost_summary_cur,
    "get_llm_costs":                llm_costs,
    "get_llm_cost_by_model":        llm_costs,
    "explain_recent_cost_drivers":  cost_drivers,
    "get_ai_engineering_report":    ai_engineering_report,
}


def get_demo_response(tool_name: str) -> dict[str, Any] | None:
    """
    Return a demo response for the given tool name, or None if not available.
    Call this at the top of each MCP tool when FINOPS_DEMO_MODE=1.
    """
    fn = DEMO_RESPONSES.get(tool_name)
    if fn is None:
        return None
    result = fn()
    result["_demo_mode"] = True
    return result
