"""Tests for cross-org priors: day-one judgment before an install has its own history.

Priors seed acceptance rates per (segment, source) so a brand-new nable ranks
findings the way a team like yours would, then fade as real signal accumulates.
"""
from __future__ import annotations

import pytest

from finops.recommendations import priors
from finops.recommendations.learning.rescorer import rescore


@pytest.fixture(autouse=True)
def _reset_segment_cache():
    priors._reset_cache_for_tests()
    yield
    priors._reset_cache_for_tests()


def test_prior_for_known_source():
    p = priors.prior_for("idle", priors.AI_NATIVE)
    assert p and p["p_accept"] == 0.85 and p["segment"] == "ai_native"
    assert "GPU" in p["rationale"] or "idle" in p["rationale"].lower()


def test_prior_for_unknown_source_is_none():
    assert priors.prior_for("does_not_exist", priors.GENERIC) is None


def test_segment_detects_ai_native_from_connected_families(monkeypatch):
    monkeypatch.setattr("finops.tool_surface.connected_families", lambda: {"aws", "llm"})
    assert priors.segment_of(force=True) == priors.AI_NATIVE


def test_segment_defaults_generic(monkeypatch):
    monkeypatch.setattr("finops.tool_surface.connected_families", lambda: {"aws"})
    assert priors.segment_of(force=True) == priors.GENERIC


def test_segment_is_cached(monkeypatch):
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return {"databricks"}

    monkeypatch.setattr("finops.tool_surface.connected_families", fake)
    a = priors.segment_of(force=True)
    b = priors.segment_of()  # cached, no second detection
    assert a == b == priors.AI_NATIVE
    assert calls["n"] == 1


# ── rescorer integration: cold ranking reflects priors ────────────────────────

def _rec(source, savings):
    return {"source": source, "environment_bucket": None,
            "estimated_monthly_savings_usd": savings, "status": "open",
            "resource_id": f"r-{source}"}


def test_cold_prior_reorders_by_acceptance(monkeypatch):
    # ai_native segment; equal dollars, but idle (p=0.85) should beat commitment (p=0.45)
    monkeypatch.setattr("finops.tool_surface.connected_families", lambda: {"llm"})
    recs = [_rec("commitment", 100.0), _rec("idle", 100.0)]
    out = rescore(recs, {"by_source": []})  # empty signal -> all COLD
    order = [r["source"] for r in out["ranked"]]
    assert order[0] == "idle", order
    top = out["ranked"][0]["learned"]
    assert top["prior"]["p_accept"] == 0.85
    assert top["prior"]["segment"] == "ai_native"
    assert "rationale" in top["prior"]


def test_no_prior_block_for_unknown_source(monkeypatch):
    monkeypatch.setattr("finops.tool_surface.connected_families", lambda: {"aws"})
    out = rescore([_rec("mystery_source", 50.0)], {"by_source": []})
    assert "prior" not in out["ranked"][0]["learned"]


def test_prior_skipped_when_use_context_false(monkeypatch):
    monkeypatch.setattr("finops.tool_surface.connected_families", lambda: {"llm"})
    out = rescore([_rec("idle", 100.0)], {"by_source": []}, use_context=False)
    assert "prior" not in out["ranked"][0]["learned"]
