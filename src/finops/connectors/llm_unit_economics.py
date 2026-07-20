"""
LLM unit economics: cost per customer, feature, deployment, and team.

Users tag their AI API calls with metadata fields. This module aggregates
those tags from the usage APIs that support metadata grouping, and computes:

  - Cost per customer (metadata.customer_id)
  - Cost per feature (metadata.feature or metadata.product_area)
  - Cost per deployment (metadata.deployment_id or metadata.git_sha)
  - Cost per team (metadata.team)
  - Gross margin impact (AI cost as % of customer MRR if provided)

For providers that don't support metadata grouping in their usage API
(most of them), we provide a tagging guide and instrument via:
  - OpenAI: project_id maps to team/product area
  - Anthropic: workspace_id maps to team
  - AWS Bedrock: cost allocation tags on the IAM role/key
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cross-provider project / workspace cost breakdown
# ---------------------------------------------------------------------------

def get_cost_per_project(
    openai_data: dict[str, Any] | None = None,
    anthropic_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Produce a unified cross-provider cost breakdown by project / workspace.

    Parameters
    ----------
    openai_data:
        Result dict from ``openai_usage.get_costs()``.  The function reads
        ``by_project_named`` (preferred) or falls back to ``by_project``.
    anthropic_data:
        Result dict from ``anthropic_usage.get_costs()``.  The function reads
        ``by_workspace`` if present.

    Returns
    -------
    dict with keys:
        by_project   — {project_name: total_usd}  (all providers merged)
        by_provider  — {provider: {project_name: total_usd}}
        total_usd    — grand total across all projects
        coverage     — "full" | "partial" (partial when some providers lack project data)
        tagging_guide — hints for providers where project-level tagging is missing
    """
    by_provider: dict[str, dict[str, float]] = {}
    by_project: dict[str, float] = {}
    missing_project_data: list[str] = []

    # ── OpenAI ──────────────────────────────────────────────────────────────
    if openai_data and openai_data.get("total_usd", 0) > 0:
        oai_projects = (
            openai_data.get("by_project_named")
            or openai_data.get("by_project")
            or {}
        )
        if oai_projects:
            by_provider["openai"] = oai_projects
            for proj, cost in oai_projects.items():
                by_project[proj] = by_project.get(proj, 0.0) + cost
        else:
            missing_project_data.append("openai")
            # Still record the total under a generic bucket
            by_provider["openai"] = {"(all projects)": openai_data.get("total_usd", 0.0)}
            by_project["(all projects)"] = (
                by_project.get("(all projects)", 0.0) + openai_data.get("total_usd", 0.0)
            )

    # ── Anthropic ───────────────────────────────────────────────────────────
    if anthropic_data and anthropic_data.get("total_usd", 0) > 0:
        ant_workspaces = anthropic_data.get("by_workspace") or {}
        if ant_workspaces:
            by_provider["anthropic"] = ant_workspaces
            for ws, cost in ant_workspaces.items():
                by_project[ws] = by_project.get(ws, 0.0) + cost
        else:
            missing_project_data.append("anthropic")
            by_provider["anthropic"] = {"(default workspace)": anthropic_data.get("total_usd", 0.0)}
            by_project["(default workspace)"] = (
                by_project.get("(default workspace)", 0.0) + anthropic_data.get("total_usd", 0.0)
            )

    total_usd = sum(by_project.values())

    tagging_guide: dict[str, str] = {}
    if "openai" in missing_project_data:
        tagging_guide["openai"] = (
            "Create separate OpenAI Projects per team/product area in the dashboard. "
            "Costs roll up per project automatically. "
            "Requires OPENAI_ADMIN_KEY with 'Read billing' scope for cost breakdown."
        )
    if "anthropic" in missing_project_data:
        tagging_guide["anthropic"] = (
            "Create Anthropic Workspaces per team (Enterprise plan). "
            "Set ANTHROPIC_ADMIN_KEY + ANTHROPIC_ORGANIZATION_ID to retrieve workspace costs. "
            "On non-enterprise plans, all usage appears under the default workspace."
        )

    return {
        "total_usd":    round(total_usd, 4),
        "by_project":   {k: round(v, 4) for k, v in
                         sorted(by_project.items(), key=lambda x: x[1], reverse=True)},
        "by_provider":  {
            prov: {k: round(v, 4) for k, v in sorted(proj.items(), key=lambda x: x[1], reverse=True)}
            for prov, proj in by_provider.items()
        },
        "coverage":     "partial" if missing_project_data else "full",
        **({"tagging_guide": tagging_guide} if tagging_guide else {}),
    }


# ---------------------------------------------------------------------------
# Deployment cost correlation
# ---------------------------------------------------------------------------

def get_deployment_cost_correlation(
    github_deployments: list[dict[str, Any]],
    ai_spend_daily: list[dict[str, Any]],
    baseline_days: int = 7,
) -> list[dict[str, Any]]:
    """
    Correlate GitHub deployment events with AI spend changes.

    For each deployment, compute the AI spend in the 24-hour window after the
    deploy versus the rolling baseline spend over the preceding ``baseline_days``
    days.  A large positive delta may indicate the new deployment is more
    expensive (e.g. richer prompts, new model, additional AI features).

    Parameters
    ----------
    github_deployments:
        List of dicts: ``{"sha": str, "deployed_at": str (ISO-8601), "environment": str}``.
    ai_spend_daily:
        List of dicts from ``get_all_llm_costs()["daily"]``:
        ``{"date": "YYYY-MM-DD", "total_usd": float}``.
    baseline_days:
        Number of days before the deployment to use as the rolling baseline.
        Default: 7.

    Returns
    -------
    List of deployment dicts enriched with:
        ``ai_spend_post_deploy``    — total AI spend in 24 h after deploy (USD)
        ``ai_spend_baseline_daily`` — mean daily spend over the prior N days (USD)
        ``ai_spend_delta``          — post_deploy - baseline (+ means cost increase)
        ``ai_spend_delta_pct``      — percentage change vs baseline
        ``signal``                  — "spike" | "drop" | "stable" | "insufficient_data"
    """
    from datetime import timedelta

    # Build a date → cost lookup
    daily_lookup: dict[str, float] = {}
    for entry in ai_spend_daily:
        d = entry.get("date", "")
        v = float(entry.get("total_usd", 0.0))
        if d:
            daily_lookup[d] = v

    enriched: list[dict[str, Any]] = []

    for deploy in github_deployments:
        sha         = deploy.get("sha", "")
        env         = deploy.get("environment", "")
        deployed_at = deploy.get("deployed_at", "")

        try:
            deploy_dt = datetime.fromisoformat(deployed_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            enriched.append({**deploy, "signal": "invalid_timestamp"})
            continue

        deploy_date = deploy_dt.date()

        # 24 h post-deploy spend (the deploy date itself)
        post_day = deploy_date.isoformat()
        ai_spend_post = daily_lookup.get(post_day)

        # Baseline: mean of preceding baseline_days
        baseline_values: list[float] = []
        for offset in range(1, baseline_days + 1):
            prior_day = (deploy_date - timedelta(days=offset)).isoformat()
            if prior_day in daily_lookup:
                baseline_values.append(daily_lookup[prior_day])

        if not baseline_values:
            enriched.append({
                **deploy,
                "ai_spend_post_deploy":    ai_spend_post,
                "ai_spend_baseline_daily": None,
                "ai_spend_delta":          None,
                "ai_spend_delta_pct":      None,
                "signal":                  "insufficient_data",
            })
            continue

        baseline_mean = sum(baseline_values) / len(baseline_values)

        if ai_spend_post is None:
            enriched.append({
                **deploy,
                "ai_spend_post_deploy":    None,
                "ai_spend_baseline_daily": round(baseline_mean, 4),
                "ai_spend_delta":          None,
                "ai_spend_delta_pct":      None,
                "signal":                  "insufficient_data",
            })
            continue

        delta     = ai_spend_post - baseline_mean
        delta_pct = (delta / baseline_mean * 100) if baseline_mean > 0 else 0.0

        if delta_pct > 20:
            signal = "spike"
        elif delta_pct < -20:
            signal = "drop"
        else:
            signal = "stable"

        enriched.append({
            **deploy,
            "ai_spend_post_deploy":    round(ai_spend_post, 4),
            "ai_spend_baseline_daily": round(baseline_mean, 4),
            "ai_spend_delta":          round(delta, 4),
            "ai_spend_delta_pct":      round(delta_pct, 2),
            "signal":                  signal,
        })

    return sorted(enriched, key=lambda d: abs(d.get("ai_spend_delta") or 0), reverse=True)


# ---------------------------------------------------------------------------
# Generic unit economics computation
# ---------------------------------------------------------------------------

def compute_unit_economics(
    total_ai_cost: float,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    """
    Compute AI cost unit economics from total spend and business metrics.

    Parameters
    ----------
    total_ai_cost:
        Total AI/LLM spend in USD for the measurement period.
    metrics:
        Dict with any subset of:
          - ``customers``    (int)   — paying customers in the period
          - ``mau``          (int)   — monthly active users
          - ``api_requests`` (int)   — total API requests handled
          - ``mrr``          (float) — monthly recurring revenue in USD

    Returns
    -------
    Dict with:
        cost_per_customer     — USD per paying customer
        cost_per_mau          — USD per monthly active user
        cost_per_request      — USD per API request (in micro-dollars if small)
        ai_as_pct_of_mrr      — AI spend as % of MRR
        break_even_arpu       — minimum ARPU to keep AI cost < 20% of revenue per customer
        health                — "healthy" | "watch" | "risk"
        flags                 — list of human-readable warnings
    """
    out: dict[str, Any] = {
        "total_ai_cost_usd": round(total_ai_cost, 4),
    }
    flags: list[str] = []

    customers    = metrics.get("customers")
    mau          = metrics.get("mau")
    api_requests = metrics.get("api_requests")
    mrr          = metrics.get("mrr")

    if customers and customers > 0:
        cpc = total_ai_cost / customers
        out["cost_per_customer"] = round(cpc, 4)
        if cpc > 10:
            flags.append(f"Cost per customer is ${cpc:.2f} — high for most SaaS businesses")
        elif cpc > 2:
            flags.append(f"Cost per customer is ${cpc:.2f} — watch as scale increases")

    if mau and mau > 0:
        out["cost_per_mau"] = round(total_ai_cost / mau, 6)

    if api_requests and api_requests > 0:
        cpr = total_ai_cost / api_requests
        out["cost_per_request"]         = round(cpr, 8)
        out["cost_per_1000_requests"]   = round(cpr * 1000, 4)
        if cpr > 0.01:
            flags.append(
                f"Cost per request is ${cpr:.4f} — consider caching, prompt compression, "
                f"or a cheaper model for high-frequency paths"
            )

    if mrr and mrr > 0:
        ai_pct = total_ai_cost / mrr * 100
        out["ai_as_pct_of_mrr"] = round(ai_pct, 2)
        if ai_pct > 30:
            flags.append(
                f"AI costs are {ai_pct:.1f}% of MRR — margin risk. "
                f"Healthy SaaS target is under 15%."
            )
        elif ai_pct > 15:
            flags.append(
                f"AI costs are {ai_pct:.1f}% of MRR — approaching margin risk threshold (15%)."
            )

        if customers and customers > 0:
            # break-even ARPU: AI cost per customer should be <20% of ARPU
            cpc = total_ai_cost / customers
            break_even = cpc / 0.20
            out["break_even_arpu"] = round(break_even, 2)
            current_arpu = mrr / customers
            if current_arpu < break_even:
                flags.append(
                    f"Current ARPU (${current_arpu:.2f}) is below the break-even ARPU "
                    f"(${break_even:.2f}) needed to keep AI cost under 20% of revenue per customer."
                )

    # Overall health signal
    if any("risk" in f.lower() for f in flags):
        health = "risk"
    elif flags:
        health = "watch"
    else:
        health = "healthy"

    out["health"] = health
    if flags:
        out["flags"] = flags

    return out
