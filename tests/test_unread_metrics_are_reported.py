"""A metric that could not be read drops the finding, and now says so.

The detectors skip a resource whose CloudWatch read failed, by design: unread
is not idle, and calling a busy NAT gateway idle because its traffic could not
be read is the worse mistake. But the skip was a log.debug. A scan whose
identity lacked cloudwatch:GetMetricStatistics lost every NAT gateway and load
balancer finding, reported partial=false, and printed a clean result. Measured
on a moto account with auth enforced: an admin saw a $32.85 idle NAT gateway
and a $16.43 idle ALB; the same scan under a policy missing the metric read
saw neither and said nothing.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from finops.analyzers import waste
from finops.analyzers.cloudwatch import MetricQuery, fetch_metric_values


def _deny(*a, **k):
    raise ClientError({"Error": {"Code": "AccessDenied", "Message": "arn:aws:iam::1:x"}},
                      "GetMetricStatistics")


def _denied_cw() -> MagicMock:
    cw = MagicMock()
    cw.get_metric_statistics.side_effect = _deny
    return cw


def _nat_client(n: int) -> MagicMock:
    ec2 = MagicMock()
    ec2.get_paginator.return_value.paginate.return_value = [{"NatGateways": [
        {"NatGatewayId": f"nat-{i}", "VpcId": "vpc-1", "SubnetId": "subnet-1"}
        for i in range(n)]}]
    return ec2


def test_the_failure_code_of_each_unread_series_is_returned():
    now = datetime.now(UTC)
    failed: dict = {}
    q = MetricQuery("k", "AWS/NATGateway", "BytesOutToDestination", (("NatGatewayId", "n"),),
                    "Sum", 86400)
    out = fetch_metric_values(_denied_cw(), [q], now - timedelta(days=1), now,
                              use_get_metric_data=False, failures=failed)
    assert out == {"k": None}
    assert failed == {"k": "AccessDenied"}


def test_a_denied_nat_metric_is_not_idle_and_is_reported():
    out = waste.check_nat_gateways(_nat_client(3), _denied_cw(), "us-east-1")
    assert list(out) == [], "an unread gateway was called idle"
    [pf] = out.partial_failures
    assert pf["call"] == "cloudwatch.get_metric_statistics"
    assert (pf["error_code"], pf["count"], pf["unit"]) == ("AccessDenied", 3, "NAT gateways")
    assert "unread is not idle" in pf["effect"]


def test_a_nat_gateway_with_no_traffic_is_still_found():
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": []}
    out = waste.check_nat_gateways(_nat_client(1), cw, "us-east-1")
    assert [f["waste_type"] for f in out] == ["idle_nat_gateway"]
    assert out.partial_failures == []


def test_a_denied_load_balancer_metric_is_reported():
    elbv2 = MagicMock()
    elbv2.get_paginator.return_value.paginate.return_value = [{"LoadBalancers": [
        {"LoadBalancerName": "a", "LoadBalancerArn": "arn:loadbalancer/app/a/1",
         "Type": "application", "State": {"Code": "active"}}]}]
    elb = MagicMock()
    elb.get_paginator.return_value.paginate.return_value = [{"LoadBalancerDescriptions": [
        {"LoadBalancerName": "classic"}]}]
    out = waste.check_idle_load_balancers(elbv2, elb, _denied_cw(), "us-east-1")
    assert list(out) == []
    [pf] = out.partial_failures
    assert (pf["error_code"], pf["count"], pf["unit"]) == ("AccessDenied", 2, "load balancers")


def test_the_scan_is_partial_and_says_how_many_went_unread(capsys):
    """End to end through the audit and the CLI renderer."""
    from unittest.mock import patch

    from finops import cli_scan
    from finops.analyzers import optimizer

    session = MagicMock()
    session.client.side_effect = lambda svc, **kw: {
        "ec2": _nat_client(2), "cloudwatch": _denied_cw()}.get(svc, MagicMock())
    with (patch.object(optimizer, "_get_boto3_session", return_value=session),
          patch.object(optimizer, "_fetch_compute_optimizer_recommendations", return_value=[])):
        report = optimizer.run_deep_audit(account_id="1", regions=["us-east-1"], checks=["nat"])
    assert report["checks_run"] == ["nat"]
    assert report["checks_failed"][0]["count"] == 2

    payload = cli_scan._json_payload(None, report, demo=False, profile="default",
                                     account_id="1", duration_s=1.0)
    assert payload["scan"]["partial"] is True

    cli_scan._render(__import__("sys").stdout, None, report, demo=False, ce_denied=False)
    out = capsys.readouterr().out
    assert "1 check(s) could not fully run" in out
    assert "nat (2 NAT gateways unread, AccessDenied)" in out
