"""Tests for the Azure optimization features (clean-room REST), mocking the Azure
REST layer so no live calls are made."""
import sys
import types
from datetime import date

import finops.connectors.azure_optimize as ao


def _force_auth(monkeypatch):
    monkeypatch.setattr(ao, "is_configured", lambda: True)
    monkeypatch.setattr(ao, "_get_access_token", lambda: "fake-token")


# ── Advisor ───────────────────────────────────────────────────────────────────

def test_advisor_parses_savings_and_sorts(monkeypatch):
    _force_auth(monkeypatch)
    items = [
        {"properties": {
            "category": "Cost", "impact": "Low",
            "shortDescription": {"problem": "Idle disk", "solution": "Delete disk"},
            "extendedProperties": {"annualSavingsAmount": "120", "savingsCurrency": "USD"},
            "resourceMetadata": {"resourceId": "/subscriptions/s1/disks/d1"}}},
        {"properties": {
            "category": "Cost", "impact": "High",
            "shortDescription": {"problem": "Oversized VM", "solution": "Resize to D2s_v3"},
            "extendedProperties": {"annualSavingsAmount": "2400", "recommendationType": "Resize",
                                   "currentSku": "D4s_v3", "targetSku": "D2s_v3"},
            "resourceMetadata": {"resourceId": "/subscriptions/s1/vms/vm1"}}},
        {"properties": {"category": "HighAvailability"}},  # non-cost, must be skipped
    ]
    monkeypatch.setattr(ao, "_arm_get_all", lambda url, tok: items)

    out = ao.get_advisor_cost_recommendations(subscription_id="s1")
    assert out["total_recommendations"] == 2  # the HA one is dropped
    assert out["total_annual_savings_usd"] == 2520.0
    assert out["total_monthly_savings_usd"] == 210.0
    # highest savings first
    assert out["recommendations"][0]["annual_savings_usd"] == 2400.0
    assert out["recommendations"][0]["target_sku"] == "D2s_v3"


# ── VM rightsizing ──────────────────────────────────────────────────────────────

def _vm(name, size="Standard_D4s_v3"):
    return {"id": f"/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/{name}",
            "location": "eastus", "properties": {"hardwareProfile": {"vmSize": size}}}


def test_vm_rightsizing_classifies_idle_underutilized_and_skips_bursty(monkeypatch):
    _force_auth(monkeypatch)
    vms = [_vm("vmIdle"), _vm("vmUnder"), _vm("vmBursty"), _vm("vmHealthy"), _vm("vmStopped")]
    monkeypatch.setattr(ao, "_list_vms", lambda tok, sub: vms)

    cpu = {
        "vmIdle": (1.0, 8.0),       # idle: low avg + low peak
        "vmUnder": (12.0, 40.0),    # underutilized: low avg, moderate peak
        "vmBursty": (15.0, 85.0),   # bursts -> NOT flagged
        "vmHealthy": (60.0, 90.0),  # busy -> NOT flagged
        "vmStopped": (None, None),  # no metrics -> skipped
    }
    monkeypatch.setattr(ao, "_vm_cpu_stats",
                        lambda tok, vm_id, days: cpu[vm_id.rsplit("/", 1)[-1]])

    # real per-VM cost join: $300 over a 30-day window -> $300/mo each
    def fake_costs(start, end, subscription_id=None, min_cost_usd=0.0, limit=0):
        return {"resources": [
            {"resource_id": v["id"], "cost_usd": 300.0} for v in vms
        ]}
    monkeypatch.setattr("finops.connectors.azure_detail.get_resource_costs", fake_costs)

    out = ao.get_vm_rightsizing(subscription_id="s1", lookback_days=30)
    flagged = {v["vm_name"]: v for v in out["vms"]}
    assert set(flagged) == {"vmIdle", "vmUnder"}
    assert flagged["vmIdle"]["classification"] == "idle"
    assert flagged["vmIdle"]["estimated_monthly_savings_usd"] == 300.0   # 100% of cost
    assert flagged["vmIdle"]["estimated_monthly_savings_is_upper_bound"] is True
    assert flagged["vmUnder"]["classification"] == "underutilized"
    assert flagged["vmUnder"]["estimated_monthly_savings_usd"] == 150.0  # 50% of cost
    assert out["total_estimated_monthly_savings_usd"] == 450.0


def test_vm_rightsizing_handles_missing_cost_join(monkeypatch):
    _force_auth(monkeypatch)
    monkeypatch.setattr(ao, "_list_vms", lambda tok, sub: [_vm("vmIdle")])
    monkeypatch.setattr(ao, "_vm_cpu_stats", lambda tok, vid, days: (1.0, 5.0))

    def boom(*a, **k):
        raise RuntimeError("cost API down")
    monkeypatch.setattr("finops.connectors.azure_detail.get_resource_costs", boom)

    out = ao.get_vm_rightsizing(subscription_id="s1")
    assert out["total_flagged"] == 1
    # cost unknown -> savings 0, still surfaced honestly (not a fabricated number)
    assert out["vms"][0]["current_monthly_cost_usd"] == 0.0
    assert out["vms"][0]["estimated_monthly_savings_usd"] == 0.0


# ── Native budgets ──────────────────────────────────────────────────────────────

def test_native_budgets_consumption_and_status(monkeypatch):
    _force_auth(monkeypatch)
    items = [
        {"name": "prod-monthly", "properties": {
            "amount": 1000, "currentSpend": {"amount": 950}, "timeGrain": "Monthly", "category": "Cost"}},
        {"name": "dev-monthly", "properties": {
            "amount": 500, "currentSpend": {"amount": 510}, "timeGrain": "Monthly", "category": "Cost"}},
        {"name": "team-monthly", "properties": {
            "amount": 2000, "currentSpend": {"amount": 100}, "timeGrain": "Monthly", "category": "Cost"}},
    ]
    monkeypatch.setattr(ao, "_arm_get_all", lambda url, tok: items)

    out = ao.get_native_budgets(subscription_id="s1")
    by = {b["name"]: b for b in out["budgets"]}
    assert by["prod-monthly"]["consumed_pct"] == 95.0 and by["prod-monthly"]["status"] == "warning"
    assert by["dev-monthly"]["consumed_pct"] == 102.0 and by["dev-monthly"]["status"] == "exceeded"
    assert by["team-monthly"]["status"] == "ok"
    assert set(out["over_or_warning"]) == {"prod-monthly", "dev-monthly"}


# ── Native forecast ─────────────────────────────────────────────────────────────

def test_forecast_splits_actual_and_forecast(monkeypatch):
    _force_auth(monkeypatch)
    monkeypatch.setattr(ao, "_subscription_ids", lambda: ["s1"])

    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"properties": {
                "columns": [{"name": "Cost"}, {"name": "CostStatus"}, {"name": "Currency"}],
                "rows": [[100.0, "Actual", "USD"], [40.0, "Forecast", "USD"], [10.0, "Forecast", "USD"]]}}
    fake_httpx = types.ModuleType("httpx")
    fake_httpx.post = lambda url, json, headers, timeout: _Resp()
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    out = ao.forecast_costs(subscription_id="s1", end_date=date(2026, 6, 30))
    assert out["actual_to_date_usd"] == 100.0
    assert out["forecast_remaining_usd"] == 50.0
    assert out["projected_total_usd"] == 150.0


# ── Cost by dimension ───────────────────────────────────────────────────────────

def test_cost_by_dimension_groups_and_sorts(monkeypatch):
    _force_auth(monkeypatch)
    rows = [
        {"ResourceGroupName": "rg-prod", "Cost": 800.0},
        {"ResourceGroupName": "rg-dev", "Cost": 200.0},
        {"ResourceGroupName": "rg-prod", "Cost": 100.0},
    ]
    monkeypatch.setattr(ao, "_query_cost_management", lambda tok, sub, body: rows)

    out = ao.get_cost_by_dimension("resource_group", date(2026, 5, 1), date(2026, 6, 1), subscription_id="s1")
    assert out["azure_dimension"] == "ResourceGroupName"
    assert out["breakdown"][0] == {"name": "rg-prod", "cost_usd": 900.0}
    assert out["total_cost_usd"] == 1100.0
    assert out["distinct_values"] == 2


def test_cost_by_dimension_rejects_unknown():
    out = ao.get_cost_by_dimension("nonsense", date(2026, 5, 1), date(2026, 6, 1))
    assert "Unknown dimension" in out["error"]
