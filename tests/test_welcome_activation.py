"""
Activation onboarding tests.

PostHog showed the cliff: of people who install, ~84% never connect a provider
and never see a number, because the old `finops welcome` only printed a dollar
figure when the user handed over cloud credentials. If they skipped, they saw
nothing and left. These tests lock in the fix:

  1. The value moment tells the truth in demo mode (sample data, not "your account").
  2. A user who skips the credential step STILL sees a number (demo fallback).
  3. Ambient AWS credentials trigger a one-keystroke real scan, no menu.

Scans are stubbed so these stay fast and never touch a network or a real cloud.
"""
from __future__ import annotations

import os

import pytest

import finops.welcome as w


@pytest.fixture(autouse=True)
def _clean_demo_env():
    """The value moment sets FINOPS_DEMO on os.environ; keep tests isolated."""
    keys = ("FINOPS_DEMO", "FINOPS_DEMO_MODE")
    before = {k: os.environ.get(k) for k in keys}
    yield
    for k, v in before.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture()
def stub_scans(monkeypatch):
    """Stub the three server scans the value moment runs, so no network is hit."""
    import finops.server as server

    async def _summary():
        return {"total_usd": 12000.0,
                "by_service": {"Amazon EC2": 7000.0, "Amazon Bedrock": 3000.0, "Amazon S3": 2000.0}}

    async def _idle():
        return {"total_monthly_waste_usd": 400.0}

    async def _ai():
        return {"addressable_savings_monthly_usd": 950.0}

    monkeypatch.setattr(server, "get_cost_summary", _summary)
    monkeypatch.setattr(server, "list_idle_resources", _idle)
    monkeypatch.setattr(server, "optimize_ai_spend", _ai)


def test_value_moment_demo_is_truthful(stub_scans, capsys):
    ok = w._show_value_moment(demo=True)
    out = capsys.readouterr().out
    assert ok is True
    assert "sample data" in out
    assert "scanned your account" not in out  # must not claim it read a real account
    assert "$12,000" in out


def test_value_moment_returns_false_on_empty(monkeypatch):
    import finops.server as server

    async def _empty():
        return {"total_usd": 0.0, "by_service": {}}

    async def _none():
        return None

    monkeypatch.setattr(server, "get_cost_summary", _empty)
    monkeypatch.setattr(server, "list_idle_resources", _none)
    monkeypatch.setattr(server, "optimize_ai_spend", _none)
    assert w._show_value_moment(demo=True) is False


def test_skip_still_shows_a_number(monkeypatch, capsys):
    """The core regression: declining the credential step must not dead-end."""
    monkeypatch.setattr("finops.setup_wizard._configure_claude_desktop", lambda *a, **k: None)

    async def _no_ambient(self):
        return False

    monkeypatch.setattr("finops.connectors.aws.AWSConnector.is_configured", _no_ambient)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")  # Enter -> "4" skip

    calls = []
    monkeypatch.setattr(w, "_show_value_moment", lambda demo=False: calls.append(demo) or (demo is True))

    w.run_welcome_flow(demo=False)
    out = capsys.readouterr().out

    assert calls == [True]  # only the demo fallback ran, no real scan
    assert "Here's nable on a sample bill" in out
    assert "finops setup aws" in out  # clear next step offered


def test_ambient_aws_offers_one_keystroke_real_scan(monkeypatch):
    """With ambient AWS creds, accept the prompt -> real scan, no menu, no demo."""
    monkeypatch.setattr("finops.setup_wizard._configure_claude_desktop", lambda *a, **k: None)

    async def _ambient(self):
        return True

    monkeypatch.setattr("finops.connectors.aws.AWSConnector.is_configured", _ambient)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")

    calls = []
    monkeypatch.setattr(w, "_show_value_moment", lambda demo=False: calls.append(demo) or True)

    w.run_welcome_flow(demo=False)

    assert calls == [False]  # one real scan, no menu, no demo fallback


def test_ambient_aws_declined_falls_through_to_menu_then_demo(monkeypatch):
    """Ambient creds present but user says no -> menu, skip, demo fallback."""
    monkeypatch.setattr("finops.setup_wizard._configure_claude_desktop", lambda *a, **k: None)

    async def _ambient(self):
        return True

    monkeypatch.setattr("finops.connectors.aws.AWSConnector.is_configured", _ambient)
    answers = iter(["n", ""])  # decline Y/n, then Enter -> skip menu
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))

    calls = []
    monkeypatch.setattr(w, "_show_value_moment", lambda demo=False: calls.append(demo) or (demo is True))

    w.run_welcome_flow(demo=False)

    assert calls == [True]  # declined real scan never ran; demo fallback did
