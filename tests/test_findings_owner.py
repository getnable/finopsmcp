"""Findings and recommendations carry their owner from the org model.

What has to stay true:
  - a recommendation or finding the org model can place (by tags, namespace,
    account) gets `owner: {team, channel?, confirmed}`, nothing more
  - a confirmed answer beats a proposed one; a proposal is marked unconfirmed
  - no org model, or a broken one, leaves the payload exactly as it was
"""
from __future__ import annotations

import asyncio

import pytest

from finops import org, org_owner
from finops import server as _server  # noqa: F401  (wires the tool modules)
from finops.cleanup.idle import IdleResource
from finops.org.cli import _who as human
from finops.tools import aws_waste, meta

ACCT = "123456789012"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_TAG_RULES", "FINOPS_REQUIRED_TAGS", "FINOPS_PROTECTED_TAGS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "none.yaml"))
    import finops.storage.db as _db
    monkeypatch.setattr(_db, "_DATA_DIR", None)
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))


def confirmed(kind, subject, value):
    org.set_fact(org.make_fact(kind, subject, value, source="human"), human("maria"))


def proposed(kind, subject, value):
    org.propose(org.make_fact(kind, subject, value, source="inference", confidence=0.9))


def _fn(tool):
    return tool.fn if hasattr(tool, "fn") else tool


def _call(tool, **kw):
    out = _fn(tool)(**kw)
    return asyncio.run(out) if asyncio.iscoroutine(out) else out


def test_owner_for_reads_tags_then_namespace_then_account():
    confirmed("team", "team:payments", {"name": "payments", "channel": "#pay"})
    confirmed("owner", f"aws_account:{ACCT}", {"team": "platform"})
    confirmed("owner", "k8s_namespace:checkout", {"team": "checkout"})
    confirmed("tag_key", "org:org", {"canonical": "team", "keys": ["Team"]})
    confirmed("tag_alias", "tag_value:pay", {"canonical_key": "team",
                                             "canonical_value": "payments"})
    o = org_owner.owner_for({"account_id": ACCT, "tags": {"Team": "pay"}})
    assert o.compact() == {"team": "payments", "channel": "#pay", "confirmed": True}
    assert org_owner.owner_for({"account_id": ACCT, "namespace": "checkout",
                                "cluster": "eu-1"}).team == "checkout"
    assert org_owner.owner_for({"account_id": ACCT}).compact() == {
        "team": "platform", "confirmed": True}
    assert org_owner.owner_for({"account_id": "999999999999"}) is None


def test_a_confirmed_owner_beats_a_proposed_one():
    proposed("owner", "k8s_namespace:checkout", {"team": "guess"})
    confirmed("owner", f"aws_account:{ACCT}", {"team": "platform"})
    assert org_owner.owner_for({"namespace": "checkout", "account_id": ACCT}).team == "platform"
    o = org_owner.owner_for({"namespace": "checkout"})
    assert o.compact() == {"team": "guess", "confirmed": False}
    assert o.people == []


def test_a_tracked_recommendation_is_placed_by_its_stored_tags():
    confirmed("owner", f"aws_account:{ACCT}", {"team": "platform"})
    rec = {"account_id": ACCT, "provider": "aws",
           "current_config": '{"instance_type": "m5.large", "tags": {"team": "search"}}'}
    confirmed("team", "team:search", {"name": "search"})
    # The conventional `team` key, unconfirmed, is only a proposal: the
    # confirmed account owner answers until a person says what `team` means.
    assert org_owner.owner_for(rec).team == "platform"
    confirmed("tag_key", "org:org", {"canonical": "team", "keys": ["team"]})
    assert org_owner.owner_for(rec).compact() == {"team": "search", "confirmed": True}


def test_no_org_model_leaves_the_items_alone(monkeypatch):
    items = [{"account_id": ACCT, "tags": {"team": "payments"}}]
    assert org_owner.annotate(items) == [{"account_id": ACCT, "tags": {"team": "payments"}}]
    confirmed("owner", f"aws_account:{ACCT}", {"team": "platform"})
    monkeypatch.setattr(org_owner, "load_model", lambda: 1 / 0)
    assert org_owner.annotate([{"account_id": ACCT}]) == [{"account_id": ACCT}]


def test_list_savings_recommendations_carries_the_owner(monkeypatch):
    from finops.recommendations import savings_tracker
    rows = [
        {"id": 1, "source": "rightsizing", "provider": "aws", "account_id": ACCT,
         "resource_id": "i-1", "estimated_monthly_savings_usd": 120.0,
         "verified_monthly_savings_usd": None, "status": "open"},
        {"id": 2, "source": "idle", "provider": "aws", "account_id": "999999999999",
         "resource_id": "vol-1", "estimated_monthly_savings_usd": 30.0,
         "verified_monthly_savings_usd": None, "status": "open"},
    ]
    monkeypatch.setattr(savings_tracker, "list_recommendations",
                        lambda **_: [dict(r) for r in rows])
    out = _call(meta.list_savings_recommendations, status="open")
    assert all("owner" not in r for r in out["recommendations"])
    confirmed("owner", f"aws_account:{ACCT}", {"team": "payments", "channel": "#pay"})
    out = _call(meta.list_savings_recommendations, status="open")
    by_id = {r["id"]: r for r in out["recommendations"]}
    assert by_id[1]["owner"] == {"team": "payments", "channel": "#pay", "confirmed": True}
    assert "owner" not in by_id[2]


def test_the_waste_audit_findings_carry_the_owner(monkeypatch):
    report = {"account_id": ACCT, "regions_scanned": ["us-east-1"], "total_findings": 1,
              "total_estimated_monthly_savings": 40.0, "total_estimated_annual_savings": 480.0,
              "findings": [{"resource_id": "vol-1", "category": "ebs", "account_id": ACCT,
                            "estimated_monthly_savings": 40.0}]}
    monkeypatch.setattr("finops.analyzers.optimizer.run_deep_audit", lambda *a, **k: report)
    proposed("owner", f"aws_account:{ACCT}", {"team": "payments"})
    out = _call(aws_waste.audit_aws_waste)
    assert out["findings"][0]["owner"] == {"team": "payments", "confirmed": False}


def test_idle_resources_carry_the_owner(monkeypatch):
    import finops.cleanup.idle as idle_mod
    monkeypatch.setattr(idle_mod, "scan_idle_resources", lambda **kw: [IdleResource(
        resource_type="ebs_volume", resource_id="vol-1", region="us-east-1",
        account_id=ACCT, name="orphan", idle_since="2026-06-01", idle_days=40,
        monthly_cost_usd=10.0, reason="Unattached volume")])
    confirmed("owner", f"aws_account:{ACCT}", {"team": "payments"})
    out = _call(aws_waste.list_idle_resources)
    assert out["resources"][0]["owner"] == {"team": "payments", "confirmed": True}
    assert "account_id" not in out["resources"][0]
