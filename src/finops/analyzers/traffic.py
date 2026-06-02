"""
Cross-cloud network-traffic cost model.

Takes per-usage-type cost rows from any cloud, classifies each into
(direction, scope) via traffic_classify, and rolls them up into the
engineer-facing picture: total network spend, the internal-vs-external split,
a per-scope breakdown, the top flows, and a ranked solve playbook.

Pure aggregation: no cloud calls. The caller supplies the rows (e.g. from
Cost Explorer grouped by USAGE_TYPE, or a billing-export query).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .traffic_classify import classify, SOLVE_PLAYBOOK


@dataclass
class TrafficFlow:
    cloud: str
    direction: str          # external | internal | ingress | other
    scope: str              # internet_egress | cross_az | ... (see traffic_classify)
    cost_usd: float
    gb: float = 0.0
    service: str = ""
    region: str = ""
    account_id: str = ""
    usage_type: str = ""    # raw, kept for drill-down


def rows_to_flows(rows: list[dict[str, Any]], cloud: str) -> list[TrafficFlow]:
    """
    Convert raw billing rows into classified TrafficFlow records.

    Each row needs a usage string and a cost. Recognised keys (first present wins):
      usage string: usage_type (AWS) | sku (GCP) | meter (Azure) | "usage"
      cost:         cost_usd | cost
      optional:     gb, service, region, account_id
    Rows that are not network line items (classify -> ("other","other")) are
    dropped, so a caller can pass an unfiltered cost dump.
    """
    flows: list[TrafficFlow] = []
    for r in rows:
        usage = (
            r.get("usage_type") or r.get("sku") or r.get("meter")
            or r.get("usage") or ""
        )
        direction, scope = classify(cloud, usage)
        if direction == "other" and scope == "other":
            continue
        cost = float(r.get("cost_usd", r.get("cost", 0.0)) or 0.0)
        flows.append(TrafficFlow(
            cloud=cloud,
            direction=direction,
            scope=scope,
            cost_usd=round(cost, 2),
            gb=round(float(r.get("gb", 0.0) or 0.0), 2),
            service=r.get("service", "") or "",
            region=r.get("region", "") or "",
            account_id=r.get("account_id", "") or "",
            usage_type=usage,
        ))
    return flows


def build_traffic_breakdown(rows: list[dict[str, Any]], cloud: str, top_n: int = 8) -> dict[str, Any]:
    """
    Roll classified flows into the engineer-facing breakdown.

    Returns: total network cost, the internal/external/ingress split (the
    headline), a per-scope breakdown, the top flows, and a ranked solve
    playbook for the scopes that actually cost money.
    """
    flows = rows_to_flows(rows, cloud)

    if not flows:
        return {
            "cloud": cloud,
            "total_network_cost_usd": 0.0,
            "message": "No network/data-transfer line items found for this period.",
            "by_direction": {},
            "by_scope": {},
            "top_flows": [],
            "solve_playbook": [],
        }

    total = round(sum(f.cost_usd for f in flows), 2)

    by_direction: dict[str, float] = {}
    by_scope: dict[str, float] = {}
    for f in flows:
        by_direction[f.direction] = round(by_direction.get(f.direction, 0.0) + f.cost_usd, 2)
        by_scope[f.scope] = round(by_scope.get(f.scope, 0.0) + f.cost_usd, 2)

    # Headline percentages on billable (non-ingress) network cost.
    billable = round(sum(c for d, c in by_direction.items() if d != "ingress"), 2)
    external = by_direction.get("external", 0.0)
    internal = by_direction.get("internal", 0.0)
    split = {
        "external_usd": external,
        "internal_usd": internal,
        "external_pct": round(external / billable * 100, 1) if billable else 0.0,
        "internal_pct": round(internal / billable * 100, 1) if billable else 0.0,
    }

    top_flows = sorted(flows, key=lambda f: -f.cost_usd)[:top_n]

    # Solve playbook: one entry per scope that costs money, ranked by cost.
    solve = [
        {
            "scope": scope,
            "monthly_cost_usd": cost,
            "fix": SOLVE_PLAYBOOK.get(scope, SOLVE_PLAYBOOK["other"]),
        }
        for scope, cost in sorted(by_scope.items(), key=lambda kv: -kv[1])
        if cost > 0 and scope not in ("ingress", "other")
    ]

    return {
        "cloud": cloud,
        "total_network_cost_usd": total,
        "internal_vs_external": split,
        "by_direction": by_direction,
        "by_scope": dict(sorted(by_scope.items(), key=lambda kv: -kv[1])),
        "top_flows": [
            {
                "scope": f.scope,
                "direction": f.direction,
                "cost_usd": f.cost_usd,
                "service": f.service,
                "region": f.region,
                "usage_type": f.usage_type,
            }
            for f in top_flows
        ],
        "solve_playbook": solve,
    }
