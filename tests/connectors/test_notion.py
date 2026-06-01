"""Tests for finops.connectors.saas.notion."""
from __future__ import annotations

import asyncio
import os
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from finops.connectors.saas.notion import NotionConnector


# ── is_configured ─────────────────────────────────────────────────────────────

def _run(coro):
    """Run a coroutine synchronously (works on Python 3.8 without pytest-asyncio)."""
    return asyncio.run(coro)


def test_is_configured_false_when_both_missing(monkeypatch):
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("NOTION_PAGE_ID", raising=False)
    connector = NotionConnector()
    assert _run(connector.is_configured()) is False


def test_is_configured_false_when_only_api_key(monkeypatch):
    monkeypatch.setenv("NOTION_API_KEY", "secret_test_key")
    monkeypatch.delenv("NOTION_PAGE_ID", raising=False)
    connector = NotionConnector()
    assert _run(connector.is_configured()) is False


def test_is_configured_false_when_only_page_id(monkeypatch):
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.setenv("NOTION_PAGE_ID", "abc123")
    connector = NotionConnector()
    assert _run(connector.is_configured()) is False


def test_is_configured_true_when_both_set(monkeypatch):
    monkeypatch.setenv("NOTION_API_KEY", "secret_test_key")
    monkeypatch.setenv("NOTION_PAGE_ID", "abc123")
    connector = NotionConnector()
    assert _run(connector.is_configured()) is True


# ── _build_report_blocks ──────────────────────────────────────────────────────

def _make_report(n_findings: int = 3) -> dict:
    findings = [
        {
            "title": f"Opportunity {i}",
            "category": "Compute",
            "monthly_savings": float(100 * i),
        }
        for i in range(1, n_findings + 1)
    ]
    total_monthly = sum(f["monthly_savings"] for f in findings)
    return {
        "findings": findings,
        "total_monthly_savings": total_monthly,
        "total_annual_savings": total_monthly * 12,
        "scan_timestamp": "2026-05-30 09:00 UTC",
        "account": "production",
    }


def _make_connector() -> NotionConnector:
    connector = NotionConnector()
    connector._api_key = "secret_test"
    connector._page_id = "page-id-1234"
    return connector


def test_build_report_blocks_has_heading1():
    connector = _make_connector()
    blocks = connector._build_report_blocks(_make_report(), "2026-05-30")
    heading1_blocks = [b for b in blocks if b.get("type") == "heading_1"]
    assert len(heading1_blocks) == 1
    text = heading1_blocks[0]["heading_1"]["rich_text"][0]["text"]["content"]
    assert "2026-05-30" in text


def test_build_report_blocks_title_includes_date():
    connector = _make_connector()
    blocks = connector._build_report_blocks(_make_report(), "2026-05-30")
    heading1 = next(b for b in blocks if b.get("type") == "heading_1")
    content = heading1["heading_1"]["rich_text"][0]["text"]["content"]
    assert "2026-05-30" in content


def test_build_report_blocks_has_callout_with_savings():
    connector = _make_connector()
    report = _make_report(n_findings=2)
    blocks = connector._build_report_blocks(report, "2026-05-30")
    callouts = [b for b in blocks if b.get("type") == "callout"]
    assert len(callouts) == 1
    text = callouts[0]["callout"]["rich_text"][0]["text"]["content"]
    assert "$" in text
    assert "/mo" in text
    assert "/yr" in text


def test_build_report_blocks_has_table():
    connector = _make_connector()
    blocks = connector._build_report_blocks(_make_report(), "2026-05-30")
    tables = [b for b in blocks if b.get("type") == "table"]
    assert len(tables) == 1


def test_table_has_correct_columns():
    connector = _make_connector()
    blocks = connector._build_report_blocks(_make_report(), "2026-05-30")
    table_block = next(b for b in blocks if b.get("type") == "table")
    assert table_block["table"]["table_width"] == 4

    rows = table_block["table"]["children"]
    header_row = rows[0]["table_row"]["cells"]

    # Extract header cell text
    header_texts = [cell[0]["text"]["content"] for cell in header_row]
    assert "Opportunity" in header_texts
    assert "Category" in header_texts
    assert "Monthly Saving" in header_texts
    assert "Annual Saving" in header_texts


def test_table_has_correct_row_count():
    connector = _make_connector()
    n = 5
    blocks = connector._build_report_blocks(_make_report(n_findings=n), "2026-05-30")
    table_block = next(b for b in blocks if b.get("type") == "table")
    rows = table_block["table"]["children"]
    # 1 header row + n data rows
    assert len(rows) == n + 1


def test_table_caps_at_20_findings():
    connector = _make_connector()
    blocks = connector._build_report_blocks(_make_report(n_findings=25), "2026-05-30")
    table_block = next(b for b in blocks if b.get("type") == "table")
    rows = table_block["table"]["children"]
    # 1 header + up to 20 data rows
    assert len(rows) == 21


def test_build_report_blocks_has_divider():
    connector = _make_connector()
    blocks = connector._build_report_blocks(_make_report(), "2026-05-30")
    dividers = [b for b in blocks if b.get("type") == "divider"]
    assert len(dividers) == 1


def test_build_report_blocks_has_footer():
    connector = _make_connector()
    blocks = connector._build_report_blocks(_make_report(), "2026-05-30")
    paragraphs = [b for b in blocks if b.get("type") == "paragraph"]
    assert len(paragraphs) >= 1
    footer_text = paragraphs[-1]["paragraph"]["rich_text"][0]["text"]["content"]
    assert "nable" in footer_text


def test_build_report_blocks_includes_account_in_title():
    connector = _make_connector()
    report = _make_report()
    report["account"] = "my-account"
    blocks = connector._build_report_blocks(report, "2026-05-30")
    heading1 = next(b for b in blocks if b.get("type") == "heading_1")
    content = heading1["heading_1"]["rich_text"][0]["text"]["content"]
    assert "my-account" in content


def test_annual_saving_in_table_row():
    connector = _make_connector()
    report = {
        "findings": [{"title": "Test opp", "category": "Storage", "monthly_savings": 100.0}],
        "total_monthly_savings": 100.0,
        "total_annual_savings": 1200.0,
        "scan_timestamp": "2026-05-30 09:00 UTC",
        "account": "",
    }
    blocks = connector._build_report_blocks(report, "2026-05-30")
    table_block = next(b for b in blocks if b.get("type") == "table")
    # First data row (index 1)
    data_row = table_block["table"]["children"][1]["table_row"]["cells"]
    monthly_cell = data_row[2][0]["text"]["content"]
    annual_cell = data_row[3][0]["text"]["content"]
    assert "$100.00" in monthly_cell
    assert "$1,200.00" in annual_cell
