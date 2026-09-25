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

import math
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


# ── Tool response stubs ───────────────────────────────────────────────────────

def _anomaly_rows() -> list[dict[str, Any]]:
    return [
            {
                "id":          "anom-001",
                "service":     "Amazon CloudFront",
                "account_id":  _ACCOUNT_ID,
                "severity":    "high",
                "detected_at": f"{(_TODAY - timedelta(days=3)).isoformat()}T14:22:00Z",
                "description": (
                    "CloudFront egress spiked $120,800 (+20%) after the new season "
                    "dropped Friday. Delivery to SmartCast devices in us-east-1 and "
                    "eu-west-1 drove the increase."
                ),
                "daily_cost_before": 20130.00,
                "daily_cost_after":  24160.00,
                "projected_monthly_impact": 120800.00,
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
                    "Origin-to-edge data transfer rose $108,100 (+36%), tracking the "
                    "CloudFront egress jump from the season launch."
                ),
                "daily_cost_before":         10000.00,
                "daily_cost_after":          13600.00,
                "projected_monthly_impact":  108100.00,
            },
            {
                "id":          "anom-003",
                "service":     "Amazon S3",
                "account_id":  _ACCOUNT_ID,
                "severity":    "medium",
                "detected_at": f"{(_TODAY - timedelta(days=1)).isoformat()}T02:41:00Z",
                "description": (
                    "S3 GET/request charges rose $28,400 (+31%) in the ad-platform "
                    "account. No matching deploy or content drop, cause not yet "
                    "isolated, flagged for review."
                ),
                "daily_cost_before":         2960.00,
                "daily_cost_after":          3870.00,
                "projected_monthly_impact":  28400.00,
                "cause":                     "unknown",
            },
    ]


def rightsizing() -> dict[str, Any]:
    # Mirrors the real rightsizing_summary shape: every rec carries a genuine-
    # savings verdict, and savings are priced on the customer's real rates (here a
    # 22% effective discount measured from CUR), not list price. The demo shows the
    # judgment doing its job: $889/mo of raw "underutilized" collapses to $218/mo of
    # genuine savings once burst, memory-bound, and the real rate are accounted for.
    return {
        "total_instances_flagged": 47,
        "total_monthly_savings":   84200.00,
        "total_annual_savings":    1010400.00,
        "genuine_monthly_savings": 21600.00,
        "genuine_annual_savings":  259200.00,
        "verdicts": {"genuine_savings": 12, "review": 19, "likely_false_positive": 16},
        "source": {
            "compute_optimizer": 31,
            "cloudwatch_fallback": 16,
            "note": "Compute Optimizer recommendations include CPU, memory, network, and disk. "
                    "CloudWatch fallback is CPU-only.",
        },
        "savings_by_resource_type": {"ec2": 61400.00, "rds": 22800.00},
        "judgment_note": (
            "47 instances flagged, $84,200/mo of raw 'underutilized'. Only $21,600/mo "
            "survives once burst, memory-bound, and prime-time headroom are checked: "
            "12 genuine, 19 need review, 16 are likely false positives. Showing the top 3."
        ),
        "recommendations": [
            {
                "instance_id":   "i-0a1b2c3d4e5f67890",
                "name":          "vod-encoder-fleet (142 instances)",
                "region":        "us-east-1",
                "resource_type": "ec2",
                "source":        "compute_optimizer",
                "current_type":  "g5.4xlarge",
                "recommended_type": "g5.2xlarge (off-peak)",
                "avg_cpu_pct":   11.2,
                "max_cpu_pct":   None,
                "avg_mem_pct":   24.0,
                "monthly_savings":          12800.00,
                "adjusted_monthly_savings": 8400.00,
                "verdict":       "genuine_savings",
                "score":         86,
                "why":           "GPU encoders sit near-idle off-peak (11% avg util); "
                                 "real saving ≈$8,400/mo on your effective rate, ~26% below list (cur_athena)",
                "action":        "Move off-peak encodes to a scheduled g5.2xlarge pool; fully reversible.",
            },
            {
                "instance_id":   "db-metadata-catalog-01",
                "name":          "metadata-catalog-01",
                "region":        "us-east-1",
                "resource_type": "rds",
                "source":        "compute_optimizer",
                "current_type":  "db.r6g.8xlarge",
                "recommended_type": "db.r6g.4xlarge",
                "avg_cpu_pct":   9.4,
                "max_cpu_pct":   None,
                "avg_mem_pct":   82.0,
                "monthly_savings":          9600.00,
                "adjusted_monthly_savings": 7200.00,
                "verdict":       "review",
                "score":         41,
                "why":           "over-provisioned; memory at 82%, likely memory-bound; "
                                 "real saving ≈$7,200/mo on your effective rate, ~26% below list (cur_athena)",
                "action":        "Modify the instance class in a maintenance window; reversible, brief failover.",
            },
            {
                "instance_id":   "i-07f3c9a1b2d4e6f80",
                "name":          "playback-api-fleet (88 instances)",
                "region":        "us-west-2",
                "resource_type": "ec2",
                "source":        "cloudwatch_fallback",
                "current_type":  "c6i.2xlarge",
                "recommended_type": "c6i.xlarge",
                "avg_cpu_pct":   12.0,
                "max_cpu_pct":   84.0,
                "avg_mem_pct":   None,
                "monthly_savings":          6200.00,
                "adjusted_monthly_savings": 4100.00,
                "verdict":       "likely_false_positive",
                "score":         7,
                "why":           "CPU-only avg 12%; peaks to 84% at prime-time, needs headroom; "
                                 "real saving ≈$4,100/mo on your effective rate, ~26% below list (cur_athena)",
                "action":        "Resize needs a stop/start (brief downtime); fully reversible.",
            },
        ],
        "pricing_basis": {
            "basis":      {"effective_rate": 47},
            "confidence": {"high": 31, "medium": 16},
            "effective_discount_pct": 26.0,
            "rate_source": "cur_athena",
        },
    }


def kubernetes_costs() -> dict[str, Any]:
    return {
        "cluster":               "prod-eks-streaming",
        "provider":              "aws",
        "node_count":            148,
        "pod_count":             1240,
        "total_monthly_cost_usd": 540000.00,
        "wasted_monthly_cost_usd": 96000.00,
        "waste_pct":             17.8,
        "cpu_efficiency_pct":    39.0,
        "mem_efficiency_pct":    56.0,
        "cost_by_namespace": {
            "recommendations": 214000.00,
            "ad-decisioning":  132000.00,
            "playback-api":     88000.00,
            "search":           54000.00,
            "platform":         34000.00,
            "kube-system":      18000.00,
        },
        "top_workloads": [
            {
                "namespace":          "recommendations",
                "workload":           "Deployment/ranker-inference",
                "pods":               120,
                "monthly_cost_usd":   152000.00,
                "wasted_usd":         58000.00,
                "cpu_efficiency_pct": 28.0,
                "mem_efficiency_pct": 51.0,
            },
            {
                "namespace":          "ad-decisioning",
                "workload":           "Deployment/bid-service",
                "pods":               84,
                "monthly_cost_usd":   84000.00,
                "wasted_usd":         11000.00,
                "cpu_efficiency_pct": 67.0,
                "mem_efficiency_pct": 62.0,
            },
        ],
        "idle_nodes":    ["ip-10-2-4-91.ec2.internal", "ip-10-2-7-33.ec2.internal", "ip-10-2-1-88.ec2.internal"],
        "idle_node_cost_usd": 14800.00,
        "summary": (
            "Cluster 'prod-eks-streaming' (AWS, 148 nodes): $540,000/month. "
            "~$96,000/month wasted — the recommendations ranker is 28% CPU efficient."
        ),
    }


def cluster_efficiency() -> dict[str, Any]:
    return {
        "cluster":  "prod-eks-streaming",
        "provider": "aws",
        "score":    54.0,
        "grade":    "C",
        "total_monthly_cost_usd":   540000.00,
        "wasted_monthly_cost_usd":  96000.00,
        "has_metrics_server": True,
        "dimensions": {
            "cpu_efficiency_pct":  39.0,
            "cpu_score":           11.7,
            "mem_efficiency_pct":  56.0,
            "mem_score":           16.8,
            "idle_node_pct":       9.1,
            "idle_node_score":     14.0,
            "waste_pct":           17.8,
            "waste_score":         11.5,
        },
        "headline": (
            "Cluster 'prod-eks-streaming' scores 54/100 (Grade C) — "
            "$540,000/mo total, $96,000/mo estimated waste. "
            "Moderate waste. Tackle the recommendations ranker and 3 idle nodes first."
        ),
        "top_recommendations": [
            {
                "priority": "high",
                "category": "rightsizing",
                "action":   "Rightsize recommendations/ranker-inference: CPU requests 480 cores, using 134 (28%) — reduce to 200 cores.",
                "potential_savings_usd": 44000.0,
            },
            {
                "priority": "medium",
                "category": "idle_nodes",
                "action":   "Drain 3 idle nodes (<10% CPU/mem for 14 days) — saving ~$14,800/mo.",
                "potential_savings_usd": 14800.0,
            },
        ],
    }


# ── Registry: maps tool name → demo response function ─────────────────────────

def _pct_text(p: float | None) -> str:
    return "n/a" if p is None else f"{p:+.1f}%"


def cost_summary(args: dict[str, Any] | None = None) -> dict[str, Any]:
    """get_cost_summary on the sample: every selected provider over the window
    the arguments name (default: the 30 days ending yesterday, like the live
    tool), derived from the one daily series every other demo tool reads."""
    args = args or {}
    provs = _pick_providers(args.get("provider"), args.get("category"))
    if provs is None:
        return _not_in_sample(f"Provider '{args.get('provider')}'")
    first, last = demo_window(args)
    period = _period(first, last)
    rows = _window_rows(provs, first, last)
    total = round(sum(r["amount"] for r in rows), 2)
    prev = round(sum(r["previous"] for r in rows), 2)
    detail = 15 if len(provs) == 1 else 5
    by_provider = {}
    for p in provs:
        mine = [r for r in rows if r["provider"] == p]
        by_provider[p] = {
            "total_usd": round(sum(r["amount"] for r in mine), 2),
            "by_service": {r["service"]: r["amount"] for r in mine[:detail]},
        }
    by_service = {r["service"]: r["amount"] for r in rows[:15]}
    top = rows[0]
    change = _pct(total, prev)
    story = (" Streaming egress rose after the new season dropped."
             if top["service"] == "Amazon CloudFront" and top["delta"] > 0 else "")
    out: dict[str, Any] = {
        "period": period,
        "grand_total_usd": total,
        "grand_total_formatted": _usd(total),
        "total_usd": total,
        "previous_period_total_usd": prev,
        "vs_previous_period_pct": change,
        "by_provider": by_provider,
        "grand_by_service": by_service,
        "by_service": by_service,
        "summary": (
            f"Sample data: {_scope(provs)} spent {_usd(total)} over {period['label']}, "
            f"{_pct_text(change)} vs the {period['days']} days before. The top line is "
            f"{top['service']} at {_usd(top['amount'])}, "
            f"{'up' if top['delta'] >= 0 else 'down'} {_usd(abs(top['delta']))}.{story}"
        ),
    }
    if "aws" in provs:
        out["account_id"] = _ACCOUNT_ID
        out["account_name"] = _ACCOUNT_NAME
        out["note"] = "AWS spans 312 linked accounts; AWS figures are the org rollup."
    return out


def cost_drivers(args: dict[str, Any] | None = None) -> dict[str, Any]:
    """'Why did the bill change': the window vs the same-length window before it,
    across every sample provider (the live tool compares everything connected)."""
    args = args or {}
    provs = _pick_providers(args.get("provider"), args.get("category"))
    if provs is None:
        return _not_in_sample(f"Provider '{args.get('provider')}'")
    first, last = demo_window(args)
    period = _period(first, last)
    n = period["days"]
    rows = _window_rows(provs, first, last)
    cur = round(sum(r["amount"] for r in rows), 2)
    prev = round(sum(r["previous"] for r in rows), 2)
    net = round(cur - prev, 2)
    top_n = args.get("top_n") or args.get("limit") or 10
    top_n = max(1, int(top_n)) if isinstance(top_n, (int, float)) else 10

    def _driver(r: dict[str, Any]) -> dict[str, Any]:
        return {"key": r["service"], "provider": r["provider"], "current": r["amount"],
                "previous": r["previous"], "delta": r["delta"], "delta_pct": r["delta_pct"],
                "direction": "increase" if r["delta"] > 0 else "decrease"}

    ups = sorted((r for r in rows if r["delta"] > 0), key=lambda r: -r["delta"])
    downs = sorted((r for r in rows if r["delta"] < 0), key=lambda r: r["delta"])
    inc = [_driver(r) for r in ups[:top_n]]
    dec = [_driver(r) for r in downs[:top_n]]
    lead = ", ".join(f"{d['key']} {_usd(d['delta'])} ({d['delta_pct']:+.0f}%)" for d in inc[:4])
    top4 = round(sum(d["delta"] for d in inc[:4]), 2)
    story = ""
    if inc and inc[0]["key"] == "Amazon CloudFront":
        story = (" The season launch is the story: delivery to SmartCast devices drove "
                 "CloudFront egress and origin data transfer.")
    fall = (f" The largest decrease is {dec[0]['key']} at {_usd(dec[0]['delta'])}."
            if dec else "")
    share = (f" The top four increases add {_usd(top4)} of the {_usd(net)} net change."
             if net > 0 else "")
    return {
        "period": period,
        "comparison_period": _period(first - timedelta(days=n), first - timedelta(days=1)),
        "scope": _scope(provs),
        "total_current_usd": cur,
        "total_previous_usd": prev,
        "net_change_usd": net,
        "net_change_pct": _pct(cur, prev),
        "top_increases": inc,
        "top_decreases": dec,
        "all_drivers": [],
        "summary": (
            f"Sample data: across {_scope(provs)}, costs "
            f"{'rose' if net >= 0 else 'fell'} {_usd(abs(net))} ({_pct_text(_pct(cur, prev))}) "
            f"over the last {n} days vs the {n} days before. Largest increases: {lead}."
            f"{story}{fall}{share}"
            + (f" Start with {inc[0]['key']}." if inc else "")
        ),
    }


# 30-day AWS spend by the `team` tag, as the CUR sample records it. It sums to the
# AWS reference total, so a window's team split is these shares of that window's
# AWS spend and always adds back up to it.
_AWS_TEAM_30D = {
    "streaming-delivery": 742000.00,
    "content-platform":   388000.00,
    "ad-platform":        296000.00,
    "data-analytics":     174000.00,
    "recommendations":    118000.00,
    "untagged":           689600.00,
}
# All-provider spend by the `env` tag (sums to the all-provider reference total).
_ENV_30D = {"production": 3708000.00, "untagged": 689600.00,
            "staging": 463000.00, "dev": 287000.00}


def _split(shares: dict[str, float], total: float) -> dict[str, float]:
    base = sum(shares.values())
    return {k: round(total * v / base, 2) for k, v in shares.items()}


def cost_summary_cur(args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Demo response for CUR/Athena line-item query (AWS only, like the CUR)."""
    args = args or {}
    tag = str(args.get("tag_key") or "team").strip().lower()
    if tag != "team":
        out = _not_in_sample(f"The AWS '{tag}' tag")
        out["note"] += (" The sample CUR carries the `team` tag; slice_costs with "
                        "dimensions ['Tags[env]'] splits all providers by env.")
        return out
    first, last = demo_window(args)
    rows = {r["service"]: r["amount"] for r in _window_rows(["aws"], first, last)}
    total = round(sum(rows.values()), 2)
    by_team = _split(_AWS_TEAM_30D, total)
    untagged_pct = round(by_team["untagged"] / total * 100, 1) if total else 0.0
    return {
        "source":  "AWS Cost and Usage Report (Athena)",
        "period":  _period(first, last),
        "total_usd": total,
        "top_resources": [
            {
                "resource_id":   "E2QK8S1TREAM01",
                "resource_name": "smartcast-cdn",
                "service":       "Amazon CloudFront",
                "instance_type": "distribution",
                "region":        "global",
                "cost_usd":      rows.get("Amazon CloudFront", 0.0),
                "tags": {},  # the biggest line has no owner tag: the core attribution gap
            },
            {
                "resource_id":   "i-0a1b2c3d4e5f67890",
                "resource_name": "vod-encoder-fleet",
                "service":       "Amazon EC2",
                "instance_type": "g5.4xlarge",
                "region":        "us-east-1",
                "cost_usd":      rows.get("Amazon EC2", 0.0),
                "tags": {"team": "content-platform", "env": "production"},
            },
        ],
        "by_tag_team": by_team,
        "untagged_pct": untagged_pct,
        "note": (f"Sample data: {untagged_pct:.0f}% of AWS spend ({_usd(by_team['untagged'])}) "
                 "is untagged, mostly shared CDN, data transfer, and cross-account networking "
                 "with no owner tag. That's the first attribution gap to close, and where most "
                 "of the unallocated egress hides."),
    }


# AI / LLM spend. The model lines are the openai/anthropic services above; Bedrock
# is billed inside AWS and priced at this much per 30 days, scaled with AWS.
_LLM_MODELS = [
    ("gpt-4o",                     "openai",    "GPT-4o"),
    ("claude-sonnet-4-5-20250929", "anthropic", "Claude Sonnet"),
    ("o3",                         "openai",    "o3"),
    ("claude-haiku-4-5-20251001",  "anthropic", "Claude Haiku"),
    ("gpt-4o-mini",                "openai",    "GPT-4o mini"),
]
_BEDROCK_30D = 40000.00


def llm_costs(args: dict[str, Any] | None = None) -> dict[str, Any]:
    """AI/LLM spend for the StreamCo story over the requested window (default
    the 30 days ending yesterday). AI powers recommendations, content metadata
    auto-tagging, search relevance, and moderation; gpt-4o leads. The wedge: show
    the money answer AND the switch that recovers it, with zero creds."""
    args = args or {}
    want = str(args.get("provider") or "").strip().lower()
    if want and want not in ("openai", "anthropic", "bedrock"):
        return _not_in_sample(f"LLM provider '{want}'")
    first, last = demo_window(args)
    period = _period(first, last)
    ref = _ref_end()
    svc = {s["service"]: s for p in ("openai", "anthropic") for s in _PROVIDER_SERVICES[p]}
    aws_ref = sum(s["amount"] for s in _PROVIDER_SERVICES["aws"])
    days = _days(first, last)

    def _model_day(name: str, d: date) -> float:
        s = svc[name]
        return _svc_day(s["amount"], s["delta_pct"], d, ref)

    def _bedrock_day(d: date) -> float:
        return _BEDROCK_30D / aws_ref * sum(
            _svc_day(s["amount"], s["delta_pct"], d, ref) for s in _PROVIDER_SERVICES["aws"])

    models = [(m, p, s) for m, p, s in _LLM_MODELS if not want or p == want]
    by_model = {m: round(sum(_model_day(s, d) for d in days), 2) for m, _, s in models}
    by_provider: dict[str, float] = {}
    for m, p, _ in models:
        by_provider[p] = round(by_provider.get(p, 0.0) + by_model[m], 2)
    if not want or want == "bedrock":
        by_model["bedrock/anthropic.claude"] = round(sum(_bedrock_day(d) for d in days), 2)
        by_provider["bedrock"] = by_model["bedrock/anthropic.claude"]
    by_model = dict(sorted(by_model.items(), key=lambda kv: -kv[1]))
    total = round(sum(by_model.values()), 2)
    daily = [{"date": d.isoformat(),
              "total_usd": round(sum(_model_day(s, d) for _, _, s in models)
                                 + (_bedrock_day(d) if "bedrock" in by_provider else 0.0), 2)}
             for d in days[-31:]]
    all_total = _sum_days(_DEMO_PROVIDERS, first, last)
    pct = round(total / all_total * 100, 1) if all_total else None
    provider_of = {m: p for m, p, _ in _LLM_MODELS}
    provider_of["bedrock/anthropic.claude"] = "bedrock"
    top_model, top_cost = next(iter(by_model.items()))
    return {
        "period": period,
        "total_usd": total,
        "pct_of_total_cloud_spend": pct,
        "by_provider": by_provider,
        "by_model": by_model,
        "model_count": len(by_model),
        "top_spenders": [{"model": m, "provider": provider_of[m], "cost_usd": c}
                         for m, c in list(by_model.items())[:3]],
        "daily": daily,
        "recommendations": [
            {
                "title": "Route title auto-tagging off o3",
                "detail": (
                    "The nightly metadata auto-tagging job runs on o3. On a sampled eval, "
                    "gpt-4o-mini matches its labels at ~1/15th the price. Routing it saves "
                    "an estimated $52,000/mo."
                ),
                "estimated_savings_usd": 52000.00,
                "effort": "medium",
            },
            {
                "title": "Cache the shared catalog context on gpt-4o",
                "detail": (
                    "Every recommendation call carries the same 8K-token catalog/system "
                    "context, billed uncached (6% cache hit rate). Prompt caching recovers "
                    "an estimated $36,000/mo at current volume."
                ),
                "estimated_savings_usd": 36000.00,
                "effort": "low",
            },
        ],
        "sources": {"openai": "ok", "anthropic": "ok", "bedrock": "ok"},
        "summary": (
            f"Sample data: AI/LLM spend over {period['label']}: {_usd(total)}"
            + (f" (~{pct:.0f}% of total spend)" if pct is not None else "")
            + f". {top_model} drives {top_cost / total * 100:.0f}% of it. Two changes "
            "recover ~$88,000/mo: route metadata auto-tagging off o3 to gpt-4o-mini "
            "($52,000) and cache the catalog context ($36,000)."
        ),
    }


def anomalies(args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = args or {}
    rows = _anomaly_rows()
    provider = str(args.get("provider") or "").strip().lower()
    if provider and provider != "aws":
        rows = []
    severity = str(args.get("severity") or "").strip().lower()
    if severity:
        rows = [a for a in rows if a["severity"] == severity]
    limit = args.get("limit")
    if isinstance(limit, int) and limit > 0:
        rows = rows[:limit]
    return {
        "anomalies": rows,
        "total_anomalies": len(rows),
        "high_severity": sum(1 for a in rows if a["severity"] == "high"),
        "summary": (
            "Sample data: 3 cost anomalies detected. The season launch drove CloudFront "
            "and data-transfer egress up ~$229k/mo combined; a $28k S3 request spike has "
            "no identified cause yet."
            if len(rows) == 3 else
            f"Sample data: {len(rows)} matching anomalies in the sample (all sample "
            "anomalies are on AWS)."
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
        {"service": "Amazon CloudFront",          "amount": 724800.00, "resources": 0,    "delta_pct": 20.0},
        {"service": "Amazon EC2",                 "amount": 431600.00, "resources": 2140, "delta_pct": 9.3},
        {"service": "AWS Data Transfer",          "amount": 408200.00, "resources": 0,    "delta_pct": 36.0},
        {"service": "Amazon S3",                  "amount": 312400.00, "resources": 180,  "delta_pct": -4.2},
        {"service": "AWS Elemental MediaConvert", "amount": 188900.00, "resources": 0,    "delta_pct": 12.0},
        {"service": "AWS Elemental MediaLive",    "amount": 151300.00, "resources": 48,   "delta_pct": 18.0},
        {"service": "Amazon RDS",                 "amount": 88400.00,  "resources": 34,   "delta_pct": 6.0},
        {"service": "Amazon CloudWatch",          "amount": 61700.00,  "resources": 0,    "delta_pct": 7.0},
        {"service": "AWS Lambda",                 "amount": 40300.00,  "resources": 1240, "delta_pct": 8.0},
    ],
    "gcp": [
        {"service": "BigQuery",           "amount": 452000.00, "resources": 0,   "delta_pct": 22.0},
        {"service": "Compute Engine",     "amount": 138000.00, "resources": 420, "delta_pct": 7.0},
        {"service": "GKE",                "amount": 52000.00,  "resources": 12,  "delta_pct": 5.0},
        {"service": "Cloud Storage",      "amount": 28000.00,  "resources": 240, "delta_pct": -2.0},
        {"service": "Cloud CDN",          "amount": 10000.00,  "resources": 0,   "delta_pct": 11.0},
    ],
    "azure": [
        {"service": "Virtual Machines",   "amount": 80000.00, "resources": 60, "delta_pct": 6.0},
        {"service": "Azure SQL Database", "amount": 22000.00, "resources": 14, "delta_pct": 4.0},
        {"service": "Blob Storage",       "amount": 12000.00, "resources": 40, "delta_pct": -1.0},
        {"service": "App Service",        "amount": 6000.00,  "resources": 22, "delta_pct": 3.0},
    ],
    # Kubernetes, read from kubeconfig (allocation view; namespaces as lines).
    "kubernetes": [
        {"service": "recommendations (ns)", "amount": 214000.00, "resources": 420, "delta_pct": 20.0},
        {"service": "ad-decisioning (ns)",  "amount": 132000.00, "resources": 260, "delta_pct": 9.0},
        {"service": "playback-api (ns)",    "amount": 88000.00,  "resources": 180, "delta_pct": 4.0},
        {"service": "search (ns)",          "amount": 54000.00,  "resources": 120, "delta_pct": 3.0},
        {"service": "platform (ns)",        "amount": 34000.00,  "resources": 90,  "delta_pct": 1.0},
        {"service": "kube-system (ns)",     "amount": 18000.00,  "resources": 70,  "delta_pct": 0.5},
    ],
    # AI / LLM token spend, genuinely separate from cloud (the AI-native wedge).
    "openai": [
        {"service": "GPT-4o",             "amount": 168000.00, "resources": 0,  "delta_pct": 30.0},
        {"service": "o3",                 "amount": 62000.00,  "resources": 0,  "delta_pct": 58.0},
        {"service": "GPT-4o mini",        "amount": 30000.00,  "resources": 0,  "delta_pct": 12.0},
    ],
    "anthropic": [
        {"service": "Claude Sonnet",      "amount": 102000.00, "resources": 0,  "delta_pct": 26.0},
        {"service": "Claude Haiku",       "amount": 48000.00,  "resources": 0,  "delta_pct": 9.0},
    ],
    # SaaS + data platforms.
    "datadog": [
        {"service": "Infrastructure",     "amount": 118000.00, "resources": 0,  "delta_pct": 12.0},
        {"service": "Log Management",     "amount": 82000.00,  "resources": 0,  "delta_pct": 19.0},
        {"service": "APM & Tracing",      "amount": 40000.00,  "resources": 0,  "delta_pct": 7.0},
    ],
    "snowflake": [
        {"service": "Compute (warehouses)","amount": 358000.00,"resources": 38, "delta_pct": 17.0},
        {"service": "Storage",            "amount": 62000.00,  "resources": 0,  "delta_pct": 4.0},
    ],
    "databricks": [
        {"service": "Jobs Compute",       "amount": 232000.00, "resources": 0,  "delta_pct": 14.0},
        {"service": "SQL Warehouses",     "amount": 98000.00,  "resources": 0,  "delta_pct": 6.0},
    ],
}
_DEMO_PROVIDERS = ["aws", "gcp", "azure", "kubernetes", "openai", "anthropic",
                   "datadog", "snowflake", "databricks"]

# Per-provider open opportunities, priced on the customer's real rate.
_PROVIDER_OPPS: dict[str, list[dict[str, Any]]] = {
    "aws": [
        {"description": "Buy a 1-year compute Savings Plan at your steady encoder + services baseline.",
         "monthly_saving": 88000.00, "resource": "compute-savings-plan", "provider": "aws"},
        {"description": "Move the CloudFront egress baseline to a committed private-pricing tier. "
                        "Steady streaming volume qualifies; priced on your ~26% effective discount.",
         "monthly_saving": 62000.00, "resource": "cloudfront-commit", "provider": "aws"},
        {"description": "Move 2.4 PB of cold VOD masters to S3 Glacier Deep Archive.",
         "monthly_saving": 41000.00, "resource": "s3://streamco-vod-masters", "provider": "aws"},
        {"description": "Schedule the off-peak VOD encoder fleet (g5.4xlarge to g5.2xlarge off-hours). "
                        "Genuine after burst + memory check.",
         "monthly_saving": 8400.00, "resource": "vod-encoder-fleet", "provider": "aws"},
    ],
    "gcp": [
        {"description": "Switch the viewership rollups to BigQuery flat-rate slots at this query volume.",
         "monthly_saving": 58000.00, "resource": "bq-flat-slots", "provider": "gcp"},
        {"description": "Set a 90-day lifecycle rule on 1.6 PB of cold Cloud Storage.",
         "monthly_saving": 14000.00, "resource": "gs://streamco-analytics-archive", "provider": "gcp"},
    ],
    "azure": [
        {"description": "Buy a 1-year Azure Reserved VM Instance for the steady D-series baseline.",
         "monthly_saving": 9800.00, "resource": "vm-reservation-dseries", "provider": "azure"},
    ],
    "openai": [
        {"description": "Route the nightly title auto-tagging job from o3 to GPT-4o mini. Same labels "
                        "on a sampled eval, ~1/15th the price.",
         "monthly_saving": 52000.00, "resource": "model-route-autotag", "provider": "openai"},
        {"description": "Cache the shared catalog context on GPT-4o: the same 8K-token context rides "
                        "every recommendation call, billed uncached. Prompt caching recovers most of it.",
         "monthly_saving": 36000.00, "resource": "prompt-cache-gpt4o", "provider": "openai"},
    ],
    "anthropic": [
        {"description": "Move content-moderation summaries from Claude Sonnet to Haiku where quality "
                        "holds on your eval set.",
         "monthly_saving": 22000.00, "resource": "model-route-moderation", "provider": "anthropic"},
    ],
    "kubernetes": [
        {"description": "Right-size the recommendations ranker: requests are 3.5x actual usage across "
                        "120 pods. Trim CPU/memory requests to the p95.",
         "monthly_saving": 44000.00, "resource": "ns/recommendations", "provider": "kubernetes"},
    ],
    "snowflake": [
        {"description": "Auto-suspend six idle ad-analytics warehouses after 60s (currently 5 min). "
                        "They sit warm most of the day.",
         "monthly_saving": 28000.00, "resource": "wh/ad_analytics_xl", "provider": "snowflake"},
    ],
    "datadog": [
        {"description": "Drop custom-metric cardinality on the playback fleet: unused per-device tags "
                        "triple the metric count.",
         "monthly_saving": 19000.00, "resource": "dd-playback-metrics", "provider": "datadog"},
    ],
    "databricks": [
        {"description": "Move nightly recommendation-model training to spot job clusters.",
         "monthly_saving": 31000.00, "resource": "dbx-reco-training", "provider": "databricks"},
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


# ── The one source every demo number comes from ────────────────────────────────
# _PROVIDER_SERVICES holds each service's cost over the REFERENCE WINDOW: the 30
# days ending yesterday (the last complete day), which is the window the live cost
# tools default to. delta_pct is the change against the 30 days before that.
#
# Every figure a demo tool reports, for any window, is a sum of _svc_day() over
# the days in that window. The sample used to hard-code each tool's answer, so
# AWS was $2,407,600 "month to date" in get_cost_summary and $2,002,363 summed
# from get_cost_trends, the all-provider total was $4,281,177 in one place and
# $5,147,600 in another, and the forecast ($5.6M) sat above a month-to-date that
# was already $5.15M on the 23rd. Two tools asked about the same days now agree
# to the cent, and a 7-day question gets a 7-day answer.

_REF_DAYS = 30


def _ref_end() -> date:
    """The last complete day the sample has data for (yesterday)."""
    return date.today() - timedelta(days=1)


def _svc_day(amount: float, delta_pct: float, d: date, ref_end: date) -> float:
    """One service's cost on day `d`.

    Days are counted back from `ref_end` in 30-day blocks. Block b totals
    amount / g**b, where g = 1 + delta_pct/100, so the reference window sums to
    exactly `amount` and the 30 days before it to exactly amount / g: the
    month-over-month change every tool quotes is the same number. Inside a block
    a linear ramp (zero-sum, and continuous across block edges) and a 7.5-day
    ripple (four whole cycles per block, so also zero-sum) make the series look
    like a bill without moving any block total. Days after `ref_end` extend the
    same curve, which is what the forecast reads."""
    k = (ref_end - d).days
    g = 1.0 + delta_pct / 100.0
    block, j = divmod(k, _REF_DAYS)
    ramp = (g - 1.0) / (g + 1.0)
    shape = (1.0 + ramp * (1.0 - 2.0 * j / (_REF_DAYS - 1))
             + 0.05 * math.sin(2.0 * math.pi * 4.0 * k / _REF_DAYS))
    return amount * g ** (-block) / _REF_DAYS * shape


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _sum_days(provs: list[str], first: date, last: date,
              service: str | None = None) -> float:
    ref = _ref_end()
    days = _days(first, last)
    return sum(_svc_day(s["amount"], s["delta_pct"], d, ref)
               for p in provs for s in _PROVIDER_SERVICES[p]
               if service is None or s["service"] == service
               for d in days)


def _iso(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def demo_window(args: dict[str, Any] | None = None,
                day_keys: tuple[str, ...] = ("days", "compare_days")) -> tuple[date, date]:
    """(first_day, last_day), both inclusive, for a tool's period arguments.

    Mirrors the live tools: start_date/end_date are ISO dates with end_date
    exclusive, a `days`-style argument is a lookback ending yesterday, and the
    default is the 30 days ending yesterday. The sample has no data after
    yesterday, so the window is clipped there."""
    args = args or {}
    ref = _ref_end()
    end = _iso(args.get("end_date"))
    last = min(end - timedelta(days=1), ref) if end else ref
    first = _iso(args.get("start_date"))
    if first is None:
        n = _REF_DAYS
        for key in day_keys:
            v = args.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                n = int(v)
                break
        first = last - timedelta(days=min(n, 731) - 1)
    first = min(first, last)
    return max(first, last - timedelta(days=730)), last


def _period(first: date, last: date) -> dict[str, Any]:
    n = (last - first).days + 1
    return {"start": first.isoformat(), "end": (last + timedelta(days=1)).isoformat(),
            "days": n, "label": f"{first.isoformat()} to {last.isoformat()} ({n} days)"}


def _pick_providers(provider: Any = None, category: Any = None) -> list[str] | None:
    """The sample providers a provider/category filter selects; None when the
    filter names a provider the sample does not have."""
    if provider:
        p = str(provider).strip().lower()
        return [p] if p in _PROVIDER_SERVICES else None
    if category == "cloud":
        return [p for p in _DEMO_PROVIDERS if _DEMO_PROVIDER_CATEGORY.get(p) == "cloud"]
    if category == "saas":
        return [p for p in _DEMO_PROVIDERS if _DEMO_PROVIDER_CATEGORY.get(p) in ("saas", "llm")]
    return list(_DEMO_PROVIDERS)


def _not_in_sample(what: str) -> dict[str, Any]:
    return {
        "_demo_mode": True,
        "not_in_sample": True,
        "note": (f"{what} is not in the StreamCo sample dataset. The sample covers "
                 f"{', '.join(_DEMO_PROVIDERS)}. Connect a real account with connect_aws, "
                 "connect_gcp or connect_azure to ask about your own."),
    }


def _window_rows(provs: list[str], first: date, last: date) -> list[dict[str, Any]]:
    """Every selected service's cost in the window and in the same-length window
    right before it, largest first."""
    ref = _ref_end()
    n = (last - first).days + 1
    cur_days = _days(first, last)
    prev_days = _days(first - timedelta(days=n), first - timedelta(days=1))
    rows = []
    for p in provs:
        for s in _PROVIDER_SERVICES[p]:
            cur = sum(_svc_day(s["amount"], s["delta_pct"], d, ref) for d in cur_days)
            prev = sum(_svc_day(s["amount"], s["delta_pct"], d, ref) for d in prev_days)
            rows.append({
                "provider": p, "service": s["service"],
                "amount": round(cur, 2), "previous": round(prev, 2),
                "delta": round(cur - prev, 2),
                "delta_pct": round((cur - prev) / prev * 100, 1) if prev else None,
            })
    rows.sort(key=lambda r: -r["amount"])
    return rows


def _usd(n: float) -> str:
    return f"-${abs(n):,.0f}" if n < 0 else f"${n:,.0f}"


def _pct(cur: float, prev: float) -> float | None:
    return round((cur - prev) / prev * 100, 1) if prev else None


def _scope(provs: list[str]) -> str:
    if len(provs) == 1:
        return _PROVIDER_LABEL.get(provs[0], provs[0])
    if provs == _DEMO_PROVIDERS:
        return f"all {len(provs)} sample providers"
    return ", ".join(_PROVIDER_LABEL.get(p, p) for p in provs)


def _month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt - timedelta(days=1)


def month_forecast(provs: list[str], service: str | None = None) -> dict[str, Any]:
    """This calendar month for the selected providers: actuals to date, the rest
    of the month projected along the same curve, and a range around it. The
    projection can never fall below what is already spent."""
    ref = _ref_end()
    today = ref + timedelta(days=1)
    m_start, m_end = _month_bounds(today)
    mtd = _sum_days(provs, m_start, ref, service) if ref >= m_start else 0.0
    rest = _sum_days(provs, today, m_end, service)
    band = 0.10
    return {
        "month": today.strftime("%B %Y"),
        "month_to_date_usd": round(mtd, 2),
        "days_elapsed": (ref - m_start).days + 1 if ref >= m_start else 0,
        "days_in_month": m_end.day,
        "projected_month_total": round(mtd + rest, 2),
        "projected_range": {"low": round(mtd + rest * (1 - band), 2),
                            "high": round(mtd + rest * (1 + band), 2)},
    }


# Monthly budgets in the sample, judged on the month-end forecast.
_DEMO_BUDGETS = [
    ("AWS Monthly Budget", "aws", 2_600_000.0),
    ("Snowflake Monthly Budget", "snowflake", 400_000.0),
    ("GCP Monthly Budget", "gcp", 780_000.0),
]
_ORG_MONTHLY_BUDGET = 5_800_000.0


def budgets() -> list[dict[str, Any]]:
    out = []
    for name, prov, limit in _DEMO_BUDGETS:
        f = month_forecast([prov])
        used, proj = f["month_to_date_usd"], f["projected_month_total"]
        proj_pct = round(proj / limit * 100, 1)
        out.append({
            "name": name, "provider": prov, "limit": limit,
            "used": used, "pct": round(used / limit * 100, 1),
            "projected_month_total": proj, "projected_pct": proj_pct,
            "status": "over" if proj_pct >= 100 else ("warn" if proj_pct >= 85 else "ok"),
            "month": f["month"],
        })
    return out


def _demo_category_payload(active_services: list[dict[str, Any]], window_total: float):
    """Category totals + AI split for the demo, classified the SAME way live is.

    Demo and live share one render path: this runs the real classify_category over
    the demo's service inventory, so the six-bucket segments and the AI card get
    exactly the shape a connected box produces.
    """
    from .categories import CATEGORY_KEYS, ai_kind, ai_label, classify_category
    totals = {k: 0.0 for k in CATEGORY_KEYS}
    ai_rows: list[dict[str, Any]] = []
    ai_delta_num = 0.0
    for s in active_services:
        cat = classify_category(s["provider"], s["service"])
        totals[cat] += s["amount"]
        if cat != "ai":
            continue
        ai_rows.append({
            "key": f"{s['provider']}:{s['service']}",
            "label": ai_label(s["provider"], s["service"]),
            "amount": round(s["amount"], 2),
            "kind": ai_kind(s["provider"], s["service"]),
        })
        ai_delta_num += s["amount"] * s.get("delta_pct", 0.0)
    ai_spend = round(totals["ai"], 2)
    ai_rows.sort(key=lambda r: -r["amount"])
    for r in ai_rows:
        r["pct"] = round(r["amount"] / ai_spend * 100, 1) if ai_spend else 0.0
    ai_pct = round(ai_spend / window_total * 100, 1) if window_total else None
    ai_delta = round(ai_delta_num / ai_spend, 1) if ai_spend else None
    return {k: round(v, 2) for k, v in totals.items()}, ai_spend, ai_pct, ai_delta, ai_rows


def _attach_daily_categories(daily: list[dict[str, Any]], category_totals: dict[str, float],
                             window_total: float) -> None:
    """Split each day's total into the six buckets by the window's category share.
    The per-day series drifts (a 1-day window lands ~115% of window_total), so the
    raw day totals do not sum to window_total. Normalize each day's split back to
    the window first, or the daily category series would not reconcile with
    category_totals."""
    from .categories import CATEGORY_KEYS
    shares = {k: (category_totals.get(k, 0.0) / window_total if window_total else 0.0)
              for k in CATEGORY_KEYS}
    day_totals = [sum(v for k, v in row.items() if k != "date") for row in daily]
    series_sum = sum(day_totals)
    scale = (window_total / series_sum) if series_sum else 0.0
    for row, day_total in zip(daily, day_totals):
        scaled = day_total * scale
        row["categories"] = {k: round(scaled * shares[k], 2) for k in CATEGORY_KEYS}


def _daily_series(days: int, provs: list[str], end: date | None = None) -> list[dict[str, Any]]:
    """Per-provider daily spend over the `days` ending on `end` (default: the
    last complete day). Read from _svc_day, the same curve every demo tool sums,
    so the chart and the tools never disagree about a day."""
    last = end or _ref_end()
    ref = _ref_end()
    out = []
    for d in _days(last - timedelta(days=days - 1), last):
        row: dict[str, Any] = {"date": d.isoformat()}
        for p in provs:
            row[p] = round(sum(_svc_day(s["amount"], s["delta_pct"], d, ref)
                               for s in _PROVIDER_SERVICES[p]), 2)
        out.append(row)
    return out


def dashboard_data(
    days: int = 30,
    provider: str = "all",
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """Full payload for the `finops serve` dashboard in demo mode, in the exact
    shape `_fetch_dashboard_data` returns. Provider- and range-aware: selecting a
    provider filters every figure, and the range scales the windowed views. A
    custom `start`/`end` (Vantage-style date range) overrides `days`: the window
    spans those dates and the daily series ends on `end`. AWS reproduces the
    classic acme-production story; Azure and GCP are smaller real footprints, so
    the provider toggle and active-services table show distinct data. MTD and the
    projection stay month-anchored (calendar figures, not lookback figures).
    """
    provider = (provider or "all").lower()
    provs = _DEMO_PROVIDERS if provider == "all" else [provider]
    provs = [p for p in provs if p in _PROVIDER_SERVICES] or ["aws"]

    # A custom date range overrides the preset lookback. Clamp to sane bounds so a
    # malformed range cannot produce a giant or empty series.
    if start and end and end >= start:
        days = max(1, min((end - start).days + 1, 366))

    # Window figures: summed from the same daily curve the MCP tools read, over
    # the days the range spans. MTD/projection stay month-anchored (they are
    # calendar figures, not lookback figures).
    w_last = min(end, _ref_end()) if end else _ref_end()
    w_first = w_last - timedelta(days=max(days, 1) - 1)
    resources = {(p, s["service"]): s["resources"] for p in provs for s in _PROVIDER_SERVICES[p]}

    # Flatten selected providers' services into the active-services inventory.
    active_services: list[dict[str, Any]] = []
    for r in _window_rows(provs, w_first, w_last):
        active_services.append({
            "service": r["service"],
            "provider": r["provider"],
            "resources": resources[(r["provider"], r["service"])],
            "amount": r["amount"],
            "delta_pct": r["delta_pct"] or 0.0,
        })
    active_services.sort(key=lambda x: -x["amount"])
    window_total = sum(s["amount"] for s in active_services) or 1.0
    for s in active_services:
        s["pct"] = round(s["amount"] / window_total * 100, 1)

    top_services = [
        {"service": s["service"], "amount": s["amount"], "pct": s["pct"]}
        for s in active_services[:8]
    ]

    # Reference 30 days vs the 30 before, the calendar month forecast, and the
    # closed calendar months, all read off the one daily curve.
    ref = _ref_end()
    month_total = round(_sum_days(provs, ref - timedelta(days=_REF_DAYS - 1), ref), 2)
    prior_30 = _sum_days(provs, ref - timedelta(days=2 * _REF_DAYS - 1),
                         ref - timedelta(days=_REF_DAYS))
    delta_pct = _pct(month_total, prior_30) or 0.0
    this_month = month_forecast(provs)
    projected = this_month["projected_month_total"]

    recent_opportunities = [o for p in provs for o in _PROVIDER_OPPS.get(p, [])]
    recent_opportunities.sort(key=lambda o: -o["monthly_saving"])
    opp_total = round(sum(o["monthly_saving"] for o in recent_opportunities), 2)

    recent_savings = [
        {"description": "Moved off-peak VOD encodes to a scheduled g5 pool.",
         "monthly_saving": 8400.00, "resource": "vod-encoder-schedule", "provider": "aws"},
    ] if "aws" in provs else []

    # Verified savings ledger: only changes nable proposed AND confirmed landed on
    # the resource (the cloud now matches nable's recommended config). This is the
    # billable figure, kept strictly separate from "identified/potential".
    verified_ledger = [
        {"description": "CloudFront egress moved to a committed private-pricing tier",
         "resource": "cloudfront-commit", "verified_monthly": 62000.00,
         "confirmed_on": (_TODAY - timedelta(days=4)).isoformat(),
         "proof": "egress now billed at the commit rate; CloudFront line down 8%"},
        {"description": "Rightsized the off-peak `vod-encoder-fleet` g5.4xlarge -> g5.2xlarge",
         "resource": "vod-encoder-fleet", "verified_monthly": 8400.00,
         "confirmed_on": (_TODAY - timedelta(days=9)).isoformat(),
         "proof": "142 encoders now g5.2xlarge off-hours; next-day EC2 line fell $280/day"},
    ] if "aws" in provs else []
    verified_monthly = round(sum(v["verified_monthly"] for v in verified_ledger), 2)

    # Score nudges a little by footprint so switching providers visibly moves it.
    score = 66.0 if "aws" in provs else (81.0 if provs == ["azure"] else 69.0)
    grade = "B" if score >= 70 else "C"

    _today = ref + timedelta(days=1)
    m1 = (_today.replace(day=1) - timedelta(days=1)).replace(day=1)      # last month
    m2 = (m1 - timedelta(days=1)).replace(day=1)                          # two months ago
    last_month = round(_sum_days(provs, *_month_bounds(m1)), 2)
    trend = [
        {"month": m2.strftime("%B"), "actual": round(_sum_days(provs, *_month_bounds(m2)), 2),
         "projected": None},
        {"month": m1.strftime("%B"), "actual": last_month, "projected": last_month},
        {"month": f"{_today.strftime('%B')} (projected)", "actual": None, "projected": projected},
    ]

    # Windowed total (what the range actually spans) and the daily provider series.
    window_total_spend = round(window_total, 2)
    daily = _daily_series(days, provs, end=w_last)

    # AI and GPU as a real number: classify the demo inventory the same way live
    # does and split the six buckets. The per-day category slice is attached at
    # the very end, after every plain sum over the daily rows has run.
    (category_totals, ai_spend_window, ai_spend_pct,
     ai_delta_pct, ai_breakdown_rows) = _demo_category_payload(active_services, window_total_spend)

    # Month TO DATE, not a whole month. This reported `month_total`, the sum of
    # every provider's full monthly figure, so on the 16th the dashboard showed
    # month-to-date equal to the 30-day window and a reader could not tell the
    # two cards apart. Summing the days of the current calendar month out of the
    # series the chart already draws keeps the number consistent with the chart
    # instead of being a second opinion about the same month.
    # Read from the same curve rather than from `daily`, which a short window
    # would cut off before the 1st.
    mtd_total = this_month["month_to_date_usd"]
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

    def _money(n: float) -> str:
        if n >= 1e6: return f"${n/1e6:.1f}M"
        if n >= 1e3: return f"${n/1e3:.1f}k"
        return f"${n:,.0f}"

    # Budgets & alerts: used is month to date, status is judged on the forecast.
    budget_rows = budgets()
    alerts = [
        {"kind": "warn", "title": f"{b['name']} forecast over",
         "body": (f"On track for {_money(b['projected_month_total'])}, "
                  f"{b['projected_pct']:.0f}% of the {_money(b['limit'])} budget")}
        for b in budget_rows if b["status"] == "over"
    ] + [
        {"kind": "info",  "title": "Forecast alert",            "body": f"{provs[0].upper() if provs else 'AWS'} forecast tracking above run rate after the season launch"},
    ]

    # Executive KPI band (tier-1): unit economics + posture, the board-slide row.
    exec_kpis = [
        {"label": "Infra $ / active account", "value": "$0.27", "delta_pct": -5.0, "good_down": True, "sub": "19.1M active accounts"},
        {"label": "CDN cost per TB delivered", "value": "$7.80", "delta_pct": 6.0, "good_down": True, "sub": "petabyte-scale egress"},
        {"label": "Infra % of revenue", "value": "12.1%", "delta_pct": -0.6, "good_down": True, "sub": "$42M MRR"},
        {"label": "Effective savings rate", "value": "24%", "delta_pct": 2.0, "good_down": False, "sub": "vs on-demand list"},
        {"label": "Commitment coverage", "value": "61%", "delta_pct": 4.0, "good_down": False, "sub": "target 80%"},
    ]
    # AI efficiency panel: the wedge no incumbent shows.
    ai_efficiency = {
        "ai_pct_of_spend": 8.8,
        "ai_spend": round(month_total * 0.088, 2),
        "metrics": [
            {"label": "Cost / 1M tokens", "value": "$2.80", "delta_pct": -12.0, "good_down": True},
            {"label": "Cost / 1M recs served", "value": "$1.90", "delta_pct": -8.0, "good_down": True},
            {"label": "GPU encoder utilization", "value": "39%", "delta_pct": 2.0, "good_down": False, "warn": True},
            {"label": "Cache hit rate", "value": "6%", "delta_pct": 0.0, "good_down": False, "warn": True},
        ],
        "callout": "GPU encoders sit at 39% utilization and prompt caching is nearly off. About $44,000/mo is recoverable by scheduling the encoder fleet and caching the catalog context.",
    }

    # "What changed since you last looked": the always-on loop as a glance.
    whats_changed = {
        "since": "Monday",
        "items": [
            {"kind": "up",    "text": "CloudFront egress up $120,800 (20%)", "prompt": "Why did CloudFront egress jump after the season launch?"},
            {"kind": "alert", "text": "S3 request spike, cause unknown",     "prompt": "Explain the $28k S3 request anomaly in the ad-platform account."},
            {"kind": "warn",  "text": "Snowflake budget crossed 100%",       "prompt": "Show our Snowflake budget status and what's driving it over the cap."},
            {"kind": "good",  "text": "$8,400/mo saved, off-peak encoder schedule", "prompt": "Show the savings we've realized in the last week."},
        ],
    }

    # Forecast vs budget with a confidence band, on the same curve as the tools.
    hist = [round(sum(v for k, v in row.items() if k != "date"), 2) for row in daily]
    forecast = [round(_sum_days(provs, d, d), 2)
                for d in _days(ref + timedelta(days=1), ref + timedelta(days=12))]
    limits = {b["provider"]: b["limit"] for b in budget_rows}
    budget = (_ORG_MONTHLY_BUDGET if provider == "all"
              else limits.get(provs[0], round(projected * 1.1, 2)))
    proj_end = projected
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

    # Compute the headline sparklines BEFORE attaching category slices: they sum
    # each daily row's provider values, which only works while a row is date +
    # provider numbers and nothing else.
    sparklines = {
        "spend": _spark(1.0), "mtd": _spark(0.62),
        "forecast": _spark(1.09), "savings": _spark(0.065),
    }
    # Now every numeric sum over `daily` (mtd, sparklines, forecast history) has
    # run, so the per-day category slice is safe to attach.
    _attach_daily_categories(daily, category_totals, window_total_spend)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account_id": _ACCOUNT_ID,
        "user": {"name": "Alex R.", "role": "Admin", "email": "alex@streamco.tv"},
        "exec_kpis": exec_kpis,
        "ai_efficiency": ai_efficiency,
        "whats_changed": whats_changed,
        "forecast_panel": forecast_panel,
        "total_spend_mtd": mtd_total,
        "total_spend_window": window_total_spend,
        "total_spend_last_month": last_month,
        "projected_month_total": projected,
        "forecast_delta_pct": -4.7,
        "delta_pct": delta_pct,
        "finops_grade": grade,
        "finops_score": score,
        "sparklines": sparklines,
        "top_services": top_services,
        "active_services": active_services,
        "daily_series": daily,
        "series_providers": provs,
        # AI and GPU category payload (matches the live /api/data shape).
        "categories_available": True,
        "category_totals": category_totals,
        "ai_spend_window": ai_spend_window,
        "ai_spend_pct": ai_spend_pct,
        "ai_delta_pct": ai_delta_pct,
        "ai_breakdown": ai_breakdown_rows,
        "top_accounts": top_accounts,
        "spend_by_region": spend_by_region,
        "recommendations_table": recommendations,
        "ai_insights": ai_insights,
        "budgets": budget_rows,
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
        "budget_pct_used": round(mtd_total / budget * 100, 1) if budget else None,
        "recent_opportunities": recent_opportunities,
        "suppressed_opportunities": [
            {"description": "RDS metadata-catalog-01 flagged underutilized, but memory sits at 82%. "
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
                {"name": "Tagging & allocation", "grade": "D", "score": 42,
                 "detail": "29% of spend is untagged, mostly shared CDN and data transfer with no owner."},
                {"name": "Commitment coverage", "grade": "C", "score": 61,
                 "detail": "61% of steady compute + CDN on commitments (target 80%); a CloudFront egress commit is open."},
                {"name": "Rightsizing", "grade": "C", "score": 58,
                 "detail": "47 instances flagged; 12 genuine, low-risk resizes after burst + memory checks."},
                {"name": "Idle & waste", "grade": "C", "score": 64,
                 "detail": "3 idle EKS nodes and six always-warm Snowflake warehouses."},
                {"name": "Storage tiering", "grade": "B", "score": 78,
                 "detail": "Most VOD masters lifecycle-managed to Glacier; 2.4 PB still on standard."},
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


_PROVIDER_LABEL = {
    "aws": "AWS", "gcp": "GCP", "azure": "Azure", "kubernetes": "Kubernetes",
    "openai": "OpenAI", "anthropic": "Anthropic", "datadog": "Datadog",
    "snowflake": "Snowflake", "databricks": "Databricks",
}

# One sentence every "what am I connected to" view uses in demo, so
# list_connected_providers, check_connector_health, nable_setup_status and
# what_can_nable_do all tell the same story: nine sample providers, none of the
# user's own accounts.
SAMPLE_PROVIDERS_NOTE = (
    "Demo mode: these are the providers in the StreamCo sample environment, shown with "
    "sample data. They are not accounts the user connected; none of the user's own "
    "accounts are connected. connect_aws, connect_gcp or connect_azure connects a real one.")


def capabilities_text(detailed: bool = False) -> str:
    """what_can_nable_do in demo mode. The live renderer reports what is really
    connected, which in demo is nothing, so it answered "nothing's connected"
    while list_connected_providers answered "nine connected". This says what is
    true: the answers are sample data, here is what the sample covers, and here
    is how to swap it for the user's own account."""
    from .capabilities import TOTAL_TOOLS

    names = ", ".join(_PROVIDER_LABEL.get(p, p) for p in _DEMO_PROVIDERS)
    lines = [
        "## What nable can do (demo mode, sample data)",
        "",
        "This session answers from the StreamCo sample environment: sample data for "
        f"{names}. None of your own accounts are connected, so no answer here is about "
        "your spend.",
        "",
        "Try asking (each one answers from the sample):",
        '- "What did we spend in the last 30 days?"  (get_cost_summary)',
        '- "Why did the bill go up?"  (explain_recent_cost_drivers)',
        '- "Break spend down by team, account or region"  (slice_costs)',
        '- "Any cost anomalies?"  (get_anomalies)',
        '- "What can we save?"  (get_savings_summary, get_rightsizing_recommendations)',
        '- "Where will this month land?"  (forecast_costs)',
        '- "What are we spending on AI?"  (get_llm_costs, optimize_ai_spend)',
        '- "How efficient is our Kubernetes cluster?"  (get_kubernetes_costs)',
        "",
        "To see your own numbers, connect an account right here: connect_aws or "
        "connect_gcp (they detect credentials already on this machine) or connect_azure. "
        "Answers switch from sample data to your account as soon as one is connected.",
        "",
        f"nable ships {TOTAL_TOOLS}+ read-only tools. The ones the sample cannot answer "
        "light up once a real account is connected.",
    ]
    if detailed:
        lines += ["", "### Tools that answer from the sample",
                  ", ".join(sorted(demo_tool_names()))]
    return "\n".join(lines)


def demo_accounts() -> dict[str, list[dict[str, Any]]]:
    """Per-provider account/subscription/org identifiers for the demo, in the
    shape list_accounts returns (provider -> list of account dicts)."""
    # A representative slice of the AWS Org, not the whole thing: a real media
    # company runs hundreds of linked accounts. Showing ~14 named ones plus the
    # count conveys the scale without inventing 312 rows.
    _aws_named = [
        {"id": "481516234203", "name": "Production"},
        {"id": "481516234211", "name": "Streaming-Delivery"},
        {"id": "481516234229", "name": "Ad-Platform"},
        {"id": "481516234237", "name": "Data-Analytics"},
        {"id": "481516234245", "name": "Recommendations"},
        {"id": "481516234253", "name": "Playback"},
        {"id": "481516234261", "name": "Content-Encoding"},
        {"id": "481516234279", "name": "Staging"},
        {"id": "481516234287", "name": "Shared-Services"},
        {"id": "481516234295", "name": "Networking"},
        {"id": "481516234303", "name": "Security-Audit"},
        {"id": "481516234311", "name": "DR"},
        {"id": "481516234329", "name": "Corp-IT"},
        {"id": "481516234337", "name": "Sandbox"},
    ]
    out: dict[str, list[dict[str, Any]]] = {
        "aws": _aws_named + [{"_note": "showing 14 of 312 linked accounts in the Org"}],
        "gcp": [{"billing_account_id": "01A2B3-C4D5E6-F7G8H9", "name": "streamco-billing",
                 "_note": "34 projects under this billing account"}],
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
         "data": {"rows": [{"team": "Streaming Delivery", "metric": 2226000.0}, {"team": "Content Platform", "metric": 1164000.0},
                           {"team": "Ad Platform", "metric": 888000.0}, {"team": "Data & Analytics", "metric": 522000.0}],
                  "total": 4800000.0, "record_count": 4}},
        {"id": 9002, "saved_by": "Alex R.", "saved_at": (_today - timedelta(days=6)).isoformat(),
         "card": {"title": "AI spend by model", "template": "bar", "metric": "Cost", "dimensions": ["model"]},
         "data": {"rows": [{"model": "gpt-4o", "metric": 168000.0}, {"model": "claude-sonnet-4-5", "metric": 102000.0},
                           {"model": "o3", "metric": 62000.0}, {"model": "bedrock", "metric": 40000.0}],
                  "total": 372000.0, "record_count": 4}},
        {"id": 9003, "saved_by": "Alex R.", "saved_at": (_today - timedelta(days=11)).isoformat(),
         "card": {"title": "CDN egress by region", "template": "bar", "metric": "EffectiveCost",
                  "dimensions": ["region"]},
         "data": {"rows": [{"region": "us-east-1", "metric": 274000.0}, {"region": "eu-west-1", "metric": 201000.0},
                           {"region": "us-west-2", "metric": 162000.0}, {"region": "ap-southeast-1", "metric": 88000.0}],
                  "total": 725000.0, "record_count": 4}},
        # Tag- and time-granularity views, so the gallery answers "show me by tag"
        # and "monthly, not just 30 days" without touching a filter UI. Totals tie
        # to the $5.15M month exactly (untagged matches the 29% story).
        {"id": 9004, "saved_by": "Priya S.", "saved_at": (_today - timedelta(days=1)).isoformat(),
         "card": {"title": "Spend by tag: env", "template": "bar", "metric": "EffectiveCost",
                  "dimensions": ["env"]},
         "data": {"rows": [{"env": "production", "metric": 3708000.0}, {"env": "untagged", "metric": 689600.0},
                           {"env": "staging", "metric": 463000.0}, {"env": "dev", "metric": 287000.0}],
                  "total": 5147600.0, "record_count": 4}},
        {"id": 9005, "saved_by": "Priya S.", "saved_at": (_today - timedelta(days=3)).isoformat(),
         "card": {"title": "Monthly spend, last 3 months", "template": "bar", "metric": "EffectiveCost",
                  "dimensions": ["month"]},
         "data": {"rows": [{"month": "May", "metric": 3966000.0}, {"month": "June", "metric": 4407000.0},
                           {"month": "July (MTD)", "metric": 5147600.0}],
                  "total": 13520600.0, "record_count": 3}},
        {"id": 9006, "saved_by": "Alex R.", "saved_at": (_today - timedelta(days=5)).isoformat(),
         "card": {"title": "Untagged spend by account", "template": "bar", "metric": "EffectiveCost",
                  "dimensions": ["account"]},
         "data": {"rows": [{"account": "Streaming-Delivery", "metric": 273600.0}, {"account": "Networking", "metric": 168000.0},
                           {"account": "Shared-Services", "metric": 142000.0}, {"account": "Ad-Platform", "metric": 106000.0}],
                  "total": 689600.0, "record_count": 4}},
    ]


def bedrock_split() -> dict[str, Any]:
    """Demo Bedrock input/output/cache split, consistent with the ~$330 Bedrock
    line in llm_costs(): input-heavy and uncached, which is the signature
    caching finding. Lets optimize_ai_spend fire the prompt-caching lever with
    no credentials."""
    return {
        "input_cost": 35600.0,     # ~89% of the $40,000 Bedrock bill
        "output_cost": 4400.0,
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
        "total_pr_count": 210,
        "ai_pr_count": 168,
        "human_pr_count": 42,
        "ai_share_pct": 80.0,
        "total_llm_spend_usd": 18400.0,
        "by_label": {
            "Claude Opus 4.8": {
                "label": "Claude Opus 4.8", "pr_count": 92, "high": 24, "medium": 48, "low": 20,
                "lines_changed": 38200, "llm_spend_usd": 9200.0, "spend_share_pct": 50.0,
                "cost_per_pr_usd": 100.0,
                "examples": [
                    {"title": "Add per-title CDN egress attribution", "magnitude": "high", "lines": 620, "url": "", "repo": "streamco/streaming-platform"},
                    {"title": "Cache catalog context on the recommender", "magnitude": "high", "lines": 480, "url": "", "repo": "streamco/recommendations"},
                ],
            },
            "Claude Sonnet 4.6": {
                "label": "Claude Sonnet 4.6", "pr_count": 58, "high": 0, "medium": 38, "low": 20,
                "lines_changed": 9400, "llm_spend_usd": 5600.0, "spend_share_pct": 30.4,
                "cost_per_pr_usd": 96.6,
                "examples": [
                    {"title": "Tune the ad-decisioning bid timeout", "magnitude": "medium", "lines": 150, "url": "", "repo": "streamco/ad-platform"},
                ],
            },
            "OpenAI Codex": {
                "label": "OpenAI Codex", "pr_count": 18, "high": 2, "medium": 8, "low": 8,
                "lines_changed": 2600, "llm_spend_usd": 3600.0, "spend_share_pct": 19.6,
                "cost_per_pr_usd": 200.0, "examples": [],
            },
            "Human": {
                "label": "Human", "pr_count": 42, "high": 8, "medium": 18, "low": 16,
                "lines_changed": 14800, "examples": [],
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


def get_demo_response(tool_name: str, args: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """
    Return a demo response for the given tool name, or None if not available.
    Call this at the top of each MCP tool when FINOPS_DEMO_MODE=1. `args` are the
    tool's own arguments, so period and provider filters shape the sample answer.
    """
    fn = DEMO_RESPONSES.get(tool_name)
    if fn is None:
        return None
    import inspect

    result = fn(args or {}) if inspect.signature(fn).parameters else fn()
    result["_demo_mode"] = True
    return result


# ── Agent (AI Analyst) demo safety net ───────────────────────────────────────
# The chat agent can call ~60 tools. Only the DEMO_RESPONSES set self-serves demo
# data; the rest would query real credentials and leak/contradict the demo. This
# layer sits at the single bridge chokepoint (slack_bot.bridge.execute_bridge_tool)
# so that in demo mode NO agent tool call ever reaches a real cloud account:
#   - self-demo / local-only tools -> None (fall through; already safe),
#   - slice_costs -> a synthetic slice built from the demo dataset,
#   - common cost tools -> a demo dict derived from the same numbers,
#   - anything else -> a safe placeholder that names the sample and leaks nothing.

# Tools that already return demo data (or are local-only) on their own path.
_AGENT_SELF_DEMO = set(DEMO_RESPONSES) | {
    "optimize_ai_spend", "list_connected_providers", "list_accounts",
    "compare_providers", "check_connector_health", "whoami", "what_can_nable_do",
}
_AGENT_LOCAL_OK = {"pin_view", "list_pinned_views", "get_pinned_view", "unpin_view"}
# The way out of demo. These run for real in demo mode, so a user trying the sample
# can connect their own account from the same chat; server.py labels the result
# with whether the session has left the sample.
_DEMO_EXIT_TOOLS = {"connect_aws", "connect_gcp", "connect_azure", "connect_opencost",
                    "nable_setup_status"}


def _demo_commitment() -> dict[str, Any]:
    return {
        "coverage_pct": 61.0, "on_demand_pct": 39.0, "target_pct": 80.0,
        "by_provider": {"aws": 63.0, "gcp": 58.0, "snowflake": 55.0},
        "recommendation": (
            "Steady CDN egress and the encoder baseline are uncommitted. A 1-year compute "
            "Savings Plan (~$88k/mo) and a CloudFront committed-egress tier (~$62k/mo) close "
            "most of the gap toward the 80% target."),
        "_demo_mode": True,
    }


def _demo_slice(args: dict[str, Any]) -> dict[str, Any]:
    """Synthetic 'moldable view' slice from the demo dataset, in the shape the
    web Ask tab renders as a pinnable cost card. Summed over the requested
    window from the same curve as every other demo tool; account, region and
    team splits are AWS's (the sample's linked accounts, regions and CUR tags
    are AWS ones), so they add back up to AWS's total for the window."""
    dims = args.get("dimensions") or []
    dim = (str(dims[0]) if dims else "provider").lower()
    metric = args.get("metric") or "EffectiveCost"
    provs = _pick_providers(args.get("provider"), None)
    if provs is None:
        return _not_in_sample(f"Provider '{args.get('provider')}'")
    first, last = demo_window(args)
    wrows = _window_rows(provs, first, last)
    aws_total = sum(r["amount"] for r in wrows if r["provider"] == "aws")
    scope = _scope(provs)
    if dim in ("service", "product", "service_name"):
        key = "service"
        rows = [{"service": r["service"], "metric": r["amount"]} for r in wrows]
    elif dim in ("team", "tag", "owner", "costcenter", "cost_center"):
        key, scope = "team", "AWS (team tag from the CUR)"
        rows = [{"team": k, "metric": v} for k, v in _split(_AWS_TEAM_30D, aws_total).items()]
    elif dim in ("account", "subaccount", "subaccountid", "account_id", "linkedaccount"):
        key, scope = "account", "AWS linked accounts"
        rows = [{"account": a["name"], "metric": round(aws_total * a["share"], 2)}
                for a in _DEMO_ACCOUNTS]
    elif dim in ("region", "regionid", "location"):
        key, scope = "region", "AWS regions"
        rows = [{"region": r["region"], "metric": round(aws_total * r["share"], 2)}
                for r in _DEMO_REGIONS]
    else:
        key = "provider"
        rows = [{"provider": p, "metric": round(sum(r["amount"] for r in wrows
                                                     if r["provider"] == p), 2)}
                for p in provs]
    rows.sort(key=lambda x: -x["metric"])
    total = round(sum(r["metric"] for r in rows), 2)
    limit = args.get("limit")
    if isinstance(limit, int) and limit > 0:
        rows = rows[:limit]
    return {
        "card": {"title": f"{metric} by {key}", "template": "bar", "metric": metric,
                 "dimensions": [key], "period": _period(first, last)},
        "result": {"rows": rows, "total": total, "record_count": len(rows),
                   "metric": metric, "dimensions": [key], "scope": scope},
        "_demo_mode": True,
    }


def _demo_total(args: dict[str, Any] | None = None,
                provs: list[str] | None = None) -> dict[str, Any]:
    first, last = demo_window(args)
    provs = provs or list(_DEMO_PROVIDERS)
    by_provider = {p: round(_sum_days([p], first, last), 2) for p in provs}
    return {"total_usd": round(sum(by_provider.values()), 2), "by_provider": by_provider,
            "period": _period(first, last), "_demo_mode": True}


def compare_providers(args: dict[str, Any] | None = None) -> dict[str, Any]:
    """compare_providers on the sample, over the requested window."""
    args = args or {}
    provs = _pick_providers(None, args.get("category")) or []
    first, last = demo_window(args)
    rows = _window_rows(provs, first, last)
    grand = round(sum(r["amount"] for r in rows), 2)
    out = []
    for p in provs:
        mine = [r for r in rows if r["provider"] == p]
        total = round(sum(r["amount"] for r in mine), 2)
        out.append({
            "provider": p,
            "category": _DEMO_PROVIDER_CATEGORY.get(p, "cloud"),
            "total_usd": total,
            "total_formatted": _usd(total),
            "pct_of_total": round(total / grand * 100, 1) if grand else 0,
            "top_services": [{"service": r["service"], "amount_usd": r["amount"]}
                             for r in mine[:5]],
        })
    out.sort(key=lambda x: -x["total_usd"])
    return {"period": _period(first, last), "grand_total_usd": grand,
            "grand_total_formatted": _usd(grand), "providers": out}


def _costs_by_service(args: dict[str, Any]) -> dict[str, Any]:
    provs = _pick_providers(args.get("provider"), args.get("category"))
    if provs is None:
        return _not_in_sample(f"Provider '{args.get('provider')}'")
    first, last = demo_window(args)
    rows = _window_rows(provs, first, last)
    want = str(args.get("service_filter") or "").strip().lower()
    if want:
        rows = [r for r in rows if want in r["service"].lower()]
        if not rows:
            return _not_in_sample(f"A service matching '{want}'")
    total = round(sum(r["amount"] for r in rows), 2)
    return {"period": _period(first, last), "total_usd": total,
            "services": [{"service": r["service"], "provider": r["provider"],
                          "total_usd": r["amount"]} for r in rows],
            "_demo_mode": True}


def _top_cost_drivers(args: dict[str, Any]) -> dict[str, Any]:
    res = _costs_by_service({k: v for k, v in args.items() if k != "service_filter"})
    if "services" not in res:
        return res
    limit = args.get("limit")
    limit = int(limit) if isinstance(limit, (int, float)) and limit > 0 else 10
    grand = res["total_usd"]
    top = [dict(s, pct_of_total=round(s["total_usd"] / grand * 100, 1) if grand else 0)
           for s in res["services"][:limit]]
    return {"period": res["period"], "top_services": top, "grand_total_usd": grand,
            "grand_total_formatted": _usd(grand), "_demo_mode": True}


def _costs_by_team(args: dict[str, Any]) -> dict[str, Any]:
    provider = str(args.get("provider") or "").strip().lower()
    if provider and provider != "aws":
        out = _not_in_sample(f"Team attribution for '{provider}'")
        out["note"] += " In the sample, team tags come from the AWS CUR."
        return out
    cur = cost_summary_cur(args)
    return {"by_team": cur["by_tag_team"], "untagged_pct": cur["untagged_pct"],
            "total_usd": cur["total_usd"], "period": cur["period"],
            "scope": "AWS (team tags from the CUR)", "_demo_mode": True}


def _cost_trends(args: dict[str, Any]) -> dict[str, Any]:
    provs = _pick_providers(args.get("provider"), args.get("category"))
    if provs is None:
        return _not_in_sample(f"Provider '{args.get('provider')}'")
    first, last = demo_window(args)
    n = (last - first).days + 1
    series = _daily_series(n, provs, end=last)
    for row in series:
        row["total"] = round(sum(v for k, v in row.items() if k != "date"), 2)
    total = round(_sum_days(provs, first, last), 2)
    out: dict[str, Any] = {"period": _period(first, last), "total_usd": total,
                           "providers": provs, "_demo_mode": True}
    if str(args.get("granularity") or "DAILY").upper() == "MONTHLY":
        months: dict[str, float] = {}
        for row in series:
            months[row["date"][:7]] = months.get(row["date"][:7], 0.0) + row["total"]
        out["monthly_series"] = [{"month": m, "total": round(v, 2)} for m, v in months.items()]
    else:
        out["daily_series"] = series
    return out


def _cost_history(args: dict[str, Any]) -> dict[str, Any]:
    provs = _pick_providers(args.get("provider"), None)
    if provs is None:
        return _not_in_sample(f"Provider '{args.get('provider')}'")
    want = str(args.get("service") or "").strip().lower()
    matches = [(p, s) for p in provs for s in _PROVIDER_SERVICES[p]
               if not want or want in s["service"].lower()]
    if not matches:
        return _not_in_sample(f"A service matching '{want}'")
    first, last = demo_window(args)
    ref = _ref_end()
    days = _days(first, last)
    series = [{"date": d.isoformat(),
               "cost_usd": round(sum(_svc_day(s["amount"], s["delta_pct"], d, ref)
                                     for _, s in matches), 2)} for d in days]
    return {"period": _period(first, last), "services": [s["service"] for _, s in matches],
            "total_usd": round(sum(r["cost_usd"] for r in series), 2),
            "history": series, "_demo_mode": True}


def _forecast(args: dict[str, Any]) -> dict[str, Any]:
    """forecast_costs on the sample: an estimate with a range, never a point
    figure presented as fact, anchored on the same actuals the cost tools show."""
    want = str(args.get("service") or "").strip()
    service = None
    if want:
        hits = [s["service"] for p in _DEMO_PROVIDERS for s in _PROVIDER_SERVICES[p]
                if want.lower() in s["service"].lower()]
        if not hits:
            return _not_in_sample(f"A service matching '{want}'")
        service = hits[0]
    provs = [p for p in _DEMO_PROVIDERS
             if service is None or any(s["service"] == service for s in _PROVIDER_SERVICES[p])]
    month = month_forecast(provs, service)
    horizon = args.get("horizon_days")
    horizon = int(horizon) if isinstance(horizon, (int, float)) and horizon > 0 else 30
    horizon = min(horizon, 365)
    today = _ref_end() + timedelta(days=1)
    ahead = _sum_days(provs, today, today + timedelta(days=horizon - 1), service)
    band = min(0.05 + 0.0025 * horizon, 0.30)
    out: dict[str, Any] = {
        "estimate": True,
        "scope": service or _scope(provs),
        **month,
        "horizon_days": horizon,
        "forecast_next_days_usd": round(ahead, 2),
        "forecast_range": {"low": round(ahead * (1 - band), 2),
                           "high": round(ahead * (1 + band), 2)},
        "method": ("Sample-data estimate: this month's actuals to date plus the remaining "
                   "days projected on the recent daily trend. The range is +/-10% on the "
                   "projected days, wider for longer horizons. It is an estimate, not a "
                   "commitment."),
        "_demo_mode": True,
    }
    rng = month["projected_range"]
    note = (f"Sample data, estimate: {month['month']} is on track to finish between "
            f"{_usd(rng['low'])} and {_usd(rng['high'])} (central {_usd(month['projected_month_total'])}), "
            f"with {_usd(month['month_to_date_usd'])} spent in the first "
            f"{month['days_elapsed']} days.")
    if service is None:
        out["budget"] = _ORG_MONTHLY_BUDGET
        out["vs_budget_pct"] = _pct(month["projected_month_total"], _ORG_MONTHLY_BUDGET)
        note += (f" That is {abs(out['vs_budget_pct']):.1f}% "
                 f"{'over' if out['vs_budget_pct'] > 0 else 'under'} the "
                 f"{_usd(_ORG_MONTHLY_BUDGET)} monthly budget.")
    out["note"] = note
    return out


def _budget_status(args: dict[str, Any]) -> dict[str, Any]:
    rows = budgets()
    name = str(args.get("budget_name") or "").strip().lower()
    if name:
        rows = [b for b in rows if name in b["name"].lower()]
        if not rows:
            return _not_in_sample(f"A budget named '{args.get('budget_name')}'")
    return {"budgets": rows, "_demo_mode": True,
            "basis": "used is month-to-date; status is judged on the month-end forecast"}


def _fixed_window(result: dict[str, Any], args: dict[str, Any], *keys: str) -> dict[str, Any]:
    """Say so when a tool whose sample figures cover a fixed 30 days was asked
    for a different window, rather than answer a 7-day question with 30 days."""
    for key in keys:
        v = args.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and int(v) != _REF_DAYS:
            result["sample_window"] = (
                f"The sample's figures for this tool cover a fixed {_REF_DAYS}-day window; "
                f"the requested {key}={int(v)} is not applied.")
    return result


def _savings_summary(args: dict[str, Any]) -> dict[str, Any]:
    d = dashboard_data()
    return {"potential_monthly": d["opportunities_total_saving"],
            "verified_monthly": d["verified_savings"]["monthly"],
            "recommendations": d["recommendations_table"], "_demo_mode": True}


def _savings_ledger(args: dict[str, Any]) -> dict[str, Any]:
    d = dashboard_data()
    return _fixed_window({"ledger": d["verified_savings"]["ledger"],
                          "monthly": d["verified_savings"]["monthly"], "_demo_mode": True},
                         args, "days")


def _savings_recommendations(args: dict[str, Any]) -> dict[str, Any]:
    d = dashboard_data()
    return {"recommendations": d["recommendations_table"],
            "open_potential_usd": d["opportunities_total_saving"], "_demo_mode": True}


def _scorecard(args: dict[str, Any]) -> dict[str, Any]:
    return {"scorecard": dashboard_data()["scorecard"], "_demo_mode": True}


def _roi(args: dict[str, Any]) -> dict[str, Any]:
    d = dashboard_data()
    return _fixed_window({"verified_monthly": d["verified_savings"]["monthly"],
                          "verified_annual": d["verified_savings"]["annual"],
                          "_demo_mode": True}, args, "period_days")


def _ai_kpis(args: dict[str, Any]) -> dict[str, Any]:
    d = dashboard_data()
    return _fixed_window({"metrics": d["ai_efficiency"]["metrics"],
                          "ai_pct_of_spend": d["ai_efficiency"]["ai_pct_of_spend"],
                          "_demo_mode": True}, args, "days")


def _agent_intercepts() -> dict[str, Any]:
    """Tool name -> demo answer builder, called with the tool's arguments. Each
    builder runs only on a hit, so dashboard_data() (heavier) is computed only
    when a tool needs one of its panels."""
    return {
        "get_costs_by_service": _costs_by_service,
        "get_top_cost_drivers": _top_cost_drivers,
        "explain_cost_change": lambda a: cost_drivers({"days": a.get("compare_days")}),
        "get_costs_by_team": _costs_by_team,
        "get_total_spend_all_sources": _demo_total,
        "get_cost_summary_all_accounts": _demo_total,
        "get_saas_spend_summary": lambda a: _demo_total(a, ["datadog", "snowflake", "databricks"]),
        "forecast_costs": _forecast,
        "get_commitment_analysis": lambda a: _demo_commitment(),
        "get_commitment_coverage_by_tag": lambda a: _demo_commitment(),
        "check_budget_status": _budget_status,
        "list_budgets": _budget_status,
        "get_savings_summary": _savings_summary,
        "get_savings_ledger": _savings_ledger,
        "list_savings_recommendations": _savings_recommendations,
        "get_efficiency_scorecard": _scorecard,
        "get_nable_roi": _roi,
        "get_cost_trends": _cost_trends,
        "get_cost_history": _cost_history,
        "get_ai_kpis": _ai_kpis,
    }


_demo_tool_names: "frozenset[str] | None" = None


def demo_tool_names() -> frozenset[str]:
    """The tools worth advertising in demo mode: every tool that answers from the
    sample dataset, plus the connect and setup tools that lead out of it.

    Demo used to advertise all ~198 tools (~48k tokens of definitions) while 107
    of them could only answer "not in the sample dataset". A model picks from
    what it is shown, so it kept reaching for tools that had nothing to say."""
    global _demo_tool_names
    if _demo_tool_names is None:
        _demo_tool_names = frozenset(
            _AGENT_SELF_DEMO | _AGENT_LOCAL_OK | _DEMO_EXIT_TOOLS
            | {"slice_costs"} | set(_agent_intercepts()))
    return _demo_tool_names


def demo_bridge_result(name: str, args: dict[str, Any] | None) -> dict[str, Any] | None:
    """Demo-safe result for an agent tool call, or None to let the real (already
    demo-safe) tool run. Guarantees no agent tool reaches real credentials in
    demo mode: unknown tools get a placeholder, never a live call."""
    args = args or {}
    # The registry tools answer here, with their arguments, rather than in each
    # tool's own demo branch: one place applies the period and provider filters,
    # and a tool that lacks a branch (get_tag_cost_breakdown_cur ran the live
    # CUR query in demo) can no longer slip past.
    if name in DEMO_RESPONSES:
        return get_demo_response(name, args)
    if name in _AGENT_SELF_DEMO or name in _AGENT_LOCAL_OK or name in _DEMO_EXIT_TOOLS:
        return None
    if name == "slice_costs":
        return _demo_slice(args)
    build = _agent_intercepts().get(name)
    if build is not None:
        return build(args)
    return {
        "_demo_mode": True,
        "demo_mode": True,
        "note": (
            "This is the StreamCo sample environment, so that specific detail isn't in the sample "
            "dataset. Ask about total spend, cost drivers, spend by service / team / account / region, "
            "anomalies, rightsizing, commitments, budgets, forecast, savings, or AI and LLM cost."),
    }


def after_connect_in_demo(result: Any) -> Any:
    """Label a connect_* result that ran while the session was in demo mode.

    A connect is how a demo user leaves the sample from chat. Whether it worked
    decides what every later answer is, so the result says it outright: either
    the sample is gone and answers are now the user's own numbers, or nothing
    changed and answers are still sample data."""
    global _real_provider_cache
    _real_provider_cache = None  # re-detect now, not in 30s
    if not isinstance(result, dict):
        return result
    if not is_demo():
        result["_demo_mode"] = False
        result["_demo_exit"] = (
            "Demo mode is off: a real account is connected, so answers from the next "
            "call on are the user's own numbers, not the StreamCo sample data. Earlier "
            "answers in this conversation were sample data; do not mix the two.")
        return result
    result["_demo_mode"] = True
    if result.get("connected") and os.environ.get("FINOPS_DEMO_FORCE", "").lower() in _TRUTHY:
        result["_demo_note"] = (
            "Connected, but FINOPS_DEMO_FORCE=1 keeps this session on sample data. "
            "Restart nable without FINOPS_DEMO_FORCE and FINOPS_DEMO to see real numbers.")
    else:
        result["_demo_note"] = (
            "Still in demo mode: no account is connected yet, so answers remain the "
            "StreamCo sample data. Follow the steps above; nable switches to the "
            "user's real numbers as soon as an account is connected.")
    return result


DEMO_TEXT_HEADER = "Sample data (demo mode): the StreamCo sample environment, not your account."
DEMO_NOTE = (
    "Sample data (demo mode): every figure comes from the StreamCo sample environment, "
    "not the user's account. None of the user's own accounts are connected. To see real "
    "numbers, connect one here with connect_aws, connect_gcp or connect_azure.")


def label_demo(value: Any) -> Any:
    """Stamp a demo-mode answer so no reader, model or human, can take it for
    the user's own numbers: `_demo_mode: true` and a sample-data note on a dict,
    the sample-data header on text, the flag on each dict in a list.

    Applied once, at the server chokepoint, to every tool that answers in demo
    mode. Several tools used to answer with no flag at all (list_connected_providers
    reported nine providers "connected"), so the label depended on each tool
    remembering it."""
    if isinstance(value, dict):
        value.setdefault("_demo_mode", True)
        value.setdefault("_demo_note", DEMO_NOTE)
    elif isinstance(value, str):
        return render_text(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                item.setdefault("_demo_mode", True)
    return value


def render_text(value: Any) -> str:
    """A demo answer as text, for a tool whose declared return type is str.

    The demo layer answers in dicts, and a tool declared `-> str` fails output
    validation on a dict, so the model saw a pydantic error instead of the
    sample. The text always opens with the sample-data label so no reader can
    take it for their own numbers."""
    if isinstance(value, str):
        body = value
    elif isinstance(value, dict) and set(value) <= {"_demo_mode", "demo_mode", "note"}:
        body = str(value.get("note", ""))
    else:
        import json

        body = json.dumps(value, indent=2, default=str)
    if body.lower().startswith("sample data"):
        return body
    return f"{DEMO_TEXT_HEADER}\n\n{body}".rstrip()
