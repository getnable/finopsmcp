"""
Tests for the finops serve web dashboard.

Tests server startup, /health, /api/data, dashboard HTML structure,
and port-conflict handling — without requiring live cloud credentials.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from http.server import HTTPServer
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _free_port() -> int:
    """Return an available port on localhost."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def mock_dashboard_data():
    """Mock out _fetch_dashboard_data so tests never call AWS."""
    data = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "account_id": "123456789012",
        "total_spend_mtd": 1234.56,
        "total_spend_last_month": 1000.00,
        "projected_month_total": 1543.20,
        "delta_pct": 23.5,
        "finops_grade": "B",
        "finops_score": 82.0,
        "top_services": [
            {"service": "Amazon EC2", "amount": 800.0, "pct": 64.8},
            {"service": "Amazon S3", "amount": 200.0, "pct": 16.2},
        ],
        "opportunities_count": 3,
        "opportunities_total_saving": 420.0,
        "savings_achieved_mtd": 150.0,
        "anomalies_open": 1,
        "budget_pct_used": 62.0,
        "recent_opportunities": [
            {
                "description": "Downsize m5.2xlarge",
                "monthly_saving": 200.0,
                "resource": "i-abc123",
                "effort": "LOW",
                "impact": "high",
                "service": "Amazon EC2",
            },
        ],
        "recent_savings": [],
        "error": None,
        "connected_providers": ["aws"],
        "trend": [
            {"month": "March", "actual": 10200.0, "projected": None},
            {"month": "April", "actual": 11800.0, "projected": None},
            {"month": "May (partial)", "actual": 13703.0, "projected": None},
            {"month": "May (projected)", "actual": None, "projected": 15742.0},
        ],
        "scorecard": {
            "overall_grade": "B",
            "overall_score": 82.0,
            "dimensions": [
                {"name": "Waste Reduction", "score": 100, "grade": "A"},
                {"name": "Anomaly Response", "score": 80, "grade": "B"},
                {"name": "Compute Efficiency", "score": 50, "grade": "C"},
                {"name": "Commitment Coverage", "score": 0, "grade": "F"},
                {"name": "Tag Hygiene", "score": 0, "grade": "F"},
            ],
        },
    }
    with patch(
        "finops.server_web._fetch_dashboard_data",
        new=AsyncMock(return_value=data),
    ):
        yield data


@pytest.fixture()
def running_server(mock_dashboard_data):
    """Start the server on a free port, yield (server, port), then shut down."""
    from finops.server_web import _make_server

    port = _free_port()
    server = _make_server("127.0.0.1", port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    yield server, port
    server.shutdown()


# ── Tests: health check ───────────────────────────────────────────────────────

def test_health_returns_ok(running_server):
    _, port = running_server
    status, body = _get(f"http://127.0.0.1:{port}/health")
    assert status == 200
    data = json.loads(body)
    assert data["status"] == "ok"


# ── Tests: /api/data ──────────────────────────────────────────────────────────

def test_api_data_returns_valid_json(running_server):
    _, port = running_server
    status, body = _get(f"http://127.0.0.1:{port}/api/data")
    assert status == 200
    data = json.loads(body)
    # Required top-level keys
    for key in (
        "generated_at",
        "total_spend_mtd",
        "top_services",
        "opportunities_count",
        "savings_achieved_mtd",
        "anomalies_open",
    ):
        assert key in data, f"Missing key: {key}"


def test_api_data_values(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/api/data")
    data = json.loads(body)
    assert data["total_spend_mtd"] == 1234.56
    assert data["delta_pct"] == 23.5
    assert data["opportunities_count"] == 3
    assert len(data["top_services"]) == 2
    assert data["top_services"][0]["service"] == "Amazon EC2"


# ── Tests: dashboard HTML ─────────────────────────────────────────────────────

def test_dashboard_html_served(running_server):
    _, port = running_server
    status, body = _get(f"http://127.0.0.1:{port}/")
    assert status == 200
    assert "text/html" in body or "<!doctype" in body.lower()


def test_dashboard_html_structure(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/")
    assert "nable" in body
    assert "Spend" in body
    assert "getnable.com" in body  # footer link
    assert "Instrument Sans" in body  # correct font


def test_dashboard_auto_refresh_script(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/")
    # The page should auto-refresh every 60 seconds
    assert "60000" in body


def test_dashboard_has_chartjs(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/")
    assert "chart.umd" in body or "Chart.js" in body


def test_dashboard_has_spend_by_service(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/")
    assert "Spend by Service" in body


def test_dashboard_has_trend_chart(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/")
    assert "3-Month Cost Trend" in body


def test_dashboard_has_efficiency_scorecard(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/")
    assert "Efficiency Scorecard" in body


def test_dashboard_has_savings_opportunities(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/")
    assert "Savings Opportunities" in body


def test_api_data_has_trend_key(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/api/data")
    data = json.loads(body)
    assert "trend" in data
    assert isinstance(data["trend"], list)
    assert len(data["trend"]) > 0
    # Each entry has month and actual/projected
    first = data["trend"][0]
    assert "month" in first
    assert "actual" in first or "projected" in first


def test_api_data_has_scorecard_key(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/api/data")
    data = json.loads(body)
    assert "scorecard" in data
    sc = data["scorecard"]
    assert "overall_grade" in sc
    assert "overall_score" in sc
    assert "dimensions" in sc
    assert isinstance(sc["dimensions"], list)


# ── Tests: 404 ───────────────────────────────────────────────────────────────

def test_unknown_path_returns_404(running_server):
    _, port = running_server
    try:
        _get(f"http://127.0.0.1:{port}/nonexistent")
        assert False, "Expected HTTP error"
    except urllib.error.HTTPError as e:
        assert e.code == 404


# ── Tests: port conflict handling ─────────────────────────────────────────────

def test_port_conflict_uses_next_port():
    """If the requested port is taken, _make_server should bind to the next one."""
    from finops.server_web import _make_server

    # Occupy a port
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 0))
    blocked_port = blocker.getsockname()[1]

    try:
        server = _make_server("127.0.0.1", blocked_port)
        actual_port = server.server_address[1]
        server.server_close()
        # Should have moved to a different port
        assert actual_port != blocked_port
    finally:
        blocker.close()


# ── Tests: data fetcher returns error flag when no providers configured ────────

@pytest.mark.asyncio
async def test_fetch_data_no_providers():
    """When no connectors are configured, the data dict should have an error flag."""
    # Connectors are lazily imported inside _fetch_dashboard_data, so patch at source
    mock_instance = MagicMock()
    mock_instance.is_configured = AsyncMock(return_value=False)
    mock_cls = MagicMock(return_value=mock_instance)

    with patch("finops.connectors.aws.AWSConnector", mock_cls), \
         patch("finops.connectors.azure.AzureConnector", mock_cls), \
         patch("finops.connectors.gcp.GCPConnector", mock_cls):

        # Patch the connector classes as they're imported inside _fetch_dashboard_data
        import finops.connectors.aws as _aws_mod
        import finops.connectors.azure as _azure_mod
        import finops.connectors.gcp as _gcp_mod
        _orig_aws = _aws_mod.AWSConnector
        _orig_azure = _azure_mod.AzureConnector
        _orig_gcp = _gcp_mod.GCPConnector
        try:
            _aws_mod.AWSConnector = mock_cls  # type: ignore[assignment]
            _azure_mod.AzureConnector = mock_cls  # type: ignore[assignment]
            _gcp_mod.GCPConnector = mock_cls  # type: ignore[assignment]

            from finops.server_web import _fetch_dashboard_data
            result = await _fetch_dashboard_data()
        finally:
            _aws_mod.AWSConnector = _orig_aws
            _azure_mod.AzureConnector = _orig_azure
            _gcp_mod.GCPConnector = _orig_gcp

    assert result["error"] is not None
    assert result["total_spend_mtd"] == 0.0


# ── Tests: local IP detection ─────────────────────────────────────────────────

def test_local_ip_returns_string():
    from finops.server_web import _local_ip

    ip = _local_ip()
    assert isinstance(ip, str)
    parts = ip.split(".")
    assert len(parts) == 4
