"""The first-run value moment falls back to the free scan, and discloses the
Cost Explorer charge before making it.

It only ever read Cost Explorer: billed at $0.01 a request, and absent from the
least-privilege policy `nable scan --dry-run` hands out. Someone who connected
exactly as told saw no number, and the next screen sent them to create a
CloudFormation key. And the charge was never mentioned.
"""
from __future__ import annotations

import pytest

from finops import welcome as W


@pytest.fixture
def _aws(monkeypatch):
    monkeypatch.setattr(W, "_aws_connected", lambda: True)
    monkeypatch.setattr(W, "_quiet_logs", lambda: None)


def _report(**kw):
    r = {"account_id": "1", "regions_scanned": ["us-east-1", "us-west-2"],
         "regions_timed_out": [], "checks_run": ["ebs", "eips"], "checks_failed": [],
         "total_estimated_monthly_savings": 53.65,
         "findings": [
             {"waste_type": "unattached_ebs_volume", "region": "us-east-1",
              "estimated_monthly_savings": 50.0},
             {"waste_type": "unassociated_elastic_ip", "region": "us-east-1",
              "estimated_monthly_savings": 3.65}]}
    r.update(kw)
    return r


def test_cost_explorer_is_disclosed_before_it_is_called(_aws, monkeypatch, capsys):
    from finops import server
    seen_before_call = []

    async def summary():
        seen_before_call.append(capsys.readouterr().out)
        return {"error": "AccessDeniedException"}

    monkeypatch.setattr(server, "get_cost_summary", summary)
    monkeypatch.setattr(W, "_free_waste_scan", lambda: None)
    W._value_moment_body(demo=False)
    assert seen_before_call and "$0.01 per request" in seen_before_call[0]


def test_a_denied_bill_falls_back_to_the_free_scan(_aws, monkeypatch, capsys):
    from finops import server

    async def summary():
        return {"error": "AccessDeniedException"}

    monkeypatch.setattr(server, "get_cost_summary", summary)
    monkeypatch.setattr(W, "_free_waste_scan", lambda: _report())
    assert W._value_moment_body(demo=False) is True
    out = capsys.readouterr().out
    assert "$53.65/mo" in out
    assert "2 findings across 2 regions" in out
    assert "unattached EBS volume" in out
    assert "free APIs only" in out


def test_an_empty_bill_falls_back_too(_aws, monkeypatch, capsys):
    from finops import server

    async def summary():
        return {"grand_total_usd": 0.0, "grand_by_service": {}}

    monkeypatch.setattr(server, "get_cost_summary", summary)
    monkeypatch.setattr(W, "_free_waste_scan", lambda: _report(checks_failed=[
        {"check": "lambda", "region": "us-east-1", "error_code": "AccessDenied"}]))
    assert W._value_moment_body(demo=False) is True
    assert "1 check(s) could not fully run" in capsys.readouterr().out


def test_a_scan_that_read_nothing_is_not_a_number(_aws, monkeypatch):
    from finops import server

    async def summary():
        return {"error": "x"}

    monkeypatch.setattr(server, "get_cost_summary", summary)
    monkeypatch.setattr(W, "_free_waste_scan", lambda: _report(checks_run=[]))
    assert W._value_moment_body(demo=False) is False


def test_no_cost_explorer_line_without_aws(monkeypatch, capsys):
    from finops import server
    monkeypatch.setattr(W, "_aws_connected", lambda: False)
    monkeypatch.setattr(W, "_quiet_logs", lambda: None)

    async def summary():
        return {"error": "x"}

    monkeypatch.setattr(server, "get_cost_summary", summary)
    called = []
    monkeypatch.setattr(W, "_free_waste_scan", lambda: called.append(1))
    assert W._value_moment_body(demo=False) is False
    assert "Cost Explorer" not in capsys.readouterr().out
    assert called == []
