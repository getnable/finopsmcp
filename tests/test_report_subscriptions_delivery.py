"""A report subscription on an install with no scheduler must say so.

The cron moved to the hosted layer in 0.8.211. subscribe_to_report kept
answering "Slack delivery is active. Reports check every 5 minutes", so a
weekly report asked for in chat was saved and never sent, with nothing telling
the user why.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

import finops.server  # noqa: F401  (tools register against the server)
from finops.tools import notifications as nt


@pytest.fixture
def _allowed(monkeypatch):
    monkeypatch.setattr(nt._srv, "require_pro", lambda *a, **k: None)
    monkeypatch.setattr(nt._srv, "require_role", lambda *a, **k: None)
    import finops.notifications.reports as reports
    monkeypatch.setattr(reports, "create_subscription", lambda **kw: {
        "id": 7, "name": kw["name"], "cron": "0 9 * * 1", "sections": kw["sections"]})


def _call(fn, **kw):
    out = fn(**kw)
    return asyncio.run(out) if inspect.isawaitable(out) else out


def test_open_install_has_no_scheduler():
    assert nt._scheduler_installed() is False


def test_open_install_says_the_report_will_not_send_itself(_allowed):
    out = _call(nt.subscribe_to_report, name="Weekly", sections=["spend"],
                slack_channels=["#finops"])
    assert out["created"] is True and out["delivery"] == "on_request"
    assert "will not send on its own" in out["message"]
    assert "send_report_now(subscription_id=7)" in out["note"]
    assert "active" not in out["message"].lower()
    assert "every 5 minutes" not in out["note"]


def test_a_scheduler_host_keeps_the_scheduled_answer(_allowed, monkeypatch):
    monkeypatch.setattr(nt, "_scheduler_installed", lambda: True)
    out = _call(nt.subscribe_to_report, name="Weekly", sections=["spend"],
                slack_channels=["#finops"])
    assert out["delivery"] == "scheduled" and "scheduled (cron: 0 9 * * 1)" in out["message"]


def test_no_surface_sells_scheduled_reports_the_install_cannot_send():
    from pathlib import Path
    src = Path(nt.__file__).resolve().parents[1]
    for rel in ("server.py", "license.py", "setup_wizard.py", "capabilities.py",
                "notifications/onboarding_email.py"):
        text = (src / rel).read_text(encoding="utf-8").lower()
        assert "scheduled email report" not in text, rel
        assert "scheduled email digest" not in text, rel


def test_handshake_reports_nables_version_not_the_sdks():
    from finops import __version__
    from finops.server import mcp
    opts = mcp._mcp_server.create_initialization_options()
    assert opts.server_name == "nable" and opts.server_version == __version__
