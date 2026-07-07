"""OpenCost as a Kubernetes cost source.

When OpenCost is configured and reachable, nable reads its Allocation API and
returns its real-rate numbers (GPU / network / PV included). When it is absent
or unreachable, allocation_report returns None so the caller falls back to the
built-in list-price estimate. nable only ever READS OpenCost.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from finops.connectors import opencost as oc


def _fake_response(payload, status=200):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    if status >= 400:
        def _raise():
            raise RuntimeError(f"HTTP {status}")
        r.raise_for_status.side_effect = _raise
    return r


# A realistic OpenCost /allocation/compute accumulated response.
_ALLOC = {
    "code": 200,
    "data": [{
        "training": {
            "name": "training", "cpuCost": 10.0, "gpuCost": 900.0, "ramCost": 20.0,
            "pvCost": 5.0, "networkCost": 3.0, "totalCost": 938.0,
            "cpuEfficiency": 0.4, "totalEfficiency": 0.7,
        },
        "web": {
            "name": "web", "cpuCost": 30.0, "gpuCost": 0.0, "ramCost": 12.0,
            "pvCost": 1.0, "networkCost": 2.0, "totalCost": 45.0,
            "cpuEfficiency": 0.6, "totalEfficiency": 0.55,
        },
        "__idle__": {"name": "__idle__", "totalCost": 100.0},
    }],
}


def test_not_configured_returns_none(monkeypatch):
    monkeypatch.delenv("NABLE_OPENCOST_URL", raising=False)
    monkeypatch.delenv("OPENCOST_URL", raising=False)
    assert oc.is_configured() is False
    assert oc.allocation_report() is None


def test_configured_reads_and_normalizes(monkeypatch):
    monkeypatch.setenv("NABLE_OPENCOST_URL", "http://opencost:9003")
    assert oc.is_configured() is True
    with patch("httpx.get", return_value=_fake_response(_ALLOC)) as g:
        rep = oc.allocation_report(window="7d", aggregate="namespace")
    assert rep is not None
    assert rep["source"] == "opencost"
    assert rep["is_estimate"] is False
    # GPU cost is captured and surfaced (the whole point vs the built-in estimate).
    assert rep["gpu_usd"] == 900.0
    # idle is separated from allocated; window total = allocated + idle.
    assert rep["idle_usd"] == 100.0
    assert rep["allocated_usd"] == 983.0
    assert rep["window_total_usd"] == 1083.0
    # rows sorted by total, idle excluded from by_key.
    assert [r["name"] for r in rep["by_key"]] == ["training", "web"]
    assert all(r["name"] != "__idle__" for r in rep["by_key"])
    # 7d window -> monthly projection present.
    assert rep["monthly_cost_usd"] == round(1083.0 / 7 * 30, 2)
    # verify it called the Allocation API with accumulate.
    _, kwargs = g.call_args
    assert kwargs["params"]["accumulate"] == "true"


def test_unreachable_returns_none_for_fallback(monkeypatch):
    monkeypatch.setenv("NABLE_OPENCOST_URL", "http://opencost:9003")
    def _boom(*a, **k):
        raise ConnectionError("no route to host")
    with patch("httpx.get", side_effect=_boom):
        assert oc.allocation_report() is None


def test_bad_payload_returns_none(monkeypatch):
    monkeypatch.setenv("NABLE_OPENCOST_URL", "http://opencost:9003")
    with patch("httpx.get", return_value=_fake_response({"code": 500})):
        assert oc.allocation_report() is None


def test_window_days_parsing():
    assert oc._window_days("7d") == 7
    assert oc._window_days("24h") == 1.0
    assert oc._window_days("today") is None  # non-numeric window -> no projection
