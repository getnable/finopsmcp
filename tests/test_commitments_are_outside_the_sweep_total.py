# SPDX-License-Identifier: Apache-2.0
"""Compute Savings Plan and RI savings are not in the sweep's total, and it says so.

The sweep runs analyze_commitments, which returns a CommitmentAnalysis, and
normalise("commitments") reads only a dict, so the commitment check never adds
a row or a dollar to run_full_cost_audit or the exports. That is the right
total: a plan discounts the same instance hours the per-instance rows already
claim (Graviton, Spot, idle, a schedule), and the two do not add up. What was
wrong is that nothing said so. An account with $30,000 of uncovered on-demand
over three months got an audit whose total was its idle volumes, and no line
saying where the largest saving on the bill is reported. These tests pin the
total (unchanged, per-resource rows only) and the sentence beside it.
"""
from __future__ import annotations

import asyncio
import csv

import pytest

import finops.server as _srv
from finops.recommendations import commitments as C
from finops.recommendations import sweep as S
from finops.tools import cost_queries as cq
from finops.tools import notifications as N


def _unwrap(tool):
    return getattr(tool, "fn", tool)


class _FakeAWS:
    async def is_configured(self):
        return True

    def _account_id(self):
        return "111122223333"

    async def list_accounts(self):
        return []


def _analysis() -> C.CommitmentAnalysis:
    # Three months of uncovered on-demand, $10,000 a month: get_commitment_analysis
    # prices a plan on that at 26%, $2,600 a month. None of it belongs in the sweep.
    return C.CommitmentAnalysis(
        savings_plan_coverage_pct=10.0, savings_plan_utilization_pct=99.0,
        savings_plan_unused_usd=0.0, ri_coverage_pct=10.0, ri_utilization_pct=99.0,
        ri_unused_usd=0.0, uncovered_on_demand_usd=30000.0,
        recommendations=C._build_recommendations(
            10.0, 30000.0, 0.0, 0.0, monthly_uncovered_series=[10000.0] * 3))


@pytest.fixture
def swept(monkeypatch):
    monkeypatch.setitem(_srv.CLOUD_CONNECTORS, "aws", _FakeAWS())
    monkeypatch.setattr("finops.recommendations.learning.signal.customer_signal",
                        lambda: {"by_source": []}, raising=False)

    def _specs(aws_client, regions):
        return [
            ("commitments", lambda **k: _analysis(), {}),
            ("ipv4", lambda **k: {"total_monthly_waste": 10.95,
                                  "unattached_eips": [1, 2, 3]}, {}),
        ]

    monkeypatch.setattr(S, "build_specs", _specs)


def test_the_sweep_adds_no_commitment_dollars():
    assert S.normalise("commitments", _analysis()) == []


def test_the_audit_total_excludes_commitments_and_says_where_they_are(swept):
    out = asyncio.run(_unwrap(cq.run_full_cost_audit)(regions=["us-east-1"], top_n=10))
    # Three addresses at $3.65 a month: the total is the per-resource rows only.
    assert "Estimated monthly saving: $10.95 | Annual: $131.40" in out, out
    assert "2,600" not in out
    assert "get_commitment_analysis" in out, out
    assert "not part of this total" in out, out


def test_the_audit_says_it_when_nothing_else_was_found(monkeypatch, swept):
    monkeypatch.setattr(S, "build_specs", lambda aws_client, regions: [
        ("commitments", lambda **k: _analysis(), {})])
    out = asyncio.run(_unwrap(cq.run_full_cost_audit)(regions=["us-east-1"], top_n=10))
    assert "get_commitment_analysis" in out, out


def test_the_csv_export_says_it_beside_its_total(swept, tmp_path):
    dest = tmp_path / "report.csv"
    asyncio.run(_unwrap(N.export_cost_report_csv)(output_path=str(dest)))
    rows = list(csv.reader(dest.read_text().splitlines()))
    assert ["Total monthly saving", "$10.95"] in rows
    said = " ".join(" ".join(r) for r in rows)
    assert "get_commitment_analysis" in said and "not part of this total" in said


def test_the_notion_report_says_it_beside_its_total(swept, monkeypatch):
    class _Notion:
        async def is_configured(self):
            return True

        async def write_cost_report(self, report):
            _Notion.seen = report
            return "https://notion.so/fake-page"

    import finops.connectors.saas.notion as notion_mod
    monkeypatch.setattr(notion_mod, "NotionConnector", _Notion)
    out = asyncio.run(_unwrap(N.publish_cost_report_to_notion)())
    assert _Notion.seen["total_monthly_savings"] == pytest.approx(10.95)
    assert "get_commitment_analysis" in out
    assert "get_commitment_analysis" in _Notion.seen.get("scope_note", "")
