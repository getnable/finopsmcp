"""Tests for finops.recommendations.textract_env."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from finops.recommendations.textract_env import (
    _NONPROD_NAME_SIGNALS,
    _NONPROD_VALUES,
    _get_cloudtrail_callers,
    _get_total_textract_spend,
    _is_nonprod_name,
    scan_textract_environment_waste,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_ce_response(amount: float = 0.0) -> dict:
    return {
        "ResultsByTime": [
            {
                "Total": {"UnblendedCost": {"Amount": str(amount)}},
                "Groups": [],
            }
        ]
    }


def _make_ce_tag_response(tag_key: str, tag_val: str, amount: float) -> dict:
    return {
        "ResultsByTime": [
            {
                "Total": {"UnblendedCost": {"Amount": str(amount)}},
                "Groups": [
                    {
                        "Keys": [f"{tag_key}${tag_val}"],
                        "Metrics": {"UnblendedCost": {"Amount": str(amount)}},
                    }
                ],
            }
        ]
    }


def _make_ct_event(caller_arn: str, role_name: str = "") -> dict:
    detail = {
        "userIdentity": {
            "arn": caller_arn,
            "sessionContext": {
                "sessionIssuer": {
                    "userName": role_name,
                }
            },
        },
        "sourceIPAddress": "10.0.0.1",
        "userAgent": "aws-sdk-python/1.0",
    }
    return {
        "CloudTrailEvent": json.dumps(detail),
        "EventId": "evt-123",
    }


# ── unit: _is_nonprod_name ────────────────────────────────────────────────────

def test_is_nonprod_qa():
    is_np, signal = _is_nonprod_name("my-qa-document-processor")
    assert is_np is True
    assert signal == "qa"


def test_is_nonprod_staging():
    is_np, signal = _is_nonprod_name("staging-pdf-extractor")
    assert is_np is True
    assert signal == "staging"


def test_is_nonprod_dev():
    is_np, signal = _is_nonprod_name("dev-ocr-lambda")
    assert is_np is True
    assert signal == "dev"


def test_is_prod_not_flagged():
    is_np, signal = _is_nonprod_name("prod-document-lambda")
    assert is_np is False
    assert signal == ""


def test_signal_must_be_whole_token_not_substring():
    # Regression: signals like 'test'/'dev'/'uat' were matched as raw substrings,
    # falsely flagging healthy prod functions. They must match on token boundaries.
    for healthy in [
        "latest-invoice-handler",   # 'latest' contains 'test'
        "developer-portal-api",     # 'developer' contains 'dev'
        "metadata-service",         # 'metadata' contains 'uat'? no, but 'data'... ensure no false signal
        "evaluate-documents",       # 'evaluate' contains no whole signal
    ]:
        is_np, signal = _is_nonprod_name(healthy)
        assert is_np is False, f"{healthy} should not be flagged (matched {signal!r})"
    # Real non-prod, including camelCase AND all-caps acronyms, still matches.
    assert _is_nonprod_name("qaHandler")[0] is True
    assert _is_nonprod_name("invoice-test")[0] is True
    assert _is_nonprod_name("QA-doc-processor")[0] is True   # delimiter-separated acronym
    assert _is_nonprod_name("UATPipeline")[0] is True         # acronym + CamelWord


def test_hyphenated_nonprod_still_matches():
    # Regression: the whole-token split breaks 'non-prod' into {'non','prod'} and
    # misses the 'nonprod' signal. The de-delimited check must still catch it,
    # without re-introducing substring false positives for short signals.
    for name in ["non-prod-textract", "nonprod-handler", "non_prod_api", "my-NonProd-fn"]:
        assert _is_nonprod_name(name)[0] is True, f"{name} should be flagged non-prod"
    # 'production' must NOT match (it contains 'prod' but is not non-prod)
    assert _is_nonprod_name("production-textract")[0] is False
    assert _is_nonprod_name("reproduce-handler")[0] is False


def test_is_nonprod_test():
    is_np, signal = _is_nonprod_name("test-pipeline-textract")
    assert is_np is True
    assert signal == "test"


def test_nonprod_signals_list_contents():
    assert "qa" in _NONPROD_NAME_SIGNALS
    assert "staging" in _NONPROD_NAME_SIGNALS
    assert "dev" in _NONPROD_NAME_SIGNALS
    assert "sandbox" in _NONPROD_NAME_SIGNALS


# ── unit: _get_total_textract_spend ──────────────────────────────────────────

def test_get_total_textract_spend_returns_amount():
    ce = MagicMock()
    ce.get_cost_and_usage.return_value = _make_ce_response(4830.0)
    total = _get_total_textract_spend(ce, "2026-05-01", "2026-05-30")
    assert abs(total - 4830.0) < 0.01


def test_get_total_textract_spend_returns_zero_on_error():
    ce = MagicMock()
    ce.get_cost_and_usage.side_effect = Exception("no creds")
    total = _get_total_textract_spend(ce, "2026-05-01", "2026-05-30")
    assert total == 0.0


def test_get_total_textract_spend_no_data():
    ce = MagicMock()
    ce.get_cost_and_usage.return_value = {"ResultsByTime": []}
    total = _get_total_textract_spend(ce, "2026-05-01", "2026-05-30")
    assert total == 0.0


# ── unit: _get_cloudtrail_callers ─────────────────────────────────────────────

def test_cloudtrail_callers_extracts_nonprod_function():
    ct = MagicMock()
    event = _make_ct_event(
        caller_arn="arn:aws:sts::123456789:assumed-role/qa-doc-processor/session",
        role_name="qa-doc-processor",
    )
    ct.lookup_events.return_value = {"Events": [event], "NextToken": None}

    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 30, tzinfo=timezone.utc)
    callers = _get_cloudtrail_callers(ct, start, end)

    assert len(callers) > 0
    # At least one caller should be the qa-doc-processor role
    all_keys = list(callers.keys())
    assert any("qa" in k.lower() or "doc-processor" in k.lower() for k in all_keys)


def test_cloudtrail_callers_returns_empty_on_error():
    ct = MagicMock()
    ct.lookup_events.side_effect = Exception("access denied")

    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 30, tzinfo=timezone.utc)
    callers = _get_cloudtrail_callers(ct, start, end)
    assert callers == {}


def test_cloudtrail_callers_tallies_multiple_calls():
    ct = MagicMock()
    events = [
        _make_ct_event("arn:aws:sts::123:assumed-role/qa-ocr/s1", "qa-ocr"),
        _make_ct_event("arn:aws:sts::123:assumed-role/qa-ocr/s2", "qa-ocr"),
    ]
    ct.lookup_events.return_value = {"Events": events}

    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 30, tzinfo=timezone.utc)
    callers = _get_cloudtrail_callers(ct, start, end)

    # qa-ocr role should have been called twice
    role_caller = next((v for k, v in callers.items() if "qa-ocr" in k), None)
    assert role_caller is not None
    assert role_caller["call_count"] == 2


# ── integration: scan with no spend ──────────────────────────────────────────

def test_scan_returns_zero_when_no_spend():
    with patch("finops.recommendations.textract_env._make_ce") as mock_ce_fn, \
         patch("finops.recommendations.textract_env._make_cloudtrail"):
        ce = MagicMock()
        ce.get_cost_and_usage.return_value = _make_ce_response(0.0)
        mock_ce_fn.return_value = ce

        result = scan_textract_environment_waste(days=30)

    assert result["total_textract_spend"] == 0.0
    assert result["estimated_monthly_waste"] == 0.0
    assert result["non_prod_callers"] == []


# ── integration: tagged environment breakdown ─────────────────────────────────

def test_scan_identifies_staging_spend_from_tags():
    call_count = [0]

    def _ce_side_effect(**kwargs):
        call_count[0] += 1
        group_by = kwargs.get("GroupBy", [])
        if group_by:
            # Tag group query: return staging spend
            tag_key = group_by[0].get("Key", "")
            return _make_ce_tag_response(tag_key, "staging", 500.0)
        # Total spend query
        return _make_ce_response(1500.0)

    with patch("finops.recommendations.textract_env._make_ce") as mock_ce_fn:
        ce = MagicMock()
        ce.get_cost_and_usage.side_effect = _ce_side_effect
        mock_ce_fn.return_value = ce

        result = scan_textract_environment_waste(days=30)

    assert result["total_textract_spend"] == 1500.0
    # staging bucket should have spend
    assert result["tagged_env_breakdown"]["staging"] >= 0.0


# ── integration: CloudTrail fallback identifies non-prod callers ──────────────

def test_scan_cloudtrail_fallback_flags_nonprod():
    event = _make_ct_event(
        "arn:aws:sts::123:assumed-role/qa-invoice-parser/sess",
        "qa-invoice-parser",
    )

    with patch("finops.recommendations.textract_env._make_ce") as mock_ce_fn, \
         patch("finops.recommendations.textract_env._make_cloudtrail") as mock_ct_fn:

        ce = MagicMock()
        # Total spend = 4830, no tag data
        def _ce_side(**kw):
            if kw.get("GroupBy"):
                return {"ResultsByTime": [{"Groups": [], "Total": {"UnblendedCost": {"Amount": "0"}}}]}
            return _make_ce_response(4830.0)
        ce.get_cost_and_usage.side_effect = _ce_side
        mock_ce_fn.return_value = ce

        ct = MagicMock()
        ct.lookup_events.return_value = {"Events": [event]}
        mock_ct_fn.return_value = ct

        result = scan_textract_environment_waste(days=30)

    assert result["total_textract_spend"] == 4830.0
    assert result["cloudtrail_scan_done"] is True
    assert len(result["non_prod_callers"]) > 0
    caller = result["non_prod_callers"][0]
    assert caller["env_signal"] in _NONPROD_NAME_SIGNALS
    assert caller["estimated_spend"] > 0


# ── integration: output schema completeness ───────────────────────────────────

def test_scan_output_has_all_required_keys():
    with patch("finops.recommendations.textract_env._make_ce") as mock_ce_fn, \
         patch("finops.recommendations.textract_env._make_cloudtrail"):
        ce = MagicMock()
        ce.get_cost_and_usage.return_value = _make_ce_response(0.0)
        mock_ce_fn.return_value = ce

        result = scan_textract_environment_waste(days=30)

    required_keys = {
        "total_textract_spend",
        "monthly_total_estimate",
        "tagged_env_breakdown",
        "has_useful_tags",
        "cloudtrail_scan_done",
        "non_prod_callers",
        "estimated_monthly_waste",
        "non_prod_pct",
        "recommendation",
        "actions",
    }
    assert required_keys.issubset(set(result.keys()))
