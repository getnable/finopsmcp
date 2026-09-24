"""A subscription whose cost query fails is reported, never folded in as $0."""
from __future__ import annotations

from datetime import date

from finops.connectors import azure_optimize as AO


def _setup(monkeypatch, fail_subs):
    monkeypatch.setattr(AO, "is_configured", lambda: True)
    monkeypatch.setattr(AO, "_get_access_token", lambda: "tok")
    monkeypatch.setattr(AO, "_subscription_ids", lambda: ["s1", "s2"])

    def q(token, sub, body):
        if sub in fail_subs:
            raise RuntimeError("403")
        return [{"ServiceName": "VMs", "Cost": 10.0}]

    monkeypatch.setattr(AO, "_query_cost_management", q)


def test_one_failed_subscription_marks_the_result_partial(monkeypatch):
    _setup(monkeypatch, {"s2"})
    out = AO.get_cost_by_dimension("service", date(2026, 9, 1), date(2026, 9, 20))
    assert out["partial"] is True
    assert out["failed_subscriptions"] == [{"subscription_id": "s2", "error": "RuntimeError"}]
    assert out["total_cost_usd"] == 10.0


def test_every_subscription_failing_is_an_error_not_zero(monkeypatch):
    _setup(monkeypatch, {"s1", "s2"})
    out = AO.get_cost_by_dimension("service", date(2026, 9, 1), date(2026, 9, 20))
    assert "error" in out and "total_cost_usd" not in out
