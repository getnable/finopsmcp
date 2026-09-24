"""The org rollup reads every account, from the right account, or says it did not.

Three ways it used to under-report without saying so:
  - GetCostAndUsage pages grouped results with NextPageToken and only the first
    page was read, so a 100-account org rolled up as 60.
  - When assume-role into a member account failed, _ce_client fell back to the
    caller's own client, which filtered to the member returned nothing, and the
    account was folded into the total as $0 with no partial flag.
  - The rollup cache key had no credential identity, so switching profile
    served the previous organization's numbers for 12 hours.
"""
from __future__ import annotations

import boto3
import pytest

from finops import cache
from finops.connectors import aws_org

MGMT = "999999999999"


@pytest.fixture(autouse=True)
def _cold_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cache, "_DISK_DISABLED", True)
    monkeypatch.setattr(aws_org, "_load_account_names", lambda: {})
    monkeypatch.delenv("AWS_ROLE_ARNS", raising=False)
    cache.clear()
    yield
    cache.clear()


def _group(keys: list[str], amount: float) -> dict:
    return {"Keys": keys,
            "Metrics": {"UnblendedCost": {"Amount": str(amount), "Unit": "USD"}}}


def _page(groups: list[dict], token: str | None) -> dict:
    """ce:GetCostAndUsage response shape."""
    resp = {
        "GroupDefinitions": [],
        "ResultsByTime": [{"TimePeriod": {"Start": "2026-08-25", "End": "2026-09-24"},
                           "Total": {}, "Groups": groups, "Estimated": False}],
        "DimensionValueAttributes": [],
    }
    if token:
        resp["NextPageToken"] = token
    return resp


class _FakeCE:
    def __init__(self, owner: str, world: dict):
        self.owner, self.world = owner, world

    def get_cost_and_usage(self, **kw):
        self.world["calls"].append((self.owner, kw))
        filt = kw.get("Filter")
        if filt:
            acct = filt["Dimensions"]["Values"][0]
            if self.owner != acct:
                # A different account's CE, filtered to this one: nothing.
                return _page([], None)
            return _page([_group(["Amazon EC2"], self.world["spend"][acct])], None)
        if self.world.get("mgmt_fails"):
            raise RuntimeError("AccessDeniedException: not the management account")
        pages = self.world["mgmt_pages"]
        idx = int(kw.get("NextPageToken") or 0)
        groups, nxt = pages[idx]
        return _page(groups, nxt)


class _FakeSTS:
    def __init__(self, world: dict):
        self.world = world

    def get_caller_identity(self):
        return {"Account": MGMT, "Arn": f"arn:aws:iam::{MGMT}:user/me", "UserId": "AIDA"}

    def assume_role(self, RoleArn, RoleSessionName, DurationSeconds):
        acct = RoleArn.split(":")[4]
        if acct in self.world["deny"]:
            raise RuntimeError(f"AccessDenied: not authorized to assume {RoleArn}")
        return {"Credentials": {"AccessKeyId": f"ASIA{acct}", "SecretAccessKey": "s",
                                "SessionToken": "t", "Expiration": "2026-09-24T01:00:00Z"}}


def _install(monkeypatch, world: dict) -> None:
    world.setdefault("calls", [])

    def _client(service, region_name=None, aws_access_key_id=None, **_):
        if service == "sts":
            return _FakeSTS(world)
        if service == "ce":
            owner = aws_access_key_id[4:] if aws_access_key_id else MGMT
            return _FakeCE(owner, world)
        raise AssertionError(f"unexpected client {service}")

    monkeypatch.setattr(boto3, "client", _client)


def test_management_rollup_reads_every_page(monkeypatch):
    accounts = [f"{i:012d}" for i in range(1, 101)]
    first = [_group([a, "Amazon EC2"], 10.0) for a in accounts[:60]]
    rest = [_group([a, "Amazon EC2"], 10.0) for a in accounts[60:]]
    _install(monkeypatch, {"mgmt_pages": [(first, "1"), (rest, None)]})

    out = aws_org.org_cost_summary(days_back=30)
    assert out["account_count"] == 100
    assert out["org_total_usd"] == 1000.0


def test_anomaly_period_totals_read_every_page(monkeypatch):
    accounts = [f"{i:012d}" for i in range(1, 101)]
    first = [_group([a], 100.0) for a in accounts[:60]]
    rest = [_group([a], 100.0) for a in accounts[60:]]
    world = {"mgmt_pages": [(first, "1"), (rest, None)]}
    _install(monkeypatch, world)

    aws_org.account_anomalies(days_back=30)
    tokens = [kw.get("NextPageToken") for _, kw in world["calls"]]
    assert tokens.count("1") == 2  # one follow-up page per period


def test_failed_assume_role_is_reported_not_folded_in_as_zero(monkeypatch):
    world = {
        "mgmt_fails": True,
        "deny": {"222222222222"},
        "spend": {"111111111111": 500.0, "222222222222": 700.0},
    }
    _install(monkeypatch, world)
    monkeypatch.setattr(aws_org, "list_org_accounts", lambda sync_to_db=True: [
        {"account_id": "111111111111", "account_name": "prod"},
        {"account_id": "222222222222", "account_name": "data"},
    ])

    out = aws_org.org_cost_summary(days_back=30)

    assert out["partial"] is True
    assert [f["account_id"] for f in out["failed_accounts"]] == ["222222222222"]
    assert "AccessDenied" in out["failed_accounts"][0]["error"]
    assert [a["account_id"] for a in out["accounts"]] == ["111111111111"]
    assert out["org_total_usd"] == 500.0
    # The denied account was never read through the management account's client.
    assert not any(owner == MGMT and kw.get("Filter", {}).get("Dimensions", {})
                   .get("Values") == ["222222222222"] for owner, kw in world["calls"])


def test_partial_rollup_is_not_cached(monkeypatch):
    world = {"mgmt_fails": True, "deny": {"222222222222"},
             "spend": {"111111111111": 500.0, "222222222222": 700.0}}
    _install(monkeypatch, world)
    monkeypatch.setattr(aws_org, "list_org_accounts", lambda sync_to_db=True: [
        {"account_id": "111111111111", "account_name": "prod"},
        {"account_id": "222222222222", "account_name": "data"},
    ])
    aws_org.org_cost_summary(days_back=30)
    world["deny"].clear()
    out = aws_org.org_cost_summary(days_back=30)
    assert "partial" not in out
    assert out["org_total_usd"] == 1200.0


def test_rollup_cache_is_keyed_by_credential(monkeypatch):
    calls = {"n": 0}

    def _uncached(days_back=30, include_zero_spend=False):
        calls["n"] += 1
        return {"org_total_usd": float(calls["n"]), "accounts": [], "method": "test"}

    monkeypatch.setattr(aws_org, "_org_cost_summary_uncached", _uncached)
    monkeypatch.setenv("AWS_PROFILE", "org-a")
    a = aws_org.org_cost_summary(days_back=30)
    monkeypatch.setenv("AWS_PROFILE", "org-b")
    b = aws_org.org_cost_summary(days_back=30)

    assert calls["n"] == 2
    assert a["org_total_usd"] != b["org_total_usd"]


def test_top_spending_accounts_keeps_the_partial_flags(monkeypatch):
    from finops.connectors import aws_org
    monkeypatch.setattr(aws_org, "org_cost_summary", lambda days_back=30: {
        "accounts": [{"account_id": "a", "total_usd": 5.0}],
        "partial": True, "failed_accounts": [{"account_id": "b", "error": "AccessDenied"}],
    })
    out = aws_org.top_spending_accounts(limit=5)
    assert out["partial"] is True and out["failed_accounts"][0]["account_id"] == "b"
    assert [a["account_id"] for a in out["top_accounts"]] == ["a"]
