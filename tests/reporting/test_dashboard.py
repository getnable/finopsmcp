"""
Tests for src/finops/reporting/dashboard.py
"""
from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from finops.reporting.dashboard import (
    _build_html,
    _delta_label,
    _fmt_usd,
    generate_account_dashboard,
)


# ── Unit tests: pure helpers ───────────────────────────────────────────────────

def test_fmt_usd_no_decimals():
    assert _fmt_usd(1234.56) == "$1,235"


def test_fmt_usd_with_decimals():
    assert _fmt_usd(1234.56, decimals=2) == "$1,234.56"


def test_delta_label_increase():
    label, css = _delta_label(1200, 1000)
    assert css == "up"
    assert "+" in label
    assert "20.0%" in label


def test_delta_label_decrease():
    label, css = _delta_label(800, 1000)
    assert css == "down"
    assert "-" in label


def test_delta_label_no_last_month():
    label, css = _delta_label(500, 0)
    assert label == "n/a"
    assert css == "neutral"


# ── HTML structure tests ───────────────────────────────────────────────────────

def _sample_html(**overrides) -> str:
    kwargs = dict(
        account_id="123456789012",
        this_month=8500.0,
        last_month=7200.0,
        projected=9100.0,
        top_services=[
            {"service": "Amazon EC2", "this_month": 4000.0, "last_month": 3500.0},
            {"service": "Amazon RDS", "this_month": 2000.0, "last_month": 1800.0},
        ],
        opportunities=[
            {
                "description": "Downsize m5.2xlarge → m5.xlarge",
                "category": "rightsizing",
                "estimated_monthly_savings_usd": 120.0,
            }
        ],
        savings_summary={
            "potential_monthly_usd": 120.0,
            "acted_on_monthly_usd": 0.0,
            "verified_monthly_usd": 50.0,
        },
        savings_ledger=[
            {
                "description": "Old rightsizing fix",
                "source": "rightsizing",
                "status": "verified",
                "estimated_monthly_savings_usd": 50.0,
                "verified_monthly_savings_usd": 48.0,
            }
        ],
        budgets=[
            {
                "name": "AWS Total",
                "pct_used": 72.0,
                "status": "ok",
                "limit_usd": 12000.0,
                "spent_usd": 8500.0,
            }
        ],
        generated_at="2026-05-31 10:00 UTC",
    )
    kwargs.update(overrides)
    return _build_html(**kwargs)


def test_html_contains_account_id():
    html = _sample_html()
    assert "123456789012" in html


def test_html_contains_total_spend():
    html = _sample_html()
    # $8,500 appears as the total spend (formatted with 0 decimals)
    assert "$8,500" in html


def test_html_contains_last_month():
    html = _sample_html()
    assert "$7,200" in html


def test_html_contains_projected():
    html = _sample_html()
    assert "$9,100" in html


def test_html_contains_top_service():
    html = _sample_html()
    assert "Amazon EC2" in html
    assert "Amazon RDS" in html


def test_html_contains_opportunity():
    html = _sample_html()
    assert "Downsize m5.2xlarge" in html
    assert "rightsizing" in html


def test_html_contains_savings_ledger():
    html = _sample_html()
    assert "Old rightsizing fix" in html
    assert "verified" in html


def test_html_contains_budget_section():
    html = _sample_html()
    assert "Budget Status" in html
    assert "AWS Total" in html


def test_html_no_budget_section_when_empty():
    html = _sample_html(budgets=[])
    assert "Budget Status" not in html


def test_html_empty_opportunities_message():
    html = _sample_html(opportunities=[])
    assert "Run rightsizing or waste scan" in html


def test_html_self_contained_no_external_js():
    """No external JS references — only Google Fonts link is allowed."""
    html = _sample_html()
    # No script src pointing to external URLs
    external_scripts = re.findall(r'<script[^>]+src=["\']https?://', html)
    assert external_scripts == []


def test_html_dark_theme_bg():
    html = _sample_html()
    assert "#0d0f10" in html


def test_html_accent_color():
    html = _sample_html()
    assert "#4db8d4" in html


def test_html_generated_at():
    html = _sample_html()
    assert "2026-05-31 10:00 UTC" in html


# ── Output path tests ──────────────────────────────────────────────────────────

def test_default_output_path_format():
    """Default path should match ~/.finops/dashboards/dashboard-{account}-{date}.html"""
    from datetime import date
    from finops.reporting.dashboard import _dashboard_dir

    d = _dashboard_dir()
    today = date.today().isoformat()
    expected_name = f"dashboard-myaccount-{today}.html"
    expected = d / expected_name
    assert expected.parent == d


def test_generate_account_dashboard_custom_path():
    """generate_account_dashboard writes HTML to the specified path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "test-dashboard.html"
        result = asyncio.run(
            generate_account_dashboard(
                aws_connector=None,
                account_id="111222333444",
                output_path=str(out),
            )
        )
        # resolve() normalizes symlinks (e.g. /tmp -> /private/tmp on macOS)
        assert Path(result["path"]).resolve() == out.resolve()
        assert out.exists() or Path(result["path"]).exists()
        content = Path(result["path"]).read_text()
        assert "111222333444" in content


def test_generate_account_dashboard_default_path():
    """generate_account_dashboard uses ~/.finops/dashboards/ by default."""
    result = asyncio.run(
        generate_account_dashboard(
            aws_connector=None,
            account_id="test-acct",
        )
    )
    assert "dashboard-test-acct-" in result["path"]
    assert result["path"].endswith(".html")
    # Clean up
    Path(result["path"]).unlink(missing_ok=True)


# ── Summary dict tests ─────────────────────────────────────────────────────────

def test_generate_account_dashboard_summary_keys():
    """Result dict has all expected keys."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "dash.html"
        result = asyncio.run(
            generate_account_dashboard(
                aws_connector=None,
                account_id="999888777666",
                output_path=str(out),
            )
        )
    expected_keys = {
        "path",
        "summary",
        "account_id",
        "this_month_usd",
        "last_month_usd",
        "projected_usd",
        "open_opportunities",
        "opportunity_savings_usd",
        "verified_savings_usd",
        "budget_count",
    }
    assert expected_keys.issubset(result.keys())


def test_generate_account_dashboard_summary_text():
    """Summary text contains account id."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "dash.html"
        result = asyncio.run(
            generate_account_dashboard(
                aws_connector=None,
                account_id="acc-001",
                output_path=str(out),
            )
        )
    assert "acc-001" in result["summary"]


def test_generate_account_dashboard_with_mock_aws():
    """Dashboard pulls cost data from the AWS connector when provided."""
    mock_summary = MagicMock()
    mock_summary.total_usd = 5000.0
    mock_summary.by_service = {"Amazon EC2": 3000.0, "Amazon S3": 2000.0}
    mock_summary.entries = []

    aws = MagicMock()
    aws.get_costs = AsyncMock(return_value=mock_summary)
    aws.list_accounts = AsyncMock(return_value=[{"id": "111000111", "name": "test"}])

    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "aws-dash.html"
        result = asyncio.run(
            generate_account_dashboard(
                aws_connector=aws,
                account_id="111000111",
                output_path=str(out),
            )
        )
        assert result["this_month_usd"] == 5000.0
        content = Path(result["path"]).read_text()
        assert "Amazon EC2" in content
        assert "Amazon S3" in content
