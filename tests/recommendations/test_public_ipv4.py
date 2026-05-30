"""Tests for finops.recommendations.public_ipv4."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from finops.recommendations.public_ipv4 import (
    IPV4_MONTHLY_RATE,
    audit_public_ipv4,
)


# ── constants ─────────────────────────────────────────────────────────────────


def test_monthly_rate_is_360():
    """$0.005/hr * 24h * 30d = $3.60/mo."""
    assert abs(IPV4_MONTHLY_RATE - 3.60) < 0.01


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_aws_client(session=None):
    client = SimpleNamespace(_session=session)
    return client


def _make_ec2_mock(addresses: list[dict], instances: list[dict] | None = None):
    """Return a mock boto3 EC2 client with preset responses."""
    mock = MagicMock()
    mock.describe_addresses.return_value = {"Addresses": addresses}

    if instances:
        reservations = [{"Instances": instances}]
    else:
        reservations = []
    mock.describe_instances.return_value = {"Reservations": reservations}

    # describe_regions returns something valid so _get_opted_in_regions won't break
    mock.describe_regions.return_value = {
        "Regions": [{"RegionName": "us-east-1"}]
    }
    return mock


# ── tests ─────────────────────────────────────────────────────────────────────


def test_returns_correct_structure():
    """audit_public_ipv4 must return all required top-level keys."""
    ec2_mock = _make_ec2_mock(addresses=[])

    with patch("finops.recommendations.public_ipv4._make_ec2", return_value=ec2_mock):
        result = asyncio.run(audit_public_ipv4(_make_aws_client(), regions=["us-east-1"]))

    assert "unattached_eips" in result
    assert "stopped_instance_eips" in result
    assert "total_monthly_waste" in result
    assert "total_ips_found" in result
    assert "by_region" in result


def test_unattached_eip_identified():
    """An EIP with no association, no instance, and no network interface is unattached."""
    addresses = [
        {
            "AllocationId": "eipalloc-aaa111",
            "PublicIp": "1.2.3.4",
            # no AssociationId, InstanceId, or NetworkInterfaceId
        }
    ]
    ec2_mock = _make_ec2_mock(addresses=addresses)

    with patch("finops.recommendations.public_ipv4._make_ec2", return_value=ec2_mock):
        result = asyncio.run(audit_public_ipv4(_make_aws_client(), regions=["us-east-1"]))

    assert len(result["unattached_eips"]) == 1
    assert result["unattached_eips"][0]["public_ip"] == "1.2.3.4"
    assert result["unattached_eips"][0]["allocation_id"] == "eipalloc-aaa111"
    assert result["unattached_eips"][0]["state"] == "unattached"
    assert len(result["stopped_instance_eips"]) == 0


def test_stopped_instance_eip_identified():
    """An EIP on a stopped instance is categorized as stopped_instance_eip."""
    addresses = [
        {
            "AllocationId": "eipalloc-bbb222",
            "PublicIp": "5.6.7.8",
            "AssociationId": "eipassoc-xyz",
            "InstanceId": "i-stopped001",
            "NetworkInterfaceId": "eni-abc",
        }
    ]
    instances = [
        {"InstanceId": "i-stopped001", "State": {"Name": "stopped"}}
    ]
    ec2_mock = _make_ec2_mock(addresses=addresses, instances=instances)

    with patch("finops.recommendations.public_ipv4._make_ec2", return_value=ec2_mock):
        result = asyncio.run(audit_public_ipv4(_make_aws_client(), regions=["us-east-1"]))

    assert len(result["stopped_instance_eips"]) == 1
    eip = result["stopped_instance_eips"][0]
    assert eip["public_ip"] == "5.6.7.8"
    assert eip["instance_id"] == "i-stopped001"
    assert eip["state"] == "stopped"
    assert len(result["unattached_eips"]) == 0


def test_running_instance_eip_not_flagged():
    """An EIP on a running instance should not appear in waste categories."""
    addresses = [
        {
            "AllocationId": "eipalloc-ccc333",
            "PublicIp": "9.10.11.12",
            "AssociationId": "eipassoc-running",
            "InstanceId": "i-running001",
            "NetworkInterfaceId": "eni-def",
        }
    ]
    instances = [
        {"InstanceId": "i-running001", "State": {"Name": "running"}}
    ]
    ec2_mock = _make_ec2_mock(addresses=addresses, instances=instances)

    with patch("finops.recommendations.public_ipv4._make_ec2", return_value=ec2_mock):
        result = asyncio.run(audit_public_ipv4(_make_aws_client(), regions=["us-east-1"]))

    assert len(result["unattached_eips"]) == 0
    assert len(result["stopped_instance_eips"]) == 0


def test_waste_calculation_two_unattached():
    """Two unattached EIPs should sum to 2 * IPV4_MONTHLY_RATE."""
    addresses = [
        {"AllocationId": "eipalloc-111", "PublicIp": "1.1.1.1"},
        {"AllocationId": "eipalloc-222", "PublicIp": "2.2.2.2"},
    ]
    ec2_mock = _make_ec2_mock(addresses=addresses)

    with patch("finops.recommendations.public_ipv4._make_ec2", return_value=ec2_mock):
        result = asyncio.run(audit_public_ipv4(_make_aws_client(), regions=["us-east-1"]))

    expected = round(2 * IPV4_MONTHLY_RATE, 2)
    assert result["total_monthly_waste"] == expected
    assert len(result["unattached_eips"]) == 2


def test_waste_calculation_mixed():
    """One unattached + one stopped = 2 * IPV4_MONTHLY_RATE total waste."""
    addresses = [
        {"AllocationId": "eipalloc-aaa", "PublicIp": "1.1.1.1"},
        {
            "AllocationId": "eipalloc-bbb",
            "PublicIp": "2.2.2.2",
            "AssociationId": "eipassoc-abc",
            "InstanceId": "i-stopped999",
            "NetworkInterfaceId": "eni-abc",
        },
    ]
    instances = [
        {"InstanceId": "i-stopped999", "State": {"Name": "stopped"}}
    ]
    ec2_mock = _make_ec2_mock(addresses=addresses, instances=instances)

    with patch("finops.recommendations.public_ipv4._make_ec2", return_value=ec2_mock):
        result = asyncio.run(audit_public_ipv4(_make_aws_client(), regions=["us-east-1"]))

    expected = round(2 * IPV4_MONTHLY_RATE, 2)
    assert result["total_monthly_waste"] == expected
    assert len(result["unattached_eips"]) == 1
    assert len(result["stopped_instance_eips"]) == 1


def test_total_ips_found():
    """total_ips_found counts all EIPs, not just wasteful ones."""
    addresses = [
        {"AllocationId": "eipalloc-aaa", "PublicIp": "1.1.1.1"},
        {
            "AllocationId": "eipalloc-bbb",
            "PublicIp": "2.2.2.2",
            "AssociationId": "eipassoc-running",
            "InstanceId": "i-running001",
            "NetworkInterfaceId": "eni-def",
        },
    ]
    instances = [
        {"InstanceId": "i-running001", "State": {"Name": "running"}}
    ]
    ec2_mock = _make_ec2_mock(addresses=addresses, instances=instances)

    with patch("finops.recommendations.public_ipv4._make_ec2", return_value=ec2_mock):
        result = asyncio.run(audit_public_ipv4(_make_aws_client(), regions=["us-east-1"]))

    assert result["total_ips_found"] == 2
    # Only one is wasteful (the unattached one)
    assert result["total_monthly_waste"] == round(IPV4_MONTHLY_RATE, 2)


def test_by_region_populated():
    """by_region should contain an entry for each region scanned."""
    addresses = [
        {"AllocationId": "eipalloc-aaa", "PublicIp": "1.1.1.1"},
    ]
    ec2_mock = _make_ec2_mock(addresses=addresses)

    with patch("finops.recommendations.public_ipv4._make_ec2", return_value=ec2_mock):
        result = asyncio.run(audit_public_ipv4(_make_aws_client(), regions=["us-east-1"]))

    assert "us-east-1" in result["by_region"]
    region_data = result["by_region"]["us-east-1"]
    assert "total_eips" in region_data
    assert "unattached" in region_data
    assert "stopped_instance" in region_data
    assert "monthly_waste" in region_data


def test_empty_account():
    """No EIPs anywhere should produce zero waste and empty lists."""
    ec2_mock = _make_ec2_mock(addresses=[])

    with patch("finops.recommendations.public_ipv4._make_ec2", return_value=ec2_mock):
        result = asyncio.run(audit_public_ipv4(_make_aws_client(), regions=["us-east-1"]))

    assert result["unattached_eips"] == []
    assert result["stopped_instance_eips"] == []
    assert result["total_monthly_waste"] == 0.0
    assert result["total_ips_found"] == 0
