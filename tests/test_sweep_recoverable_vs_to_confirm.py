"""The sweep total separates what was measured from what is inferred, as the scan does.

`nable scan` prints the measured savings as recoverable and the rest beside it
as "to confirm" (recoverable.monthly_usd, recoverable.to_confirm_monthly_usd,
recoverable.to_confirm_findings). run_full_cost_audit, the CSV export and the
Notion page added every row into one "Estimated monthly saving", though most
of the sweep's scanners file what they find as an INFERRED investigation in
their own envelope: a Graviton move (graviton.py, "assumes ... performs the
same"), a Database Savings Plan (database_savings_plans.py), a schedule, an
old snapshot priced at its full volume size. On a two-region fixture account
the audit said $757.81/mo; $363.44 of it was measured and $394.37, 13 rows,
rests on an inference.

Now each row a scanner filed as an inference is marked to_confirm, the total
is the measured rows, and the inferred ones are named beside it under the
scan's keys. A row whose scanner says nothing about its evidence counts as
measured, as in the scan.
"""
from __future__ import annotations

import asyncio
import csv

import pytest

from finops import server as _srv
from finops.cleanup.idle import IdleResource
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


_INFERRED = {"kind": "investigation", "est_monthly_savings": None}
_MEASURED = {"kind": "recommendation", "est_monthly_savings": 10.95}


def _idle(rtype: str, rid: str, usd: float) -> IdleResource:
    return IdleResource(resource_type=rtype, resource_id=rid, region="us-east-1",
                        account_id="111122223333", name=rid, idle_since="2026-01-01",
                        idle_days=200, monthly_cost_usd=usd, reason="idle")


@pytest.fixture
def swept(monkeypatch):
    monkeypatch.setitem(_srv.CLOUD_CONNECTORS, "aws", _FakeAWS())
    monkeypatch.setattr(_srv, "require_role", lambda *a, **k: None)
    monkeypatch.setattr(S, "build_specs", lambda aws, regions: [
        ("graviton", lambda **k: [{
            "instance_id": "i-big", "instance_type": "r5.2xlarge",
            "graviton_equivalent": "r7g.2xlarge", "savings_estimate": 55.19,
            "savings_pct": 15.0, "region": "us-east-1", "finding": dict(_INFERRED)}], {}),
        ("db_sp", lambda **k: {"estimated_monthly_savings": 68.83,
                               "finding": dict(_INFERRED)}, {}),
        ("ipv4", lambda **k: {"total_monthly_waste": 10.95, "unattached_eips": [1, 2, 3],
                              "finding": dict(_MEASURED)}, {}),
        ("idle_resources", lambda **k: [_idle("ebs_volume", "vol-1", 40.0),
                                        _idle("snapshot", "snap-1", 10.0)], {}),
    ])


def test_rows_filed_as_an_inference_are_to_confirm(swept):
    res = asyncio.run(S.sweep(_srv.CLOUD_CONNECTORS["aws"], deadline_s=30))
    to_confirm = {f["title"] for f in res.findings if f.get("to_confirm")}
    assert to_confirm == {
        "Migrate i-big (r5.2xlarge → r7g.2xlarge)",
        "Purchase Database Savings Plan for RDS/Aurora",
        "Snapshot: snap-1",   # an old snapshot is an inference in the audit too
    }, to_confirm
    assert S.split_total(res.findings) == (pytest.approx(50.95), pytest.approx(134.02), 3)


def test_the_audit_headline_is_the_measured_total(swept):
    out = asyncio.run(_unwrap(cq.run_full_cost_audit)())
    assert "Estimated monthly saving: $50.95 | Annual: $611.40** (recoverable" in out, out
    assert "$134.02/mo to confirm" in out, out
    assert "$184.97" not in out, out
    assert "| $55.19 (to confirm) |" in out, out


def test_the_csv_and_notion_carry_the_same_split(swept, tmp_path, monkeypatch):
    dest = tmp_path / "r.csv"
    msg = asyncio.run(_unwrap(N.export_cost_report_csv)(output_path=str(dest)))
    rows = list(csv.reader(dest.open()))
    assert ["Total monthly saving", "$50.95"] in rows, rows
    assert ["To confirm monthly", "$134.02"] in rows, rows
    assert ["To confirm findings", "3"] in rows, rows
    assert "$50.95/mo" in msg and "$134.02/mo to confirm" in msg, msg
    graviton = next(r for r in rows if len(r) > 1 and r[1].startswith("Migrate i-big"))
    assert graviton[5].startswith("to confirm"), graviton

    class _Notion:
        async def is_configured(self):
            return True

        async def write_cost_report(self, report):
            _Notion.seen = report
            return "https://notion.so/fake-page"

    import finops.connectors.saas.notion as notion_mod
    monkeypatch.setattr(notion_mod, "NotionConnector", _Notion)
    asyncio.run(_unwrap(N.publish_cost_report_to_notion)())
    seen = _Notion.seen
    assert seen["total_monthly_savings"] == pytest.approx(50.95)
    assert seen["to_confirm_monthly_usd"] == pytest.approx(134.02)
    assert seen["to_confirm_findings"] == 3


def test_the_notion_page_prints_the_split():
    from finops.connectors.saas.notion import NotionConnector

    blocks = NotionConnector.__new__(NotionConnector)._build_report_blocks(
        {"findings": [{"title": "Migrate i-big", "category": "Compute",
                       "monthly_savings": 55.19, "to_confirm": True}],
         "total_monthly_savings": 0.0, "total_annual_savings": 0.0,
         "to_confirm_monthly_usd": 55.19, "to_confirm_findings": 1}, "2026-10-17")
    text = str(blocks)
    assert "$55.19/mo to confirm" in text, text
    assert "$55.19 (to confirm)" in text, text
