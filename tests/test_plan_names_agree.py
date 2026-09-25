"""Every surface names the plan the user is on, and the trial for the day it is.

Dogfooding a Pro key: the MCP start banner said "nable Team", license-status
said "Team (Pro)", the status resource called the trial a "Team trial" that
kept "Team features ($25/mo)", and the upgrade notes on email delivery and
commitment recommendations said "Team feature ($25/mo)". Team is the
$1,000/mo plan; Pro is $25/mo. The banner's deadline, "Subscribe before day
N", computed days_remaining + 1, a number with no calendar behind it, and
require_pro told a user whose trial had ended to start a "7-day free trial".
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

import finops.license as L
from finops import server
from finops.license import LicenseStatus


def _trial(days_left: int = 3) -> LicenseStatus:
    start = date.today() - timedelta(days=L._TRIAL_DAYS - days_left)
    return LicenseStatus(mode="trial", email="", issued=start.isoformat(),
                         message="", days_remaining=days_left)


def _free_after_trial(days_ago: int = 5) -> LicenseStatus:
    start = date.today() - timedelta(days=L._TRIAL_DAYS + days_ago)
    return LicenseStatus(mode="free", email="", issued=start.isoformat(),
                         message="Free tier active.", days_remaining=0)


def _key(mode: str) -> LicenseStatus:
    return LicenseStatus(mode=mode, email="buyer@example.com", issued="2026-09-01",
                         message="", expires="2026-10-08")


def _banner(status) -> str:
    return "\n".join(server._plan_banner_lines(status))


# ── MCP start banner ──────────────────────────────────────────────────────────

def test_a_pro_key_is_greeted_as_pro():
    text = _banner(_key("pro"))
    assert "nable Pro" in text
    assert "nable Team" not in text


def test_a_team_key_is_greeted_as_team_not_free():
    text = _banner(_key("team"))
    assert "nable Team" in text
    assert "free tier" not in text.lower()


def test_the_trial_banner_prints_the_last_day_not_a_day_number():
    st = _trial(3)
    text = _banner(st)
    assert "Subscribe before day" not in text
    assert L.fmt_day(L.trial_last_day(st)) in text
    assert "Team" not in text
    assert L._PRO_CHECKOUT_URL in text


def test_the_free_banner_does_not_promise_a_free_month():
    text = _banner(_free_after_trial())
    assert "First month free" not in text
    assert L._PRO_CHECKOUT_URL in text


# ── finops://status resource ──────────────────────────────────────────────────

def _status_resource(monkeypatch, st) -> str:
    async def _active(subset=None):
        return {"aws": object()}
    monkeypatch.setattr(server, "_active", _active)
    monkeypatch.setattr(server, "get_status", lambda: st)
    return asyncio.run(server.connection_status())


def test_the_status_resource_calls_the_trial_a_pro_trial(monkeypatch):
    text = _status_resource(monkeypatch, _trial(2))
    assert "Team trial" not in text and "Team features" not in text
    assert "Pro trial" in text
    assert L.fmt_day(L.trial_last_day(_trial(2))) in text


@pytest.mark.parametrize("mode,name", [("pro", "Pro"), ("team", "Team")])
def test_the_status_resource_names_a_paid_plan(monkeypatch, mode, name):
    text = _status_resource(monkeypatch, _key(mode))
    assert f"Plan: {name}" in text


# ── license-status ────────────────────────────────────────────────────────────

def test_license_status_names_pro_and_shows_expiry(monkeypatch, capsys):
    import finops.setup_wizard as W
    monkeypatch.setattr(L, "check_license", lambda: _key("pro"))
    W._run_license_status()
    out = capsys.readouterr().out
    assert "Team (Pro)" not in out
    assert "Pro ($25/mo)" in out
    assert "2026-10-08" in out


def test_license_status_shows_the_trial_last_day(monkeypatch, capsys):
    import finops.setup_wizard as W
    st = _trial(4)
    monkeypatch.setattr(L, "check_license", lambda: st)
    W._run_license_status()
    out = capsys.readouterr().out
    assert L.fmt_day(L.trial_last_day(st)) in out
    assert L._PRO_CHECKOUT_URL in out


# ── require_pro for a user whose trial or key ended ───────────────────────────

def _gate(monkeypatch, st) -> dict:
    monkeypatch.setattr(L, "get_status", lambda: st)
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: False)
    err = L.require_pro("ticket_creation")
    assert err is not None
    return err


def test_an_ended_trial_is_not_offered_a_new_free_trial(monkeypatch):
    err = _gate(monkeypatch, _free_after_trial(60))
    assert "free trial" not in err["message"].lower()
    assert "trial ended" in err["message"].lower()


def test_an_expired_key_is_told_it_expired(monkeypatch):
    st = LicenseStatus(mode="invalid", email="buyer@example.com", issued="2025-01-01",
                       message="License key expired on 2025-02-01.", expires="2025-02-01")
    err = _gate(monkeypatch, st)
    assert "free trial" not in err["message"].lower()
    assert "expired on 2025-02-01" in err["message"]


# ── other copy that named the wrong plan ──────────────────────────────────────

def test_no_tool_calls_a_pro_feature_a_team_feature():
    from pathlib import Path
    src = Path(server.__file__).resolve().parent
    for rel in ("tools/notifications.py", "tools/commitments.py", "tools/meta.py",
                "server.py", "notifications/onboarding_email.py"):
        text = (src / rel).read_text(encoding="utf-8")
        assert "Team feature ($25/mo)" not in text, rel
        assert "Team trial" not in text, rel
        assert "Keep Team features" not in text, rel
        assert "keep Team features" not in text, rel


def test_the_trial_ending_email_links_the_pro_checkout_once():
    from finops.notifications import onboarding_email as oe
    html = oe.trial_ending_html(3)
    assert L._PRO_CHECKOUT_URL in html
    assert "buy.stripe.com/eVq14" not in html
    assert "first month free" not in html.lower()


def test_the_welcome_trial_line_counts_the_days(monkeypatch):
    st = _trial(2)
    line = L.trial_line(st)
    assert "2 days left" in line
    assert L.fmt_day(L.trial_last_day(st)) in line
    assert "7-day" not in line


def test_the_welcome_banner_reads_the_trial_day(monkeypatch):
    import finops.welcome as wl
    monkeypatch.setattr(L, "get_status", lambda: _trial(2))
    text = "\n".join(wl._plan_lines())
    assert "2 days left" in text
    assert "7-day free trial" not in text


def test_the_welcome_banner_after_the_trial_says_it_ended(monkeypatch):
    import finops.welcome as wl
    monkeypatch.setattr(L, "get_status", lambda: _free_after_trial(3))
    text = "\n".join(wl._plan_lines())
    assert "trial ended" in text
    assert "all features unlocked" not in text.lower()
