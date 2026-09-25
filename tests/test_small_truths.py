"""Small answers that were not quite true, or not quite findable.

- NABLE_NO_UPDATE_CHECK did nothing; only FINOPS_NO_UPDATE_CHECK stopped the
  PyPI check, while every other nable switch answers to its NABLE_ name.
- `nable whoami` and `nable plan`, the first guesses of a buyer checking what
  they paid for, were unknown commands.
- create_anomaly_tickets with no tracker configured answered
  {"tickets_created": 0}, which reads as "nothing needed a ticket".
- get_org_cost_summary with AWS not connected answered "No accounts found in
  organization", which reads as an empty org rather than no connection.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

import finops.server  # noqa: F401  (tools register against the server)
import finops.setup_wizard as W


def _call(fn, **kw):
    out = fn(**kw)
    return asyncio.run(out) if inspect.isawaitable(out) else out


@pytest.mark.parametrize("var", ["NABLE_NO_UPDATE_CHECK", "FINOPS_NO_UPDATE_CHECK"])
def test_either_update_check_switch_turns_it_off(monkeypatch, var):
    from finops import update_check
    for v in ("NABLE_NO_UPDATE_CHECK", "FINOPS_NO_UPDATE_CHECK", "FINOPS_AIRGAP",
              "NABLE_NO_TELEMETRY", "DO_NOT_TRACK"):
        monkeypatch.delenv(v, raising=False)
    assert update_check._disabled() is False
    monkeypatch.setenv(var, "1")
    assert update_check._disabled() is True


def test_the_update_check_switches_are_documented():
    from pathlib import Path
    root = Path(W.__file__).resolve().parents[2]
    text = (root / "docs" / "DEPLOY.md").read_text(encoding="utf-8")
    assert "NABLE_NO_UPDATE_CHECK" in text and "FINOPS_NO_UPDATE_CHECK" in text


@pytest.mark.parametrize("cmd", ["whoami", "plan"])
def test_whoami_and_plan_show_the_license(monkeypatch, cmd):
    seen: list = []
    monkeypatch.setattr(W, "_run_license_status", lambda: seen.append(cmd))
    import contextlib
    import io
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        W.main([cmd])
    assert seen == [cmd]


def test_anomaly_tickets_with_no_tracker_say_none_is_configured(monkeypatch):
    from finops.tools import tickets as tk
    monkeypatch.setattr(tk._srv, "require_pro", lambda *a, **k: None)
    for v in ("JIRA_BASE_URL", "JIRA_API_TOKEN", "JIRA_USER_EMAIL", "JIRA_PROJECT_KEY",
              "LINEAR_API_KEY", "LINEAR_TEAM_ID", "GITHUB_TOKEN", "GITHUB_FINOPS_REPO"):
        monkeypatch.delenv(v, raising=False)
    out = _call(tk.create_anomaly_tickets)
    assert out["tickets_created"] == 0
    assert "no ticket tracker" in out["message"].lower()
    assert "JIRA_BASE_URL" in out["message"]


def test_org_summary_without_aws_says_aws_is_not_connected(monkeypatch):
    from finops.tools import attribution as at
    monkeypatch.setattr(at._srv, "require_pro", lambda *a, **k: None)
    monkeypatch.setattr(at, "_aws_credentials_present", lambda: False)
    out = _call(at.get_org_cost_summary)
    assert "No accounts found" not in str(out)
    assert "not connected" in out["message"]


def test_org_summary_with_aws_but_no_org_says_what_it_needs(monkeypatch):
    from finops.connectors import aws_org
    from finops.tools import attribution as at
    monkeypatch.setattr(at._srv, "require_pro", lambda *a, **k: None)
    monkeypatch.setattr(at, "_aws_credentials_present", lambda: True)
    monkeypatch.setattr(aws_org, "org_cost_summary", lambda **k: {
        "error": "No accounts found in organization", "org_total_usd": 0})
    out = _call(at.get_org_cost_summary)
    assert "organizations:ListAccounts" in out["message"]
    assert "org_total_usd" not in out
