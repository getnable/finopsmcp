"""
Tests for Tableau Web Data Connector endpoints in server_web.py.

Spins up the HTTP server on an ephemeral port, then hits each endpoint.
DB calls are patched so tests don't require a real nable database.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer
from unittest.mock import MagicMock, patch

import pytest

# Import the handler class directly so we can patch at the module level
from finops.server_web import _Handler, _fetch_tableau_costs, _to_csv


# ── Helpers ──────────────────────────────────────────────────────────────────

def _start_server() -> tuple[HTTPServer, str]:
    """Start the dashboard server on a random available port. Returns (server, base_url)."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{port}"


def _get(url: str) -> tuple[int, str, dict]:
    """Return (status_code, body_text, headers_dict)."""
    with urllib.request.urlopen(url) as resp:
        return resp.status, resp.read().decode(), dict(resp.headers)


MOCK_COSTS = [
    {"service": "Amazon EC2", "provider": "aws", "account_id": "123456789",
     "snapshot_date": "2026-05-30", "amount_usd": 1234.56, "region": "us-east-1"},
    {"service": "Amazon S3", "provider": "aws", "account_id": "123456789",
     "snapshot_date": "2026-05-30", "amount_usd": 45.00, "region": "us-east-1"},
]

MOCK_OPPORTUNITIES = [
    {"title": "Migrate to Graviton", "category": "Rightsizing", "monthly_savings": 980.0,
     "annual_savings": 11760.0, "status": "open", "created_at": "2026-05-01"},
]

MOCK_ANOMALIES = [
    {"service": "Amazon Textract", "detected_at": "2026-05-30T12:00:00",
     "severity": "high", "pct_change": 127.0, "current_amount": 4830.0,
     "baseline_mean": 2120.0, "acknowledged": False},
]


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def server_url():
    """Single server instance shared across all tests."""
    server, base = _start_server()
    yield base
    server.shutdown()


# ── /tableau endpoint ─────────────────────────────────────────────────────────

def test_tableau_page_returns_200(server_url):
    status, body, headers = _get(f"{server_url}/tableau")
    assert status == 200


def test_tableau_page_content_type_is_html(server_url):
    _, _, headers = _get(f"{server_url}/tableau")
    assert "text/html" in headers.get("Content-Type", "")


def test_tableau_page_contains_wdc_marker(server_url):
    _, body, _ = _get(f"{server_url}/tableau")
    assert "tableau.makeConnector" in body


def test_tableau_page_loads_sdk(server_url):
    _, body, _ = _get(f"{server_url}/tableau")
    assert "tableauwdc-2.3.latest.js" in body


def test_tableau_page_has_three_table_schemas(server_url):
    _, body, _ = _get(f"{server_url}/tableau")
    # All three table IDs must be present in the JS schema definitions
    assert '"costs"' in body
    assert '"opportunities"' in body
    assert '"anomalies"' in body


# ── /api/tableau/costs ────────────────────────────────────────────────────────

def test_api_tableau_costs_returns_200(server_url):
    with patch("finops.server_web._fetch_tableau_costs", return_value=MOCK_COSTS):
        status, _, _ = _get(f"{server_url}/api/tableau/costs")
    assert status == 200


def test_api_tableau_costs_returns_json_array(server_url):
    with patch("finops.server_web._fetch_tableau_costs", return_value=MOCK_COSTS):
        _, body, headers = _get(f"{server_url}/api/tableau/costs")
    assert "application/json" in headers.get("Content-Type", "")
    data = json.loads(body)
    assert isinstance(data, list)


def test_api_tableau_costs_empty_when_no_db(server_url):
    """If the DB is unavailable, the endpoint returns an empty array, not an error."""
    with patch("finops.server_web._fetch_tableau_costs", return_value=[]):
        status, body, _ = _get(f"{server_url}/api/tableau/costs")
    assert status == 200
    assert json.loads(body) == []


# ── /api/tableau/opportunities ────────────────────────────────────────────────

def test_api_tableau_opportunities_returns_200(server_url):
    with patch("finops.server_web._fetch_tableau_opportunities", return_value=MOCK_OPPORTUNITIES):
        status, _, _ = _get(f"{server_url}/api/tableau/opportunities")
    assert status == 200


def test_api_tableau_opportunities_returns_json_array(server_url):
    with patch("finops.server_web._fetch_tableau_opportunities", return_value=MOCK_OPPORTUNITIES):
        _, body, headers = _get(f"{server_url}/api/tableau/opportunities")
    assert "application/json" in headers.get("Content-Type", "")
    data = json.loads(body)
    assert isinstance(data, list)


# ── /api/tableau/anomalies ────────────────────────────────────────────────────

def test_api_tableau_anomalies_returns_200(server_url):
    with patch("finops.server_web._fetch_tableau_anomalies", return_value=MOCK_ANOMALIES):
        status, _, _ = _get(f"{server_url}/api/tableau/anomalies")
    assert status == 200


def test_api_tableau_anomalies_returns_json_array(server_url):
    with patch("finops.server_web._fetch_tableau_anomalies", return_value=MOCK_ANOMALIES):
        _, body, headers = _get(f"{server_url}/api/tableau/anomalies")
    assert "application/json" in headers.get("Content-Type", "")
    data = json.loads(body)
    assert isinstance(data, list)


# ── CSV endpoints ─────────────────────────────────────────────────────────────

def test_costs_csv_returns_200(server_url):
    with patch("finops.server_web._fetch_tableau_costs", return_value=MOCK_COSTS):
        status, _, _ = _get(f"{server_url}/tableau/costs.csv")
    assert status == 200


def test_costs_csv_content_type(server_url):
    with patch("finops.server_web._fetch_tableau_costs", return_value=MOCK_COSTS):
        _, _, headers = _get(f"{server_url}/tableau/costs.csv")
    assert "text/csv" in headers.get("Content-Type", "")


def test_costs_csv_has_correct_headers(server_url):
    with patch("finops.server_web._fetch_tableau_costs", return_value=MOCK_COSTS):
        _, body, _ = _get(f"{server_url}/tableau/costs.csv")
    first_line = body.strip().splitlines()[0]
    assert "service" in first_line
    assert "amount_usd" in first_line
    assert "snapshot_date" in first_line


def test_opportunities_csv_returns_200(server_url):
    with patch("finops.server_web._fetch_tableau_opportunities", return_value=MOCK_OPPORTUNITIES):
        status, _, _ = _get(f"{server_url}/tableau/opportunities.csv")
    assert status == 200


def test_opportunities_csv_content_type(server_url):
    with patch("finops.server_web._fetch_tableau_opportunities", return_value=MOCK_OPPORTUNITIES):
        _, _, headers = _get(f"{server_url}/tableau/opportunities.csv")
    assert "text/csv" in headers.get("Content-Type", "")


def test_opportunities_csv_has_correct_headers(server_url):
    with patch("finops.server_web._fetch_tableau_opportunities", return_value=MOCK_OPPORTUNITIES):
        _, body, _ = _get(f"{server_url}/tableau/opportunities.csv")
    first_line = body.strip().splitlines()[0]
    assert "title" in first_line
    assert "monthly_savings" in first_line
    assert "annual_savings" in first_line


def test_anomalies_csv_returns_200(server_url):
    with patch("finops.server_web._fetch_tableau_anomalies", return_value=MOCK_ANOMALIES):
        status, _, _ = _get(f"{server_url}/tableau/anomalies.csv")
    assert status == 200


def test_anomalies_csv_content_type(server_url):
    with patch("finops.server_web._fetch_tableau_anomalies", return_value=MOCK_ANOMALIES):
        _, _, headers = _get(f"{server_url}/tableau/anomalies.csv")
    assert "text/csv" in headers.get("Content-Type", "")


def test_anomalies_csv_has_correct_headers(server_url):
    with patch("finops.server_web._fetch_tableau_anomalies", return_value=MOCK_ANOMALIES):
        _, body, _ = _get(f"{server_url}/tableau/anomalies.csv")
    first_line = body.strip().splitlines()[0]
    assert "service" in first_line
    assert "severity" in first_line
    assert "pct_change" in first_line


# ── _to_csv unit tests ────────────────────────────────────────────────────────

def test_to_csv_empty_list():
    assert _to_csv([]) == b""


def test_to_csv_produces_valid_csv():
    rows = [{"a": 1, "b": "hello"}, {"a": 2, "b": "world"}]
    result = _to_csv(rows).decode()
    lines = result.strip().splitlines()
    assert lines[0] == "a,b"
    assert "1,hello" in lines[1]
    assert "2,world" in lines[2]
