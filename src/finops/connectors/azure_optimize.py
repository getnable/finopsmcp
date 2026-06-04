"""
Azure optimization and intelligence, built nable's way (clean-room from the Azure
REST APIs, no heavy mgmt SDKs). Reuses the token + httpx helpers from
azure_detail so auth and error handling stay consistent.

Five capabilities, each the Azure parallel of what nable already does for AWS:

  - get_advisor_cost_recommendations  Azure Advisor cost recs (Microsoft-computed
                                      annual savings), the Azure parallel of
                                      Compute Optimizer / Trusted Advisor.
  - get_vm_rightsizing                Idle and oversized VMs from Azure Monitor CPU,
                                      with real per-VM cost joined from Cost
                                      Management. Parallel of idle-EC2 + rightsizing.
  - get_native_budgets                Reads budgets the user already set in Azure
                                      (Consumption Budgets API), with consumption %.
  - forecast_costs                    Azure Cost Management's own forecast endpoint
                                      (Microsoft's model), not a statistical guess.
  - get_cost_by_dimension             Group spend by any Azure dimension
                                      (service, resource group, location, meter).

All functions return a dict and never raise: failures degrade to {"error": ...}.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .azure_detail import (
    _MGMT_BASE,
    _auth_headers,
    _error,
    _float,
    _get_access_token,
    _query_cost_management,
    _str,
    _subscription_ids,
    is_configured,
)

log = logging.getLogger(__name__)

_ADVISOR_API_VER = "2023-01-01"
_COMPUTE_API_VER = "2023-09-01"
_METRICS_API_VER = "2023-10-01"
_BUDGETS_API_VER = "2023-11-01"
_FORECAST_API_VER = "2023-11-01"

# Idle and underutilized CPU thresholds for VM rightsizing. An idle VM (very low
# average CPU and a low peak) is a deallocate/delete candidate; an underutilized
# VM (low average but some headroom used) is a downsize candidate. A high peak
# means the VM bursts and is NOT safe to flag, the same guard nable applies to EC2.
_IDLE_AVG_CPU_PCT = 3.0
_IDLE_MAX_CPU_PCT = 15.0
_UNDERUTIL_AVG_CPU_PCT = 20.0
_UNDERUTIL_MAX_CPU_PCT = 50.0
# Downsizing one VM tier roughly halves compute cost; deallocating an idle VM
# removes the compute cost entirely (disks may remain, so this is an upper bound).
_DOWNSIZE_SAVINGS_FRACTION = 0.5


# ── shared REST GET helper (paginated) ────────────────────────────────────────

def _arm_get_all(url: str, token: str) -> list[dict]:
    """GET an Azure Resource Manager list endpoint, following `nextLink`.

    Returns the concatenated `value` arrays. Never raises: on error it logs and
    returns what it has so far.
    """
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx is required. Run: pip install httpx")

    headers = _auth_headers(token)
    out: list[dict] = []
    while url:
        try:
            resp = httpx.get(url, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("Azure ARM GET failed (%s): %s", url.split("?")[0], exc)
            break
        out.extend(data.get("value", []))
        url = data.get("nextLink") or ""
    return out


# ── 1. Azure Advisor cost recommendations ─────────────────────────────────────

def get_advisor_cost_recommendations(
    subscription_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Azure Advisor recommendations in the Cost category, with annual savings.

    Advisor is Azure's native recommendation engine (rightsizing, idle resources,
    reservation/savings-plan purchases). Microsoft pre-computes the savings, so we
    surface them directly rather than re-deriving.
    """
    if not is_configured():
        return _error("Azure not configured.")
    try:
        token = _get_access_token()
    except RuntimeError as exc:
        return _error(str(exc))

    subs = [subscription_id] if subscription_id else _subscription_ids()
    recs: list[dict] = []
    total_annual_savings = 0.0

    for sub in subs:
        url = (
            f"{_MGMT_BASE}/subscriptions/{sub}/providers/Microsoft.Advisor/recommendations"
            f"?api-version={_ADVISOR_API_VER}&$filter=Category eq 'Cost'"
        )
        for item in _arm_get_all(url, token):
            props = item.get("properties", {})
            if _str(props.get("category")).lower() != "cost":
                continue
            ext = props.get("extendedProperties", {}) or {}
            annual = _float(ext.get("annualSavingsAmount") or ext.get("savingsAmount"))
            total_annual_savings += annual
            short = props.get("shortDescription", {}) or {}
            recs.append({
                "recommendation": _str(short.get("solution") or short.get("problem")),
                "problem": _str(short.get("problem")),
                "impact": _str(props.get("impact")),
                "recommendation_type": _str(ext.get("recommendationType") or props.get("recommendationTypeId")),
                "resource_id": _str((props.get("resourceMetadata") or {}).get("resourceId")),
                "current_sku": _str(ext.get("currentSku")),
                "target_sku": _str(ext.get("targetSku")),
                "annual_savings_usd": round(annual, 2),
                "currency": _str(ext.get("savingsCurrency")) or "USD",
                "subscription_id": sub,
            })

    recs.sort(key=lambda r: r["annual_savings_usd"], reverse=True)
    out = {
        "recommendations": recs[:limit],
        "total_recommendations": len(recs),
        "total_annual_savings_usd": round(total_annual_savings, 2),
        "total_monthly_savings_usd": round(total_annual_savings / 12, 2),
        "subscription_id": subscription_id or ",".join(subs),
        "source": "azure_advisor",
    }
    if not recs:
        out["permission_hint"] = (
            "No Advisor cost recommendations returned. This is normal if Azure has "
            "nothing to suggest, but if you expect recommendations the service principal "
            "may lack the 'Reader' role. Grant it: az role assignment create "
            "--assignee <client-id> --role Reader --scope /subscriptions/<sub-id>."
        )
    return out


# ── 2. VM rightsizing via Azure Monitor ───────────────────────────────────────

def _list_vms(token: str, sub: str) -> list[dict]:
    url = (
        f"{_MGMT_BASE}/subscriptions/{sub}/providers/Microsoft.Compute/virtualMachines"
        f"?api-version={_COMPUTE_API_VER}"
    )
    return _arm_get_all(url, token)


def _vm_cpu_stats(token: str, vm_id: str, lookback_days: int) -> tuple[float | None, float | None]:
    """Return (avg_cpu_pct, max_cpu_pct) for a VM over the lookback window.

    Reads the Percentage CPU metric from Azure Monitor at hourly granularity,
    then averages the hourly averages and takes the peak of the hourly maxima.
    Returns (None, None) when no datapoints exist (e.g. a stopped VM).
    """
    try:
        import httpx
    except ImportError:
        return None, None
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    url = (
        f"{_MGMT_BASE}{vm_id}/providers/Microsoft.Insights/metrics"
        f"?api-version={_METRICS_API_VER}"
        f"&metricnames=Percentage CPU"
        f"&timespan={start.isoformat()}/{end.isoformat()}"
        f"&interval=PT1H&aggregation=Average,Maximum"
    )
    try:
        resp = httpx.get(url, headers=_auth_headers(token), timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.debug("Azure Monitor CPU fetch failed for %s: %s", vm_id, exc)
        return None, None

    avgs: list[float] = []
    maxs: list[float] = []
    for metric in data.get("value", []):
        for series in metric.get("timeseries", []):
            for dp in series.get("data", []):
                if dp.get("average") is not None:
                    avgs.append(_float(dp.get("average")))
                if dp.get("maximum") is not None:
                    maxs.append(_float(dp.get("maximum")))
    if not avgs:
        return None, None
    return sum(avgs) / len(avgs), (max(maxs) if maxs else None)


def get_vm_rightsizing(
    subscription_id: str | None = None,
    lookback_days: int = 14,
    limit: int = 100,
    max_vms_scanned: int = 200,
) -> dict[str, Any]:
    """Flag idle and oversized Azure VMs from Azure Monitor CPU, with real cost.

    Idle VM: average CPU below the idle threshold AND a low peak, so it is a
    deallocate/delete candidate. Underutilized VM: low average but some real peak
    usage, so it is a downsize candidate (~50% compute saving). A high peak means
    the VM bursts and is left alone, the same burst guard nable uses for EC2.

    Per-VM monthly cost is joined from Cost Management so savings are real dollars,
    not a guess. VMs with no CPU datapoints (stopped/deallocated) are skipped.
    """
    if not is_configured():
        return _error("Azure not configured.")
    try:
        token = _get_access_token()
    except RuntimeError as exc:
        return _error(str(exc))

    # Real per-VM cost over the same window, joined by resource id.
    cost_by_resource: dict[str, float] = {}
    try:
        from .azure_detail import get_resource_costs
        end = date.today()
        start = end - timedelta(days=lookback_days)
        detail = get_resource_costs(start, end, subscription_id=subscription_id, min_cost_usd=0.0, limit=100000)
        for r in detail.get("resources", []):
            rid = _str(r.get("resource_id")).lower()
            if rid:
                cost_by_resource[rid] = cost_by_resource.get(rid, 0.0) + _float(r.get("cost_usd"))
    except Exception as exc:
        log.debug("VM cost join unavailable: %s", exc)

    subs = [subscription_id] if subscription_id else _subscription_ids()
    findings: list[dict] = []
    total_monthly_savings = 0.0
    days = max(1, lookback_days)

    # List every VM first, then scan the COSTLIEST ones for CPU metrics. Each VM
    # needs its own Azure Monitor call, so scanning an entire large estate would be
    # a slow N+1 (hundreds of serial calls). Prioritizing by cost focuses the
    # expensive metric reads on the VMs where rightsizing actually saves money.
    all_vms: list[tuple[str, dict, str, float]] = []
    for sub in subs:
        for vm in _list_vms(token, sub):
            vm_id = _str(vm.get("id"))
            if not vm_id:
                continue
            window_cost = cost_by_resource.get(vm_id.lower(), 0.0)
            all_vms.append((sub, vm, vm_id, window_cost))
    all_vms.sort(key=lambda t: t[3], reverse=True)

    vms_listed = len(all_vms)
    to_scan = all_vms[:max_vms_scanned]
    metrics_seen = 0

    for sub, vm, vm_id, window_cost in to_scan:
        avg_cpu, max_cpu = _vm_cpu_stats(token, vm_id, lookback_days)
        if avg_cpu is None:
            continue  # stopped/deallocated, or no metrics access; do not flag
        metrics_seen += 1
        size = _str((vm.get("properties", {}).get("hardwareProfile", {}) or {}).get("vmSize"))
        monthly_cost = round(window_cost / days * 30, 2)

        classification = None
        saving_fraction = 0.0
        action = ""
        if avg_cpu < _IDLE_AVG_CPU_PCT and (max_cpu is None or max_cpu < _IDLE_MAX_CPU_PCT):
            classification = "idle"
            saving_fraction = 1.0
            action = "Deallocate or delete (idle). Disks may persist, so savings are an upper bound."
        elif avg_cpu < _UNDERUTIL_AVG_CPU_PCT and (max_cpu is None or max_cpu < _UNDERUTIL_MAX_CPU_PCT):
            classification = "underutilized"
            saving_fraction = _DOWNSIZE_SAVINGS_FRACTION
            action = "Downsize one VM tier."
        if classification is None:
            continue

        est_savings = round(monthly_cost * saving_fraction, 2)
        total_monthly_savings += est_savings
        findings.append({
            "vm_name": vm_id.rsplit("/", 1)[-1],
            "resource_id": vm_id,
            "vm_size": size,
            "location": _str(vm.get("location")),
            "avg_cpu_pct": round(avg_cpu, 1),
            "max_cpu_pct": round(max_cpu, 1) if max_cpu is not None else None,
            "classification": classification,
            "current_monthly_cost_usd": monthly_cost,
            "estimated_monthly_savings_usd": est_savings,
            "estimated_monthly_savings_is_upper_bound": classification == "idle",
            "recommendation": action,
            "subscription_id": sub,
        })

    findings.sort(key=lambda f: f["estimated_monthly_savings_usd"], reverse=True)
    out: dict[str, Any] = {
        "vms": findings[:limit],
        "total_flagged": len(findings),
        "total_estimated_monthly_savings_usd": round(total_monthly_savings, 2),
        "total_estimated_annual_savings_usd": round(total_monthly_savings * 12, 2),
        "vms_listed": vms_listed,
        "vms_scanned": len(to_scan),
        "lookback_days": lookback_days,
        "subscription_id": subscription_id or ",".join(subs),
        "note": (
            "Savings join real per-VM cost from Cost Management when available; "
            "if cost data is missing, monthly_cost is 0 and savings cannot be quantified. "
            "Idle savings are an upper bound because attached disks may persist."
        ),
        "source": "azure_monitor",
    }
    if vms_listed > len(to_scan):
        out["scan_truncated"] = vms_listed - len(to_scan)
        out["scan_hint"] = (
            f"Scanned the {len(to_scan)} costliest of {vms_listed} VMs to bound the "
            f"number of Azure Monitor calls. Raise max_vms_scanned or scope to a "
            f"subscription for the rest."
        )
    # Listed VMs but Azure Monitor returned no CPU for any of them. The most common
    # cause is a missing role, so tell the user how to fix it rather than silently
    # reporting "nothing found".
    if len(to_scan) > 0 and metrics_seen == 0:
        out["permission_hint"] = (
            "Listed VMs but got no CPU data from Azure Monitor for any of them. This "
            "usually means the service principal is missing the 'Monitoring Reader' role. "
            "Grant it on each subscription: "
            "az role assignment create --assignee <client-id> --role 'Monitoring Reader' "
            "--scope /subscriptions/<sub-id>. (If every VM is deallocated, an empty "
            "result is expected.)"
        )
    if vms_listed == 0:
        out["permission_hint"] = (
            "No VMs were listed. If you have VMs, the service principal likely lacks the "
            "'Reader' role. Grant it: az role assignment create --assignee <client-id> "
            "--role Reader --scope /subscriptions/<sub-id>."
        )
    return out


# ── 3. Native budgets (Consumption Budgets API) ───────────────────────────────

def get_native_budgets(subscription_id: str | None = None) -> dict[str, Any]:
    """Read the budgets the user already set in Azure (Consumption Budgets API).

    nable has its own budget system, but customers who set budgets in the Azure
    Portal expect nable to see them. This reads those native budgets and reports
    consumption against each.
    """
    if not is_configured():
        return _error("Azure not configured.")
    try:
        token = _get_access_token()
    except RuntimeError as exc:
        return _error(str(exc))

    subs = [subscription_id] if subscription_id else _subscription_ids()
    budgets: list[dict] = []
    for sub in subs:
        url = (
            f"{_MGMT_BASE}/subscriptions/{sub}/providers/Microsoft.Consumption/budgets"
            f"?api-version={_BUDGETS_API_VER}"
        )
        for item in _arm_get_all(url, token):
            props = item.get("properties", {}) or {}
            amount = _float(props.get("amount"))
            spent = _float((props.get("currentSpend") or {}).get("amount"))
            pct = round(spent / amount * 100, 1) if amount > 0 else 0.0
            status = "ok"
            if pct >= 100:
                status = "exceeded"
            elif pct >= 80:
                status = "warning"
            budgets.append({
                "name": _str(item.get("name")),
                "amount_usd": round(amount, 2),
                "current_spend_usd": round(spent, 2),
                "consumed_pct": pct,
                "status": status,
                "time_grain": _str(props.get("timeGrain")),
                "category": _str(props.get("category")),
                "subscription_id": sub,
            })

    budgets.sort(key=lambda b: b["consumed_pct"], reverse=True)
    return {
        "budgets": budgets,
        "total_budgets": len(budgets),
        "over_or_warning": [b["name"] for b in budgets if b["status"] in ("exceeded", "warning")],
        "subscription_id": subscription_id or ",".join(subs),
        "source": "azure_consumption_budgets",
    }


# ── 4. Native forecast (Cost Management forecast endpoint) ─────────────────────

def forecast_costs(
    subscription_id: str | None = None,
    end_date: date | None = None,
) -> dict[str, Any]:
    """Forecast spend to a date using Azure Cost Management's own forecast model.

    Posts to the Cost Management forecast endpoint, which returns actual rows for
    days already billed and forecast rows for future days, with a CostStatus
    column. We sum both to a projected total for the window. Defaults to forecasting
    to the end of the current month.
    """
    if not is_configured():
        return _error("Azure not configured.")
    try:
        import httpx
    except ImportError:
        return _error("httpx is required. Run: pip install httpx")
    try:
        token = _get_access_token()
    except RuntimeError as exc:
        return _error(str(exc))

    today = date.today()
    start = today.replace(day=1)
    if end_date is None:
        # last day of current month
        nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        end_date = nxt - timedelta(days=1)

    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": start.isoformat(), "to": end_date.isoformat()},
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
        },
        "includeActualCost": True,
        "includeFreshPartialCost": False,
    }

    subs = [subscription_id] if subscription_id else _subscription_ids()
    headers = _auth_headers(token)
    per_sub: list[dict] = []
    grand_actual = 0.0
    grand_forecast = 0.0

    for sub in subs:
        url = (
            f"{_MGMT_BASE}/subscriptions/{sub}/providers/Microsoft.CostManagement/forecast"
            f"?api-version={_FORECAST_API_VER}"
        )
        try:
            resp = httpx.post(url, json=body, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("Azure forecast failed for sub %s: %s", sub, exc)
            continue
        props = data.get("properties", data)
        cols = {c.get("name"): i for i, c in enumerate(props.get("columns", []))}
        ci = cols.get("Cost", 0)
        si = cols.get("CostStatus")
        actual = forecast = 0.0
        for row in props.get("rows", []):
            amt = _float(row[ci])
            statuses = _str(row[si]).lower() if si is not None and si < len(row) else "forecast"
            if statuses == "actual":
                actual += amt
            else:
                forecast += amt
        grand_actual += actual
        grand_forecast += forecast
        per_sub.append({
            "subscription_id": sub,
            "actual_to_date_usd": round(actual, 2),
            "forecast_remaining_usd": round(forecast, 2),
            "projected_total_usd": round(actual + forecast, 2),
        })

    return {
        "period": f"{start} to {end_date}",
        "actual_to_date_usd": round(grand_actual, 2),
        "forecast_remaining_usd": round(grand_forecast, 2),
        "projected_total_usd": round(grand_actual + grand_forecast, 2),
        "by_subscription": per_sub,
        "subscription_id": subscription_id or ",".join(subs),
        "source": "azure_cost_management_forecast",
    }


# ── 5. Cost by dimension (service, resource group, location, meter) ───────────

_ALLOWED_DIMENSIONS = {
    "service": "ServiceName",
    "resource_group": "ResourceGroupName",
    "location": "ResourceLocation",
    "meter": "MeterCategory",
    "meter_subcategory": "MeterSubCategory",
}


def get_cost_by_dimension(
    dimension: str,
    start_date: date,
    end_date: date,
    subscription_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Group Azure spend by an arbitrary dimension.

    dimension is one of: service, resource_group, location, meter,
    meter_subcategory. Maps to the corresponding Azure Cost Management dimension
    name and returns a sorted breakdown.
    """
    az_dim = _ALLOWED_DIMENSIONS.get(dimension.lower())
    if not az_dim:
        return _error(
            f"Unknown dimension '{dimension}'. Use one of: {', '.join(sorted(_ALLOWED_DIMENSIONS))}."
        )
    if not is_configured():
        return _error("Azure not configured.")
    try:
        token = _get_access_token()
    except RuntimeError as exc:
        return _error(str(exc))

    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": start_date.isoformat(), "to": end_date.isoformat()},
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": az_dim}],
        },
    }

    subs = [subscription_id] if subscription_id else _subscription_ids()
    by_value: dict[str, float] = {}
    total = 0.0
    for sub in subs:
        try:
            rows = _query_cost_management(token, sub, body)
        except Exception as exc:
            log.warning("Azure cost-by-%s failed for sub %s: %s", dimension, sub, exc)
            continue
        for row in rows:
            key = _str(row.get(az_dim)) or "__unknown__"
            cost = _float(row.get("Cost", row.get("totalCost", 0)))
            by_value[key] = by_value.get(key, 0.0) + cost
            total += cost

    ranked = sorted(by_value.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "dimension": dimension,
        "azure_dimension": az_dim,
        "breakdown": [{"name": k, "cost_usd": round(v, 4)} for k, v in ranked[:limit]],
        "total_cost_usd": round(total, 4),
        "distinct_values": len(by_value),
        "period": f"{start_date} to {end_date}",
        "subscription_id": subscription_id or ",".join(subs),
        "source": "azure_cost_management",
    }
