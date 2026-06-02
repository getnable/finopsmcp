"""Tests for the cross-cloud traffic classifier and cost breakdown."""
from src.finops.analyzers.traffic_classify import (
    classify_aws, classify_gcp, classify_azure, classify,
)
from src.finops.analyzers.traffic import build_traffic_breakdown, rows_to_flows


# ── AWS classification ────────────────────────────────────────────────────────

def test_aws_internet_egress():
    assert classify_aws("USE1-DataTransfer-Out-Bytes") == ("external", "internet_egress")
    assert classify_aws("DataTransfer-Out-Bytes") == ("external", "internet_egress")


def test_aws_cross_az():
    assert classify_aws("USE1-DataTransfer-Regional-Bytes") == ("internal", "cross_az")


def test_aws_nat_processing():
    assert classify_aws("USE1-NatGateway-Bytes") == ("internal", "nat_processing")


def test_aws_ingress_free():
    assert classify_aws("USE1-DataTransfer-In-Bytes") == ("ingress", "ingress")


def test_aws_firewall_and_endpoint():
    assert classify_aws("USE1-AWSNetworkFirewall-Bytes")[1] == "firewall"
    assert classify_aws("USE1-VpcEndpoint-Bytes") == ("internal", "private_endpoint")


def test_aws_cdn_and_unknown():
    assert classify_aws("CloudFront-Out-Bytes")[1] == "cdn_egress"
    assert classify_aws("USE1-BoxUsage:m5.large") == ("other", "other")
    assert classify_aws("") == ("other", "other")


def test_nat_beats_generic_out_rule_order():
    # NatGateway-Bytes must classify as nat_processing, not internet_egress,
    # even though it would also match a generic out rule. Order matters.
    assert classify_aws("USE1-NatGateway-Bytes")[1] == "nat_processing"


def test_aws_inter_region_not_internet_egress():
    # CUR inter-region rows are "<src>-<dst>-AWS-Out-Bytes" with no "interregion"
    # substring. They must classify as cross_region (internal), NOT internet egress.
    assert classify_aws("USE1-USW2-AWS-Out-Bytes") == ("internal", "cross_region")
    assert classify_aws("USE1-EUW1-AWS-In-Bytes") == ("internal", "cross_region")
    # A plain internet egress row stays external.
    assert classify_aws("USE1-DataTransfer-Out-Bytes") == ("external", "internet_egress")


def test_azure_geographic_zone_is_not_cross_az():
    # "Zone 1" is an Azure geographic billing zone (internet egress), not an AZ.
    assert classify_azure("Standard Data Transfer Out - Zone 1")[1] == "internet_egress"
    # A real availability-zone meter still maps to cross_az.
    assert classify_azure("Availability Zone Data Transfer")[1] == "cross_az"


# ── GCP / Azure ───────────────────────────────────────────────────────────────

def test_gcp_scopes():
    assert classify_gcp("Network Internet Egress from Americas to Americas")[1] == "internet_egress"
    assert classify_gcp("Network Inter Zone Egress")[1] == "cross_az"
    assert classify_gcp("Network Inter Region Egress") [1] == "cross_region"
    assert classify_gcp("Cloud NAT Data Processing")[1] == "nat_processing"


def test_azure_scopes():
    assert classify_azure("Inter-Region Egress")[1] == "cross_region"
    assert classify_azure("Standard Data Transfer Out")[1] == "internet_egress"
    assert classify_azure("ExpressRoute")[1] == "vpn_directconnect"


def test_dispatch():
    assert classify("aws", "DataTransfer-Out-Bytes")[1] == "internet_egress"
    assert classify("gcp", "Cloud NAT Data Processing")[1] == "nat_processing"
    assert classify("unknown", "whatever") == ("other", "other")


# ── Aggregation ───────────────────────────────────────────────────────────────

def _rows():
    return [
        {"usage_type": "USE1-DataTransfer-Out-Bytes", "cost_usd": 1000.0, "service": "EC2"},
        {"usage_type": "USE1-DataTransfer-Regional-Bytes", "cost_usd": 600.0, "service": "EKS"},
        {"usage_type": "USE1-NatGateway-Bytes", "cost_usd": 400.0, "service": "VPC"},
        {"usage_type": "USE1-DataTransfer-In-Bytes", "cost_usd": 0.0, "service": "EC2"},
        {"usage_type": "USE1-BoxUsage:m5.large", "cost_usd": 5000.0, "service": "EC2"},  # not traffic, dropped
    ]


def test_breakdown_drops_non_traffic_rows():
    flows = rows_to_flows(_rows(), "aws")
    # BoxUsage compute line is dropped; 4 traffic rows remain
    assert len(flows) == 4
    assert all(f.scope != "other" for f in flows)


def test_breakdown_totals_and_split():
    b = build_traffic_breakdown(_rows(), "aws")
    # Total network cost excludes the compute line
    assert b["total_network_cost_usd"] == 2000.0
    split = b["internal_vs_external"]
    # external = 1000 internet egress; internal = 600 cross_az + 400 nat = 1000
    assert split["external_usd"] == 1000.0
    assert split["internal_usd"] == 1000.0
    assert split["external_pct"] == 50.0
    assert split["internal_pct"] == 50.0


def test_breakdown_solve_playbook_excludes_ingress():
    b = build_traffic_breakdown(_rows(), "aws")
    scopes = {p["scope"] for p in b["solve_playbook"]}
    assert "internet_egress" in scopes
    assert "nat_processing" in scopes
    assert "ingress" not in scopes
    # NAT fix should mention VPC endpoints
    nat = next(p for p in b["solve_playbook"] if p["scope"] == "nat_processing")
    assert "endpoint" in nat["fix"].lower()


def test_empty_breakdown():
    b = build_traffic_breakdown([], "aws")
    assert b["total_network_cost_usd"] == 0.0
    assert b["by_scope"] == {}
