"""The savings ledger splits what was measured from what is inferred, as the scan does.

get_savings_summary's potential_monthly_usd and get_nable_roi's
found_monthly_usd added every open recommendation into one figure, a Graviton
move (which its scanner files as an INFERRED investigation) beside an
unattached volume (measured). On the fixture account the ledger said $779.95
found. Both now carry, beside the figures existing consumers read, the
scan's split: recoverable.monthly_usd, recoverable.to_confirm_monthly_usd and
recoverable.to_confirm_findings, one fix per resource and the same rule as
the sweep: a resource counts its largest measured saving as recoverable, and
its largest inferred one only for what it claims beyond that.

The recorders now store each row's evidence. A row written before that
(evidence NULL) is classified by what always files inferences: Graviton,
Spot, non-prod schedules, commitments, and an old snapshot; anything else
counts as measured, as an unclassified finding does in the scan.
"""
from __future__ import annotations

import pytest

from finops.recommendations import savings_tracker as st


def _row(source, rid, usd, evidence=None, status="open", resource_type="ec2"):
    return {"status": status, "source": source, "provider": "aws", "resource_id": rid,
            "description": f"{source} {rid}", "estimated_monthly_savings_usd": usd,
            "verified_monthly_savings_usd": None, "verified_basis": None,
            "evidence": evidence, "resource_type": resource_type}


def test_one_resource_measured_ten_inferred_twenty_five():
    t = st.headline_totals([_row("idle", "i-1", 10.0, "measured"),
                            _row("graviton", "i-1", 25.0, "inferred")])
    assert t["potential"] == pytest.approx(25.0)          # unchanged: one fix per resource
    assert t["potential_recoverable"] == pytest.approx(10.0)
    assert t["potential_to_confirm"] == pytest.approx(15.0)
    assert t["potential_to_confirm_count"] == 1
    assert t["found_recoverable"] + t["found_to_confirm"] == pytest.approx(t["found"])


def test_a_legacy_row_is_classified_by_what_always_infers():
    rows = [_row("graviton", "i-1", 20.0), _row("idle", "snap-1", 5.0, resource_type="snapshot"),
            _row("idle", "vol-1", 8.0, resource_type="ebs_volume"),
            _row("rightsizing", "i-2", 30.0)]
    t = st.headline_totals(rows)
    assert t["potential_recoverable"] == pytest.approx(38.0)
    assert t["potential_to_confirm"] == pytest.approx(25.0)
    assert t["potential_to_confirm_count"] == 2


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    from finops.storage import db

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "_ENGINE", None)
    yield
    monkeypatch.setattr(db, "_ENGINE", None)


def _record(rid, usd, source, evidence, rtype="ec2"):
    return st.record_recommendation(
        source=source, provider="aws", resource_id=rid, resource_type=rtype,
        resource_name=rid, current_config={}, recommended_config={"to": source},
        description=f"{source} {rid}", estimated_monthly_savings_usd=usd, evidence=evidence)


def test_the_summary_and_the_roi_carry_the_split(ledger):
    _record("vol-1", 10.0, "idle", "measured", "ebs_volume")
    _record("vol-1", 25.0, "graviton", "inferred")
    _record("i-2", 30.0, "rightsizing", "measured")
    s = st.get_summary()
    assert s["potential_monthly_usd"] == pytest.approx(55.0)
    assert s["recoverable"] == {"monthly_usd": 40.0, "to_confirm_monthly_usd": 15.0,
                                "to_confirm_findings": 1}

    import asyncio
    import inspect

    from finops import server  # noqa: F401  (loads the tools modules in order)
    from finops.tools import cost_queries as cq

    roi = getattr(cq.get_nable_roi, "fn", cq.get_nable_roi)()
    if inspect.isawaitable(roi):
        roi = asyncio.run(roi)
    assert roi["found_monthly_usd"] == pytest.approx(55.0)
    assert roi["recoverable"] == {"monthly_usd": 40.0, "to_confirm_monthly_usd": 15.0,
                                  "to_confirm_findings": 1}
    assert "$40/mo recoverable" in roi["summary"] and "$15/mo to confirm" in roi["summary"]


def test_the_idle_recorder_stores_its_evidence(monkeypatch):
    import asyncio

    from finops import server as srv
    from finops.cleanup import idle
    from finops.tools import aws_waste

    seen = []
    monkeypatch.setattr(st, "record_recommendation", lambda **kw: seen.append(kw))
    monkeypatch.setattr(idle, "scan_idle_resources", lambda **kw: [
        idle.IdleResource(resource_type=t, resource_id=f"{t}-1", region="us-east-1",
                          account_id="1", name="", idle_since="2026-01-01", idle_days=90,
                          monthly_cost_usd=5.0, reason="idle")
        for t in ("ebs_volume", "snapshot")])
    assert srv is not None
    asyncio.run(getattr(aws_waste.list_idle_resources, "fn", aws_waste.list_idle_resources)())
    assert {kw["resource_id"]: kw["evidence"] for kw in seen} == {
        "ebs_volume-1": "measured", "snapshot-1": "inferred"}
