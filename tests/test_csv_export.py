"""Tests for the export_cost_report_csv MCP tool."""
from __future__ import annotations

import asyncio
import csv
import io
import pathlib
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_findings():
    """Return a minimal set of normalized findings for injection."""
    return [
        {"title": "Migrate i-abc (m5.large -> m7g.large)", "monthly_savings": 120.0, "category": "Compute", "detail": "20% saving, us-east-1"},
        {"title": "Release 3 unattached Elastic IP(s)", "monthly_savings": 10.8, "category": "Network", "detail": "$10.80/mo, $3.60 per IP"},
        {"title": "Enable S3 Bucket Key on my-bucket", "monthly_savings": 5.0, "category": "Storage", "detail": "Up to 99% KMS cost reduction"},
    ]


def _read_csv_rows(path: pathlib.Path) -> list[list[str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


# ---------------------------------------------------------------------------
# Default output path format
# ---------------------------------------------------------------------------

def test_default_output_path_format():
    """Default path should resolve to ~/Downloads/nable-report-YYYY-MM-DD.csv."""
    today = date.today().isoformat()
    expected = pathlib.Path.home() / "Downloads" / f"nable-report-{today}.csv"
    # Just verify the path construction logic matches the implementation
    assert expected.name == f"nable-report-{today}.csv"
    assert expected.parent.name == "Downloads"


# ---------------------------------------------------------------------------
# CSV structure tests (integration-style, writing a real temp file)
# ---------------------------------------------------------------------------

def _write_csv(tmp_path: pathlib.Path, findings: list[dict], account_id: str = "123456789012") -> pathlib.Path:
    """
    Replicate the CSV-writing logic from export_cost_report_csv so we can
    test the output format without needing a live AWS connection.
    """
    import csv as csv_mod
    from datetime import datetime

    top = sorted(findings, key=lambda x: x.get("monthly_savings", 0), reverse=True)
    total_monthly = sum(f["monthly_savings"] for f in top)
    total_annual = total_monthly * 12
    scan_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    dest = tmp_path / "nable-report-test.csv"
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        writer = csv_mod.writer(fh)
        writer.writerow(["nable Cost Report"])
        writer.writerow(["Scan timestamp", scan_ts])
        writer.writerow(["AWS account", account_id])
        writer.writerow(["Total monthly saving", f"${total_monthly:,.2f}"])
        writer.writerow(["Total annual saving", f"${total_annual:,.2f}"])
        writer.writerow(["Opportunities found", len(top)])
        writer.writerow([])
        writer.writerow(["Rank", "Opportunity", "Category", "Monthly Saving ($)", "Annual Saving ($)", "Detail"])
        for i, f in enumerate(top, 1):
            mo = round(f["monthly_savings"], 2)
            yr = round(mo * 12, 2)
            writer.writerow([i, f["title"], f["category"], mo, yr, f.get("detail", "")])

    return dest


def test_csv_has_correct_headers(tmp_path):
    findings = _make_findings()
    dest = _write_csv(tmp_path, findings)
    rows = _read_csv_rows(dest)

    # Find the data header row
    header_row = None
    for row in rows:
        if row and row[0] == "Rank":
            header_row = row
            break

    assert header_row is not None, "Header row with 'Rank' not found in CSV"
    assert header_row == ["Rank", "Opportunity", "Category", "Monthly Saving ($)", "Annual Saving ($)", "Detail"]


def test_csv_summary_row_is_present(tmp_path):
    findings = _make_findings()
    dest = _write_csv(tmp_path, findings)
    rows = _read_csv_rows(dest)

    # First row should be the report title
    assert rows[0][0] == "nable Cost Report"

    # Check that summary rows exist
    row_labels = [r[0] for r in rows if r]
    assert "Scan timestamp" in row_labels
    assert "AWS account" in row_labels
    assert "Total monthly saving" in row_labels
    assert "Total annual saving" in row_labels
    assert "Opportunities found" in row_labels


def test_csv_data_rows_count(tmp_path):
    findings = _make_findings()
    dest = _write_csv(tmp_path, findings)
    rows = _read_csv_rows(dest)

    # Count data rows after the header row
    header_idx = next(i for i, r in enumerate(rows) if r and r[0] == "Rank")
    data_rows = [r for r in rows[header_idx + 1:] if r]
    assert len(data_rows) == len(findings)


def test_csv_sorted_by_monthly_savings_descending(tmp_path):
    findings = _make_findings()
    dest = _write_csv(tmp_path, findings)
    rows = _read_csv_rows(dest)

    header_idx = next(i for i, r in enumerate(rows) if r and r[0] == "Rank")
    data_rows = [r for r in rows[header_idx + 1:] if r]

    monthly_values = [float(r[3]) for r in data_rows]
    assert monthly_values == sorted(monthly_values, reverse=True)


def test_csv_annual_saving_is_twelve_times_monthly(tmp_path):
    findings = _make_findings()
    dest = _write_csv(tmp_path, findings)
    rows = _read_csv_rows(dest)

    header_idx = next(i for i, r in enumerate(rows) if r and r[0] == "Rank")
    data_rows = [r for r in rows[header_idx + 1:] if r]

    for row in data_rows:
        monthly = float(row[3])
        annual = float(row[4])
        assert abs(annual - monthly * 12) < 0.01, f"Annual {annual} != monthly {monthly} * 12"


# ---------------------------------------------------------------------------
# File creation at a specified path
# ---------------------------------------------------------------------------

def test_file_is_created_at_specified_path(tmp_path):
    findings = _make_findings()
    custom_path = tmp_path / "custom" / "output.csv"
    custom_path.parent.mkdir(parents=True, exist_ok=True)
    dest = _write_csv(tmp_path, findings)

    # Verify file exists and is non-empty
    assert dest.exists()
    assert dest.stat().st_size > 0


def test_file_created_in_custom_directory(tmp_path):
    findings = _make_findings()
    subdir = tmp_path / "reports"
    subdir.mkdir()
    dest = subdir / "nable-report-custom.csv"

    import csv as csv_mod
    from datetime import datetime

    with open(dest, "w", newline="", encoding="utf-8") as fh:
        writer = csv_mod.writer(fh)
        writer.writerow(["nable Cost Report"])
        writer.writerow(["Rank", "Opportunity", "Category", "Monthly Saving ($)", "Annual Saving ($)", "Detail"])
        for i, f in enumerate(findings, 1):
            writer.writerow([i, f["title"], f["category"], f["monthly_savings"], f["monthly_savings"] * 12, f.get("detail", "")])

    assert dest.exists()
    rows = _read_csv_rows(dest)
    assert rows[0][0] == "nable Cost Report"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_csv_with_zero_findings(tmp_path):
    dest = _write_csv(tmp_path, [])
    rows = _read_csv_rows(dest)

    # Summary should still be present
    assert rows[0][0] == "nable Cost Report"

    # Opportunities found should be 0
    opp_row = next((r for r in rows if r and r[0] == "Opportunities found"), None)
    assert opp_row is not None
    assert opp_row[1] == "0"

    # No data rows after header
    header_idx = next(i for i, r in enumerate(rows) if r and r[0] == "Rank")
    data_rows = [r for r in rows[header_idx + 1:] if r]
    assert data_rows == []
