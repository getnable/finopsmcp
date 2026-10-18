# SPDX-License-Identifier: Apache-2.0
"""`nable commitments status`: what this account has committed to, and when it ends.

The free on-ramp to the paid reservation agent. It reads, for free, every
reservation and Savings Plan the account holds and says when each ends, and
it is honest about what it could not read.

What has to stay true:
  - free reads only by default: RDS, EC2, ElastiCache, Redshift, OpenSearch
    and MemoryDB reservations in each region (resolved the way `nable scan`
    resolves them) and the Savings Plans once, every page of each; no Cost
    Explorer call without --spend; a service botocore offers nowhere in a
    region is not called there, and is listed as not offered
  - every commitment read is listed with its end date, days left, state and
    charges exactly as AWS reports them
  - a read that failed is labelled not read, per service and region, with its
    reason class: never zero, never "none found"
  - "matched now" compares each active reservation with what is running this
    minute; it is never called billed utilization, and Savings Plans say
    billed utilization needs Cost Explorer
  - --spend prints its cost on stderr before the first Cost Explorer request
    and caps its pages
  - exit codes: 0 every read succeeded, 5 some failed, 4 every commitment read
    denied, 3 credentials expired, 6 no credentials; with --fail-within DAYS,
    1 a commitment read ends inside the window (this wins), 2 a commitment
    read failed (cannot check), else 0
  - --json is one document on stdout, a failure included; no escape codes in
    a pipe; nothing waits on stdin; --dry-run reads no credentials
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

from finops import cli_commitments as cli
from finops import commitments_status as cs

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
YEAR = 31_536_000
THREE_YEARS = 94_608_000
ACCOUNT = "123456789012"


# ── a session made of stubbed clients ─────────────────────────────────────────

def _client(service: str, region: str):
    return boto3.client(service, region_name=region, aws_access_key_id="testing",
                        aws_secret_access_key="testing")  # pragma: allowlist secret


# The services read since Redshift, OpenSearch and MemoryDB were added, and
# the empty answer each gives in a test that does not stub it.
NEW = {"redshift": ("describe_reserved_nodes", {"ReservedNodes": []}),
       "opensearch": ("describe_reserved_instances", {"ReservedInstances": []}),
       "memorydb": ("describe_reserved_nodes", {"ReservedNodes": []})}


class FakeSession:
    """boto3.Session as far as the command uses it: one stubbed client per
    (service, region), and a record of every client asked for. A Redshift,
    OpenSearch or MemoryDB client no test stubbed answers with nothing.
    `offered` is botocore's endpoint list as the session reports it
    ({service: regions}); without it the session cannot say, as a session
    that is not boto3's cannot."""

    def __init__(self, regions=("us-east-1", "eu-west-1"), *, creds=True, offered=None):
        self.creds = creds
        self.offered = offered
        self.requested: list[tuple[str, str | None]] = []
        self.clients: dict[tuple[str, str], object] = {}
        self.stubs: dict[tuple[str, str], Stubber] = {}
        self.touched: set[tuple[str, str]] = set()
        for region in regions:
            for svc in ("rds", "ec2", "elasticache", *NEW):
                self._add(svc, region)
        for svc in ("sts", "savingsplans", "ce"):
            self._add(svc, "us-east-1")
        if "us-east-1" not in regions:
            self._add("ec2", "us-east-1")

    def _add(self, svc, region):
        c = _client(svc, region)
        self.clients[(svc, region)] = c
        self.stubs[(svc, region)] = Stubber(c)

    def stub(self, svc, region="us-east-1") -> Stubber:
        self.touched.add((svc, region))
        return self.stubs[(svc, region)]

    def get_credentials(self):
        return object() if self.creds else None

    def get_partition_for_region(self, region):
        if self.offered is None:
            raise AttributeError("get_partition_for_region")
        return "aws"

    def get_available_regions(self, service, partition_name="aws"):
        return list(self.offered.get(service, ())) if self.offered else []

    def client(self, service, region_name=None, **_kw):
        self.requested.append((service, region_name))
        key = (service, region_name or "us-east-1")
        if service in NEW and key not in self.touched:
            op, empty = NEW[service]
            self.stub(*key).add_response(op, empty, None)
        return self.clients[key]

    def __enter__(self):
        for s in self.stubs.values():
            s.activate()
        # Cost Explorer is billed: conftest blocks every call to it unless the
        # client is stubbed, which this one is (the billed_stub fixture's flag).
        self.clients[("ce", "us-east-1")]._nable_billed_stub = True
        return self

    def __exit__(self, *exc):
        self.clients[("ce", "us-east-1")]._nable_billed_stub = False
        for s in self.stubs.values():
            s.deactivate()


def _identity(sess):
    sess.stub("sts").add_response("get_caller_identity", {
        "Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/me", "UserId": "AIDA0"})


def _regions(sess, *names):
    sess.stub("ec2").add_response("describe_regions", {
        "Regions": [{"RegionName": n, "OptInStatus": "opt-in-not-required"} for n in names]}, None)


def _rds_ri(rid, cls, product, count, start, duration, *, multi_az=False, state="active",
            lease=None):
    return {"ReservedDBInstanceId": rid, "ReservedDBInstancesOfferingId": f"off-{rid}",
            "DBInstanceClass": cls, "StartTime": start, "Duration": duration,
            "FixedPrice": 1000.0, "UsagePrice": 0.0, "CurrencyCode": "USD",
            "DBInstanceCount": count, "ProductDescription": product,
            "OfferingType": "Partial Upfront", "MultiAZ": multi_az, "State": state,
            "RecurringCharges": [{"RecurringChargeAmount": 0.05,
                                  "RecurringChargeFrequency": "Hourly"}],
            "ReservedDBInstanceArn": f"arn:aws:rds:us-east-1:{ACCOUNT}:ri:{rid}",
            **({"LeaseId": lease} if lease else {})}


def _ec2_ri(rid, itype, count, start, end, *, state="active", scope="Region", az=None):
    return {"ReservedInstancesId": rid, "InstanceType": itype, "InstanceCount": count,
            "Start": start, "End": end, "Duration": int((end - start).total_seconds()),
            "State": state, "ProductDescription": "Linux/UNIX", "OfferingType": "No Upfront",
            "OfferingClass": "standard", "InstanceTenancy": "default", "Scope": scope,
            "CurrencyCode": "USD", "FixedPrice": 0.0, "UsagePrice": 0.0,
            "RecurringCharges": [{"Amount": 0.06, "Frequency": "Hourly"}],
            **({"AvailabilityZone": az} if az else {})}


def _cache_ri(rid, ntype, product, count, start, duration, *, state="active"):
    return {"ReservedCacheNodeId": rid, "ReservedCacheNodesOfferingId": f"off-{rid}",
            "CacheNodeType": ntype, "StartTime": start, "Duration": duration,
            "FixedPrice": 500.0, "UsagePrice": 0.0, "CacheNodeCount": count,
            "ProductDescription": product, "OfferingType": "All Upfront", "State": state,
            "RecurringCharges": [],
            "ReservationARN": f"arn:aws:elasticache:us-east-1:{ACCOUNT}:reserved-instance:{rid}"}


def _sp(pid, kind, end, *, state="active", commitment="2.5", family=None, region=None):
    return {"savingsPlanId": pid,
            "savingsPlanArn": f"arn:aws:savingsplans::{ACCOUNT}:savingsplan/{pid}",
            "savingsPlanType": kind, "state": state, "start": "2024-03-01T00:00:00.000Z",
            "end": end, "commitment": commitment, "currency": "USD",
            "paymentOption": "No Upfront", "productTypes": ["EC2", "Fargate", "Lambda"],
            "termDurationInSeconds": THREE_YEARS, "upfrontPaymentAmount": "0.0",
            "recurringPaymentAmount": commitment,
            **({"ec2InstanceFamily": family} if family else {}),
            **({"region": region} if region else {})}


def _full_account(sess, *, discover=True, sp_page_2_denied=False):
    """Two regions, four services, more than one page where AWS pages."""
    _identity(sess)
    if discover:
        _regions(sess, "us-east-1", "eu-west-1")
    rds = sess.stub("rds")
    rds.add_response("describe_reserved_db_instances", {
        "ReservedDBInstances": [_rds_ri("ri-db-1", "db.m5.large", "mysql", 2,
                                        datetime(2025, 11, 1, tzinfo=UTC), YEAR,
                                        lease="lease-1")],
        "Marker": "m2"}, {"MaxRecords": 100})
    rds.add_response("describe_reserved_db_instances", {
        "ReservedDBInstances": [_rds_ri("ri-db-2", "db.r6g.large", "postgresql", 1,
                                        datetime(2024, 6, 1, tzinfo=UTC), THREE_YEARS,
                                        multi_az=True)]},
        {"MaxRecords": 100, "Marker": "m2"})
    ec2 = sess.stub("ec2")
    ec2.add_response("describe_reserved_instances", {"ReservedInstances": [
        _ec2_ri("ri-ec2-1", "m5.large", 4, datetime(2026, 1, 10, tzinfo=UTC),
                datetime(2027, 1, 10, tzinfo=UTC)),
        _ec2_ri("ri-ec2-old", "m4.large", 1, datetime(2022, 1, 10, tzinfo=UTC),
                datetime(2023, 1, 10, tzinfo=UTC), state="retired")]}, {})
    cache = sess.stub("elasticache")
    cache.add_response("describe_reserved_cache_nodes", {"ReservedCacheNodes": [
        _cache_ri("rcn-1", "cache.r6g.large", "redis", 2,
                  datetime(2026, 3, 1, tzinfo=UTC), YEAR)]}, {"MaxRecords": 100})
    # What is running now in us-east-1, read because reservations are active there.
    rds.add_response("describe_db_instances", {"DBInstances": [
        {"DBInstanceIdentifier": "a", "DBInstanceClass": "db.m5.large", "Engine": "mysql",
         "DBInstanceStatus": "available", "MultiAZ": False,
         "LicenseModel": "general-public-license"},
        {"DBInstanceIdentifier": "b", "DBInstanceClass": "db.m5.large", "Engine": "mysql",
         "DBInstanceStatus": "stopped", "MultiAZ": False,
         "LicenseModel": "general-public-license"},
        {"DBInstanceIdentifier": "c", "DBInstanceClass": "db.r6g.large", "Engine": "postgres",
         "DBInstanceStatus": "available", "MultiAZ": True,
         "LicenseModel": "postgresql-license"}]}, {"MaxRecords": 100})
    ec2.add_response("describe_instances", {"Reservations": [{"Instances": [
        {"InstanceId": f"i-{n}", "InstanceType": "m5.large",
         "State": {"Name": "running", "Code": 16}, "PlatformDetails": "Linux/UNIX",
         "Placement": {"AvailabilityZone": "us-east-1a", "Tenancy": "default"},
         **({"InstanceLifecycle": "spot"} if n == 3 else {})} for n in (1, 2, 3)]}]},
        {"Filters": [{"Name": "instance-state-name", "Values": ["running"]}],
         "MaxResults": 1000})
    cache.add_response("describe_cache_clusters", {"CacheClusters": [
        {"CacheClusterId": "c1", "CacheNodeType": "cache.r6g.large", "Engine": "redis",
         "CacheClusterStatus": "available", "NumCacheNodes": 3}]}, {"MaxRecords": 100})
    # eu-west-1: one EC2 reservation ending soon, nothing running for it.
    sess.stub("rds", "eu-west-1").add_response("describe_reserved_db_instances",
                                                {"ReservedDBInstances": []}, {"MaxRecords": 100})
    eu = sess.stub("ec2", "eu-west-1")
    eu.add_response("describe_reserved_instances", {"ReservedInstances": [
        _ec2_ri("ri-ec2-eu", "c6g.xlarge", 1, datetime(2025, 12, 1, tzinfo=UTC),
                datetime(2026, 12, 1, tzinfo=UTC))]}, {})
    sess.stub("elasticache", "eu-west-1").add_response(
        "describe_reserved_cache_nodes", {"ReservedCacheNodes": []}, {"MaxRecords": 100})
    eu.add_response("describe_instances", {"Reservations": []},
                    {"Filters": [{"Name": "instance-state-name", "Values": ["running"]}],
                     "MaxResults": 1000})
    sp = sess.stub("savingsplans")
    sp.add_response("describe_savings_plans", {
        "savingsPlans": [_sp("sp-1", "Compute", "2027-03-01T00:00:00.000Z")], "nextToken": "t2"},
        {"maxResults": 1000})
    if sp_page_2_denied:
        _denied(sp, "describe_savings_plans", "AccessDeniedException")
        return
    sp.add_response("describe_savings_plans", {
        "savingsPlans": [_sp("sp-2", "EC2Instance", "2026-12-15T00:00:00.000Z", family="m5",
                             region="us-east-1", commitment="0.75")]},
        {"maxResults": 1000, "nextToken": "t2"})


def _redshift_ri(rid, ntype, count, start, duration, *, state="active"):
    return {"ReservedNodeId": rid, "ReservedNodeOfferingId": f"off-{rid}", "NodeType": ntype,
            "StartTime": start, "Duration": duration, "FixedPrice": 2000.0, "UsagePrice": 0.0,
            "CurrencyCode": "USD", "NodeCount": count, "State": state,
            "OfferingType": "All Upfront", "RecurringCharges": [],
            "ReservedNodeOfferingType": "Regular"}


def _opensearch_ri(rid, itype, count, start, duration, *, state="active"):
    return {"ReservationName": f"name-{rid}", "ReservedInstanceId": rid,
            "BillingSubscriptionId": 4242, "ReservedInstanceOfferingId": f"off-{rid}",
            "InstanceType": itype, "StartTime": start, "Duration": duration,
            "FixedPrice": 0.0, "UsagePrice": 0.0, "CurrencyCode": "USD",
            "InstanceCount": count, "State": state, "PaymentOption": "NO_UPFRONT",
            "RecurringCharges": [{"RecurringChargeAmount": 0.12,
                                  "RecurringChargeFrequency": "Hourly"}]}


def _memorydb_ri(rid, ntype, count, start, duration, *, state="active"):
    return {"ReservationId": rid, "ReservedNodesOfferingId": f"off-{rid}", "NodeType": ntype,
            "StartTime": start, "Duration": duration, "FixedPrice": 300.0, "NodeCount": count,
            "OfferingType": "Partial Upfront", "State": state,
            "RecurringCharges": [{"RecurringChargeAmount": 0.03,
                                  "RecurringChargeFrequency": "Hourly"}],
            "ARN": f"arn:aws:memorydb:us-east-1:{ACCOUNT}:reservednode/{rid}"}


def _quiet_core(sess, region="us-east-1"):
    """RDS, EC2 and ElastiCache in `region` answering with nothing."""
    sess.stub("rds", region).add_response("describe_reserved_db_instances",
                                          {"ReservedDBInstances": []}, None)
    sess.stub("ec2", region).add_response("describe_reserved_instances",
                                          {"ReservedInstances": []}, {})
    sess.stub("elasticache", region).add_response("describe_reserved_cache_nodes",
                                                  {"ReservedCacheNodes": []}, None)


def _new_services_on_several_pages(sess):
    """Redshift on two pages (Marker), OpenSearch on three and MemoryDB on two
    (NextToken), with the request each page must carry."""
    rs = sess.stub("redshift")
    rs.add_response("describe_reserved_nodes", {"ReservedNodes": [
        _redshift_ri("rn-1", "ra3.4xlarge", 2, datetime(2025, 12, 1, tzinfo=UTC), YEAR)],
        "Marker": "rm2"}, {"MaxRecords": 100})
    rs.add_response("describe_reserved_nodes", {"ReservedNodes": [
        _redshift_ri("rn-2", "dc2.large", 4, datetime(2024, 1, 1, tzinfo=UTC), THREE_YEARS,
                     state="pending-payment"),
        _redshift_ri("rn-old", "dc2.large", 1, datetime(2020, 1, 1, tzinfo=UTC), YEAR,
                     state="retired")]}, {"MaxRecords": 100, "Marker": "rm2"})
    os_ = sess.stub("opensearch")
    os_.add_response("describe_reserved_instances", {"ReservedInstances": [
        _opensearch_ri("11111111-1111-1111-1111-111111111111", "r6g.large.search", 3,
                       datetime(2026, 2, 1, tzinfo=UTC), YEAR)], "NextToken": "o2"},
        {"MaxResults": 100})
    os_.add_response("describe_reserved_instances", {"ReservedInstances": [],
                                                     "NextToken": "o3"},
                     {"MaxResults": 100, "NextToken": "o2"})
    os_.add_response("describe_reserved_instances", {"ReservedInstances": [
        _opensearch_ri("22222222-2222-2222-2222-222222222222", "m6g.large.search", 1,
                       datetime(2026, 8, 1, tzinfo=UTC), YEAR)]},
        {"MaxResults": 100, "NextToken": "o3"})
    mdb = sess.stub("memorydb")
    mdb.add_response("describe_reserved_nodes", {"ReservedNodes": [
        _memorydb_ri("mrn-1", "db.r6g.large", 2, datetime(2026, 1, 15, tzinfo=UTC), YEAR)],
        "NextToken": "m2"}, {"MaxResults": 100})
    mdb.add_response("describe_reserved_nodes", {"ReservedNodes": [
        _memorydb_ri("mrn-2", "db.r6g.xlarge", 1, datetime(2025, 11, 20, tzinfo=UTC), YEAR,
                     state="payment-pending")]}, {"MaxResults": 100, "NextToken": "m2"})


def _parse(*argv):
    import argparse
    p = argparse.ArgumentParser(prog="nable")
    sub = p.add_subparsers(dest="cmd")
    cli.add_parser(sub)
    return p.parse_args(["commitments", "status", *argv])


def _run(sess, *argv, now=NOW):
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(_parse(*argv), session=sess, now=now, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _json(sess, *argv, now=NOW):
    code, out, err = _run(sess, "--json", *argv, now=now)
    return code, json.loads(out), err


# ── reads ─────────────────────────────────────────────────────────────────────

def test_every_commitment_on_every_page_in_both_regions_is_listed():
    with FakeSession() as sess:
        _full_account(sess)
        code, doc, _err = _json(sess)
        for s in sess.stubs.values():
            s.assert_no_pending_responses()
    assert code == cli.EXIT_OK, doc
    assert doc["account"] == ACCOUNT and doc["regions"] == ["us-east-1", "eu-west-1"]
    got = {c["id"]: c for c in doc["commitments"]}
    assert set(got) == {"ri-db-1", "ri-db-2", "ri-ec2-1", "ri-ec2-old", "ri-ec2-eu", "rcn-1",
                        "sp-1", "sp-2"}
    assert doc["not_read"] == []
    rds = got["ri-db-1"]
    assert (rds["service"], rds["region"], rds["class"], rds["count"], rds["product"]) == \
        ("rds", "us-east-1", "db.m5.large", 2, "mysql")
    assert rds["end"] == "2026-11-01T00:00:00+00:00" and rds["end_basis"] == "start plus term"
    assert rds["days_left"] == 14 and rds["state"] == "active" and rds["term"] == "1 year"
    assert rds["offering_type"] == "Partial Upfront"
    assert rds["charges"] == {"FixedPrice": 1000.0, "UsagePrice": 0.0, "CurrencyCode": "USD",
                              "RecurringCharges": [{"RecurringChargeAmount": 0.05,
                                                    "RecurringChargeFrequency": "Hourly"}]}
    assert got["ri-db-2"]["term"] == "3 years" and got["ri-db-2"]["multi_az"] is True
    ec2 = got["ri-ec2-1"]
    assert ec2["end"] == "2027-01-10T00:00:00+00:00" and ec2["end_basis"] == "reported"
    assert ec2["charges"]["RecurringCharges"] == [{"Amount": 0.06, "Frequency": "Hourly"}]
    assert got["ri-ec2-old"]["state"] == "retired" and got["ri-ec2-old"]["days_left"] < 0
    assert got["ri-ec2-eu"]["region"] == "eu-west-1" and got["ri-ec2-eu"]["days_left"] == 44
    sp = got["sp-2"]
    assert (sp["service"], sp["class"], sp["end"], sp["state"]) == \
        ("savingsplans", "EC2Instance m5", "2026-12-15T00:00:00+00:00", "active")
    assert sp["charges"] == {"commitment": "0.75", "currency": "USD",
                             "upfrontPaymentAmount": "0.0", "recurringPaymentAmount": "0.75"}
    assert got["sp-1"]["region"] == "all regions"


def test_redshift_opensearch_and_memorydb_are_listed_on_every_page():
    with FakeSession(regions=("us-east-1",)) as sess:
        _identity(sess)
        _regions(sess, "us-east-1")
        _quiet_core(sess)
        _new_services_on_several_pages(sess)
        sess.stub("savingsplans").add_response("describe_savings_plans", {"savingsPlans": []}, None)
        code, doc, _ = _json(sess)
        for st in sess.stubs.values():
            st.assert_no_pending_responses()
    assert code == cli.EXIT_OK, doc["not_read"]
    got = {c["id"]: c for c in doc["commitments"]}
    assert set(got) == {"rn-1", "rn-2", "rn-old", "11111111-1111-1111-1111-111111111111",
                        "22222222-2222-2222-2222-222222222222", "mrn-1", "mrn-2"}
    rs = got["rn-1"]
    assert (rs["service"], rs["region"], rs["class"], rs["count"], rs["offering_type"]) == \
        ("redshift", "us-east-1", "ra3.4xlarge", 2, "All Upfront")
    # AWS gives no end: it is the start plus the term, and says so.
    assert rs["end"] == "2026-12-01T00:00:00+00:00" and rs["end_basis"] == "start plus term"
    assert rs["days_left"] == 44 and rs["term"] == "1 year" and rs["state"] == "active"
    assert rs["charges"] == {"FixedPrice": 2000.0, "UsagePrice": 0.0, "CurrencyCode": "USD",
                             "RecurringCharges": []}
    assert got["rn-2"]["state"] == "pending-payment" and got["rn-2"]["term"] == "3 years"
    assert got["rn-old"]["state"] == "retired" and got["rn-old"]["days_left"] < 0
    osr = got["22222222-2222-2222-2222-222222222222"]
    assert (osr["service"], osr["class"], osr["count"], osr["offering_type"]) == \
        ("opensearch", "m6g.large.search", 1, "NO_UPFRONT")
    assert osr["end"] == "2027-08-01T00:00:00+00:00" and osr["end_basis"] == "start plus term"
    assert osr["charges"]["RecurringCharges"] == [{"RecurringChargeAmount": 0.12,
                                                   "RecurringChargeFrequency": "Hourly"}]
    mdb = got["mrn-1"]
    assert (mdb["service"], mdb["class"], mdb["count"]) == ("memorydb", "db.r6g.large", 2)
    assert mdb["end"] == "2027-01-15T00:00:00+00:00" and mdb["days_left"] == 89
    assert mdb["charges"] == {"FixedPrice": 300.0, "RecurringCharges": [
        {"RecurringChargeAmount": 0.03, "RecurringChargeFrequency": "Hourly"}]}
    assert got["mrn-2"]["state"] == "payment-pending"
    # Matched now is not compared for these, and says so.
    for rid in ("rn-1", "11111111-1111-1111-1111-111111111111", "mrn-1"):
        assert got[rid]["matched_now"] == {"basis": "matched now", "compared": False,
                                           "reason": "not compared"}
    assert doc["summary"]["us-east-1"] == {"rds": 0, "ec2": 0, "elasticache": 0,
                                           "redshift": 3, "opensearch": 2, "memorydb": 2}
    # OpenSearch is read once, through its own API: no es client was asked for.
    assert not [r for r in sess.requested if r[0] == "es"]


def test_the_new_services_in_the_human_listing_and_the_gate():
    with FakeSession(regions=("us-east-1",)) as sess:
        _identity(sess)
        _regions(sess, "us-east-1")
        _quiet_core(sess)
        _new_services_on_several_pages(sess)
        sess.stub("savingsplans").add_response("describe_savings_plans", {"savingsPlans": []}, None)
        code, out, _ = _run(sess, "--fail-within", "60")
    assert code == cli.EXIT_ENDING
    line = next(ln for ln in out.splitlines() if "rn-1" in ln)
    assert "2026-12-01" in line and "44 days left" in line and "matched now not compared" in line
    assert "matched now not read" not in out
    # A pending payment is still bought: it counts for the gate.
    assert "2 commitments end within 60 days (mrn-2, rn-1)" in out


def test_a_new_service_read_that_fails_is_not_read_per_service_and_region():
    with FakeSession() as sess:
        _identity(sess)
        _regions(sess, "us-east-1", "eu-west-1")
        for region in ("us-east-1", "eu-west-1"):
            _quiet_core(sess, region)
        _denied(sess.stub("redshift", "eu-west-1"), "describe_reserved_nodes",
                "AccessDenied", 403)
        _denied(sess.stub("opensearch", "eu-west-1"), "describe_reserved_instances",
                "ThrottlingException", 429)
        mdb = sess.stub("memorydb")
        mdb.add_response("describe_reserved_nodes", {"ReservedNodes": [
            _memorydb_ri("mrn-1", "db.r6g.large", 2, datetime(2026, 1, 15, tzinfo=UTC), YEAR)],
            "NextToken": "m2"}, {"MaxResults": 100})
        _denied(mdb, "describe_reserved_nodes", "ThrottlingException", 400)
        sess.stub("savingsplans").add_response("describe_savings_plans", {"savingsPlans": []}, None)
        code, doc, _ = _json(sess, "--fail-within", "30")
    assert code == cli.EXIT_CANNOT_CHECK
    nr = {(n["service"], n["region"]): n for n in doc["not_read"]}
    assert set(nr) == {("redshift", "eu-west-1"), ("opensearch", "eu-west-1"),
                       ("memorydb", "us-east-1")}
    assert (nr[("redshift", "eu-west-1")]["reason"],
            nr[("redshift", "eu-west-1")]["iam_action"]) == ("denied",
                                                            "redshift:DescribeReservedNodes")
    assert (nr[("opensearch", "eu-west-1")]["reason"],
            nr[("opensearch", "eu-west-1")]["iam_action"]) == ("throttled",
                                                              "es:DescribeReservedInstances")
    assert nr[("memorydb", "us-east-1")]["page"] == 2
    assert nr[("memorydb", "us-east-1")]["iam_action"] == "memorydb:DescribeReservedNodes"
    # The page that was read is kept; nothing says a region holds none.
    assert [c["id"] for c in doc["commitments"]] == ["mrn-1"]
    assert doc["summary"]["eu-west-1"]["redshift"] == "not read"
    assert doc["summary"]["eu-west-1"]["opensearch"] == "not read"
    assert doc["summary"]["us-east-1"]["memorydb"] == "not read"
    with FakeSession(regions=("us-east-1",)) as sess:
        _identity(sess)
        _regions(sess, "us-east-1")
        _quiet_core(sess)
        _denied(sess.stub("redshift"), "describe_reserved_nodes", "AccessDenied", 403)
        sess.stub("savingsplans").add_response("describe_savings_plans", {"savingsPlans": []}, None)
        code, _doc, _ = _json(sess)
    assert code == cli.EXIT_PARTIAL


def test_a_service_with_no_endpoint_in_a_region_is_not_offered_and_not_a_failure():
    """MemoryDB is offered in fewer regions than EC2 (botocore lists none in
    ap-northeast-3): it is not called there, it is listed as not offered, and
    an account whose every read answered still exits 0, and its gate still
    answers."""
    offered = {"memorydb": ["us-east-1"], "redshift": ["us-east-1", "ap-northeast-3"],
               "opensearch": ["us-east-1", "ap-northeast-3"]}
    with FakeSession(regions=("us-east-1", "ap-northeast-3"), offered=offered) as sess:
        _identity(sess)
        _regions(sess, "us-east-1", "ap-northeast-3")
        for region in ("us-east-1", "ap-northeast-3"):
            _quiet_core(sess, region)
        sess.stub("savingsplans").add_response("describe_savings_plans", {"savingsPlans": []}, None)
        code, doc, _ = _json(sess, "--fail-within", "30")
    assert code == 0 and doc["gate"]["cannot_check"] is False
    assert ("memorydb", "ap-northeast-3") not in sess.requested
    assert ("memorydb", "us-east-1") in sess.requested
    assert doc["not_offered"] == [{"service": "memorydb", "region": "ap-northeast-3",
                                   "reason": "botocore's endpoint list has no memorydb "
                                             "endpoint in ap-northeast-3"}]
    assert doc["not_read"] == []
    assert doc["summary"]["ap-northeast-3"]["memorydb"] == "not offered"


def test_an_endpoint_list_that_cannot_be_read_never_skips_a_read():
    class NoList(FakeSession):
        def get_available_regions(self, service, partition_name="aws"):
            raise RuntimeError("no endpoint data")
    with NoList(regions=("us-east-1",), offered={}) as sess:
        assert cs.offered_in(sess, "memorydb", "us-east-1") is True
    # botocore naming the service nowhere in the partition is not a no either.
    with FakeSession(regions=("us-east-1",), offered={"redshift": ["us-east-1"]}) as sess:
        assert cs.offered_in(sess, "memorydb", "us-east-1") is True
        assert cs.offered_in(sess, "rds", "anywhere-1") is True


def test_the_human_listing_names_each_commitment_with_its_end():
    with FakeSession() as sess:
        _full_account(sess)
        code, out, _err = _run(sess)
    assert code == cli.EXIT_OK
    for rid, end, left in (("ri-db-1", "2026-11-01", "14 days left"),
                           ("ri-ec2-eu", "2026-12-01", "44 days left"),
                           ("sp-2", "2026-12-15", "58 days left"),
                           ("ri-ec2-old", "2023-01-10", "ended")):
        line = next(ln for ln in out.splitlines() if rid in ln)
        assert end in line and left in line, line
    assert "FixedPrice 1000.0 USD" in out and "RecurringCharges 0.05 Hourly" in out
    assert "this account only" in out


# ── failures are labelled, never counted as zero ──────────────────────────────

def _denied(stub, op, code="AccessDenied", status=403):
    stub.add_client_error(op, service_error_code=code, service_message="no",
                          http_status_code=status)


def test_a_denied_or_throttled_region_is_not_read_and_exit_is_5():
    with FakeSession() as sess:
        _identity(sess)
        _regions(sess, "us-east-1", "eu-west-1")
        sess.stub("rds").add_response("describe_reserved_db_instances",
                                      {"ReservedDBInstances": []}, None)
        sess.stub("ec2").add_response("describe_reserved_instances", {"ReservedInstances": []}, {})
        sess.stub("elasticache").add_response("describe_reserved_cache_nodes",
                                              {"ReservedCacheNodes": []}, None)
        _denied(sess.stub("rds", "eu-west-1"), "describe_reserved_db_instances")
        _denied(sess.stub("ec2", "eu-west-1"), "describe_reserved_instances",
                "RequestLimitExceeded", 503)
        _denied(sess.stub("elasticache", "eu-west-1"), "describe_reserved_cache_nodes",
                "Throttling", 400)
        sess.stub("savingsplans").add_response("describe_savings_plans", {"savingsPlans": []}, None)
        code, doc, _ = _json(sess)
    assert code == cli.EXIT_PARTIAL == 5
    nr = {(n["service"], n["region"]): n for n in doc["not_read"]}
    assert set(nr) == {("rds", "eu-west-1"), ("ec2", "eu-west-1"), ("elasticache", "eu-west-1")}
    assert nr[("rds", "eu-west-1")]["reason"] == "denied"
    assert nr[("rds", "eu-west-1")]["code"] == "AccessDenied"
    assert nr[("rds", "eu-west-1")]["iam_action"] == "rds:DescribeReservedDBInstances"
    assert nr[("ec2", "eu-west-1")]["reason"] == "throttled"
    assert nr[("elasticache", "eu-west-1")]["reason"] == "throttled"
    # Nothing was read there, and nothing says it holds none.
    assert not [c for c in doc["commitments"] if c["region"] == "eu-west-1"]
    assert doc["summary"]["eu-west-1"] == {"rds": "not read", "ec2": "not read",
                                           "elasticache": "not read", "redshift": 0,
                                           "opensearch": 0, "memorydb": 0}
    assert doc["summary"]["us-east-1"] == {"rds": 0, "ec2": 0, "elasticache": 0,
                                           "redshift": 0, "opensearch": 0, "memorydb": 0}


def test_the_human_output_says_not_read_and_never_none_found():
    with FakeSession(regions=("us-east-1",)) as sess:
        _identity(sess)
        _regions(sess, "us-east-1")
        _denied(sess.stub("rds"), "describe_reserved_db_instances")
        sess.stub("ec2").add_response("describe_reserved_instances", {"ReservedInstances": []}, {})
        sess.stub("elasticache").add_response("describe_reserved_cache_nodes",
                                              {"ReservedCacheNodes": []}, None)
        sess.stub("savingsplans").add_response("describe_savings_plans", {"savingsPlans": []}, None)
        code, out, _ = _run(sess)
    assert code == 5
    assert re.search(r"not read.*rds.*us-east-1.*denied.*rds:DescribeReservedDBInstances", out,
                     re.DOTALL), out
    assert "none found" not in out.lower() and "no commitments" not in out.lower()


def test_a_later_page_that_fails_keeps_the_pages_read_and_is_not_read():
    with FakeSession(regions=("us-east-1",)) as sess:
        _identity(sess)
        _regions(sess, "us-east-1")
        rds = sess.stub("rds")
        rds.add_response("describe_reserved_db_instances", {"ReservedDBInstances": [
            _rds_ri("ri-1", "db.m5.large", "mysql", 1, datetime(2026, 1, 1, tzinfo=UTC), YEAR,
                    state="retired")], "Marker": "m2"}, None)
        _denied(rds, "describe_reserved_db_instances", "Throttling", 400)
        sess.stub("ec2").add_response("describe_reserved_instances", {"ReservedInstances": []}, {})
        sess.stub("elasticache").add_response("describe_reserved_cache_nodes",
                                              {"ReservedCacheNodes": []}, None)
        sess.stub("savingsplans").add_response("describe_savings_plans", {"savingsPlans": []}, None)
        code, doc, _ = _json(sess)
    assert code == 5
    assert [c["id"] for c in doc["commitments"]] == ["ri-1"]
    [nr] = doc["not_read"]
    assert nr["service"] == "rds" and nr["page"] == 2 and nr["reason"] == "throttled"
    assert doc["summary"]["us-east-1"]["rds"] == "not read"


# ── credentials ───────────────────────────────────────────────────────────────

def test_no_credentials_exits_6_with_a_document():
    with FakeSession(creds=False) as sess:
        code, doc, _ = _json(sess)
    assert code == cli.EXIT_NO_CREDS == 6
    assert doc["error"]["class"] == "no-credentials" and doc["error"]["exit_code"] == 6
    assert sess.requested == []


def test_expired_credentials_exit_3():
    with FakeSession() as sess:
        sess.stub("sts").add_client_error("get_caller_identity", service_error_code="ExpiredToken",
                                          service_message="expired", http_status_code=403)
        code, doc, _ = _json(sess)
    assert code == cli.EXIT_EXPIRED == 3
    assert doc["error"]["class"] == "expired"


def test_access_denied_in_every_region_exits_4():
    with FakeSession() as sess:
        _identity(sess)
        _denied(sess.stub("ec2"), "describe_regions", "UnauthorizedOperation")
        for region in ("us-east-1",):
            _deny_every_read(sess, region)
        _denied(sess.stub("savingsplans"), "describe_savings_plans", "AccessDeniedException")
        code, doc, _ = _json(sess)
    assert code == cli.EXIT_DENIED == 4
    assert doc["commitments"] == []
    assert {n["reason"] for n in doc["not_read"]} == {"denied"}
    assert {n["iam_action"] for n in doc["not_read"]} >= {
        "rds:DescribeReservedDBInstances", "ec2:DescribeReservedInstances",
        "elasticache:DescribeReservedCacheNodes", "savingsplans:DescribeSavingsPlans",
        "redshift:DescribeReservedNodes", "es:DescribeReservedInstances",
        "memorydb:DescribeReservedNodes"}


def _deny_every_read(sess, region):
    _denied(sess.stub("rds", region), "describe_reserved_db_instances")
    _denied(sess.stub("ec2", region), "describe_reserved_instances", "UnauthorizedOperation")
    _denied(sess.stub("elasticache", region), "describe_reserved_cache_nodes")
    _denied(sess.stub("redshift", region), "describe_reserved_nodes",
            "AccessDenied")
    _denied(sess.stub("opensearch", region), "describe_reserved_instances",
            "AccessDeniedException")
    _denied(sess.stub("memorydb", region), "describe_reserved_nodes",
            "AccessDeniedException", 400)


def test_one_new_service_answering_keeps_it_from_exit_4():
    """Every read denied but MemoryDB's, which answered with nothing: the
    account is partly read (5), not denied everywhere (4)."""
    with FakeSession(regions=("us-east-1",)) as sess:
        _identity(sess)
        _regions(sess, "us-east-1")
        for svc, op, code in (("rds", "describe_reserved_db_instances", "AccessDenied"),
                              ("ec2", "describe_reserved_instances", "UnauthorizedOperation"),
                              ("elasticache", "describe_reserved_cache_nodes", "AccessDenied"),
                              ("redshift", "describe_reserved_nodes", "AccessDenied"),
                              ("opensearch", "describe_reserved_instances",
                               "AccessDeniedException")):
            _denied(sess.stub(svc), op, code)
        _denied(sess.stub("savingsplans"), "describe_savings_plans", "AccessDeniedException")
        code, doc, _ = _json(sess)
    assert code == cli.EXIT_PARTIAL == 5
    assert doc["summary"]["us-east-1"]["memorydb"] == 0


def test_every_commitment_read_denied_in_two_regions_exits_4_even_with_regions_listed():
    with FakeSession() as sess:
        _identity(sess)
        _regions(sess, "us-east-1", "eu-west-1")
        for region in ("us-east-1", "eu-west-1"):
            _deny_every_read(sess, region)
        _denied(sess.stub("savingsplans"), "describe_savings_plans", "AccessDeniedException")
        code, out, _ = _run(sess)
    assert code == 4
    assert "permission" in out.lower() and "IncludeCommitments" in out


# ── the gate ──────────────────────────────────────────────────────────────────

def test_fail_within_exits_1_when_a_read_commitment_ends_inside_even_if_a_read_failed():
    with FakeSession() as sess:
        _full_account(sess, sp_page_2_denied=True)
        code, doc, _ = _json(sess, "--fail-within", "90")
    assert code == cli.EXIT_ENDING == 1
    # Ending inside 90 days of NOW: 2026-11-01, 2026-12-01 and 2027-01-10.
    assert set(doc["gate"]["ending"]) == {"ri-db-1", "ri-ec2-eu", "ri-ec2-1"}
    assert doc["gate"]["cannot_check"] is True and doc["not_read"]


def test_fail_within_exits_2_when_nothing_read_ends_inside_but_a_read_failed():
    with FakeSession(regions=("us-east-1",)) as sess:
        _identity(sess)
        _regions(sess, "us-east-1")
        sess.stub("rds").add_response("describe_reserved_db_instances",
                                      {"ReservedDBInstances": []}, None)
        sess.stub("ec2").add_response("describe_reserved_instances", {"ReservedInstances": [
            _ec2_ri("ri-long", "m5.large", 1, datetime(2026, 1, 1, tzinfo=UTC),
                    datetime(2029, 1, 1, tzinfo=UTC))]}, {})
        _denied(sess.stub("elasticache"), "describe_reserved_cache_nodes")
        sess.stub("savingsplans").add_response("describe_savings_plans", {"savingsPlans": []}, None)
        sess.stub("ec2").add_response("describe_instances", {"Reservations": []}, None)
        code, doc, _ = _json(sess, "--fail-within", "90")
    assert code == cli.EXIT_CANNOT_CHECK == 2
    assert doc["gate"]["ending"] == [] and doc["gate"]["cannot_check"] is True


def test_fail_within_exits_0_when_every_read_succeeded_and_nothing_ends():
    with FakeSession() as sess:
        _full_account(sess)
        code, doc, _ = _json(sess, "--fail-within", "7")
    assert code == 0 and doc["gate"] == {"days": 7, "ending": [], "cannot_check": False}


def test_a_retired_commitment_is_not_ending():
    with FakeSession() as sess:
        _full_account(sess)
        code, doc, _ = _json(sess, "--fail-within", "10000")
    assert code == 1 and "ri-ec2-old" not in doc["gate"]["ending"]


def test_the_gate_cannot_check_without_credentials():
    with FakeSession(creds=False) as sess:
        code, doc, _ = _json(sess, "--fail-within", "30")
    assert code == 2 and doc["error"]["class"] == "no-credentials"


# ── utilization ───────────────────────────────────────────────────────────────

def test_the_default_matches_what_runs_now_and_never_calls_cost_explorer():
    with FakeSession() as sess:
        _full_account(sess)
        code, doc, _ = _json(sess)
    assert code == 0
    assert not [r for r in sess.requested if r[0] == "ce"]
    got = {c["id"]: c for c in doc["commitments"]}
    m = got["ri-db-1"]["matched_now"]
    assert (m["running"], m["matched"], m["reserved"]) == (1, 1, 2)     # one stopped
    assert got["ri-db-2"]["matched_now"]["matched"] == 1                # Multi-AZ postgres
    assert got["ri-ec2-1"]["matched_now"]["matched"] == 2              # the spot one left out
    assert got["rcn-1"]["matched_now"]["matched"] == 2
    assert got["ri-ec2-eu"]["matched_now"]["matched"] == 0
    assert got["ri-ec2-old"]["matched_now"] is None                     # retired
    assert doc["utilization"]["basis"] == "matched now"
    assert "billed utilization" in doc["utilization"]["note"]
    blob = json.dumps(doc).lower()
    assert "billed utilization:" not in blob
    for sp in ("sp-1", "sp-2"):
        assert got[sp]["matched_now"] is None
    assert "Cost Explorer" in doc["savings_plans_note"]
    assert "only its own" in doc["savings_plans_note"]
    assert doc["billed_utilization"] is None


def test_the_human_output_calls_it_matched_now():
    with FakeSession() as sess:
        _full_account(sess)
        _code, out, _ = _run(sess)
    assert "matched now 1 of 2" in out
    assert "not billed utilization" in out
    assert "Savings Plans" in out and "Cost Explorer" in out and "only its own" in out


def test_matched_now_shares_what_runs_between_reservations_of_one_kind():
    """Two reservations for the same class share the instances running: two
    running m5.large are two matched, never four."""
    with FakeSession(regions=("us-east-1",)) as sess:
        _identity(sess)
        _regions(sess, "us-east-1")
        sess.stub("rds").add_response("describe_reserved_db_instances",
                                      {"ReservedDBInstances": []}, None)
        ec2 = sess.stub("ec2")
        ec2.add_response("describe_reserved_instances", {"ReservedInstances": [
            _ec2_ri("ri-a", "m5.large", 2, datetime(2026, 1, 1, tzinfo=UTC),
                    datetime(2027, 1, 1, tzinfo=UTC)),
            _ec2_ri("ri-b", "m5.large", 2, datetime(2026, 2, 1, tzinfo=UTC),
                    datetime(2027, 2, 1, tzinfo=UTC))]}, {})
        sess.stub("elasticache").add_response("describe_reserved_cache_nodes",
                                              {"ReservedCacheNodes": []}, None)
        ec2.add_response("describe_instances", {"Reservations": [{"Instances": [
            {"InstanceId": f"i-{n}", "InstanceType": "m5.large",
             "State": {"Name": "running", "Code": 16}, "PlatformDetails": "Linux/UNIX",
             "Placement": {"AvailabilityZone": "us-east-1a", "Tenancy": "default"}}
            for n in (1, 2)]}]}, None)
        sess.stub("savingsplans").add_response("describe_savings_plans", {"savingsPlans": []}, None)
        _code, doc, _ = _json(sess)
    matched = sum(c["matched_now"]["matched"] for c in doc["commitments"])
    assert matched == 2


def test_an_inventory_read_that_fails_is_not_read_and_matched_now_says_so():
    with FakeSession(regions=("us-east-1",)) as sess:
        _identity(sess)
        _regions(sess, "us-east-1")
        sess.stub("rds").add_response("describe_reserved_db_instances",
                                      {"ReservedDBInstances": []}, None)
        ec2 = sess.stub("ec2")
        ec2.add_response("describe_reserved_instances", {"ReservedInstances": [
            _ec2_ri("ri-a", "m5.large", 2, datetime(2026, 1, 1, tzinfo=UTC),
                    datetime(2027, 1, 1, tzinfo=UTC))]}, {})
        sess.stub("elasticache").add_response("describe_reserved_cache_nodes",
                                              {"ReservedCacheNodes": []}, None)
        _denied(ec2, "describe_instances", "UnauthorizedOperation")
        sess.stub("savingsplans").add_response("describe_savings_plans", {"savingsPlans": []}, None)
        code, doc, _ = _json(sess, "--fail-within", "30")
    [c] = doc["commitments"]
    assert c["matched_now"] == {"basis": "matched now", "read": False, "reason": "denied"}
    assert [n["iam_action"] for n in doc["not_read"]] == ["ec2:DescribeInstances"]
    # The commitments were all read: the gate can answer.
    assert code == 0 and doc["gate"]["cannot_check"] is False


def _ce_pages(sess, n_ri_pages=1, n_sp_pages=1, *, more=False):
    ce = sess.stub("ce")
    for i in range(n_ri_pages):
        page = {"UtilizationsByTime": [{"TimePeriod": {"Start": "2026-09-17", "End": "2026-10-17"},
                                        "Groups": [{
            "Key": "SUBSCRIPTION_ID", "Value": f"sub-{i}",
            "Attributes": {"leaseId": "lease-1", "instanceType": "db.m5.large"},
            "Utilization": {"UtilizationPercentage": "87.5", "PurchasedHours": "1440",
                            "TotalActualHours": "1260", "UnusedHours": "180"}}]}],
            **({"NextPageToken": f"p{i + 1}"} if more or i < n_ri_pages - 1 else {})}
        ce.add_response("get_reservation_utilization", page, None)
    for i in range(n_sp_pages):
        page = {"SavingsPlansUtilizationDetails": [{
            "SavingsPlanArn": f"arn:aws:savingsplans::{ACCOUNT}:savingsplan/sp-1",
            "Utilization": {"TotalCommitment": "1800", "UsedCommitment": "1710",
                            "UnusedCommitment": "90", "UtilizationPercentage": "95"}}],
            "TimePeriod": {"Start": "2026-09-17", "End": "2026-10-17"},
            **({"NextToken": f"t{i + 1}"} if more or i < n_sp_pages - 1 else {})}
        ce.add_response("get_savings_plans_utilization_details", page, None)


def test_spend_prints_its_cost_before_the_first_cost_explorer_request():
    seen: list[str] = []
    with FakeSession() as sess:
        _full_account(sess)
        _ce_pages(sess)
        err = io.StringIO()
        ce = sess.clients[("ce", "us-east-1")]
        ce.meta.events.register("before-parameter-build",
                                lambda **_: seen.append(err.getvalue()))
        out = io.StringIO()
        code = cli.run(_parse("--spend", "--json"), session=sess, now=NOW, stdout=out, stderr=err)
        sess.stub("ce").assert_no_pending_responses()
    assert code == 0
    assert seen and "Cost Explorer" in seen[0] and "$0.01" in seen[0]
    doc = json.loads(out.getvalue())
    bu = doc["billed_utilization"]
    assert bu["requests"] == 2 and bu["period"] == {"start": "2026-09-17", "end": "2026-10-17"}
    got = {c["id"]: c for c in doc["commitments"]}
    assert got["ri-db-1"]["billed_utilization"]["UtilizationPercentage"] == "87.5"
    assert got["sp-1"]["billed_utilization"]["UtilizationPercentage"] == "95"
    assert "2 Cost Explorer requests" in err.getvalue()


def test_spend_caps_its_pages():
    with FakeSession() as sess:
        _full_account(sess)
        _ce_pages(sess, cs.CE_MAX_PAGES, cs.CE_MAX_PAGES, more=True)
        code, doc, err = _json(sess, "--spend")
        sess.stub("ce").assert_no_pending_responses()
    assert code == 0
    bu = doc["billed_utilization"]
    assert bu["requests"] == 2 * cs.CE_MAX_PAGES and bu["truncated"] is True
    assert f"up to {2 * cs.CE_MAX_PAGES} Cost Explorer requests" in err


def test_spend_denied_is_not_read():
    with FakeSession() as sess:
        _full_account(sess)
        _denied(sess.stub("ce"), "get_reservation_utilization", "AccessDeniedException", 400)
        _denied(sess.stub("ce"), "get_savings_plans_utilization_details",
                "AccessDeniedException", 400)
        code, doc, _ = _json(sess, "--spend")
    assert code == 5
    assert {n["iam_action"] for n in doc["not_read"]} == {
        "ce:GetReservationUtilization", "ce:GetSavingsPlansUtilizationDetails"}


# ── output and the shape a script relies on ───────────────────────────────────

def test_json_is_one_document_and_has_no_escape_codes():
    with FakeSession() as sess:
        _full_account(sess)
        code, out, err = _run(sess, "--json")
    json.loads(out)
    assert "\x1b" not in out and "\x1b" not in err
    assert code == 0


def test_human_output_in_a_pipe_has_no_escape_codes(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    with FakeSession() as sess:
        _full_account(sess)
        _code, out, err = _run(sess)
    assert "\x1b" not in out + err


def test_dry_run_lists_calls_and_actions_without_credentials():
    class Boom:
        def __getattr__(self, name):
            raise AssertionError(f"the dry run touched the session: {name}")
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(_parse("--dry-run", "--json"), session=Boom(), now=NOW, stdout=out,
                   stderr=err)
    assert code == 0
    doc = json.loads(out.getvalue())
    assert doc["dry_run"] is True and doc["executed"] is False
    calls = {c["call"] for c in doc["calls"]}
    assert {"rds.describe_reserved_db_instances", "ec2.describe_reserved_instances",
            "elasticache.describe_reserved_cache_nodes",
            "redshift.describe_reserved_nodes", "opensearch.describe_reserved_instances",
            "memorydb.describe_reserved_nodes",
            "savingsplans.describe_savings_plans"} <= calls
    # OpenSearch once: never the es API's call as well.
    assert not [c for c in calls if c.startswith("es.")]
    assert {"redshift:DescribeReservedNodes", "es:DescribeReservedInstances",
            "memorydb:DescribeReservedNodes"} <= set(doc["iam_actions"])
    assert not [c for c in doc["calls"] if c["call"].startswith("ce.")]
    assert set(doc["iam_actions"]) >= {"rds:DescribeReservedDBInstances",
                                       "savingsplans:DescribeSavingsPlans"}
    assert all(":Describe" in a or ":Get" in a for a in doc["iam_actions"])
    out = io.StringIO()
    cli.run(_parse("--dry-run", "--spend"), session=Boom(), now=NOW, stdout=out, stderr=err)
    assert "ce.get_reservation_utilization" in out.getvalue() and "$0.01" in out.getvalue()


def test_the_human_dry_run_names_the_new_calls():
    out = io.StringIO()
    assert cli.run(_parse("--dry-run"), session=None, now=NOW, stdout=out,
                   stderr=io.StringIO()) == 0
    text = out.getvalue()
    for call, action in (("redshift.describe_reserved_nodes", "redshift:DescribeReservedNodes"),
                         ("opensearch.describe_reserved_instances",
                          "es:DescribeReservedInstances"),
                         ("memorydb.describe_reserved_nodes",
                          "memorydb:DescribeReservedNodes")):
        assert re.search(rf"{re.escape(call)}\s+{re.escape(action)}", text), text


def test_each_new_read_is_an_operation_in_botocore():
    """Each call and its paging token as botocore's model has them; botocore
    has no paginator for OpenSearch's, so its NextToken is followed by hand."""
    import botocore.session
    session = botocore.session.get_session()
    for service, op, token in (("redshift", "DescribeReservedNodes", "Marker"),
                               ("opensearch", "DescribeReservedInstances", "NextToken"),
                               ("memorydb", "DescribeReservedNodes", "NextToken")):
        model = session.get_service_model(service).operation_model(op)
        assert token in model.input_shape.members and token in model.output_shape.members
    assert not session.get_paginator_model("opensearch")._paginator_config.get(
        "DescribeReservedInstances")


def test_the_dry_run_actions_are_the_iam_template_s():
    from finops.security import iam_setup
    [group] = [g for g in iam_setup._OPTIONAL_GROUPS if g[0] == "OptionalCommitments"]
    reads = {c["iam_action"] for c in cs.dry_run_plan(spend=False)["calls"]
             if c["what"] == "commitments"}
    assert reads == set(group[3])


def test_a_bad_region_is_a_usage_error():
    with FakeSession() as sess:
        code, _out, err = _run(sess, "--regions", "Not A Region")
    assert code == 2 and "region" in err


def test_regions_given_are_read_without_listing_them():
    with FakeSession(regions=("eu-west-1",)) as sess:
        _identity(sess)
        sess.stub("rds", "eu-west-1").add_response("describe_reserved_db_instances",
                                                    {"ReservedDBInstances": []}, None)
        sess.stub("ec2", "eu-west-1").add_response("describe_reserved_instances",
                                                    {"ReservedInstances": []}, {})
        sess.stub("elasticache", "eu-west-1").add_response(
            "describe_reserved_cache_nodes", {"ReservedCacheNodes": []}, None)
        sess.stub("savingsplans").add_response("describe_savings_plans", {"savingsPlans": []}, None)
        code, doc, _ = _json(sess, "--regions", "eu-west-1")
    assert code == 0 and doc["regions"] == ["eu-west-1"]
    assert ("ec2", "us-east-1") not in sess.requested


def test_a_region_list_that_could_not_be_read_is_said():
    with FakeSession(regions=("us-east-1",)) as sess:
        _identity(sess)
        _denied(sess.stub("ec2"), "describe_regions", "UnauthorizedOperation")
        sess.stub("rds").add_response("describe_reserved_db_instances",
                                      {"ReservedDBInstances": []}, None)
        sess.stub("ec2").add_response("describe_reserved_instances", {"ReservedInstances": []}, {})
        sess.stub("elasticache").add_response("describe_reserved_cache_nodes",
                                              {"ReservedCacheNodes": []}, None)
        sess.stub("savingsplans").add_response("describe_savings_plans", {"savingsPlans": []}, None)
        code, doc, _ = _json(sess, "--fail-within", "30")
    assert code == 2
    [nr] = doc["not_read"]
    assert nr["iam_action"] == "ec2:DescribeRegions" and nr["reason"] == "denied"
    assert doc["regions"] == ["us-east-1"]


# ── the CLI as a pipeline runs it ─────────────────────────────────────────────

_DROP = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
         "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
         "FINOPS_DATA_DIR")


def _nable(tmp_path, *argv):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in _DROP and not k.startswith("NABLE_")}
    env.update(HOME=str(home), PYTHONPATH=str(ROOT / "src"), FINOPS_NO_KEYRING="1",
               FINOPS_DATA_DIR=str(tmp_path / "data"),
               AWS_CONFIG_FILE=str(home / "no-aws-config"),
               AWS_SHARED_CREDENTIALS_FILE=str(home / "no-aws-credentials"),
               AWS_EC2_METADATA_DISABLED="true", NABLE_NO_UPDATE_CHECK="1")
    return subprocess.run([sys.executable, "-m", "finops.entry", *argv], env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=120, cwd=str(home), check=False)


def test_headless_no_credentials_is_a_document_and_exit_6(tmp_path):
    r = _nable(tmp_path, "commitments", "status", "--json")
    assert r.returncode == 6, r.stderr[-800:]
    doc = json.loads(r.stdout)
    assert doc["error"]["class"] == "no-credentials"
    assert "\x1b" not in r.stdout + r.stderr


def test_headless_gate_without_credentials_cannot_check(tmp_path):
    r = _nable(tmp_path, "commitments", "status", "--fail-within", "30")
    assert r.returncode == 2, r.stderr[-800:]
    assert "no AWS credentials" in r.stdout + r.stderr


def test_headless_dry_run_needs_nothing(tmp_path):
    r = _nable(tmp_path, "commitments", "status", "--dry-run", "--json")
    assert r.returncode == 0, r.stderr[-800:]
    assert json.loads(r.stdout)["executed"] is False


def test_the_command_is_registered_with_the_cli(capsys):
    from finops import setup_wizard
    with pytest.raises(SystemExit) as e:
        setup_wizard.main(["commitments", "status", "--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--regions", "--json", "--spend", "--fail-within", "--dry-run"):
        assert flag in out


# ── the docs say what the code does ───────────────────────────────────────────

def _headless_rows() -> list[str]:
    doc = (ROOT / "docs" / "HEADLESS.md").read_text(encoding="utf-8")
    return [ln for ln in doc.splitlines()
            if ln.startswith(("| `commitments status", "| `nable commitments status"))]


def test_headless_md_has_the_scripts_row_and_the_exit_codes():
    rows = _headless_rows()
    script_rows = [r for r in rows if r.startswith("| `nable commitments status")]
    code_rows = [r for r in rows if r.startswith("| `commitments status")]
    assert script_rows and len(code_rows) == 1, rows
    assert any("6" in r.rsplit("|", 2)[-2] for r in script_rows)
    for code, meaning in cli.EXIT_CODES.items():
        if code == 0:
            continue
        assert f"{code} {meaning}" in code_rows[0], (code, meaning)


def test_the_scripts_row_exit_matches_the_code(tmp_path):
    """The row for `nable commitments status` with no credentials says 6."""
    [row] = [r for r in _headless_rows()
             if r.startswith("| `nable commitments status` |")]
    assert row.rstrip(" |").endswith(str(cli.EXIT_NO_CREDS))


def test_no_waiting_on_stdin(monkeypatch):
    """Nothing in the command reads stdin: a closed stdin cannot block it."""
    class NoStdin(io.StringIO):
        def read(self, *a):
            raise AssertionError("read stdin")
        readline = read
    monkeypatch.setattr(sys, "stdin", NoStdin())
    with FakeSession(creds=False) as sess:
        code, _doc, _ = _json(sess)
    assert code == 6


def test_days_left_round_down():
    end = NOW + timedelta(days=3, hours=23)
    assert cs.days_left(end, NOW) == 3
    assert cs.days_left(NOW - timedelta(hours=1), NOW) == -1
