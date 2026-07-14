"""The pull/push licensing line.

Free = ask on demand (cost queries, anomalies, rightsizing findings, and the full
multi-cloud normalized view across every provider). Pro = it runs for you:
proactive alerts + scheduled push, forecasting, AI unit economics, drafting the fix.

These tests lock in the gates so the free tier can't silently drift back to
giving the whole product away, and so the first value moment stays free.
"""
from __future__ import annotations

import pathlib
import re

import finops.demo_data as D
import finops.license as L
from finops.license import LicenseStatus, PRO_FEATURES, require_pro

NEW_PRO = ["alerts", "forecasting", "ai_unit_economics", "remediation"]

# Every tool that must refuse to run on the free tier, and the feature it gates on.
GATED_TOOLS = {
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
    # Breadth is the wedge, not the upsell: the unified multi-cloud view is free.
    "compare_providers",
    "get_total_spend_all_sources",
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
    # Tools now live in server.py AND the per-family modules under finops/tools/
    # (server.py was split up). Concatenate all of them so a tool's body is found
    # wherever it was extracted to.
    import finops.server

    server_path = pathlib.Path(finops.server.__file__)
    src = server_path.read_text()
    tools_dir = server_path.parent / "tools"
    for f in sorted(tools_dir.glob("*.py")):
        if f.name != "__init__.py":
            src += "\n" + f.read_text()
    return src


def _free(monkeypatch):
    monkeypatch.setattr(
        L, "get_status", lambda: LicenseStatus(mode="free", email="", issued="", message="")
    )
    monkeypatch.setattr(D, "is_demo", lambda: False)


def test_new_features_are_registered_pro():
    for f in NEW_PRO:
        assert f in PRO_FEATURES, f


def test_free_tier_gating_respects_the_ai_ungate_hold(monkeypatch):
    # Temporary hold (2026-07-10): the AI/agent features run free while early
    # users get set up; everything else still gates on the free tier.
    _free(monkeypatch)
    for f in NEW_PRO:
        err = require_pro(f)
        if f in L._UNGATED_AI_FEATURES:
            assert err is None, f"{f} is on the temporary free hold, must not gate"
        else:
            assert err and err["error"] == "pro_required", f


def test_upsell_does_not_advertise_ungated_features(monkeypatch):
    # A free user hitting a still-gated feature must not see the temporarily-free
    # AI/agent features listed as paid unlocks (they already have them).
    _free(monkeypatch)
    msg = require_pro("ticket_creation")["message"]
    for banned in ("Budget Guard", "The Ledger", "fix as a pull request",
                   "Savings Plan recommendations"):
        assert banned not in msg, f"upsell leaked ungated feature: {banned}"
    assert "Auto-create Jira" in msg  # a genuinely gated feature still shows


def test_regating_ai_features_restores_the_gate(monkeypatch):
    # The hold is reversible: turn it off and the AI features gate again, proving
    # they stay wired into PRO_FEATURES for when the paid model ships.
    _free(monkeypatch)
    monkeypatch.setattr(L, "_HOLD_AI_UNGATE", False)
    for f in L._UNGATED_AI_FEATURES:
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
