"""Every RDS figure is priced by engine, through aws_prices.rds_hourly.

rds_hourly(class, engine) existed, but only guard.py called it. The idle and
rightsizing checks, the Terraform estimator, the PR-comment estimator and the
VS Code extension all read the MySQL table whatever the engine: a PostgreSQL
instance came out 4-7% low, and an Aurora, SQL Server, Oracle or Db2 instance
was charged the MySQL rate for its class. rds_hourly returns None for the
engines it has no table for, and None now means unpriced everywhere.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from finops import aws_prices
from finops.aws_prices import RDS_HOURLY, RDS_HOURLY_POSTGRES, rds_hourly


def _rds_clients(db_class: str, engine: str, multi_az: bool = False, value: float = 0.0):
    rds = MagicMock()
    rds.get_paginator.return_value.paginate.return_value = [{"DBInstances": [{
        "DBInstanceIdentifier": "db-1", "DBInstanceClass": db_class, "Engine": engine,
        "DBInstanceStatus": "available", "MultiAZ": multi_az,
    }]}]
    cw = MagicMock()
    now = datetime.now(timezone.utc)
    cw.get_metric_statistics.return_value = {"Datapoints": [
        {"Average": 2.0, "Maximum": value, "Timestamp": now - timedelta(hours=h)}
        for h in range(48)
    ]}
    return rds, cw


def test_db_m5_8xlarge_is_the_list_rate():
    # 16x the db.m5.large rate, and the AWS list price; 2.74 was a rounding.
    assert RDS_HOURLY["db.m5.8xlarge"] == 2.736 == round(16 * RDS_HOURLY["db.m5.large"], 3)


# ── analyzers/waste.py ───────────────────────────────────────────────────────

def test_idle_postgres_is_priced_on_the_postgres_table():
    from finops.analyzers.waste import check_rds_idle

    [f] = check_rds_idle(*_rds_clients("db.r6g.large", "postgres", multi_az=True), "us-east-1")
    assert f["estimated_monthly_savings"] == round(0.225 * 730 * 2, 2) == 328.5
    assert f["unpriced"] is False


@pytest.mark.parametrize("engine", ["sqlserver-se", "oracle-ee", "aurora-postgresql", "db2-ae"])
def test_idle_engine_without_a_table_is_unpriced(engine):
    from finops.analyzers.waste import check_rds_idle

    [f] = check_rds_idle(*_rds_clients("db.m5.xlarge", engine), "us-east-1")
    assert f["estimated_monthly_savings"] is None
    assert f["unpriced"] is True
    assert engine in f["detail"]


def test_rightsizing_uses_the_engine_rate():
    from finops.analyzers.waste import check_rds_rightsizing

    [f] = check_rds_rightsizing(*_rds_clients("db.r6g.xlarge", "postgres"), "us-east-1")
    assert f["estimated_monthly_savings"] == round((0.45 - 0.225) * 730, 2)


@pytest.mark.parametrize("engine", ["sqlserver-ee", "aurora-mysql"])
def test_rightsizing_skips_an_engine_it_cannot_price(engine):
    from finops.analyzers.waste import check_rds_rightsizing

    assert list(check_rds_rightsizing(*_rds_clients("db.r6g.xlarge", engine), "us-east-1")) == []


# ── connectors/terraform_estimate.py ─────────────────────────────────────────

def _plan_line(rtype: str, after: dict, before: dict | None = None, actions=("create",)):
    from finops.connectors import terraform_estimate as te

    rc = te.ResourceChange(f"{rtype}.a", rtype, list(actions), before, after)
    return te._ESTIMATORS[rtype](rc)


def test_terraform_prices_postgres_on_its_table():
    line = _plan_line("aws_db_instance", {"instance_class": "db.m5.large", "engine": "postgres"})
    assert line.monthly_delta == pytest.approx(0.178 * 730)
    assert line.confidence == "high"


def test_terraform_sql_server_is_unpriced():
    line = _plan_line("aws_db_instance", {"instance_class": "db.m5.large", "engine": "sqlserver-se"})
    assert line.monthly_delta == 0.0
    assert line.confidence == "low"
    assert "not priced" in line.detail


def test_terraform_aurora_is_unpriced_not_mysql_plus_ten_percent():
    line = _plan_line("aws_rds_cluster_instance",
                      {"instance_class": "db.r6g.large", "engine": "aurora-postgresql"})
    assert line.monthly_delta == 0.0
    assert line.confidence == "low"
    assert line.monthly_delta != pytest.approx(0.215 * 1.1 * 730)


def test_terraform_update_uses_each_side_engine_rate():
    line = _plan_line(
        "aws_db_instance",
        after={"instance_class": "db.m5.xlarge", "engine": "postgres"},
        before={"instance_class": "db.m5.large", "engine": "postgres"},
        actions=("update",))
    assert line.monthly_delta == pytest.approx((0.356 - 0.178) * 730)


def test_terraform_update_into_an_unpriced_engine_is_unpriced():
    line = _plan_line(
        "aws_db_instance",
        after={"instance_class": "db.m5.xlarge", "engine": "oracle-se2"},
        before={"instance_class": "db.m5.large", "engine": "oracle-se2"},
        actions=("update",))
    assert line.monthly_delta == 0.0 and line.confidence == "low"


# ── the VS Code extension and its Python counterpart ─────────────────────────

def test_vscode_python_counterpart_prices_by_engine():
    from finops.vscode_extension_prices import price_resource_py

    pg = price_resource_py("aws_db_instance", {"instance_class": "db.r6g.large",
                                               "engine": "postgres"})
    assert pg["monthly"] == round(RDS_HOURLY_POSTGRES["db.r6g.large"] * 730, 2)
    mssql = price_resource_py("aws_db_instance", {"instance_class": "db.r6g.large",
                                                  "engine": "sqlserver-web"})
    assert mssql["monthly"] == 0.0 and "not priced" in mssql["detail"]
    aurora = price_resource_py("aws_rds_cluster_instance", {"instance_class": "db.r6g.large"})
    assert aurora["monthly"] == 0.0


def test_rds_hourly_contract_the_callers_rely_on():
    assert rds_hourly("db.m5.large", "MySQL") == RDS_HOURLY["db.m5.large"]
    assert rds_hourly("db.m5.large", "mariadb") == RDS_HOURLY["db.m5.large"]
    assert rds_hourly("db.m5.large", "postgres") == RDS_HOURLY_POSTGRES["db.m5.large"]
    for engine in ("aurora", "aurora-mysql", "aurora-postgresql", "sqlserver-ex",
                   "oracle-ee", "custom-oracle-ee", "db2-se"):
        assert rds_hourly("db.m5.large", engine) is None
    assert aws_prices.rds_hourly("db.nope.large", "mysql") is None
