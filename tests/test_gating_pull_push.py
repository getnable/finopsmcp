"""The pull/push licensing line.

Free = ask on demand (cost queries, anomalies, rightsizing findings, single-cloud
views). Pro = it runs for you: proactive alerts + scheduled push, forecasting,
AI unit economics, drafting the fix, and the unified cross-cloud view.

These tests lock in the gates so the free tier can't silently drift back to
giving the whole product away, and so the first value moment stays free.
"""
from __future__ import annotations

import pathlib
import re

import finops.demo_data as D
import finops.license as L
from finops.license import LicenseStatus, PRO_FEATURES, require_pro

NEW_PRO = ["alerts", "forecasting", "ai_unit_economics", "remediation", "cross_cloud"]

# Every tool that must refuse to run on the free tier, and the feature it gates on.
GATED_TOOLS = {
    "compare_providers": "cross_cloud",
    "get_total_spend_all_sources": "cross_cloud",
    "set_alert_policy": "alerts",
    "push_weekly_insight": "alerts",
    "send_weekly_digest_now": "alerts",
    "send_digest_now": "alerts",
    "send_report_now": "alerts",
    "subscribe_to_report": "alerts",
    "push_to_n8n": "alerts",
    "forecast_costs": "forecasting",
    "forecast_azure_costs": "forecasting",
    "forecast_llm_costs": "forecasting",
    "get_llm_unit_economics": "ai_unit_economics",
    "get_llm_unit_economics_full": "ai_unit_economics",
    "get_ai_kpis": "ai_unit_economics",
    "get_ai_engineering_report": "ai_unit_economics",
    "get_ai_spend_monitor": "ai_unit_economics",
    "open_rightsizing_pr": "remediation",
    "open_terraform_tag_pr": "remediation",
}

# The first value moment: these stay free forever. Activation is the bottleneck,
# not generosity, so the connect -> ask -> see-a-dollar-figure path is never gated.
FREE_AHA_TOOLS = [
    "get_cost_summary",
    "get_anomalies",
    "get_rightsizing_recommendations",
    "get_llm_costs",
    "scan_waste_patterns",
]


def _func_src(src: str, name: str) -> str | None:
    m = re.search(r"\n(?:async def|def) " + re.escape(name) + r"\s*\(", src)
    if not m:
        return None
    start = m.start()
    nxt = re.search(r"\n@mcp\.tool|\nasync def |\ndef ", src[start + 1 :])
    end = start + 1 + nxt.start() if nxt else len(src)
    return src[start:end]


def _server_src() -> str:
    import finops.server

    return pathlib.Path(finops.server.__file__).read_text()


def _free(monkeypatch):
    monkeypatch.setattr(
        L, "get_status", lambda: LicenseStatus(mode="free", email="", issued="", message="")
    )
    monkeypatch.setattr(D, "is_demo", lambda: False)


def test_new_features_are_registered_pro():
    for f in NEW_PRO:
        assert f in PRO_FEATURES, f


def test_free_tier_is_blocked_on_each_new_feature(monkeypatch):
    _free(monkeypatch)
    for f in NEW_PRO:
        err = require_pro(f)
        assert err and err["error"] == "pro_required", f


def test_pro_tier_passes_every_new_feature(monkeypatch):
    monkeypatch.setattr(
        L, "get_status", lambda: LicenseStatus(mode="pro", email="x", issued="", message="")
    )
    for f in NEW_PRO:
        assert require_pro(f) is None, f


def test_demo_mode_unlocks_everything(monkeypatch):
    # Free license, demo on: the product must still demo in full to anyone evaluating.
    monkeypatch.setattr(
        L, "get_status", lambda: LicenseStatus(mode="free", email="", issued="", message="")
    )
    monkeypatch.setattr(D, "is_demo", lambda: True)
    for f in NEW_PRO:
        assert require_pro(f) is None, f


def test_every_gated_tool_actually_calls_require_pro():
    src = _server_src()
    for tool, feat in GATED_TOOLS.items():
        body = _func_src(src, tool)
        assert body is not None, f"{tool} not found in server.py"
        assert f'require_pro("{feat}")' in body, f"{tool} is not gated on {feat}"


def test_free_aha_tools_are_never_gated():
    src = _server_src()
    for tool in FREE_AHA_TOOLS:
        body = _func_src(src, tool)
        if body is None:
            continue
        assert "require_pro(" not in body, f"{tool} must stay free (the activation aha)"
