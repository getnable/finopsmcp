"""The PR-comment estimator gets attribute values as raw diff text.

pr_comments/parser.py hands every Terraform attribute over as the string it
found, so `iops = var.data_iops` arrives as "var.data_iops" and `iops = 3000 #
burst` as "3000 # burst". The estimator passed those straight to
aws_prices.ebs_volume_monthly, whose float() raised ValueError, and one
unreadable attribute cost the whole PR comment. RDS priced any class it did
not know at a made-up $140/mo, ignored multi_az, and charged every engine the
MySQL rate.
"""
from __future__ import annotations

import pytest

from finops import aws_prices
from finops.pr_comments import estimator, parser
from finops.pr_comments.parser import ResourceChange


class _ListPrice:
    has_private_pricing = False
    confidence = "low"

    def effective_multiplier(self):
        return 1.0


@pytest.fixture(autouse=True)
def _list_price(monkeypatch):
    monkeypatch.setattr(estimator, "detect_effective_rates", lambda: _ListPrice())


def _estimate(rtype: str, props: dict) -> estimator.CostEstimate:
    [est] = estimator.estimate_changes([ResourceChange("add", rtype, "r", "aws", props)])
    return est


# ── aws_prices tolerates what the parser hands over ──────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("3000", 3000.0), ("3000 # burst", 3000.0), ('"250"', 250.0), ("250 // mib", 250.0),
    ("var.data_iops", 0.0), ("", 0.0), (None, 0.0), ("nan", 0.0), (125, 125.0),
])
def test_as_number(raw, want):
    assert aws_prices.as_number(raw) == want


def test_as_number_can_tell_unreadable_from_zero():
    assert aws_prices.as_number("var.x", None) is None
    assert aws_prices.as_number("0", None) == 0.0


def test_ebs_volume_monthly_treats_non_numeric_input_as_zero():
    # 500 GB gp3 is $40; an unreadable IOPS figure is the included baseline.
    assert aws_prices.ebs_volume_monthly("gp3", "500", "var.data_iops", "var.tput") == 40.0
    assert aws_prices.ebs_volume_monthly("gp3", 500, "4000 # burst", None) == 45.0
    assert aws_prices.ebs_volume_monthly("GP3", "var.size") == 0.0


# ── EBS through the estimator ────────────────────────────────────────────────

def test_a_terraform_reference_does_not_cost_the_pr_comment():
    diff = (
        '+resource "aws_ebs_volume" "data" {\n'
        '+  availability_zone = "us-east-1a"\n'
        '+  size              = 500\n'
        '+  type              = "gp3"\n'
        '+  iops              = var.data_iops\n'
        '+}\n'
    )
    [est] = estimator.estimate_changes(parser.parse_diff(diff, "main.tf"))
    assert est.monthly_usd == 40.0
    assert est.confidence == "medium"
    assert any("var.data_iops" in n for n in est.notes)
    assert estimator.format_pr_comment([est]) is not None


def test_a_trailing_comment_is_read_past():
    est = _estimate("aws_ebs_volume", {"type": "gp3", "size": "500", "iops": "4000 # burst"})
    assert est.monthly_usd == 45.0          # 40 storage + 1,000 IOPS over 3,000 at $0.005
    assert est.confidence == "high"


def test_an_unreadable_size_is_not_priced_and_says_so():
    est = _estimate("aws_ebs_volume", {"type": "gp3", "size": "var.size"})
    assert est.monthly_usd == 0.0
    assert est.confidence == "low"
    assert any("var.size" in n for n in est.notes)


# ── RDS ──────────────────────────────────────────────────────────────────────

def test_an_unknown_class_is_not_a_made_up_140():
    est = _estimate("aws_db_instance", {"instance_class": "db.x2iedn.32xlarge",
                                        "engine": "mysql"})
    assert est.monthly_usd == 0.0
    assert est.confidence == "low"
    assert any("not in nable's RDS price table" in n for n in est.notes)


@pytest.mark.parametrize("engine", ["sqlserver-se", "oracle-ee", "db2-se", "aurora-postgresql"])
def test_engines_without_a_table_are_unpriced(engine):
    est = _estimate("aws_db_instance", {"instance_class": "db.m5.xlarge", "engine": engine})
    assert est.monthly_usd == 0.0 and est.confidence == "low"


def test_an_aurora_cluster_instance_is_unpriced_even_without_an_engine():
    est = _estimate("aws_rds_cluster_instance", {"instance_class": "db.r6g.large"})
    assert est.monthly_usd == 0.0 and est.confidence == "low"


def test_postgres_is_priced_on_the_postgres_table():
    est = _estimate("aws_db_instance", {"instance_class": "db.r6g.large", "engine": '"postgres"'})
    assert est.breakdown["compute"] == round(0.225 * 730, 2)
    assert est.confidence == "high"


def test_multi_az_doubles_compute_and_storage():
    single = _estimate("aws_db_instance", {"instance_class": "db.m5.large", "engine": "mysql",
                                           "allocated_storage": "100"})
    multi = _estimate("aws_db_instance", {"instance_class": "db.m5.large", "engine": "mysql",
                                          "allocated_storage": "100", "multi_az": "true"})
    assert multi.breakdown["compute"] == round(2 * 0.171 * 730, 2)
    assert multi.breakdown["storage"] == round(2 * 100 * 0.115, 2)
    assert multi.monthly_usd == pytest.approx(2 * single.monthly_usd, abs=0.01)


def test_no_engine_is_the_mysql_rate_and_says_so():
    est = _estimate("aws_db_instance", {"instance_class": "db.m5.large"})
    assert est.breakdown["compute"] == round(0.171 * 730, 2)
    assert est.confidence == "medium"
    assert any("MySQL rate" in n for n in est.notes)


def test_an_unreadable_node_count_does_not_raise():
    est = _estimate("aws_eks_node_group", {"instance_types": "m5.large",
                                           "desired_size": "var.nodes"})
    assert est.monthly_usd == aws_prices.EC2_MONTHLY["m5.large"]
