"""
Tests for the savings ledger feature.

Tests the savings_tracker data layer and the report formatting logic
without importing server.py (which requires the `mcp` package).
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Formatting helper (mirrors the logic in server.get_savings_ledger)
# We test the logic directly, not through the MCP server import.
# ---------------------------------------------------------------------------

def _fmt_row_status(status: str) -> bool:
    """Open rows go to 'still open'; acted_on/verified go to 'acted on'."""
    return status in ("acted_on", "verified")


def _format_ledger(rows, days: int = 30) -> str:
    """Mirrors the report formatting logic from get_savings_ledger."""
    if not rows:
        period = f"last {days} day{'s' if days != 1 else ''}"
        return (
            f"No savings recommendations found in the {period}. "
            "Run get_rightsizing_recommendations() or scan_waste_patterns() to surface opportunities."
        )

    found_rows = [r for r in rows if r["status"] not in ("dismissed", "expired")]
    acted_rows = [r for r in rows if r["status"] in ("acted_on", "verified")]
    verified_rows = [r for r in rows if r["status"] == "verified"]
    open_rows = [r for r in rows if r["status"] == "open"]

    found_total = sum(r["estimated"] or 0 for r in found_rows)
    acted_total = sum(r["estimated"] or 0 for r in acted_rows)
    verified_total = sum(
        r.get("verified") or r["estimated"] or 0
        for r in verified_rows
    )

    period_label = f"Last {days} day{'s' if days != 1 else ''}"
    lines = [
        f"## Savings Ledger: {period_label}",
        "",
        f"FOUND:    ${found_total:,.0f}/mo across {len(found_rows)} opportunit{'ies' if len(found_rows) != 1 else 'y'}",
        f"ACTED ON: ${acted_total:,.0f}/mo across {len(acted_rows)} opportunit{'ies' if len(acted_rows) != 1 else 'y'}",
        f"VERIFIED: ${verified_total:,.0f}/mo in realized savings ({len(verified_rows)} confirmed)",
    ]

    if acted_rows:
        lines += ["", "### Opportunities acted on"]
        for r in acted_rows:
            lines.append(f"| {r['desc'][:40]:<40} | {r['status']:<8} |")

    if open_rows:
        lines += ["", "### Still open (not yet acted on)"]
        for r in open_rows:
            lines.append(f"| {r['desc'][:40]:<40} |")

    lines += [
        "",
        "Run mark_recommendation_acted_on(id) to move an opportunity to acted_on.",
        "Run verify_savings() to confirm realized savings from acted-on recommendations.",
    ]

    return "\n".join(lines)


def _row(status: str, desc: str, estimated: float, verified: float | None = None) -> dict:
    return {"status": status, "desc": desc, "estimated": estimated, "verified": verified}


# ---------------------------------------------------------------------------
# Tests: empty state
# ---------------------------------------------------------------------------

def test_format_ledger_empty_returns_helpful_message():
    result = _format_ledger([], days=30)
    assert "No savings recommendations found" in result
    assert "30 days" in result


def test_format_ledger_single_day():
    result = _format_ledger([], days=1)
    assert "1 day" in result
    assert "1 days" not in result


# ---------------------------------------------------------------------------
# Tests: summary header format
# ---------------------------------------------------------------------------

def test_format_ledger_header_contains_all_sections():
    rows = [
        _row("open", "DB right-size", 500.0),
        _row("acted_on", "EC2 downsize", 200.0),
        _row("verified", "EIP release", 43.0, verified=43.0),
    ]
    result = _format_ledger(rows, days=30)
    assert "FOUND:" in result
    assert "ACTED ON:" in result
    assert "VERIFIED:" in result
    assert "Savings Ledger" in result
    assert "Last 30 days" in result


def test_format_ledger_period_label_90_days():
    result = _format_ledger([], days=90)
    assert "90 days" in result


# ---------------------------------------------------------------------------
# Tests: status counts and totals
# ---------------------------------------------------------------------------

def test_open_rows_go_to_still_open_section():
    rows = [_row("open", "Open opportunity", 300.0)]
    result = _format_ledger(rows)
    assert "Still open" in result
    assert "Open opportunity" in result


def test_acted_on_rows_go_to_acted_section():
    rows = [_row("acted_on", "Done thing", 100.0)]
    result = _format_ledger(rows)
    assert "Opportunities acted on" in result
    assert "Done thing" in result


def test_verified_rows_go_to_acted_section():
    rows = [_row("verified", "Confirmed saving", 50.0, verified=50.0)]
    result = _format_ledger(rows)
    assert "Opportunities acted on" in result
    assert "Confirmed saving" in result


def test_dismissed_excluded_from_found_total():
    rows = [
        _row("open", "Real opportunity", 400.0),
        _row("dismissed", "Wont fix", 999.0),
        _row("expired", "Stale", 888.0),
    ]
    result = _format_ledger(rows)
    # found_total = 400 (only open)
    assert "$400/mo" in result
    # dismissed and expired do not inflate the FOUND total
    assert "$999" not in result
    assert "$888" not in result


def test_found_total_includes_open_and_acted():
    """FOUND counts open + acted_on + verified (anything not dismissed/expired)."""
    rows = [
        _row("open", "A", 100.0),
        _row("acted_on", "B", 200.0),
        _row("verified", "C", 300.0, verified=300.0),
    ]
    result = _format_ledger(rows)
    # found_total = 600
    assert "$600/mo" in result


# ---------------------------------------------------------------------------
# Tests: verified savings use verified amount, not estimated
# ---------------------------------------------------------------------------

def test_verified_uses_verified_amount_over_estimated():
    rows = [_row("verified", "EC2 downsize", estimated=500.0, verified=420.0)]
    result = _format_ledger(rows)
    assert "$420/mo" in result
    assert "1 confirmed" in result


def test_verified_falls_back_to_estimated_when_no_verified():
    rows = [_row("verified", "EC2 downsize", estimated=300.0, verified=None)]
    result = _format_ledger(rows)
    assert "$300/mo" in result


# ---------------------------------------------------------------------------
# Tests: action tips
# ---------------------------------------------------------------------------

def test_action_tips_always_present():
    rows = [_row("open", "Some recommendation", 100.0)]
    result = _format_ledger(rows)
    assert "mark_recommendation_acted_on" in result
    assert "verify_savings" in result


# ---------------------------------------------------------------------------
# Tests: savings_tracker data layer (no server import needed)
# ---------------------------------------------------------------------------

class TestSavingsTrackerLayer:
    def test_get_summary_returns_expected_keys(self):
        """get_summary returns the expected aggregation keys."""
        from src.finops.recommendations.savings_tracker import get_summary
        # Use a temp SQLite DB so we don't touch the real one
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"FINOPS_DB_PATH": str(Path(td) / "test.db")}):
                # Reset engine cache
                from src.finops.storage import db as db_mod
                db_mod._ENGINE = None

                summary = get_summary()

                assert "potential_monthly_usd" in summary
                assert "acted_on_monthly_usd" in summary
                assert "verified_monthly_usd" in summary
                assert "verified_annual_usd" in summary
                assert "total_recommendations" in summary
                assert "by_status" in summary
                assert "by_source" in summary

                db_mod._ENGINE = None

    def test_record_and_list_recommendations(self):
        """Can record a recommendation and retrieve it via list_recommendations."""
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"FINOPS_DB_PATH": str(Path(td) / "test.db")}):
                from src.finops.storage import db as db_mod
                db_mod._ENGINE = None

                from src.finops.recommendations.savings_tracker import (
                    record_recommendation, list_recommendations,
                )
                rec_id = record_recommendation(
                    source="rightsizing",
                    provider="aws",
                    resource_id="i-abc123",
                    resource_type="ec2",
                    resource_name="web-server",
                    current_config={"instance_type": "m5.xlarge"},
                    recommended_config={"instance_type": "m5.large"},
                    description="Downsize m5.xlarge to m5.large",
                    estimated_monthly_savings_usd=120.0,
                )

                assert rec_id is not None

                recs = list_recommendations(status="open")
                assert len(recs) == 1
                assert recs[0]["description"] == "Downsize m5.xlarge to m5.large"
                assert recs[0]["estimated_monthly_savings_usd"] == 120.0

                db_mod._ENGINE = None

    def test_mark_acted_on_transitions_status(self):
        """mark_acted_on() moves a recommendation from open to acted_on."""
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"FINOPS_DB_PATH": str(Path(td) / "test.db")}):
                from src.finops.storage import db as db_mod
                db_mod._ENGINE = None

                from src.finops.recommendations.savings_tracker import (
                    record_recommendation, mark_acted_on, list_recommendations,
                )
                rec_id = record_recommendation(
                    source="idle",
                    provider="aws",
                    resource_id="eip-xyz",
                    resource_type="eip",
                    resource_name="unused-eip",
                    current_config={},
                    recommended_config={"action": "release"},
                    description="Release unattached EIP",
                    estimated_monthly_savings_usd=3.6,
                )

                assert mark_acted_on(rec_id) is True

                acted = list_recommendations(status="acted_on")
                assert any(r["id"] == rec_id for r in acted)

                db_mod._ENGINE = None

    def test_get_summary_totals_after_verification(self):
        """get_summary reflects verified savings after mark_verified is called."""
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"FINOPS_DB_PATH": str(Path(td) / "test.db")}):
                from src.finops.storage import db as db_mod
                db_mod._ENGINE = None

                from src.finops.recommendations.savings_tracker import (
                    record_recommendation, mark_acted_on, mark_verified, get_summary,
                )
                rec_id = record_recommendation(
                    source="rightsizing",
                    provider="aws",
                    resource_id="i-test",
                    resource_type="ec2",
                    resource_name="test",
                    current_config={},
                    recommended_config={},
                    description="Test rec",
                    estimated_monthly_savings_usd=500.0,
                )
                mark_acted_on(rec_id)
                mark_verified(rec_id, actual_monthly_savings_usd=480.0)

                summary = get_summary()
                assert summary["verified_monthly_usd"] == 480.0
                assert summary["verified_annual_usd"] == round(480.0 * 12, 2)

                db_mod._ENGINE = None
