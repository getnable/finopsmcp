"""A "locked on free" list names only what is actually locked today.

The AI/agent features are on a temporary free hold (_HOLD_AI_UNGATE), so
commitment recommendations, forecasts, remediation PRs and the Ledger run free.
The free-tier banner, the pro_required message, the status resource, the
trial-ending email and the README still listed commitment recommendations as
Pro, and three of the banner and pro_required lines (line-item CUR, Azure
resource detail, business metrics) name features that were never in
PRO_FEATURES at all. A free user was told they lacked things they already had.

get_nable_roi read `lic.plan`, an attribute LicenseStatus does not have, so the
tool answered every real call with an AttributeError. Its tests passed because
they stubbed get_status with an object that had `.plan`.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

import finops.license as L
from finops import server
from finops.license import LicenseStatus

_NEVER_GATED = ("Line-item CUR", "Azure resource-level", "Unit economics",
                "business metrics", "Business metrics")


def _free() -> LicenseStatus:
    start = date.today() - timedelta(days=L._TRIAL_DAYS + 3)
    return LicenseStatus(mode="free", email="", issued=start.isoformat(),
                         message="Free tier active.", days_remaining=0)


def _held_copy() -> list[str]:
    return [L.PRO_FEATURE_COPY[f] for f in L.PRO_FEATURE_COPY if L._is_ungated_now(f)]


def test_locked_features_is_pro_features_minus_the_hold():
    locked = set(L.locked_features())
    assert locked == {f for f in L.PRO_FEATURES if not L._is_ungated_now(f)}
    if L._HOLD_AI_UNGATE:
        assert "commitment_recommendations" not in locked


def test_every_gated_feature_has_copy():
    assert set(L.PRO_FEATURES) <= set(L.PRO_FEATURE_COPY)


def test_the_free_banner_lists_only_locked_features():
    text = "\n".join(server._plan_banner_lines(_free()))
    locked_part = text.split("Locked on free tier", 1)[1]
    for phrase in _NEVER_GATED:
        assert phrase not in text, phrase
    for held in _held_copy():
        assert held not in locked_part, held
    for f in L.locked_features():
        assert L.PRO_FEATURE_COPY[f] in locked_part


def test_pro_required_lists_only_locked_features(monkeypatch):
    monkeypatch.setattr(L, "get_status", _free)
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: False)
    msg = L.require_pro("ticket_creation")["message"]
    for phrase in _NEVER_GATED:
        assert phrase not in msg, phrase
    for held in _held_copy():
        assert held not in msg, held


def test_the_status_resource_pitch_lists_only_locked_features(monkeypatch):
    async def _active(subset=None):
        return {"aws": object()}
    monkeypatch.setattr(server, "_active", _active)
    monkeypatch.setattr(server, "get_status", _free)
    text = asyncio.run(server.connection_status())
    if L._HOLD_AI_UNGATE:
        assert "commitment recommendations" not in text.split("adds", 1)[-1].lower()
    assert "Slack anomaly alerts" not in text


def test_the_trial_ending_email_lists_only_locked_features():
    from finops.notifications import onboarding_email as oe
    html = oe.trial_ending_html(3)
    if L._HOLD_AI_UNGATE:
        assert "Commitment purchase recommendations" not in html
    for f in L.locked_features():
        assert L.PRO_FEATURE_COPY[f] in html


def test_the_readme_faq_does_not_sell_a_free_feature_as_pro():
    from pathlib import Path
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    faq = readme.split("**Is nable free?**", 1)[1].split("\n\n", 1)[0]
    if L._HOLD_AI_UNGATE:
        assert "commitment recommendations are Pro" not in faq
        assert "and commitment recommendations are Pro" not in faq
    assert "$25/mo" in faq


# ── get_nable_roi ──────────────────────────────────────────────────────────────

@pytest.fixture
def _db(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "t.db"))
    import finops.storage.db as db_mod
    db_mod._ENGINE = None
    yield
    db_mod._ENGINE = None


@pytest.mark.parametrize("mode,cost", [("free", 0.0), ("trial", 0.0), ("pro", 25.0),
                                       ("team", 1000.0)])
def test_get_nable_roi_runs_on_a_real_license_status(_db, monkeypatch, mode, cost):
    st = LicenseStatus(mode=mode, email="", issued=date.today().isoformat(),
                       message="", days_remaining=3 if mode == "trial" else -1)
    monkeypatch.setattr(server, "get_status", lambda: st)
    out = asyncio.run(server.get_nable_roi())
    assert "error" not in out, out
    assert out["plan"] == mode
    assert out["monthly_cost_usd"] == cost
    if mode == "free" and L._HOLD_AI_UNGATE:
        assert "auto-remediation" not in out["summary"]
        assert "verified savings tracking" not in out["summary"]
