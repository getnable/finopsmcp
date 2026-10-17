"""A CloudWatch custom-metric bill is never counted as a saving.

audit-savings-other made the cardinality scanner's figure what it is: the list
cost of the custom metrics it counted, an upper bound, filed as an INFERRED
investigation with no est_monthly_savings, because what a cardinality cut saves
depends on which dimensions are dropped. audit-mcp then wired the scanner's dict
into the sweep, where each namespace became a row whose "monthly_savings" was
that namespace's whole metric bill, and every total summed it.

Worked example: namespaces App/Orders ($450.00/mo of metric list cost) and
App/Search ($120.00/mo), and three unattached public IPv4 addresses ($10.80/mo).
The audit headline, the CSV export and the Notion report all said $580.80/mo
saved. The only saving anything measured is the $10.80; the $570.00 is a bill
to investigate.
"""
from __future__ import annotations

import asyncio
import csv

import pytest

import finops.server as _srv
from finops.recommendations import sweep as S
from finops.recommendations.envelope import INVESTIGATION
from finops.tools import cost_queries as cq
from finops.tools import notifications as N

_CARDINALITY = {
    "total_custom_metrics": 2_100,
    "estimated_monthly_cost": 570.0,
    "high_cardinality_namespaces": [
        {"namespace": "App/Orders", "region": "us-east-1", "metric_count": 1_500,
         "estimated_monthly_cost": 450.0, "high_cardinality_dimensions": ["pod_id"],
         "recommendation": "Drop pod_id"},
        {"namespace": "App/Search", "region": "us-east-1", "metric_count": 400,
         "estimated_monthly_cost": 120.0, "high_cardinality_dimensions": [],
         "recommendation": "Review"},
    ],
}


def _unwrap(tool):
    return getattr(tool, "fn", tool)


class _FakeAWS:
    async def is_configured(self):
        return True

    def _client(self, name):
        raise RuntimeError("no AWS in tests")

    async def list_accounts(self):
        return []


@pytest.fixture
def swept(monkeypatch):
    monkeypatch.setitem(_srv.CLOUD_CONNECTORS, "aws", _FakeAWS())
    monkeypatch.setattr(S, "build_specs", lambda aws, regions: [
        ("cw_cardinality", lambda **k: _CARDINALITY, {}),
        ("ipv4", lambda **k: {"total_monthly_waste": 10.8,
                              "unattached_eips": [1, 2, 3]}, {}),
    ])


def test_each_namespace_is_an_investigation_with_no_saving():
    rows = S.normalise("cw_cardinality", _CARDINALITY)
    assert len(rows) == 2, rows
    for row in rows:
        assert row["monthly_savings"] is None, row
        assert row["kind"] == INVESTIGATION
        assert row["rough_monthly"] > 0
        assert "not a saving" in row["detail"]


def test_the_sweep_total_holds_no_metric_bill(swept):
    result = asyncio.run(S.sweep(_srv.CLOUD_CONNECTORS["aws"], deadline_s=30))
    cw = [f for f in result.findings if "CloudWatch metric" in f["title"]]
    assert len(cw) == 2, result.findings
    assert all(f["monthly_savings"] is None for f in cw)
    total = sum(f.get("monthly_savings") or 0 for f in result.findings)
    assert round(total, 2) == 10.80


def test_the_audit_headline_holds_no_metric_bill(swept):
    out = asyncio.run(_unwrap(cq.run_full_cost_audit)())
    assert "Estimated monthly saving: $10.80" in out, out
    assert "$580.80" not in out, out
    # still listed, as an investigation to confirm
    assert "Review CloudWatch metric cardinality in App/Orders" in out, out


def test_the_csv_and_notion_totals_hold_no_metric_bill(swept, tmp_path, monkeypatch):
    dest = tmp_path / "report.csv"
    msg = asyncio.run(_unwrap(N.export_cost_report_csv)(output_path=str(dest)))
    assert "Total estimated saving: $10.80/mo" in msg, msg
    rows = list(csv.reader(dest.open()))
    assert ["Total monthly saving", "$10.80"] in rows, rows

    class _Notion:
        async def is_configured(self):
            return True

        async def write_cost_report(self, report):
            _Notion.seen = report
            return "https://notion.so/fake-page"

    import finops.connectors.saas.notion as notion_mod
    monkeypatch.setattr(notion_mod, "NotionConnector", _Notion)
    asyncio.run(_unwrap(N.publish_cost_report_to_notion)())
    assert round(_Notion.seen["total_monthly_savings"], 2) == 10.80, _Notion.seen
