"""What analyzers/waste.py reads for load balancers, NAT gateways and S3 buckets.

Three findings from one family: an empty CloudWatch series read as zero traffic.

- S3: GetRequests is published only for a bucket with request metrics enabled
  (under the filter id this check reads, "AllRequests"). A bucket without them
  answered with no datapoints, which counted as 0 GETs/day and recommended
  STANDARD_IA for a bucket nobody had measured.
- Gateway Load Balancers were read from AWS/NetworkELB, answered nothing, and
  every GWLB came back as an idle "NLB" at the NLB rate.
- NLBs summed 14 daily Averages of ActiveFlowCount and compared that against a
  request threshold. They now read NewFlowCount as a Sum, and an empty flow
  series is not read as idle.

Also: a NAT gateway or load balancer created inside the lookback has not had
the whole window to carry traffic, and is skipped the way check_idle_ec2 skips
an instance launched inside it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from finops.analyzers import waste
from finops.aws_prices import GWLB_HOURLY, NAT_GATEWAY_PER_MONTH, NLB_PER_MONTH


class _CW:
    """CloudWatch that answers like the real one: a series nobody publishes is
    a successful read with no datapoints."""

    def __init__(self, data: dict):
        self.data = data
        self.asked: list[tuple] = []

    def get_metric_statistics(self, **kw):
        dims = tuple((d["Name"], d["Value"]) for d in kw["Dimensions"])
        key = (kw["Namespace"], kw["MetricName"], dims)
        self.asked.append((kw["Namespace"], kw["MetricName"], kw["Statistics"][0]))
        now = datetime.now(timezone.utc)
        stat = kw["Statistics"][0]
        return {"Datapoints": [{"Timestamp": now - timedelta(days=i), stat: v}
                               for i, v in enumerate(self.data.get(key, []))]}


class _Pages:
    def __init__(self, pages):
        self.pages = pages

    def get_paginator(self, _name):
        pages = self.pages

        class _P:
            def paginate(self, **_kw):
                return iter(pages)
        return _P()


_OLD = datetime.now(timezone.utc) - timedelta(days=90)
_NEW = datetime.now(timezone.utc) - timedelta(days=2)


def _v2(name: str, lb_type: str, kind: str, created=_OLD) -> dict:
    return {"LoadBalancerName": name, "Type": lb_type, "State": {"Code": "active"},
            "CreatedTime": created,
            "LoadBalancerArn": f"arn:aws:elasticloadbalancing:us-east-1:1:loadbalancer/{kind}/{name}/abc"}


def _lbs(v2: list[dict], cw: _CW, classic: list[dict] | None = None) -> dict[str, dict]:
    findings = waste.check_idle_load_balancers(
        _Pages([{"LoadBalancers": v2}]),
        _Pages([{"LoadBalancerDescriptions": classic or []}]), cw, "us-east-1")
    return {f["lb_name"]: f for f in findings}


# ── load balancers ───────────────────────────────────────────────────────────

def test_a_busy_gwlb_is_not_an_idle_nlb():
    cw = _CW({("AWS/GatewayELB", "NewFlowCount", (("LoadBalancer", "gwy/gwlb/abc"),)):
              [50_000.0] * 14})
    assert _lbs([_v2("gwlb", "gateway", "gwy")], cw) == {}
    assert ("AWS/GatewayELB", "NewFlowCount", "Sum") in cw.asked
    assert not [a for a in cw.asked if a[0] == "AWS/NetworkELB"]


def test_an_idle_gwlb_is_labelled_and_priced_as_a_gwlb():
    cw = _CW({("AWS/GatewayELB", "NewFlowCount", (("LoadBalancer", "gwy/gwlb/abc"),)):
              [0.0] * 14})
    f = _lbs([_v2("gwlb", "gateway", "gwy")], cw)["gwlb"]
    assert f["resource_type"] == "GWLB"
    assert f["estimated_monthly_savings"] == round(GWLB_HOURLY * 730, 2) == 9.12


def test_a_gwlb_with_no_flow_datapoints_is_not_assessed():
    # What the old AWS/NetworkELB read returned for every GWLB.
    assert _lbs([_v2("gwlb", "gateway", "gwy")], _CW({})) == {}


def test_nlb_reads_new_flows_as_a_sum():
    dim = (("LoadBalancer", "net/nlb/abc"),)
    busy = _CW({("AWS/NetworkELB", "NewFlowCount", dim): [20.0] * 14})    # 280 flows
    assert _lbs([_v2("nlb", "network", "net")], busy) == {}
    assert busy.asked == [("AWS/NetworkELB", "NewFlowCount", "Sum")]

    quiet = _CW({("AWS/NetworkELB", "NewFlowCount", dim): [0.0] * 13 + [3.0]})
    f = _lbs([_v2("nlb", "network", "net")], quiet)["nlb"]
    assert f["resource_type"] == "NLB"
    assert f["estimated_monthly_savings"] == NLB_PER_MONTH
    assert f["total_requests_14d"] == 3.0
    assert "new flows" in f["detail"]


def test_an_nlb_with_no_flow_datapoints_is_not_called_idle():
    assert _lbs([_v2("nlb", "network", "net")], _CW({})) == {}


def test_an_alb_with_no_requests_is_still_idle():
    # RequestCount is published only while requests arrive, so for an ALB no
    # datapoints IS the quiet answer.
    f = _lbs([_v2("alb", "application", "app")], _CW({}))["alb"]
    assert f["resource_type"] == "ALB"


def test_a_load_balancer_created_inside_the_lookback_is_skipped():
    got = _lbs([_v2("new-alb", "application", "app", created=_NEW),
                _v2("old-alb", "application", "app")], _CW({}),
               classic=[{"LoadBalancerName": "new-clb", "CreatedTime": _NEW},
                        {"LoadBalancerName": "old-clb", "CreatedTime": _OLD}])
    assert set(got) == {"old-alb", "old-clb"}


# ── NAT gateways ─────────────────────────────────────────────────────────────

def test_a_nat_gateway_created_inside_the_lookback_is_skipped():
    ec2 = _Pages([{"NatGateways": [
        {"NatGatewayId": "nat-new", "CreateTime": _NEW},
        {"NatGatewayId": "nat-old", "CreateTime": _OLD},
        {"NatGatewayId": "nat-undated"},
    ]}])
    findings = waste.check_nat_gateways(ec2, _CW({}), "us-east-1")
    assert {f["resource_id"] for f in findings} == {"nat-old", "nat-undated"}
    assert {f["estimated_monthly_savings"] for f in findings} == {NAT_GATEWAY_PER_MONTH}


def test_a_nat_gateway_that_downloads_is_not_idle():
    """Private subnets pulling images and packages through a NAT gateway send
    requests and ACKs out (here 0.5 GB/day) and receive the data back (30
    GB/day). Reading BytesOutToDestination alone, this gateway averaged under
    the 1 GB/day threshold and was flagged idle, measured at high confidence,
    with a $32.85/mo saving for deleting it. It processes about
    30.5 GB/day x $0.045 x 30.4 = $41.73/mo of data and is in use."""
    gb = 1024 ** 3
    dims = (("NatGatewayId", "nat-old"),)
    cw = _CW({("AWS/NATGateway", "BytesOutToDestination", dims): [0.5 * gb] * 7,
              ("AWS/NATGateway", "BytesInFromDestination", dims): [30 * gb] * 7})
    ec2 = _Pages([{"NatGateways": [{"NatGatewayId": "nat-old", "CreateTime": _OLD}]}])
    assert waste.check_nat_gateways(ec2, cw, "us-east-1") == []


def test_a_nat_gateway_quiet_in_both_directions_is_still_idle():
    gb = 1024 ** 3
    dims = (("NatGatewayId", "nat-old"),)
    cw = _CW({("AWS/NATGateway", "BytesOutToDestination", dims): [0.1 * gb] * 7,
              ("AWS/NATGateway", "BytesInFromDestination", dims): [0.2 * gb] * 7})
    ec2 = _Pages([{"NatGateways": [{"NatGatewayId": "nat-old", "CreateTime": _OLD}]}])
    [f] = waste.check_nat_gateways(ec2, cw, "us-east-1")
    assert f["avg_gb_per_day"] == 0.3


def test_nat_rates_come_from_aws_prices():
    from finops.aws_prices import NAT_GATEWAY_PER_GB

    assert waste._NAT_GW_BASE_MONTHLY is NAT_GATEWAY_PER_MONTH
    assert waste._NAT_GW_DATA_PER_GB is NAT_GATEWAY_PER_GB


# ── S3 storage class ─────────────────────────────────────────────────────────

class _S3:
    def list_buckets(self):
        return {"Buckets": [{"Name": "hot-bucket"}]}


def _s3_cw(gets: list[float] | None, objects: int | None = 100_000,
           size_gb: float = 500) -> _CW:
    size = (("BucketName", "hot-bucket"), ("StorageType", "StandardStorage"))
    count = (("BucketName", "hot-bucket"), ("StorageType", "AllStorageTypes"))
    data = {("AWS/S3", "BucketSizeBytes", size): [size_gb * 1024 ** 3] * 30}
    if objects is not None:
        data[("AWS/S3", "NumberOfObjects", count)] = [objects] * 30
    if gets is not None:
        data[("AWS/S3", "GetRequests",
              (("BucketName", "hot-bucket"), ("FilterId", "AllRequests")))] = gets
    return _CW(data)


def test_a_bucket_without_request_metrics_is_not_called_low_access():
    assert waste.check_s3_storage_class(_S3(), _s3_cw(None), "us-east-1") == []


def test_a_bucket_with_measured_low_access_is_still_flagged():
    # 500 GB in 100,000 objects, about 5 MiB each.
    [f] = waste.check_s3_storage_class(_S3(), _s3_cw([5.0] * 30), "us-east-1")
    assert f["recommendation"] == "STANDARD_IA"
    assert f["avg_daily_gets"] == 5.0
    # $11.50 in STANDARD against $6.25 in STANDARD_IA.
    assert f["estimated_monthly_savings"] == 5.25
    # One lifecycle transition per object, $0.01 per 1,000 (AWS Price List,
    # AmazonS3 us-east-1, Requests-Tier4), paid once.
    assert f["one_time_transition_cost"] == 1.0
    assert "aws s3 cp" not in f["detail"]


def test_small_objects_are_not_sent_to_standard_ia():
    """500 GB in 10M objects averages 52 KiB. Standard-IA bills an object
    under 128 KB as 128 KB (the price list's TimedStorage-SIA-SmObjects line),
    and a lifecycle rule does not move objects under 128 KB by default. This
    bucket used to get STANDARD_IA with a "$5.25/mo" net saving; copied in, its
    10M objects bill as 1,220.7 GiB at $0.0125 = $15.26/mo against $11.50 in
    STANDARD, $3.76/mo more."""
    cw = _s3_cw([5.0] * 30, objects=10_000_000)
    assert waste.check_s3_storage_class(_S3(), cw, "us-east-1") == []


def test_a_bucket_whose_object_count_was_not_read_is_skipped():
    """The docstring says a bucket with no object count is skipped. It was
    recommended STANDARD_IA anyway, '(object count unknown)'."""
    cw = _s3_cw([5.0] * 30, objects=None)
    assert waste.check_s3_storage_class(_S3(), cw, "us-east-1") == []


def test_intelligent_tiering_advice_does_not_opt_into_the_archive_tier():
    """The command shipped was put-bucket-intelligent-tiering-configuration with
    ARCHIVE_ACCESS. That configuration does not move a STANDARD object into
    Intelligent-Tiering, and it opts objects already there into a tier that
    must be restored before it can be read."""
    cw = _s3_cw([50.0] * 30, objects=1_000, size_gb=1000)
    [f] = waste.check_s3_storage_class(_S3(), cw, "us-east-1")
    assert f["recommendation"] == "INTELLIGENT_TIERING"
    assert "ARCHIVE_ACCESS" not in f["detail"]
    assert "put-bucket-intelligent-tiering-configuration" not in f["detail"]
    assert "lifecycle" in f["detail"].lower()
    assert f["one_time_transition_cost"] == 0.01
