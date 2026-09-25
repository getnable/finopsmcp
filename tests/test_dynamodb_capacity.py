"""DynamoDB provisioned capacity, read with free APIs and priced from the Price List.

A provisioned table bills for every read and write unit it reserves, used or
not. The check reads only ListTables, DescribeTable and CloudWatch consumed
capacity through GetMetricStatistics, so it adds nothing to the scan's bill.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from finops.analyzers import waste

_OLD = datetime.now(UTC) - timedelta(days=90)


def _ddb(*tables):
    c = MagicMock()
    c.get_paginator.return_value.paginate.return_value = [
        {"TableNames": [t["TableName"] for t in tables]}]
    by_name = {t["TableName"]: t for t in tables}
    c.describe_table.side_effect = lambda TableName: {"Table": by_name[TableName]}
    return c


def _table(name="orders", rcu=1000, wcu=1000, mode="PROVISIONED", created=_OLD, cls=None):
    t = {"TableName": name, "TableStatus": "ACTIVE", "CreationDateTime": created,
         "BillingModeSummary": {"BillingMode": mode},
         "ProvisionedThroughput": {"ReadCapacityUnits": rcu, "WriteCapacityUnits": wcu}}
    if cls:
        t["TableClassSummary"] = {"TableClass": cls}
    return t


def _cw(peak_per_sec: float):
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": [
        {"Timestamp": _OLD, "Sum": peak_per_sec * 3600},
        {"Timestamp": _OLD + timedelta(hours=1), "Sum": 1.0}]}
    return cw


def test_an_overprovisioned_table_is_priced_from_the_price_list():
    out = waste.check_dynamodb_provisioned(_ddb(_table()), _cw(10.0), "us-east-1")
    [f] = out
    # 20 units keeps 2x the busiest hour; 980 spare of each, at $0.00013 and
    # $0.00065 per unit-hour, 730 hours.
    assert f["recommended_rcu"] == 20 and f["recommended_wcu"] == 20
    assert f["estimated_monthly_savings"] == pytest.approx(980 * (0.00013 + 0.00065) * 730,
                                                           abs=0.01)
    assert "update-table --table-name orders" in f["detail"]
    assert f["waste_type"] == "dynamodb_overprovisioned_capacity"


def test_standard_ia_tables_use_their_own_prices():
    out = waste.check_dynamodb_provisioned(
        _ddb(_table(cls="STANDARD_INFREQUENT_ACCESS")), _cw(10.0), "us-east-1")
    assert out[0]["estimated_monthly_savings"] == pytest.approx(
        980 * (0.00016 + 0.00081) * 730, abs=0.01)


def test_on_demand_and_new_tables_are_not_judged():
    fresh = _table("new", created=datetime.now(UTC) - timedelta(days=2))
    out = waste.check_dynamodb_provisioned(
        _ddb(_table("od", mode="PAY_PER_REQUEST"), fresh), _cw(0.0), "us-east-1")
    assert list(out) == []


def test_a_table_using_its_capacity_is_left_alone():
    out = waste.check_dynamodb_provisioned(_ddb(_table(rcu=50, wcu=50)), _cw(30.0), "us-east-1")
    assert list(out) == []


def test_an_unread_metric_is_not_idle_and_is_reported():
    cw = MagicMock()
    cw.get_metric_statistics.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "x"}}, "GetMetricStatistics")
    out = waste.check_dynamodb_provisioned(_ddb(_table()), cw, "us-east-1")
    assert list(out) == []
    [pf] = out.partial_failures
    assert (pf["error_code"], pf["count"], pf["unit"]) == ("AccessDenied", 1, "tables")


def test_the_check_is_in_the_scan_and_the_policy():
    from finops.analyzers.optimizer import _ALL_CHECKS
    from finops.scan_manifest import iam_actions
    assert "dynamodb" in _ALL_CHECKS
    assert {"dynamodb:ListTables", "dynamodb:DescribeTable"} <= set(iam_actions())
