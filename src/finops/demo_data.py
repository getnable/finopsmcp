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
from datetime import date, datetime, timedelta, timezone
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
    if not found:
        # A named-profile or role connect (e.g. via connect_aws or the wizard's
        # profile path) writes accounts.yaml but sets no credential env var, so a
        # configured account is also a real connection. Demo never writes this
        # file, so any entry here is a genuine user connect.
        try:
            from .accounts import list_accounts

            found = bool(list_accounts())
        except Exception:
            pass
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
    # Mirrors the real rightsizing_summary shape: every rec carries a genuine-
    # savings verdict, and savings are priced on the customer's real rates (here a
    # 22% effective discount measured from CUR), not list price. The demo shows the
    # judgment doing its job: $889/mo of raw "underutilized" collapses to $218/mo of
    # genuine savings once burst, memory-bound, and the real rate are accounted for.
    return {
        "total_instances_flagged": 3,
        "total_monthly_savings":   889.00,
        "total_annual_savings":    10668.00,
        "genuine_monthly_savings": 218.40,
        "genuine_annual_savings":  2620.80,
        "verdicts": {"genuine_savings": 1, "review": 1, "likely_false_positive": 1},
        "source": {
            "compute_optimizer": 2,
            "cloudwatch_fallback": 1,
            "note": "Compute Optimizer recommendations include CPU, memory, network, and disk. "
                    "CloudWatch fallback is CPU-only.",
        },
        "savings_by_resource_type": {"ec2": 342.00, "rds": 547.00},
        "recommendations": [
            {
                "instance_id":   "i-0a1b2c3d4e5f67890",
                "name":          "data-platform-worker-01",
                "region":        "us-east-1",
                "resource_type": "ec2",
                "source":        "compute_optimizer",
                "current_type":  "m5.4xlarge",
                "recommended_type": "m5.2xlarge",
                "avg_cpu_pct":   5.8,
                "max_cpu_pct":   None,
                "avg_mem_pct":   22.1,
                "monthly_savings":          280.00,
                "adjusted_monthly_savings": 218.40,
                "verdict":       "genuine_savings",
                "score":         90,
                "why":           "sustained over-provisioning (CPU+mem+net+disk); "
                                 "real saving ≈$218/mo on your effective rate, ~22% below list (cur_athena)",
                "action":        "Resize needs a stop/start (brief downtime); fully reversible.",
            },
            {
                "instance_id":   "db-prod-analytics-01",
                "name":          "prod-analytics",
                "region":        "us-east-1",
                "resource_type": "rds",
                "source":        "compute_optimizer",
                "current_type":  "db.r5.2xlarge",
                "recommended_type": "db.r5.xlarge",
                "avg_cpu_pct":   8.1,
                "max_cpu_pct":   None,
                "avg_mem_pct":   78.4,
                "monthly_savings":          547.00,
                "adjusted_monthly_savings": 426.66,
                "verdict":       "review",
                "score":         42,
                "why":           "over-provisioned; memory at 78%, likely memory-bound; "
                                 "real saving ≈$427/mo on your effective rate, ~22% below list (cur_athena)",
                "action":        "Modify the instance class in a maintenance window; reversible, brief failover.",
            },
            {
                "instance_id":   "i-07f3c9a1b2d4e6f80",
                "name":          "api-gateway-02",
                "region":        "us-west-2",
                "resource_type": "ec2",
                "source":        "cloudwatch_fallback",
                "current_type":  "c5.xlarge",
                "recommended_type": "c5.large",
                "avg_cpu_pct":   9.2,
                "max_cpu_pct":   82.0,
                "avg_mem_pct":   None,
                "monthly_savings":          62.00,
                "adjusted_monthly_savings": 48.36,
                "verdict":       "likely_false_positive",
                "score":         5,
                "why":           "CPU-only avg 9%; peaks to 82% CPU, needs headroom; "
                                 "real saving ≈$48/mo on your effective rate, ~22% below list (cur_athena)",
                "action":        "Resize needs a stop/start (brief downtime); fully reversible.",
            },
        ],
        "pricing_basis": {
            "basis":      {"effective_rate": 3},
            "confidence": {"high": 3},
            "effective_discount_pct": 22.0,
            "rate_source": "cur_athena",
        },
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


# Per-provider monthly service inventory for the demo dashboard. Each entry:
# monthly cost, live resource count, and the month-over-month delta. AWS is the
# familiar acme-production story ($12,847, +23.4%); Azure and GCP are smaller,
# realistic multi-cloud footprints so the provider toggle and the active-services
# table have real, distinct data to show. Selecting AWS reproduces the classic
# single-cloud demo numbers exactly.
_PROVIDER_SERVICES: dict[str, list[dict[str, Any]]] = {
    "aws": [
        {"service": "Amazon EC2",         "amount": 7240.10, "resources": 42, "delta_pct": 18.7},
        {"service": "Amazon RDS",         "amount": 2100.44, "resources": 8,  "delta_pct": 22.1},
        {"service": "AWS Data Transfer",  "amount": 1890.33, "resources": 0,  "delta_pct": 60.2},
        {"service": "Amazon S3",          "amount": 822.15,  "resources": 31, "delta_pct": -10.3},
        {"service": "Amazon CloudWatch",  "amount": 412.88,  "resources": 0,  "delta_pct": 4.1},
        {"service": "AWS Lambda",         "amount": 201.44,  "resources": 74, "delta_pct": 9.0},
        {"service": "Amazon EKS",         "amount": 180.00,  "resources": 2,  "delta_pct": 1.2},
    ],
    "azure": [
        {"service": "Virtual Machines",   "amount": 1980.00, "resources": 16, "delta_pct": 12.4},
        {"service": "Azure SQL Database", "amount": 640.00,  "resources": 5,  "delta_pct": 6.8},
        {"service": "Blob Storage",       "amount": 305.00,  "resources": 12, "delta_pct": -3.1},
        {"service": "App Service",        "amount": 240.00,  "resources": 9,  "delta_pct": 14.0},
        {"service": "Azure Monitor",      "amount": 110.00,  "resources": 0,  "delta_pct": 2.0},
    ],
    "gcp": [
        {"service": "Compute Engine",     "amount": 1120.00, "resources": 22, "delta_pct": 9.5},
        {"service": "BigQuery",           "amount": 640.00,  "resources": 0,  "delta_pct": 31.7},
        {"service": "GKE",                "amount": 410.00,  "resources": 3,  "delta_pct": 5.4},
        {"service": "Cloud Storage",      "amount": 250.00,  "resources": 18, "delta_pct": -2.2},
        {"service": "Cloud Networking",   "amount": 140.00,  "resources": 0,  "delta_pct": 7.1},
    ],
}
_DEMO_PROVIDERS = ["aws", "azure", "gcp"]

# Per-provider open opportunities, priced on the customer's real rate.
_PROVIDER_OPPS: dict[str, list[dict[str, Any]]] = {
    "aws": [
        {"description": "Rightsize data-platform-worker-01 (m5.4xlarge to m5.2xlarge). "
                        "Genuine after burst + memory check, priced on your ~22% effective discount.",
         "monthly_saving": 218.40, "resource": "i-0a1b2c3d4e5f67890", "provider": "aws"},
        {"description": "Buy a 1-year compute Savings Plan at your steady EC2 baseline.",
         "monthly_saving": 412.00, "resource": "compute-savings-plan", "provider": "aws"},
        {"description": "Delete 4 unattached gp2 EBS volumes, idle 30 to 90 days.",
         "monthly_saving": 96.20, "resource": "vol-0f3d5a2c9b1e40718", "provider": "aws"},
        {"description": "Move 2.1 TB of infrequently read S3 to Intelligent-Tiering.",
         "monthly_saving": 61.80, "resource": "s3://acme-data-platform-logs", "provider": "aws"},
    ],
    "azure": [
        {"description": "Buy a 1-year Azure Reserved VM Instance for the steady D-series baseline.",
         "monthly_saving": 176.00, "resource": "vm-reservation-dseries", "provider": "azure"},
        {"description": "Downsize 2 over-provisioned App Service plans (P2v3 to P1v3).",
         "monthly_saving": 88.00, "resource": "asp-web-frontend", "provider": "azure"},
    ],
    "gcp": [
        {"description": "Apply a committed-use discount to the stable Compute Engine baseline.",
         "monthly_saving": 132.00, "resource": "cud-compute-n2", "provider": "gcp"},
        {"description": "Set a 90-day lifecycle rule on 1.4 TB of cold Cloud Storage.",
         "monthly_saving": 44.00, "resource": "gs://acme-analytics-archive", "provider": "gcp"},
    ],
}


# Demo accounts and regions, so Top Accounts and Spend by Region are real panels.
_DEMO_ACCOUNTS = [
    {"name": "Production",     "id": "111111111111", "share": 0.42},
    {"name": "Data Platform",  "id": "222222222222", "share": 0.24},
    {"name": "Staging",        "id": "333333333333", "share": 0.14},
    {"name": "Shared Services","id": "444444444444", "share": 0.11},
    {"name": "Sandbox",        "id": "555555555555", "share": 0.09},
]
_DEMO_REGIONS = [
    {"region": "us-east-1",      "code": "US", "label": "N. Virginia",  "share": 0.38},
    {"region": "us-west-2",      "code": "US", "label": "Oregon",       "share": 0.19},
    {"region": "eu-west-1",      "code": "IE", "label": "Ireland",      "share": 0.16},
    {"region": "eu-central-1",   "code": "DE", "label": "Frankfurt",    "share": 0.11},
    {"region": "ap-southeast-1", "code": "SG", "label": "Singapore",    "share": 0.09},
    {"region": "ap-northeast-1", "code": "JP", "label": "Tokyo",        "share": 0.07},
]


def _daily_series(days: int, provs: list[str]) -> list[dict[str, Any]]:
    """A believable per-provider daily spend series over the window. Deterministic
    (seeded by day index) so it does not jump on every refresh, with a gentle
    upward drift and weekly ripple."""
    import math
    out = []
    base = {p: sum(s["amount"] for s in _PROVIDER_SERVICES[p]) / 30.0 for p in provs}
    today = date.today()
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        pos = (days - i) / max(days, 1)              # 0..1 across the window
        drift = 0.85 + 0.30 * pos                    # ramps up over the window
        ripple = 1.0 + 0.06 * math.sin(i / 7.0 * math.tau)  # weekly wobble
        row: dict[str, Any] = {"date": d.isoformat()}
        for p in provs:
            row[p] = round(base[p] * drift * ripple, 2)
        out.append(row)
    return out


def dashboard_data(days: int = 30, provider: str = "all") -> dict[str, Any]:
    """Full payload for the `finops serve` dashboard in demo mode, in the exact
    shape `_fetch_dashboard_data` returns. Provider- and range-aware: selecting a
    provider filters every figure, and the range scales the windowed views. AWS
    reproduces the classic acme-production story ($12,847 this month, +23.4% vs
    last, a Data Transfer spike, a genuine rightsizing find); Azure and GCP are
    smaller real footprints, so the provider toggle and active-services table
    show distinct data. No account needed.
    """
    provider = (provider or "all").lower()
    provs = _DEMO_PROVIDERS if provider == "all" else [provider]
    provs = [p for p in provs if p in _PROVIDER_SERVICES] or ["aws"]

    # Window scaling: figures for the selected lookback. 30d is the reference
    # month; 7d shows ~a quarter of it, 90d ~three months. MTD/projection stay
    # month-anchored (they are calendar figures, not lookback figures).
    win_factor = max(days, 1) / 30.0

    # Flatten selected providers' services into the active-services inventory.
    active_services: list[dict[str, Any]] = []
    for p in provs:
        for s in _PROVIDER_SERVICES[p]:
            active_services.append({
                "service": s["service"],
                "provider": p,
                "resources": s["resources"],
                "amount": round(s["amount"] * win_factor, 2),
                "delta_pct": s["delta_pct"],
            })
    active_services.sort(key=lambda x: -x["amount"])
    window_total = sum(s["amount"] for s in active_services) or 1.0
    for s in active_services:
        s["pct"] = round(s["amount"] / window_total * 100, 1)

    top_services = [
        {"service": s["service"], "amount": s["amount"], "pct": s["pct"]}
        for s in active_services[:8]
    ]

    # Month figures: sum the selected providers' full monthly service cost.
    month_total = round(sum(s["amount"] for p in provs for s in _PROVIDER_SERVICES[p]), 2)
    delta_pct = 23.4 if "aws" in provs else round(sum(
        s["amount"] * s["delta_pct"] for p in provs for s in _PROVIDER_SERVICES[p]
    ) / max(month_total, 1), 1)
    last_month = round(month_total / (1 + delta_pct / 100), 2)
    projected = round(month_total * 1.088, 2)

    recent_opportunities = [o for p in provs for o in _PROVIDER_OPPS.get(p, [])]
    recent_opportunities.sort(key=lambda o: -o["monthly_saving"])
    opp_total = round(sum(o["monthly_saving"] for o in recent_opportunities), 2)

    recent_savings = [
        {"description": "Turned off 6 non-prod RDS instances on a nights/weekends schedule.",
         "monthly_saving": 340.00, "resource": "nonprod-scheduler", "provider": "aws"},
    ] if "aws" in provs else []

    # Score nudges a little by footprint so switching providers visibly moves it.
    score = 74.0 if "aws" in provs else (81.0 if provs == ["azure"] else 69.0)
    grade = "B" if score >= 70 else "C"

    _today = date.today()
    m1 = (_today.replace(day=1) - timedelta(days=1)).replace(day=1)      # last month
    m2 = (m1 - timedelta(days=1)).replace(day=1)                          # two months ago
    trend = [
        {"month": m2.strftime("%B"), "actual": round(last_month * 0.90, 2), "projected": None},
        {"month": m1.strftime("%B"), "actual": last_month, "projected": last_month},
        {"month": f"{_today.strftime('%B')} (projected)", "actual": None, "projected": projected},
    ]

    # Windowed total (what the range actually spans) and the daily provider series.
    window_total_spend = round(window_total, 2)
    daily = _daily_series(days, provs)
    # Headline sparklines: last ~12 windowed daily totals, smoothed.
    def _spark(scale: float) -> list[float]:
        tail = daily[-12:] if len(daily) >= 12 else daily
        return [round(sum(v for k, v in r.items() if k != "date") * scale, 2) for r in tail]

    # Structured recommendations for the table (impact / effort / accounts / saving).
    _rec_meta = {
        "aws": [("High", "Low", 3), ("High", "Medium", 1), ("Medium", "Low", 2), ("Low", "Low", 1)],
        "azure": [("High", "Medium", 1), ("Medium", "Low", 1)],
        "gcp": [("Medium", "Low", 2), ("Low", "Low", 1)],
    }
    recommendations = []
    for o in recent_opportunities:
        p = o.get("provider", "aws")
        meta = _rec_meta.get(p, [("Medium", "Low", 1)])
        m = meta[len(recommendations) % len(meta)]
        recommendations.append({
            "title": o["description"].rstrip("."),
            "subtitle": o.get("resource", ""),
            "provider": p, "impact": m[0], "effort": m[1], "accounts": m[2],
            "monthly_saving": o["monthly_saving"], "resource": o.get("resource", ""),
        })

    # AI Insights rail: the top three savings, phrased as insights.
    ai_insights = [{
        "title": r["title"], "body": r["subtitle"],
        "monthly_saving": r["monthly_saving"], "provider": r["provider"],
    } for r in recommendations[:3]]

    # Top accounts and regions, scaled to the windowed total.
    top_accounts = [{
        "name": a["name"], "id": a["id"],
        "amount": round(window_total_spend * a["share"], 2),
    } for a in _DEMO_ACCOUNTS]
    spend_by_region = [{
        "region": r["region"], "code": r["code"], "label": r["label"],
        "amount": round(window_total_spend * r["share"], 2),
    } for r in _DEMO_REGIONS]

    # Budgets & alerts.
    budgets = [
        {"name": "AWS Monthly Budget",   "provider": "aws",   "used": 3200000, "limit": 3600000},
        {"name": "GCP Monthly Budget",   "provider": "gcp",   "used": 420000,  "limit": 500000},
        {"name": "Azure Monthly Budget", "provider": "azure", "used": 780000,  "limit": 920000},
    ]
    for b in budgets:
        b["pct"] = round(b["used"] / b["limit"] * 100, 1)
        b["status"] = "over" if b["pct"] >= 100 else ("warn" if b["pct"] >= 85 else "ok")
    alerts = [
        {"kind": "warn",  "title": "Azure budget alert", "body": "85% of budget used"},
        {"kind": "info",  "title": "Forecast alert",     "body": f"{provs[0].upper() if provs else 'AWS'} forecast tracking above run rate"},
    ]

    # Executive KPI band (tier-1): unit economics + posture, the board-slide row.
    exec_kpis = [
        {"label": "Cost per customer", "value": "$6.98", "delta_pct": -8.0, "good_down": True, "sub": "1,840 active customers"},
        {"label": "Infra % of revenue", "value": "7.8%", "delta_pct": -1.2, "good_down": True, "sub": "$164K MRR"},
        {"label": "Effective savings rate", "value": "22%", "delta_pct": 3.0, "good_down": False, "sub": "vs on-demand list"},
        {"label": "Commitment coverage", "value": "68%", "delta_pct": 5.0, "good_down": False, "sub": "target 80%"},
        {"label": "Cost per 1M tokens", "value": "$3.10", "delta_pct": -14.0, "good_down": True, "sub": "blended across models"},
    ]
    # AI efficiency panel: the wedge no incumbent shows.
    ai_efficiency = {
        "ai_pct_of_spend": 22.0,
        "ai_spend": round(month_total * 0.22, 2),
        "metrics": [
            {"label": "Cost / 1M tokens", "value": "$3.10", "delta_pct": -14.0, "good_down": True},
            {"label": "Cost / AI-authored PR", "value": "$4.20", "delta_pct": 6.0, "good_down": True},
            {"label": "GPU utilization", "value": "44%", "delta_pct": 2.0, "good_down": False, "warn": True},
            {"label": "Cache hit rate", "value": "0%", "delta_pct": 0.0, "good_down": False, "warn": True},
        ],
        "callout": "GPU utilization is 44% and prompt caching is off. About $740/mo is recoverable by right-sizing the GPU pool and turning on caching.",
    }

    # "What changed since you last looked": the always-on loop as a glance.
    whats_changed = {
        "since": "Monday",
        "items": [
            {"kind": "up",    "text": "Data Transfer up $710 (60%)", "prompt": "Why did data transfer spend jump 60% this week?"},
            {"kind": "alert", "text": "New anomaly on `Amazon EC2`", "prompt": "Explain this week's EC2 anomaly and what caused it."},
            {"kind": "warn",  "text": "AWS budget crossed 85%",      "prompt": "Show our AWS budget status and what's driving it toward the cap."},
            {"kind": "good",  "text": "$340/mo saved, non-prod schedule", "prompt": "Show the savings we've realized in the last week."},
        ],
    }

    # Forecast vs budget with a confidence band.
    def _money(n: float) -> str:
        if n >= 1e6: return f"${n/1e6:.1f}M"
        if n >= 1e3: return f"${n/1e3:.1f}k"
        return f"${n:,.0f}"
    hist = [round(sum(v for k, v in row.items() if k != "date"), 2) for row in daily]
    last = hist[-1] if hist else (month_total / 30.0)
    forecast = [round(last * (1.0 + 0.012 * i), 2) for i in range(1, 13)]
    budget = round(month_total * 1.14, 2)
    proj_end = round(month_total * 1.088, 2)
    vs_budget = round((proj_end - budget) / budget * 100, 1)
    forecast_panel = {
        "history": hist,
        "forecast": forecast,
        "band_pct": 0.08,
        "budget": budget,
        "projected_end": proj_end,
        "vs_budget_pct": vs_budget,
        "note": f"Projected to finish the month at {_money(proj_end)}, "
                f"{abs(vs_budget)}% {'over' if proj_end > budget else 'under'} "
                f"the {_money(budget)} budget.",
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account_id": _ACCOUNT_ID,
        "user": {"name": "Chandan B.", "role": "Admin", "email": "chandan@acme.io"},
        "exec_kpis": exec_kpis,
        "ai_efficiency": ai_efficiency,
        "whats_changed": whats_changed,
        "forecast_panel": forecast_panel,
        "total_spend_mtd": month_total,
        "total_spend_window": window_total_spend,
        "total_spend_last_month": last_month,
        "projected_month_total": projected,
        "forecast_delta_pct": -4.7,
        "delta_pct": delta_pct,
        "finops_grade": grade,
        "finops_score": score,
        "sparklines": {
            "spend": _spark(1.0), "mtd": _spark(0.62),
            "forecast": _spark(1.09), "savings": _spark(0.065),
        },
        "top_services": top_services,
        "active_services": active_services,
        "daily_series": daily,
        "series_providers": provs,
        "top_accounts": top_accounts,
        "spend_by_region": spend_by_region,
        "recommendations_table": recommendations,
        "ai_insights": ai_insights,
        "budgets": budgets,
        "alerts": alerts,
        "window_days": days,
        "provider": provider,
        "opportunities_count": len(recent_opportunities),
        "opportunities_total_saving": opp_total,
        "savings_achieved_mtd": round(sum(s["monthly_saving"] for s in recent_savings), 2),
        "anomalies_open": 2 if "aws" in provs else 1,
        "budget_pct_used": 68.0,
        "recent_opportunities": recent_opportunities,
        "suppressed_opportunities": [
            {"description": "RDS prod-analytics flagged underutilized, but memory sits at 78%. "
                            "Held back: rightsizing it risks a memory-bound stall, not genuine savings.",
             "monthly_saving": 0.0, "resource": "db-prod-analytics-01", "provider": "aws"},
        ] if "aws" in provs else [],
        "learning_active": True,
        "recent_savings": recent_savings,
        "error": None,
        "connected_providers": _DEMO_PROVIDERS,
        "trend": trend,
        "scorecard": {
            "overall_grade": grade,
            "overall_score": score,
            "dimensions": [
                {"name": "Commitment coverage", "grade": "B", "score": 72,
                 "detail": "68% of steady compute on commitments; room for one more 1-yr plan."},
                {"name": "Rightsizing", "grade": "C", "score": 61,
                 "detail": "3 instances over-provisioned; 1 is a genuine, low-risk resize."},
                {"name": "Idle & waste", "grade": "B", "score": 78,
                 "detail": "Unattached volumes and always-on non-prod databases."},
                {"name": "Storage tiering", "grade": "A", "score": 90,
                 "detail": "Most object storage already lifecycle-managed."},
            ],
        },
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
