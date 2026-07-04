"""GCP Committed Use Discount detection in the FOCUS translator.

A CUD applies its saving to EffectiveCost via the credits[] array, but the
CommitmentDiscountType must also be set so spend can be grouped by commitment.
Real exports label the credit two ways (coded `type` and human `name`/`full_name`);
both must be detected, or GCP CUDs silently drop out of any commitment rollup while
AWS Savings Plans and Azure reservations show up.
"""
from __future__ import annotations

from finops.focus import normalize


def _gcp_row(credit: dict) -> dict:
    return {
        "service": {"description": "Compute Engine"},
        "location": {"region": "us-central1"},
        "project": {"id": "acme-prod", "name": "Acme Prod"},
        "cost": 6.30,
        "credits": [credit],
        "usage_start_time": "2026-07-04T00:00:00Z",
        "usage_end_time": "2026-07-04T01:00:00Z",
    }


def test_cud_detected_by_coded_type():
    rec = normalize("gcp", _gcp_row({
        "name": "Committed use discount: CPU",
        "amount": -1.85,
        "type": "COMMITTED_USAGE_DISCOUNT",
        "id": "cud-n1-cpu",
    }))
    assert rec.CommitmentDiscountType == "Committed Use"
    assert rec.CommitmentDiscountId == "cud-n1-cpu"
    # The saving reaches EffectiveCost regardless: 6.30 - 1.85 = 4.45.
    assert round(rec.EffectiveCost, 2) == 4.45


def test_cud_detected_by_name_when_type_is_absent():
    # Aggregated / trimmed exports drop the coded type but keep the human name.
    rec = normalize("gcp", _gcp_row({
        "name": "Committed use discount: CPU",
        "amount": -1.85,
    }))
    assert rec.CommitmentDiscountType == "Committed Use"
    assert round(rec.EffectiveCost, 2) == 4.45


def test_cud_detected_by_full_name():
    rec = normalize("gcp", _gcp_row({
        "full_name": "Committed Use Discount: N1 predefined vCPUs",
        "amount": -1.85,
    }))
    assert rec.CommitmentDiscountType == "Committed Use"


def test_non_commitment_credit_is_not_labeled():
    # A promotional or free-tier credit still reduces EffectiveCost but is not a
    # commitment, so CommitmentDiscountType stays None.
    rec = normalize("gcp", _gcp_row({
        "name": "Promotional credit",
        "amount": -1.00,
        "type": "PROMOTION",
    }))
    assert rec.CommitmentDiscountType is None
    assert round(rec.EffectiveCost, 2) == 5.30
