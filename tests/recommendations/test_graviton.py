"""Tests for finops.recommendations.graviton."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from finops.recommendations.graviton import (
    GRAVITON_MAP,
    _compute_savings,
    _estimate_monthly_cost,
    scan_graviton_opportunities,
)
from finops.recommendations.graviton_prices import HOURLY_PRICE, HOURS_PER_MONTH


# ── GRAVITON_MAP correctness ──────────────────────────────────────────────────

def test_graviton_map_contains_m5_family() -> None:
    assert GRAVITON_MAP["m5.large"] == "m7g.large"
    assert GRAVITON_MAP["m5.xlarge"] == "m7g.xlarge"
    assert GRAVITON_MAP["m5.2xlarge"] == "m7g.2xlarge"
    assert GRAVITON_MAP["m5.4xlarge"] == "m7g.4xlarge"


def test_graviton_map_contains_c5_family() -> None:
    assert GRAVITON_MAP["c5.large"] == "c7g.large"
    assert GRAVITON_MAP["c5.xlarge"] == "c7g.xlarge"
    assert GRAVITON_MAP["c5.2xlarge"] == "c7g.2xlarge"
    assert GRAVITON_MAP["c5.4xlarge"] == "c7g.4xlarge"


def test_graviton_map_contains_r5_family() -> None:
    assert GRAVITON_MAP["r5.large"] == "r7g.large"
    assert GRAVITON_MAP["r5.xlarge"] == "r7g.xlarge"
    assert GRAVITON_MAP["r5.2xlarge"] == "r7g.2xlarge"
    assert GRAVITON_MAP["r5.4xlarge"] == "r7g.4xlarge"


def test_graviton_map_contains_t3_family() -> None:
    assert GRAVITON_MAP["t3.medium"] == "t4g.medium"
    assert GRAVITON_MAP["t3.large"] == "t4g.large"
    assert GRAVITON_MAP["t3.xlarge"] == "t4g.xlarge"


def test_graviton_map_contains_m6i_and_c6i() -> None:
    assert GRAVITON_MAP["m6i.large"] == "m7g.large"
    assert GRAVITON_MAP["c6i.large"] == "c7g.large"
    assert GRAVITON_MAP["r6i.large"] == "r7g.large"


def test_graviton_map_all_values_are_graviton_types() -> None:
    graviton_prefixes = ("m6g.", "m7g.", "c6g.", "c7g.", "r6g.", "r7g.", "t4g.")
    for x86_type, arm_type in GRAVITON_MAP.items():
        assert any(arm_type.startswith(p) for p in graviton_prefixes), (
            f"{x86_type} maps to {arm_type} which is not a recognized Graviton type"
        )


# ── Savings calculation ───────────────────────────────────────────────────────

def test_estimate_monthly_cost_known_type() -> None:
    # m5.large is $0.096/hr in the price table
    cost = _estimate_monthly_cost("m5.large")
    assert cost == round(0.096 * HOURS_PER_MONTH, 2)


def test_estimate_monthly_cost_unknown_type_returns_zero() -> None:
    assert _estimate_monthly_cost("x999.huge") == 0.0


def test_compute_savings_both_types_known() -> None:
    # m5.large ($0.096/hr) -> m7g.large ($0.0816/hr)
    current_cost, savings, pct = _compute_savings("m5.large", "m7g.large")
    expected_current = round(0.096 * HOURS_PER_MONTH, 2)
    expected_arm = round(0.0816 * HOURS_PER_MONTH, 2)
    expected_savings = round(expected_current - expected_arm, 2)
    expected_pct = round(expected_savings / expected_current * 100, 1)

    assert current_cost == expected_current
    assert savings == expected_savings
    assert pct == expected_pct
    assert savings > 0


def test_compute_savings_fallback_when_graviton_price_missing() -> None:
    # Use a real x86 type but a fake Graviton type not in the price table
    current_cost, savings, pct = _compute_savings("m5.large", "m99g.huge")
    expected_current = round(0.096 * HOURS_PER_MONTH, 2)
    assert current_cost == expected_current
    assert savings == round(expected_current * 0.20, 2)
    assert pct == 20.0


def test_compute_savings_both_unknown_returns_zeros_with_fallback_pct() -> None:
    current_cost, savings, pct = _compute_savings("x1.unknown", "y1.unknown")
    assert current_cost == 0.0
    assert savings == 0.0
    assert pct == 20.0


# ── scan_graviton_opportunities output structure ──────────────────────────────

def _make_mock_instance(
    instance_id: str = "i-abc123",
    instance_type: str = "m5.large",
    architecture: str = "x86_64",
    name: str = "web-server",
) -> dict:
    return {
        "InstanceId": instance_id,
        "InstanceType": instance_type,
        "Architecture": architecture,
        "Tags": [{"Key": "Name", "Value": name}],
    }


def _make_mock_ec2_client(instances: list[dict]) -> MagicMock:
    page = {
        "Reservations": [{"Instances": instances}]
    }
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [page]

    mock_ec2 = MagicMock()
    mock_ec2.get_paginator.return_value = mock_paginator
    return mock_ec2


def _make_mock_aws_connector(session: MagicMock | None = None) -> MagicMock:
    connector = MagicMock()
    connector._session = session
    return connector


def test_scan_returns_empty_for_no_running_instances() -> None:
    mock_session = MagicMock()
    mock_ec2 = _make_mock_ec2_client([])
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
    mock_session.client.side_effect = lambda svc, **kw: (
        mock_ec2 if svc == "ec2" else mock_sts
    )

    connector = _make_mock_aws_connector(session=mock_session)
    results = asyncio.run(scan_graviton_opportunities(connector, regions=["us-east-1"]))
    assert results == []


def test_scan_skips_arm64_instances() -> None:
    arm_instance = _make_mock_instance(
        instance_id="i-arm",
        instance_type="m7g.large",
        architecture="arm64",
    )
    mock_session = MagicMock()
    mock_ec2 = _make_mock_ec2_client([arm_instance])
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
    mock_session.client.side_effect = lambda svc, **kw: (
        mock_ec2 if svc == "ec2" else mock_sts
    )

    connector = _make_mock_aws_connector(session=mock_session)
    results = asyncio.run(scan_graviton_opportunities(connector, regions=["us-east-1"]))
    assert results == []


def test_scan_skips_x86_instances_without_mapping() -> None:
    unmapped = _make_mock_instance(
        instance_id="i-old",
        instance_type="m1.small",
        architecture="x86_64",
    )
    mock_session = MagicMock()
    mock_ec2 = _make_mock_ec2_client([unmapped])
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
    mock_session.client.side_effect = lambda svc, **kw: (
        mock_ec2 if svc == "ec2" else mock_sts
    )

    connector = _make_mock_aws_connector(session=mock_session)
    results = asyncio.run(scan_graviton_opportunities(connector, regions=["us-east-1"]))
    assert results == []


def test_scan_returns_correct_structure_for_candidate() -> None:
    inst = _make_mock_instance(
        instance_id="i-aabbcc",
        instance_type="m5.large",
        architecture="x86_64",
        name="api-server",
    )
    mock_session = MagicMock()
    mock_ec2 = _make_mock_ec2_client([inst])
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {"Account": "111122223333"}
    mock_session.client.side_effect = lambda svc, **kw: (
        mock_ec2 if svc == "ec2" else mock_sts
    )

    connector = _make_mock_aws_connector(session=mock_session)
    results = asyncio.run(scan_graviton_opportunities(connector, regions=["us-east-1"]))

    assert len(results) == 1
    r = results[0]

    assert r["instance_id"] == "i-aabbcc"
    assert r["instance_type"] == "m5.large"
    assert r["graviton_equivalent"] == "m7g.large"
    assert r["region"] == "us-east-1"
    assert r["name_tag"] == "api-server"
    assert r["account_id"] == "111122223333"

    # Cost fields must be positive
    assert r["current_monthly_cost_estimate"] > 0
    assert r["savings_estimate"] > 0
    assert r["savings_pct"] > 0


def test_scan_sorted_by_savings_descending() -> None:
    instances = [
        _make_mock_instance("i-small", "t3.nano", "x86_64", "tiny"),
        _make_mock_instance("i-large", "m5.4xlarge", "x86_64", "big"),
        _make_mock_instance("i-medium", "m5.xlarge", "x86_64", "mid"),
    ]
    mock_session = MagicMock()
    mock_ec2 = _make_mock_ec2_client(instances)
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {"Account": "000000000000"}
    mock_session.client.side_effect = lambda svc, **kw: (
        mock_ec2 if svc == "ec2" else mock_sts
    )

    connector = _make_mock_aws_connector(session=mock_session)
    results = asyncio.run(scan_graviton_opportunities(connector, regions=["us-east-1"]))

    assert len(results) == 3
    savings = [r["savings_estimate"] for r in results]
    assert savings == sorted(savings, reverse=True)
