"""Tests for policy inference: turning repeated dismissals into proposed standing rules.

The loop: a team dismisses the same class of finding for a business reason enough
times, and nable proposes one durable rule (a context_memory annotation) instead of
nagging per finding. Propose-only: inference never writes; confirming a candidate is
what calls remember().
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from finops.recommendations import context_memory as cm
from finops.recommendations import savings_tracker as st
from finops.recommendations.learning import policy_inference as pi


@pytest.fixture
def ledger(monkeypatch):
    td = tempfile.TemporaryDirectory()
    monkeypatch.setenv("FINOPS_DB_PATH", str(Path(td.name) / "t.db"))
    import finops.storage.db as db_mod
    db_mod._ENGINE = None
    yield db_mod
    db_mod._ENGINE = None
    td.cleanup()


def _seed(source, resource_id, bucket=None, provider="aws", resource_type="ec2"):
    return st.record_recommendation(
        source=source, provider=provider, account_id="123",
        resource_id=resource_id, resource_type=resource_type, resource_name=resource_id,
        current_config={}, recommended_config={"a": 1}, description="finding",
        estimated_monthly_savings_usd=100.0, environment=bucket,
    )


def _dismiss_business(rec_id, reason="reserved for peak"):
    assert st.mark_dismissed(rec_id, reason) is True


def test_no_candidates_below_support(ledger):
    for i in range(2):  # only 2 dismissals, threshold is 3
        rid = _seed("rightsizing", f"i-{i}")
        _dismiss_business(rid)
    assert pi.infer_policies() == []


def test_source_level_policy_inferred(ledger):
    for i in range(4):
        rid = _seed("rightsizing", f"i-{i}")
        _dismiss_business(rid, "reserved for our black friday peak")
    cands = pi.infer_policies()
    src = [c for c in cands if c["scope"] == "source" and c["match_value"] == "rightsizing"]
    assert src, f"expected a source-level candidate, got {cands}"
    c = src[0]
    assert c["support"] == 4
    assert c["consistency"] == 1.0
    assert c["dominant_reason"] == "reserved_for_peak"
    assert 'scope="source"' in c["confirm"]


def test_consistency_gate_blocks_mixed_behavior(ledger):
    # 3 dismissed as intentional, but 3 acted on -> consistency 0.5, below 0.8
    for i in range(3):
        _dismiss_business(_seed("idle", f"d-{i}"))
    for i in range(3):
        st.mark_acted_on(_seed("idle", f"a-{i}"))
    assert [c for c in pi.infer_policies() if c["match_value"] == "idle"] == []


def test_quality_dismissals_do_not_count(ledger):
    # "estimate is wrong" is a quality miss, not a business keep -> no policy
    for i in range(4):
        rid = _seed("rightsizing", f"i-{i}")
        st.mark_dismissed(rid, "the estimate is wrong, it doesn't save that")
    assert pi.infer_policies() == []


def test_bucket_level_policy_inferred(ledger):
    # different sources, same (recognized) env bucket, all rejected -> a bucket rule.
    # Two distinct sources satisfy the over-suppression diversity guard.
    for i in range(4):
        rid = _seed("idle" if i % 2 else "rightsizing", f"p-{i}", bucket="production")
        _dismiss_business(rid, "SLA-critical prod, can't risk it")
    cands = pi.infer_policies()
    bucket_cands = [c for c in cands if c["scope"] == "bucket"]
    assert bucket_cands, cands
    assert bucket_cands[0]["match_value"].startswith("prod")


def test_broad_rule_needs_multiple_sources(ledger):
    # 4 dismissals but all one source: propose the source rule, NOT provider/type rules.
    for i in range(4):
        _dismiss_business(_seed("rightsizing", f"i-{i}"))
    scopes = {c["scope"] for c in pi.infer_policies()}
    assert scopes == {"source"}, f"broad rules leaked from a single source: {scopes}"


def test_covered_candidates_are_skipped(ledger):
    for i in range(4):
        _dismiss_business(_seed("rightsizing", f"i-{i}"))
    # once a rule exists, stop proposing it
    cm.remember("source", "rightsizing", "handled manually")
    assert [c for c in pi.infer_policies() if c["match_value"] == "rightsizing"] == []


def test_policy_for_rec_matches_crossed_threshold(ledger):
    for i in range(4):
        _dismiss_business(_seed("rightsizing", f"i-{i}"))
    hit = pi.policy_for_rec({"source": "rightsizing", "environment_bucket": None,
                             "provider": "aws", "resource_type": "ec2"})
    assert hit and hit["scope"] == "source" and hit["match_value"] == "rightsizing"
    miss = pi.policy_for_rec({"source": "commitment", "environment_bucket": None,
                              "provider": "aws", "resource_type": "ec2"})
    assert miss is None


def test_inference_never_writes(ledger):
    for i in range(4):
        _dismiss_business(_seed("rightsizing", f"i-{i}"))
    pi.infer_policies()
    # proposing must not create any annotation; only remember() does
    assert cm.list_context() == []
