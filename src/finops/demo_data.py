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

_ACCOUNT_ID   = "481516234203"
_ACCOUNT_NAME = "streamco-production"
_REGION       = "us-east-1"

_TODAY = date.today()
_MONTH_START = _TODAY.replace(day=1).isoformat()
_YESTERDAY   = (_TODAY - timedelta(days=1)).isoformat()


# ── Tool response stubs ───────────────────────────────────────────────────────

def cost_summary() -> dict[str, Any]:
    return {
        "period": f"{_MONTH_START} to {_YESTERDAY}",
        "total_usd": 286402.34,
        "vs_last_month_pct": 19.2,
        "by_service": {
            "Amazon CloudFront":            84200.10,
            "Amazon EC2":                   52400.00,
            "AWS Data Transfer":            46800.33,
            "Amazon S3":                    38600.15,
            "AWS Elemental MediaConvert":   24900.00,
            "AWS Elemental MediaLive":      18300.00,
            "Amazon RDS":                   12700.44,
            "Amazon CloudWatch":             5400.88,
            "AWS Lambda":                    3100.44,
        },
        "account_id":   _ACCOUNT_ID,
        "account_name": _ACCOUNT_NAME,
        "summary": (
            "Total AWS spend this month: $286,402 (+19% vs last month). "
            "CloudFront is the top driver at $84,200, up $18,400 as streaming "
            "egress rose after the new season dropped."
        ),
    }


def anomalies() -> dict[str, Any]:
    return {
        "anomalies": [
            {
                "id":          "anom-001",
                "service":     "Amazon CloudFront",
                "account_id":  _ACCOUNT_ID,
                "severity":    "high",
                "detected_at": f"{(_TODAY - timedelta(days=3)).isoformat()}T14:22:00Z",
                "description": (
                    "CloudFront egress spiked $18,400 (+28%) after the new season "
                    "dropped Friday. Delivery to SmartCast devices in us-east-1 and "
                    "eu-west-1 drove the increase."
                ),
                "daily_cost_before": 2190.00,
                "daily_cost_after":  3050.00,
                "projected_monthly_impact": 18400.00,
                "resource_ids":  ["E2QK8S1TREAM01"],
                "tags":          {"team": "streaming-delivery", "env": "production"},
            },
            {
                "id":          "anom-002",
                "service":     "AWS Data Transfer",
                "account_id":  _ACCOUNT_ID,
                "severity":    "medium",
                "detected_at": f"{(_TODAY - timedelta(days=2)).isoformat()}T09:15:00Z",
                "description": (
                    "Origin-to-edge data transfer rose $13,600 (+41%), tracking the "
                    "CloudFront egress jump from the season launch."
                ),
                "daily_cost_before":         1105.00,
                "daily_cost_after":          1560.00,
                "projected_monthly_impact":  13600.00,
            },
        ],
        "total_anomalies": 2,
        "high_severity":   1,
        "summary": "2 cost anomalies detected. The season launch drove CloudFront and data-transfer egress up ~$32,000/mo combined.",
    }


def rightsizing() -> dict[str, Any]:
    # Mirrors the real rightsizing_summary shape: every rec carries a genuine-
    # savings verdict, and savings are priced on the customer's real rates (here a
    # 22% effective discount measured from CUR), not list price. The demo shows the
    # judgment doing its job: $889/mo of raw "underutilized" collapses to $218/mo of
    # genuine savings once burst, memory-bound, and the real rate are accounted for.
    return {
        "total_instances_flagged": 3,
        "total_monthly_savings":   6180.00,
        "total_annual_savings":    74160.00,
        "genuine_monthly_savings": 2140.00,
        "genuine_annual_savings":  25680.00,
        "verdicts": {"genuine_savings": 1, "review": 1, "likely_false_positive": 1},
        "source": {
            "compute_optimizer": 2,
            "cloudwatch_fallback": 1,
            "note": "Compute Optimizer recommendations include CPU, memory, network, and disk. "
                    "CloudWatch fallback is CPU-only.",
        },
        "savings_by_resource_type": {"ec2": 4380.00, "rds": 1800.00},
        "recommendations": [
            {
                "instance_id":   "i-0a1b2c3d4e5f67890",
                "name":          "vod-encoder-07",
                "region":        "us-east-1",
                "resource_type": "ec2",
                "source":        "compute_optimizer",
                "current_type":  "g5.4xlarge",
                "recommended_type": "g5.2xlarge",
                "avg_cpu_pct":   11.2,
                "max_cpu_pct":   None,
                "avg_mem_pct":   24.0,
                "monthly_savings":          3200.00,
                "adjusted_monthly_savings": 2140.00,
                "verdict":       "genuine_savings",
                "score":         88,
                "why":           "GPU encoders sit near-idle off-peak (11% avg util); "
                                 "real saving ≈$2,140/mo on your effective rate, ~26% below list (cur_athena)",
                "action":        "Move off-peak encodes to a scheduled g5.2xlarge pool; fully reversible.",
            },
            {
                "instance_id":   "db-metadata-catalog-01",
                "name":          "metadata-catalog-01",
                "region":        "us-east-1",
                "resource_type": "rds",
                "source":        "compute_optimizer",
                "current_type":  "db.r6g.4xlarge",
                "recommended_type": "db.r6g.2xlarge",
                "avg_cpu_pct":   9.4,
                "max_cpu_pct":   None,
                "avg_mem_pct":   81.0,
                "monthly_savings":          1800.00,
                "adjusted_monthly_savings": 1420.00,
                "verdict":       "review",
                "score":         40,
                "why":           "over-provisioned; memory at 81%, likely memory-bound; "
                                 "real saving ≈$1,420/mo on your effective rate, ~26% below list (cur_athena)",
                "action":        "Modify the instance class in a maintenance window; reversible, brief failover.",
            },
            {
                "instance_id":   "i-07f3c9a1b2d4e6f80",
                "name":          "playback-api-04",
                "region":        "us-west-2",
                "resource_type": "ec2",
                "source":        "cloudwatch_fallback",
                "current_type":  "c6i.2xlarge",
                "recommended_type": "c6i.xlarge",
                "avg_cpu_pct":   12.0,
                "max_cpu_pct":   84.0,
                "avg_mem_pct":   None,
                "monthly_savings":          1180.00,
                "adjusted_monthly_savings": 780.00,
                "verdict":       "likely_false_positive",
                "score":         6,
                "why":           "CPU-only avg 12%; peaks to 84% at prime-time, needs headroom; "
                                 "real saving ≈$780/mo on your effective rate, ~26% below list (cur_athena)",
                "action":        "Resize needs a stop/start (brief downtime); fully reversible.",
            },
        ],
        "pricing_basis": {
            "basis":      {"effective_rate": 3},
            "confidence": {"high": 3},
            "effective_discount_pct": 26.0,
            "rate_source": "cur_athena",
        },
    }


def kubernetes_costs() -> dict[str, Any]:
    return {
        "cluster":               "prod-eks-streaming",
        "provider":              "aws",
        "node_count":            24,
        "pod_count":             186,
        "total_monthly_cost_usd": 78000.00,
        "wasted_monthly_cost_usd": 14200.00,
        "waste_pct":             18.2,
        "cpu_efficiency_pct":    41.0,
        "mem_efficiency_pct":    58.4,
        "cost_by_namespace": {
            "recommendations": 31200.00,
            "ad-decisioning":  18400.00,
            "playback-api":    12600.00,
            "search":           8100.00,
            "platform":         4900.00,
            "kube-system":      2800.00,
        },
        "top_workloads": [
            {
                "namespace":          "recommendations",
                "workload":           "Deployment/ranker-inference",
                "pods":               18,
                "monthly_cost_usd":   22400.00,
                "wasted_usd":         8600.00,
                "cpu_efficiency_pct": 29.0,
                "mem_efficiency_pct": 51.0,
            },
            {
                "namespace":          "ad-decisioning",
                "workload":           "Deployment/bid-service",
                "pods":               12,
                "monthly_cost_usd":   11800.00,
                "wasted_usd":         1600.00,
                "cpu_efficiency_pct": 68.0,
                "mem_efficiency_pct": 63.0,
            },
        ],
        "idle_nodes":    ["ip-10-2-4-91.ec2.internal"],
        "idle_node_cost_usd": 3250.00,
        "summary": (
            "Cluster 'prod-eks-streaming' (AWS, 24 nodes): $78,000/month. "
            "~$14,200/month wasted — the recommendations ranker is 29% CPU efficient."
        ),
    }


def cluster_efficiency() -> dict[str, Any]:
    return {
        "cluster":  "prod-eks-streaming",
        "provider": "aws",
        "score":    55.8,
        "grade":    "C",
        "total_monthly_cost_usd":   78000.00,
        "wasted_monthly_cost_usd":  14200.00,
        "has_metrics_server": True,
        "dimensions": {
            "cpu_efficiency_pct":  41.0,
            "cpu_score":           12.3,
            "mem_efficiency_pct":  58.4,
            "mem_score":           17.5,
            "idle_node_pct":       8.3,
            "idle_node_score":     15.0,
            "waste_pct":           18.2,
            "waste_score":         11.0,
        },
        "headline": (
            "Cluster 'prod-eks-streaming' scores 56/100 (Grade C) — "
            "$78,000/mo total, $14,200/mo estimated waste. "
            "Moderate waste. Tackle the recommendations ranker and idle nodes first."
        ),
        "top_recommendations": [
            {
                "priority": "high",
                "category": "idle_nodes",
                "action":   "Drain ip-10-2-4-91 (idle node, <10% CPU/mem) — saving ~$3,250/mo.",
                "potential_savings_usd": 3250.0,
            },
            {
                "priority": "medium",
                "category": "rightsizing",
                "action":   "Rightsize recommendations/ranker-inference: CPU requests 96 cores, using 28 (29%) — reduce to 40 cores.",
                "potential_savings_usd": 5200.0,
            },
        ],
    }


def cost_summary_cur() -> dict[str, Any]:
    """Demo response for CUR/Athena line-item query."""
    return {
        "source":  "AWS Cost and Usage Report (Athena)",
        "period":  f"{_MONTH_START} to {_YESTERDAY}",
        "total_usd": 286402.34,
        "top_resources": [
            {
                "resource_id":   "E2QK8S1TREAM01",
                "resource_name": "smartcast-cdn",
                "service":       "Amazon CloudFront",
                "instance_type": "distribution",
                "region":        "global",
                "monthly_cost":  84200.10,
                "tags": {"team": "streaming-delivery", "env": "production"},
            },
            {
                "resource_id":   "i-0a1b2c3d4e5f67890",
                "resource_name": "vod-encoder-07",
                "service":       "Amazon EC2",
                "instance_type": "g5.4xlarge",
                "region":        "us-east-1",
                "monthly_cost":  4380.00,
                "tags": {"team": "content-platform", "env": "production"},
            },
        ],
        "by_tag_team": {
            "streaming-delivery": 131000.00,
            "content-platform":    58400.00,
            "ad-platform":         41200.00,
            "data-analytics":      24600.00,
            "recommendations":     18900.00,
            "untagged":            12302.34,
        },
        "untagged_pct": 4.3,
        "note": "Only 4% of spend is untagged — add 'team' tags on the remaining shared services to close the last allocation gap.",
    }


# ── Registry: maps tool name → demo response function ─────────────────────────

def llm_costs() -> dict[str, Any]:
    # AI/LLM spend for the streamco-production story: ~$66,000/mo, ~10% of the
    # ~$673k total bill. AI powers recommendations, content metadata auto-tagging,
    # search relevance, and moderation. gpt-4o leads. The wedge: show the money
    # answer AND the switch that recovers it, with zero creds.
    daily = []
    for i in range(13, -1, -1):
        d = (_TODAY - timedelta(days=i)).isoformat()
        # gentle upward drift, ~$2,150/day average
        daily.append({"date": d, "total_usd": round(2000 + (13 - i) * 24.0, 2)})
    return {
        "period": f"{_MONTH_START} to {_YESTERDAY}",
        "total_usd": 66000.00,
        "pct_of_total_cloud_spend": 9.8,
        "by_provider": {
            "openai":    38000.00,
            "anthropic": 22000.00,
            "bedrock":    6000.00,
        },
        "by_model": {
            "gpt-4o":                       24000.00,
            "claude-sonnet-4-5-20250929":   15000.00,
            "o3":                            9200.00,
            "claude-haiku-4-5-20251001":     7000.00,
            "bedrock/anthropic.claude":      6000.00,
            "gpt-4o-mini":                   4800.00,
        },
        "model_count": 6,
        "top_spenders": [
            {"model": "gpt-4o",            "provider": "openai",    "cost_usd": 24000.00},
            {"model": "claude-sonnet-4-5", "provider": "anthropic", "cost_usd": 15000.00},
            {"model": "o3",                "provider": "openai",    "cost_usd":  9200.00},
        ],
        "daily": daily,
        "recommendations": [
            {
                "title": "Route title auto-tagging off o3",
                "detail": (
                    "The nightly metadata auto-tagging job runs on o3 ($9,200/mo). On a "
                    "sampled eval, gpt-4o-mini matches its labels at ~1/15th the price. "
                    "Routing it saves an estimated $7,800/mo."
                ),
                "estimated_savings_usd": 7800.00,
                "effort": "medium",
            },
            {
                "title": "Cache the shared catalog context on gpt-4o",
                "detail": (
                    "Every recommendation call carries the same 8K-token catalog/system "
                    "context, billed uncached (6% cache hit rate). Prompt caching recovers "
                    "an estimated $5,400/mo at current volume."
                ),
                "estimated_savings_usd": 5400.00,
                "effort": "low",
            },
        ],
        "sources": {"openai": "ok", "anthropic": "ok", "bedrock": "ok"},
        "summary": (
            "AI/LLM spend this month: $66,000 (~10% of total cloud cost). gpt-4o drives "
            "36% of it. Two changes recover ~$13,200/mo: route metadata auto-tagging off "
            "o3 to gpt-4o-mini ($7,800) and cache the catalog context ($5,400)."
        ),
    }


def cost_drivers() -> dict[str, Any]:
    """Demo 'why did the bill change' answer, consistent with the $12,847
    acme-production story (up 23.4% vs the prior month)."""
    return {
        "period": f"{_MONTH_START} to {_TODAY}",
        "comparison_period": "prior 30 days",
        "total_current_usd": 286402.34,
        "total_previous_usd": 240300.00,
        "net_change_usd": 46102.34,
        "net_change_pct": 19.2,
        "top_increases": [
            {"key": "Amazon CloudFront", "current": 84200.10, "previous": 65800.00, "delta": 18400.10, "delta_pct": 28.0, "direction": "increase"},
            {"key": "AWS Data Transfer", "current": 46800.33, "previous": 33200.00, "delta": 13600.33, "delta_pct": 41.0, "direction": "increase"},
            {"key": "Amazon EC2", "current": 52400.00, "previous": 46800.00, "delta": 5600.00, "delta_pct": 12.0, "direction": "increase"},
            {"key": "AWS Elemental MediaLive", "current": 18300.00, "previous": 15000.00, "delta": 3300.00, "delta_pct": 22.0, "direction": "increase"},
        ],
        "top_decreases": [
            {"key": "Amazon S3", "current": 38600.15, "previous": 40800.00, "delta": -2199.85, "delta_pct": -5.4, "direction": "decrease"},
        ],
        "all_drivers": [],
        "summary": (
            "Costs rose $46,102 (+19.2%) vs the prior 30 days. The season launch is the "
            "story: CloudFront egress up $18,400 (28%) and data transfer up $13,600 (41%) "
            "as delivery to SmartCast devices spiked. EC2 added $5,600 and MediaLive $3,300 "
            "from more live channels. S3 fell $2,200 on Glacier tiering. Start with "
            "CloudFront: it is the fastest to trace to the launch."
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
        {"service": "Amazon CloudFront",          "amount": 84200.10, "resources": 0,   "delta_pct": 28.0},
        {"service": "Amazon EC2",                 "amount": 52400.00, "resources": 118, "delta_pct": 12.0},
        {"service": "AWS Data Transfer",          "amount": 46800.33, "resources": 0,   "delta_pct": 41.0},
        {"service": "Amazon S3",                  "amount": 38600.15, "resources": 64,  "delta_pct": -5.4},
        {"service": "AWS Elemental MediaConvert", "amount": 24900.00, "resources": 0,   "delta_pct": 15.0},
        {"service": "AWS Elemental MediaLive",    "amount": 18300.00, "resources": 12,  "delta_pct": 22.0},
        {"service": "Amazon RDS",                 "amount": 12700.44, "resources": 9,   "delta_pct": 8.0},
        {"service": "Amazon CloudWatch",          "amount": 5400.88,  "resources": 0,   "delta_pct": 4.0},
        {"service": "AWS Lambda",                 "amount": 3100.44,  "resources": 220, "delta_pct": 9.0},
    ],
    "gcp": [
        {"service": "BigQuery",           "amount": 61000.00, "resources": 0,  "delta_pct": 24.0},
        {"service": "Compute Engine",     "amount": 18400.00, "resources": 40, "delta_pct": 7.0},
        {"service": "GKE",                "amount": 7200.00,  "resources": 4,  "delta_pct": 5.0},
        {"service": "Cloud Storage",      "amount": 4100.00,  "resources": 52, "delta_pct": -2.0},
        {"service": "Cloud CDN",          "amount": 1300.00,  "resources": 0,  "delta_pct": 11.0},
    ],
    "azure": [
        {"service": "Virtual Machines",   "amount": 9800.00, "resources": 12, "delta_pct": 6.0},
        {"service": "Azure SQL Database", "amount": 2600.00, "resources": 4,  "delta_pct": 4.0},
        {"service": "Blob Storage",       "amount": 1400.00, "resources": 9,  "delta_pct": -1.0},
        {"service": "App Service",        "amount": 900.00,  "resources": 5,  "delta_pct": 3.0},
    ],
    # Kubernetes, read from kubeconfig (allocation view; namespaces as lines).
    "kubernetes": [
        {"service": "recommendations (ns)", "amount": 31200.00, "resources": 62, "delta_pct": 22.0},
        {"service": "ad-decisioning (ns)",  "amount": 18400.00, "resources": 44, "delta_pct": 9.0},
        {"service": "playback-api (ns)",    "amount": 12600.00, "resources": 28, "delta_pct": 4.0},
        {"service": "search (ns)",          "amount": 8100.00,  "resources": 20, "delta_pct": 3.0},
        {"service": "platform (ns)",        "amount": 4900.00,  "resources": 18, "delta_pct": 1.0},
        {"service": "kube-system (ns)",     "amount": 2800.00,  "resources": 14, "delta_pct": 0.5},
    ],
    # AI / LLM token spend, genuinely separate from cloud (the AI-native wedge).
    "openai": [
        {"service": "GPT-4o",             "amount": 24000.00, "resources": 0,  "delta_pct": 33.0},
        {"service": "o3",                 "amount": 9200.00,  "resources": 0,  "delta_pct": 61.0},
        {"service": "GPT-4o mini",        "amount": 4800.00,  "resources": 0,  "delta_pct": 12.0},
    ],
    "anthropic": [
        {"service": "Claude Sonnet",      "amount": 15000.00, "resources": 0,  "delta_pct": 28.0},
        {"service": "Claude Haiku",       "amount": 7000.00,  "resources": 0,  "delta_pct": 9.0},
    ],
    # SaaS + data platforms.
    "datadog": [
        {"service": "Infrastructure",     "amount": 16800.00, "resources": 0,  "delta_pct": 12.0},
        {"service": "Log Management",     "amount": 11400.00, "resources": 0,  "delta_pct": 19.0},
        {"service": "APM & Tracing",      "amount": 5800.00,  "resources": 0,  "delta_pct": 7.0},
    ],
    "snowflake": [
        {"service": "Compute (warehouses)","amount": 52000.00,"resources": 14, "delta_pct": 17.0},
        {"service": "Storage",            "amount": 9000.00,  "resources": 0,  "delta_pct": 4.0},
    ],
    "databricks": [
        {"service": "Jobs Compute",       "amount": 33000.00, "resources": 0,  "delta_pct": 14.0},
        {"service": "SQL Warehouses",     "amount": 14000.00, "resources": 0,  "delta_pct": 6.0},
    ],
}
_DEMO_PROVIDERS = ["aws", "gcp", "azure", "kubernetes", "openai", "anthropic",
                   "datadog", "snowflake", "databricks"]

# Per-provider open opportunities, priced on the customer's real rate.
_PROVIDER_OPPS: dict[str, list[dict[str, Any]]] = {
    "aws": [
        {"description": "Move the CloudFront egress baseline to a committed private-pricing tier. "
                        "Steady streaming volume qualifies; priced on your ~26% effective discount.",
         "monthly_saving": 4200.00, "resource": "cloudfront-commit", "provider": "aws"},
        {"description": "Buy a 1-year compute Savings Plan at your steady encoder + services baseline.",
         "monthly_saving": 5800.00, "resource": "compute-savings-plan", "provider": "aws"},
        {"description": "Move 640 TB of cold VOD masters to S3 Glacier Deep Archive.",
         "monthly_saving": 3100.00, "resource": "s3://streamco-vod-masters", "provider": "aws"},
        {"description": "Schedule the off-peak VOD encoder pool (g5.4xlarge to g5.2xlarge off-hours). "
                        "Genuine after burst + memory check.",
         "monthly_saving": 2140.00, "resource": "vod-encoder-07", "provider": "aws"},
    ],
    "gcp": [
        {"description": "Switch the viewership rollups to BigQuery flat-rate slots at this query volume.",
         "monthly_saving": 4600.00, "resource": "bq-flat-slots", "provider": "gcp"},
        {"description": "Set a 90-day lifecycle rule on 220 TB of cold Cloud Storage.",
         "monthly_saving": 1200.00, "resource": "gs://streamco-analytics-archive", "provider": "gcp"},
    ],
    "azure": [
        {"description": "Buy a 1-year Azure Reserved VM Instance for the steady D-series baseline.",
         "monthly_saving": 1100.00, "resource": "vm-reservation-dseries", "provider": "azure"},
    ],
    "openai": [
        {"description": "Route the nightly title auto-tagging job from o3 to GPT-4o mini. Same labels "
                        "on a sampled eval, ~1/15th the price.",
         "monthly_saving": 7800.00, "resource": "model-route-autotag", "provider": "openai"},
        {"description": "Cache the shared catalog context on GPT-4o: the same 8K-token context rides "
                        "every recommendation call, billed uncached. Prompt caching recovers most of it.",
         "monthly_saving": 5400.00, "resource": "prompt-cache-gpt4o", "provider": "openai"},
    ],
    "anthropic": [
        {"description": "Move content-moderation summaries from Claude Sonnet to Haiku where quality "
                        "holds on your eval set.",
         "monthly_saving": 3200.00, "resource": "model-route-moderation", "provider": "anthropic"},
    ],
    "kubernetes": [
        {"description": "Right-size the recommendations ranker: requests are 3x actual usage across "
                        "18 pods. Trim CPU/memory requests to the p95.",
         "monthly_saving": 5200.00, "resource": "ns/recommendations", "provider": "kubernetes"},
    ],
    "snowflake": [
        {"description": "Auto-suspend two idle ad-analytics warehouses after 60s (currently 5 min). "
                        "They sit warm most of the day.",
         "monthly_saving": 3400.00, "resource": "wh/ad_analytics_xl", "provider": "snowflake"},
    ],
    "datadog": [
        {"description": "Drop custom-metric cardinality on the playback fleet: unused per-device tags "
                        "triple the metric count.",
         "monthly_saving": 2600.00, "resource": "dd-playback-metrics", "provider": "datadog"},
    ],
    "databricks": [
        {"description": "Move nightly recommendation-model training to spot job clusters.",
         "monthly_saving": 4100.00, "resource": "dbx-reco-training", "provider": "databricks"},
    ],
}


# Demo accounts and regions, so Top Accounts and Spend by Region are real panels.
_DEMO_ACCOUNTS = [
    {"name": "Production",         "id": "481516234203", "share": 0.38},
    {"name": "Streaming Delivery", "id": "481516234211", "share": 0.24},
    {"name": "Ad Platform",        "id": "481516234229", "share": 0.16},
    {"name": "Data & Analytics",   "id": "481516234237", "share": 0.13},
    {"name": "Staging",            "id": "481516234245", "share": 0.09},
]
_DEMO_REGIONS = [
    {"region": "us-east-1",      "code": "US", "label": "N. Virginia",  "share": 0.34},
    {"region": "us-west-2",      "code": "US", "label": "Oregon",       "share": 0.20},
    {"region": "eu-west-1",      "code": "IE", "label": "Ireland",      "share": 0.18},
    {"region": "eu-central-1",   "code": "DE", "label": "Frankfurt",    "share": 0.12},
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
    delta_pct = 19.2 if "aws" in provs else round(sum(
        s["amount"] * s["delta_pct"] for p in provs for s in _PROVIDER_SERVICES[p]
    ) / max(month_total, 1), 1)
    last_month = round(month_total / (1 + delta_pct / 100), 2)
    projected = round(month_total * 1.088, 2)

    recent_opportunities = [o for p in provs for o in _PROVIDER_OPPS.get(p, [])]
    recent_opportunities.sort(key=lambda o: -o["monthly_saving"])
    opp_total = round(sum(o["monthly_saving"] for o in recent_opportunities), 2)

    recent_savings = [
        {"description": "Moved off-peak VOD encodes to a scheduled g5 pool.",
         "monthly_saving": 2140.00, "resource": "vod-encoder-schedule", "provider": "aws"},
    ] if "aws" in provs else []

    # Verified savings ledger: only changes nable proposed AND confirmed landed on
    # the resource (the cloud now matches nable's recommended config). This is the
    # billable figure, kept strictly separate from "identified/potential".
    verified_ledger = [
        {"description": "CloudFront egress moved to a committed private-pricing tier",
         "resource": "cloudfront-commit", "verified_monthly": 4200.00,
         "confirmed_on": (_TODAY - timedelta(days=4)).isoformat(),
         "proof": "egress now billed at the commit rate; CloudFront line down 5%"},
        {"description": "Rightsized `vod-encoder-07` g5.4xlarge -> g5.2xlarge off-peak",
         "resource": "vod-encoder-07", "verified_monthly": 2140.00,
         "confirmed_on": (_TODAY - timedelta(days=9)).isoformat(),
         "proof": "instance now g5.2xlarge off-hours; next-day EC2 line fell $71/day"},
    ] if "aws" in provs else []
    verified_monthly = round(sum(v["verified_monthly"] for v in verified_ledger), 2)

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
        {"name": "AWS Monthly Budget",       "provider": "aws",       "used": 286402, "limit": 320000},
        {"name": "Snowflake Monthly Budget", "provider": "snowflake", "used": 61000,  "limit": 60000},
        {"name": "GCP Monthly Budget",       "provider": "gcp",       "used": 92000,  "limit": 110000},
    ]
    for b in budgets:
        b["pct"] = round(b["used"] / b["limit"] * 100, 1)
        b["status"] = "over" if b["pct"] >= 100 else ("warn" if b["pct"] >= 85 else "ok")
    alerts = [
        {"kind": "warn",  "title": "Snowflake budget exceeded", "body": "102% of budget used, ad-analytics warehouses running hot"},
        {"kind": "info",  "title": "Forecast alert",            "body": f"{provs[0].upper() if provs else 'AWS'} forecast tracking above run rate after the season launch"},
    ]

    # Executive KPI band (tier-1): unit economics + posture, the board-slide row.
    exec_kpis = [
        {"label": "Cost per 1K active viewers", "value": "$0.42", "delta_pct": -6.0, "good_down": True, "sub": "18.4M monthly active viewers"},
        {"label": "CDN cost per TB delivered", "value": "$8.10", "delta_pct": 4.0, "good_down": True, "sub": "season-launch spike"},
        {"label": "Infra % of revenue", "value": "6.2%", "delta_pct": -0.8, "good_down": True, "sub": "$10.9M MRR"},
        {"label": "Effective savings rate", "value": "26%", "delta_pct": 3.0, "good_down": False, "sub": "vs on-demand list"},
        {"label": "Commitment coverage", "value": "64%", "delta_pct": 5.0, "good_down": False, "sub": "target 80%"},
    ]
    # AI efficiency panel: the wedge no incumbent shows.
    ai_efficiency = {
        "ai_pct_of_spend": 9.8,
        "ai_spend": round(month_total * 0.098, 2),
        "metrics": [
            {"label": "Cost / 1M tokens", "value": "$2.80", "delta_pct": -12.0, "good_down": True},
            {"label": "Cost / 1M recs served", "value": "$1.90", "delta_pct": -8.0, "good_down": True},
            {"label": "GPU encoder utilization", "value": "41%", "delta_pct": 2.0, "good_down": False, "warn": True},
            {"label": "Cache hit rate", "value": "6%", "delta_pct": 0.0, "good_down": False, "warn": True},
        ],
        "callout": "GPU encoders sit at 41% utilization and prompt caching is nearly off. About $7,500/mo is recoverable by scheduling the encoder pool and caching the catalog context.",
    }

    # "What changed since you last looked": the always-on loop as a glance.
    whats_changed = {
        "since": "Monday",
        "items": [
            {"kind": "up",    "text": "CloudFront egress up $18,400 (28%)", "prompt": "Why did CloudFront egress jump after the season launch?"},
            {"kind": "alert", "text": "New anomaly on `AWS Data Transfer`", "prompt": "Explain this week's data-transfer anomaly and what caused it."},
            {"kind": "warn",  "text": "Snowflake budget crossed 100%",      "prompt": "Show our Snowflake budget status and what's driving it over the cap."},
            {"kind": "good",  "text": "$2,140/mo saved, off-peak encoder schedule", "prompt": "Show the savings we've realized in the last week."},
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
        "user": {"name": "Alex R.", "role": "Admin", "email": "alex@streamco.tv"},
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
        "verified_savings": {
            "monthly": verified_monthly,
            "annual": round(verified_monthly * 12, 2),
            "count": len(verified_ledger),
            "delta_pct": 32.0,
            "ledger": verified_ledger,
        },
        "anomalies_open": 2 if "aws" in provs else 1,
        "budget_pct_used": 68.0,
        "recent_opportunities": recent_opportunities,
        "suppressed_opportunities": [
            {"description": "RDS metadata-catalog-01 flagged underutilized, but memory sits at 81%. "
                            "Held back: rightsizing it risks a memory-bound stall, not genuine savings.",
             "monthly_saving": 0.0, "resource": "metadata-catalog-01", "provider": "aws"},
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
                 "detail": "64% of steady compute + CDN on commitments; room for a CloudFront egress commit."},
                {"name": "Rightsizing", "grade": "C", "score": 58,
                 "detail": "GPU encoders over-provisioned off-peak; 1 genuine, low-risk resize."},
                {"name": "Idle & waste", "grade": "B", "score": 76,
                 "detail": "Idle EKS nodes and always-warm Snowflake warehouses."},
                {"name": "Storage tiering", "grade": "A", "score": 88,
                 "detail": "Most VOD masters already lifecycle-managed to Glacier."},
            ],
        },
    }


# Category per demo provider, so the MCP connected-view tools can group them the
# same way the live registry does (cloud / llm / saas). Kubernetes reads from a
# kubeconfig, grouped with cloud.
_DEMO_PROVIDER_CATEGORY: dict[str, str] = {
    "aws": "cloud", "gcp": "cloud", "azure": "cloud", "kubernetes": "cloud",
    "openai": "llm", "anthropic": "llm",
    "datadog": "saas", "snowflake": "saas", "databricks": "saas",
}


def connected_providers() -> list[dict[str, str]]:
    """The providers a demo instance advertises as connected, in display order.
    Used by list_connected_providers / check_connector_health so the MCP "what am
    I connected to" view is populated in demo mode instead of showing everything
    as not-configured (those tools otherwise probe real credentials)."""
    return [
        {"name": p, "category": _DEMO_PROVIDER_CATEGORY.get(p, "cloud")}
        for p in _DEMO_PROVIDERS
    ]


def demo_accounts() -> dict[str, list[dict[str, Any]]]:
    """Per-provider account/subscription/org identifiers for the demo, in the
    shape list_accounts returns (provider -> list of account dicts)."""
    out: dict[str, list[dict[str, Any]]] = {
        "aws": [{"id": a["id"], "name": a["name"]} for a in _DEMO_ACCOUNTS],
        "gcp": [{"billing_account_id": "01A2B3-C4D5E6-F7G8H9", "name": "streamco-billing"}],
        "azure": [{"subscription_id": "9f8e7d6c-5b4a-3210-fedc-ba9876543210",
                   "name": "streamco-prod"}],
        "kubernetes": [{"context": "prod-eks-streaming", "name": "prod-eks-streaming"}],
    }
    for p in ("openai", "anthropic", "datadog", "snowflake", "databricks"):
        out[p] = [{"org": "streamco", "name": f"streamco ({p})"}]
    return out


def saved_views() -> list[dict[str, Any]]:
    """Demo saved dashboards for the gallery, in the shape /api/views returns:
    a list of {id, card, data, saved_by, saved_at}. Lets the Saved dashboards
    surface show a populated shelf with no account connected."""
    _today = date.today()
    return [
        {"id": 9001, "saved_by": "Alex R.", "saved_at": (_today - timedelta(days=2)).isoformat(),
         "card": {"title": "Spend by team, this quarter", "template": "bar", "metric": "EffectiveCost",
                  "dimensions": ["team"]},
         "data": {"rows": [{"team": "Streaming Delivery", "metric": 393000.0}, {"team": "Content Platform", "metric": 175200.0},
                           {"team": "Ad Platform", "metric": 123600.0}, {"team": "Data & Analytics", "metric": 73800.0}],
                  "total": 765600.0, "record_count": 4}},
        {"id": 9002, "saved_by": "Alex R.", "saved_at": (_today - timedelta(days=6)).isoformat(),
         "card": {"title": "AI spend by model", "template": "bar", "metric": "Cost", "dimensions": ["model"]},
         "data": {"rows": [{"model": "gpt-4o", "metric": 24000.0}, {"model": "claude-sonnet-4-5", "metric": 15000.0},
                           {"model": "o3", "metric": 9200.0}, {"model": "bedrock", "metric": 6000.0}],
                  "total": 54200.0, "record_count": 4}},
        {"id": 9003, "saved_by": "Alex R.", "saved_at": (_today - timedelta(days=11)).isoformat(),
         "card": {"title": "CDN egress by region", "template": "bar", "metric": "EffectiveCost",
                  "dimensions": ["region"]},
         "data": {"rows": [{"region": "us-east-1", "metric": 31200.0}, {"region": "eu-west-1", "metric": 22800.0},
                           {"region": "us-west-2", "metric": 18400.0}, {"region": "ap-southeast-1", "metric": 9600.0}],
                  "total": 82000.0, "record_count": 4}},
    ]


def bedrock_split() -> dict[str, Any]:
    """Demo Bedrock input/output/cache split, consistent with the ~$330 Bedrock
    line in llm_costs(): input-heavy and uncached, which is the signature
    caching finding. Lets optimize_ai_spend fire the prompt-caching lever with
    no credentials."""
    return {
        "input_cost": 5340.0,      # ~89% of the $6,000 Bedrock bill
        "output_cost": 660.0,
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
        "total_pr_count": 34,
        "ai_pr_count": 27,
        "human_pr_count": 7,
        "ai_share_pct": 79.4,
        "total_llm_spend_usd": 2100.0,
        "by_label": {
            "Claude Opus 4.8": {
                "label": "Claude Opus 4.8", "pr_count": 15, "high": 4, "medium": 8, "low": 3,
                "lines_changed": 6200, "llm_spend_usd": 1040.0, "spend_share_pct": 49.5,
                "cost_per_pr_usd": 69.3,
                "examples": [
                    {"title": "Add per-title CDN egress attribution", "magnitude": "high", "lines": 620, "url": "", "repo": "streamco/streaming-platform"},
                    {"title": "Cache catalog context on the recommender", "magnitude": "high", "lines": 480, "url": "", "repo": "streamco/recommendations"},
                ],
            },
            "Claude Sonnet 4.6": {
                "label": "Claude Sonnet 4.6", "pr_count": 9, "high": 0, "medium": 6, "low": 3,
                "lines_changed": 1400, "llm_spend_usd": 640.0, "spend_share_pct": 30.5,
                "cost_per_pr_usd": 71.1,
                "examples": [
                    {"title": "Tune the ad-decisioning bid timeout", "magnitude": "medium", "lines": 150, "url": "", "repo": "streamco/ad-platform"},
                ],
            },
            "OpenAI Codex": {
                "label": "OpenAI Codex", "pr_count": 3, "high": 0, "medium": 1, "low": 2,
                "lines_changed": 240, "llm_spend_usd": None, "spend_share_pct": None,
                "cost_per_pr_usd": None, "examples": [],
            },
            "Human": {
                "label": "Human", "pr_count": 7, "high": 1, "medium": 3, "low": 3,
                "lines_changed": 2100, "examples": [],
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
