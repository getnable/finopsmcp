"""Tests for context memory: the operating model nable learns from a human answering once.

Covers remember/forget/list + match/partition, the rescore integration (the
suppressed_by_context bucket), and the auto-remember that a business-reason dismiss
performs so nable never re-flags a resource it was told is fine.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from finops.recommendations import context_memory as cm
from finops.recommendations import savings_tracker as st
from finops.recommendations.learning.rescorer import rescore


@pytest.fixture
def ledger(monkeypatch):
    td = tempfile.TemporaryDirectory()
    monkeypatch.setenv("FINOPS_DB_PATH", str(Path(td.name) / "t.db"))
    import finops.storage.db as db_mod
    db_mod._ENGINE = None
    yield db_mod
    db_mod._ENGINE = None
    td.cleanup()


def _seed(source="idle", resource_id="i-0abc", resource_type="ec2",
          provider="aws", account_id="123", est=100.0, bucket=None):
    return st.record_recommendation(
        source=source, provider=provider, account_id=account_id,
        resource_id=resource_id, resource_type=resource_type,
        resource_name=resource_id, current_config={}, recommended_config={"a": 1},
        description="finding", estimated_monthly_savings_usd=est, environment=bucket,
    )


# ── remember / list / forget ──────────────────────────────────────────────────

def test_remember_and_list(ledger):
    ann = cm.remember("resource", "i-0abc", "DR standby", provider="aws", created_by="user")
    assert ann["scope"] == "resource"
    assert ann["match_value"] == "i-0abc"
    entries = cm.list_context()
    assert len(entries) == 1
    assert entries[0]["reason"] == "DR standby"
    assert entries[0]["active"] is True


def test_remember_is_idempotent_and_refreshes_reason(ledger):
    a = cm.remember("source", "rightsizing", "handled manually")
    b = cm.remember("source", "rightsizing", "handled in weekly review")
    assert a["id"] == b["id"]
    entries = cm.list_context()
    assert len(entries) == 1
    assert entries[0]["reason"] == "handled in weekly review"


def test_remember_rejects_bad_scope(ledger):
    with pytest.raises(ValueError):
        cm.remember("nonsense", "x", "why")


def test_remember_requires_match_value(ledger):
    with pytest.raises(ValueError):
        cm.remember("resource", "   ", "why")


def test_forget_is_soft_delete(ledger):
    ann = cm.remember("resource", "i-0abc", "DR standby")
    assert cm.forget(ann["id"]) is True
    assert cm.list_context() == []
    # trail preserved
    all_entries = cm.list_context(include_inactive=True)
    assert len(all_entries) == 1 and all_entries[0]["active"] is False
    # forgetting again is a no-op
    assert cm.forget(ann["id"]) is False


# ── match / partition across scopes ───────────────────────────────────────────

def _rec(**kw):
    base = {"resource_id": "i-0abc", "resource_type": "ec2", "provider": "aws",
            "account_id": "123", "source": "idle", "environment_bucket": None,
            "estimated_monthly_savings_usd": 100.0, "status": "open"}
    base.update(kw)
    return base


def test_match_resource_scope(ledger):
    cm.remember("resource", "i-0abc", "DR standby")
    assert cm.match(_rec(resource_id="i-0abc")) is not None
    assert cm.match(_rec(resource_id="i-0other")) is None


def test_match_resource_type_scope(ledger):
    cm.remember("resource_type", "nat_gateway", "load-bearing")
    assert cm.match(_rec(resource_type="nat_gateway")) is not None
    assert cm.match(_rec(resource_type="ec2")) is None


def test_match_bucket_scope(ledger):
    cm.remember("bucket", "dr", "disaster recovery, keep warm")
    assert cm.match(_rec(environment_bucket="dr")) is not None
    assert cm.match(_rec(environment_bucket="prod")) is None


def test_match_source_scope(ledger):
    cm.remember("source", "rightsizing", "we do this manually")
    assert cm.match(_rec(source="rightsizing")) is not None
    assert cm.match(_rec(source="idle")) is None


def test_match_provider_narrowing(ledger):
    # a source rule scoped to only the aws provider must not silence gcp findings
    cm.remember("source", "idle", "aws idle is fine", provider="aws")
    assert cm.match(_rec(source="idle", provider="aws")) is not None
    assert cm.match(_rec(source="idle", provider="gcp")) is None


def test_match_narrowest_wins(ledger):
    cm.remember("provider", "aws", "broad rule")
    cm.remember("resource", "i-0abc", "specific reason")
    hit = cm.match(_rec(resource_id="i-0abc", provider="aws"))
    assert hit["scope"] == "resource"
    assert hit["reason"] == "specific reason"


def test_partition_splits_and_annotates(ledger):
    cm.remember("resource", "i-0abc", "DR standby")
    recs = [_rec(resource_id="i-0abc"), _rec(resource_id="i-0keep")]
    visible, suppressed = cm.partition(recs)
    assert [r["resource_id"] for r in visible] == ["i-0keep"]
    assert len(suppressed) == 1
    assert suppressed[0]["context"]["reason"] == "DR standby"
    assert suppressed[0]["context"]["suppressed"] is True
    # inputs never mutated
    assert "context" not in recs[0]


def test_partition_empty_memory_is_noop(ledger):
    recs = [_rec(), _rec(resource_id="i-0z")]
    visible, suppressed = cm.partition(recs)
    assert len(visible) == 2 and suppressed == []


# ── rescore integration ───────────────────────────────────────────────────────

def test_rescore_routes_intentional_to_context_bucket(ledger):
    cm.remember("resource", "i-0abc", "DR standby")
    recs = [_rec(resource_id="i-0abc", est=120.0), _rec(resource_id="i-0keep", est=80.0)]
    rs = rescore(recs, {"by_source": []})
    ranked_ids = [r["resource_id"] for r in rs["ranked"]]
    assert ranked_ids == ["i-0keep"]
    assert [r["resource_id"] for r in rs["suppressed_by_context"]] == ["i-0abc"]


def test_rescore_use_context_false_keeps_everything(ledger):
    cm.remember("resource", "i-0abc", "DR standby")
    recs = [_rec(resource_id="i-0abc"), _rec(resource_id="i-0keep")]
    rs = rescore(recs, {"by_source": []}, use_context=False)
    assert rs["suppressed_by_context"] == []
    assert len(rs["ranked"]) == 2


# ── dismiss → auto-remember (through the real tracker) ─────────────────────────

def test_business_reason_dismiss_could_feed_context(ledger):
    # The server tool wires get_recommendation + remember; here we prove the tracker
    # pieces it needs exist and round-trip.
    rec_id = _seed(resource_id="i-0dr")
    assert st.mark_dismissed(rec_id, "reserved for our DR failover") is True
    fetched = st.get_recommendation(rec_id)
    assert fetched["resource_id"] == "i-0dr"
    assert fetched["status"] == "dismissed"
    # simulate the server auto-remember
    cm.remember("resource", fetched["resource_id"], "reserved for our DR failover",
                provider=fetched["provider"], account_id=fetched["account_id"],
                created_by="dismiss", source_rec_id=rec_id)
    # a fresh identical finding is now suppressed
    recs = [_rec(resource_id="i-0dr")]
    visible, suppressed = cm.partition(recs)
    assert visible == [] and len(suppressed) == 1
