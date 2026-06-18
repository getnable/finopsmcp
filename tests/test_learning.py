"""Tests for the per-customer recommendation learning loop (finops.recommendations.learning)."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from finops.recommendations.learning.reasons import classify_dismiss_reason
from finops.recommendations.learning.rescorer import rescore
from finops.recommendations.learning.signal import signal_for


# ── ledger fixture + seeding ──────────────────────────────────────────────────

@pytest.fixture
def ledger(monkeypatch):
    td = tempfile.TemporaryDirectory()
    monkeypatch.setenv("FINOPS_DB_PATH", str(Path(td.name) / "t.db"))
    import finops.storage.db as db_mod
    db_mod._ENGINE = None
    yield db_mod
    db_mod._ENGINE = None
    td.cleanup()


_seq = [0]


def _seed(source, status, est=100.0, ver=None, n=1):
    from finops.storage.db import get_engine, savings_recommendations
    now = datetime.now(timezone.utc)
    with get_engine().begin() as conn:
        for _ in range(n):
            _seq[0] += 1
            conn.execute(savings_recommendations.insert().values(
                source=source, provider="aws", status=status,
                estimated_monthly_savings_usd=est, verified_monthly_savings_usd=ver,
                generated_at=now, dedup_key=f"k{_seq[0]}", resource_id=f"r{_seq[0]}",
            ))


def _by_source(sig):
    return {s["source"]: s for s in sig["by_source"]}


# ── signal: coverage ladder + shrinkage ───────────────────────────────────────

def test_cold_source_uses_prior_not_zero(ledger):
    from finops.recommendations.learning.signal import customer_signal
    _seed("commitment", "open", n=5)  # only open recs => 0 resolved => COLD
    s = _by_source(customer_signal())["commitment"]
    assert s["coverage"] == "COLD"
    assert s["resolved"] == 0
    assert s["act_rate"] == 0.4          # pulled to the prior, not 0
    assert s["verdict"] == "neutral"


def test_single_dismissal_cannot_nuke_a_source(ledger):
    """Shrinkage: one dismissal stays near the prior, never collapses to 0."""
    from finops.recommendations.learning.signal import customer_signal
    _seed("spot", "dismissed", n=1)      # 1 resolved, 0 acted
    s = _by_source(customer_signal())["spot"]
    assert s["coverage"] == "WARMING"
    assert s["act_rate"] > 0.3 and s["act_rate"] < 0.4   # near prior, shrunk
    assert s["verdict"] == "neutral"     # WARMING never suppresses


def test_warm_low_act_rate_is_suppressed(ledger):
    from finops.recommendations.learning.signal import customer_signal
    _seed("spot", "dismissed", n=12)     # 12 resolved, 0 acted => WARM + very low act-rate
    s = _by_source(customer_signal())["spot"]
    assert s["coverage"] == "WARM"
    assert s["act_rate"] < 0.15
    assert s["verdict"] == "suppress"


def test_warm_high_act_rate_accurate_is_boosted(ledger):
    from finops.recommendations.learning.signal import customer_signal
    _seed("rightsizing", "verified", est=100.0, ver=100.0, n=9)  # acted + accurate
    _seed("rightsizing", "dismissed", n=1)                        # 10 resolved, 9 acted
    s = _by_source(customer_signal())["rightsizing"]
    assert s["coverage"] == "WARM"
    assert s["act_rate"] >= 0.5
    assert s["accuracy"] == 1.0          # realized == predicted
    assert s["verdict"] == "boost"


def test_accuracy_reflects_over_prediction(ledger):
    from finops.recommendations.learning.signal import customer_signal
    _seed("idle", "verified", est=100.0, ver=60.0, n=4)  # realized 60% of predicted
    s = _by_source(customer_signal())["idle"]
    assert s["accuracy"] == 0.6
    # over-prediction drags the confidence multiplier below the act-rate
    assert s["confidence_multiplier"] < s["act_rate"]


# ── rescorer: reorder + suppress + propose-only ───────────────────────────────

def test_rescore_reorders_and_suppresses(ledger):
    from finops.recommendations.learning.signal import customer_signal
    _seed("spot", "dismissed", n=12)                                  # -> suppress
    _seed("rightsizing", "verified", est=100, ver=100, n=9)
    _seed("rightsizing", "dismissed", n=1)                            # -> boost
    sig = customer_signal()
    recs = [
        {"source": "spot", "estimated_monthly_savings_usd": 500, "status": "open", "id": 1},
        {"source": "rightsizing", "estimated_monthly_savings_usd": 100, "status": "open", "id": 2},
        {"source": "rightsizing", "estimated_monthly_savings_usd": 50, "status": "open", "id": 3},
    ]
    out = rescore(recs, sig)
    # the big spot rec is suppressed despite the largest raw savings
    assert out["suppressed_count"] == 1
    assert out["suppressed_for_you"][0]["id"] == 1
    # the two rightsizing recs are ranked (boosted source), bigger one first
    assert [r["id"] for r in out["ranked"]] == [2, 3]
    assert out["ranked"][0]["learned"]["new_rank"] == 0
    assert out["ranked"][0]["learned"]["why_ranked"]


def test_rescore_is_propose_only_never_mutates_status(ledger):
    from finops.recommendations.learning.signal import customer_signal
    sig = customer_signal()
    recs = [{"source": "rightsizing", "estimated_monthly_savings_usd": 100, "status": "open", "id": 1}]
    out = rescore(recs, sig)
    # input object untouched (no 'learned' leaked back, status intact)
    assert recs[0] == {"source": "rightsizing", "estimated_monthly_savings_usd": 100, "status": "open", "id": 1}
    # every output rec keeps its original status exactly
    for r in out["ranked"] + out["suppressed_for_you"]:
        assert r["status"] == "open"


def test_rescorer_imports_nothing_that_can_mutate_cloud():
    """Structural 'save 100% can't destroy anything' guarantee: the rescorer must not
    IMPORT or CALL any cloud/mutating capability. Checked via the AST so docstring
    prose (which names what it avoids) doesn't count, only real imports and references."""
    import ast
    import inspect
    from finops.recommendations.learning import rescorer

    tree = ast.parse(inspect.getsource(rescorer))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {n.name.split(".")[0] for n in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    forbidden_modules = {"boto3", "botocore", "subprocess", "socket", "urllib",
                         "requests", "os", "sys"}
    assert not (imported & forbidden_modules), \
        f"rescorer imports forbidden modules: {imported & forbidden_modules}"

    referenced = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    referenced |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    forbidden_calls = {"call_tool", "execute_bridge_tool", "mark_acted_on",
                       "mark_verified", "open_rightsizing_pr", "create_ticket", "system"}
    assert not (referenced & forbidden_calls), \
        f"rescorer references forbidden calls: {referenced & forbidden_calls}"


# ── reasons classifier ────────────────────────────────────────────────────────

def test_classify_dismiss_reason():
    assert classify_dismiss_reason("reserved for our Black Friday peak") == "reserved_for_peak"
    assert classify_dismiss_reason("this is SLA critical, can't risk it") == "sla_sensitive"
    assert classify_dismiss_reason("already in next sprint") == "already_planned"
    assert classify_dismiss_reason("the estimate is wrong, it doesn't save that") == "wrong_estimate"
    assert classify_dismiss_reason("owned by another team") == "not_our_resource"
    assert classify_dismiss_reason("nah") == "other"
    assert classify_dismiss_reason("") == "other"
    assert classify_dismiss_reason(None) == "other"


# ── signal_for default ────────────────────────────────────────────────────────

def test_signal_for_unknown_source_returns_cold_default():
    s = signal_for({"by_source": []}, "brand_new_source")
    assert s["coverage"] == "COLD" and s["verdict"] == "neutral"
    assert s["act_rate"] == 0.4
