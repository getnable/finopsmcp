"""The recommendation trust envelope: measured -> recommendation (precise $),
inferred -> investigation (magnitude band, never a precise number)."""
from __future__ import annotations

from finops.recommendations import envelope as env
from finops.recommendations import textract_env as t


def test_classify_by_evidence_not_dollar_size():
    assert env.classify(env.MEASURED) == "recommendation"
    assert env.classify(env.INFERRED) == "investigation"


def test_magnitude_band_is_order_of_magnitude_only():
    assert env.magnitude_band(40) == "under ~$100/mo"
    assert env.magnitude_band(300) == "~hundreds/mo"
    assert env.magnitude_band(1654) == "~thousands/mo"
    assert env.magnitude_band(25_000) == "~tens of thousands/mo"
    assert env.magnitude_band(None) == "unknown size"   # no estimate != "small"
    assert env.magnitude_band(0) == "under ~$100/mo"    # a real ~zero estimate is small


def test_investigation_strips_precise_dollars():
    f = env.Finding(source="x", title="t", why="w", evidence=env.INFERRED,
                    rough_monthly=1654, est_monthly_savings=1654)
    d = f.to_dict()
    assert d["kind"] == "investigation"
    assert d["est_monthly_savings"] is None       # precise number forced off
    assert d["magnitude"] == "~thousands/mo"       # only a band survives
    assert "rough_monthly" not in d                # internal proxy not leaked


def test_recommendation_keeps_precise_dollars():
    f = env.Finding(source="x", title="t", why="w", evidence=env.MEASURED,
                    est_monthly_savings=1654.0)
    d = f.to_dict()
    assert d["kind"] == "recommendation"
    assert d["est_monthly_savings"] == 1654.0
    assert d["magnitude"] == ""


def _stub_clients(monkeypatch):
    monkeypatch.setattr(t, "_make_ce", lambda role_arn=None: object())
    monkeypatch.setattr(t, "_make_lambda", lambda region, role_arn=None: object())
    monkeypatch.setattr(t, "_make_cloudtrail", lambda region="us-east-1", role_arn=None: object())
    monkeypatch.setattr(t, "_get_cloudtrail_callers", lambda ct, s, e: {})


def test_textract_name_heuristic_is_an_investigation(monkeypatch):
    # No useful env tags + Lambda name heuristic -> investigation, no precise $.
    _stub_clients(monkeypatch)
    monkeypatch.setattr(t, "_get_total_textract_spend", lambda ce, s, e: 5000.0)
    monkeypatch.setattr(t, "_get_tagged_env_breakdown",
                        lambda ce, s, e: {"prod": 0.0, "staging": 0.0, "qa": 0.0, "unknown": 0.0})
    monkeypatch.setattr(t, "_get_lambda_nonprod_callers",
                        lambda lam, total: [{"function_name": "devFoo", "env_signal": "dev",
                                             "estimated_spend": 1600.0, "call_count": None,
                                             "source": "lambda"}])
    f = t.scan_textract_environment_waste(days=30)["finding"]
    assert f and f["kind"] == "investigation"
    assert f["est_monthly_savings"] is None        # never a precise number on a heuristic
    assert f["magnitude"]                          # but it conveys size
    assert f["confirm_steps"]                      # and how to get to certainty
    assert f["why_unsure"]                         # and is honest about the gap
    # The honest upsell: Pro data access (CUR/CloudTrail) auto-confirms it.
    assert f["pro_can_confirm"] is True
    assert "CUR" in f["pro_unlock"] or "CloudTrail" in f["pro_unlock"]


def test_textract_tagged_spend_is_a_recommendation(monkeypatch):
    # Environment-tagged spend is measured -> a recommendation with a precise number.
    _stub_clients(monkeypatch)
    monkeypatch.setattr(t, "_get_total_textract_spend", lambda ce, s, e: 5000.0)
    monkeypatch.setattr(t, "_get_tagged_env_breakdown",
                        lambda ce, s, e: {"prod": 3000.0, "staging": 1000.0,
                                          "qa": 1000.0, "unknown": 0.0})
    f = t.scan_textract_environment_waste(days=30)["finding"]
    assert f and f["kind"] == "recommendation"
    assert f["est_monthly_savings"] and f["est_monthly_savings"] > 0
    assert f["magnitude"] == ""
    assert f["remediation"]
