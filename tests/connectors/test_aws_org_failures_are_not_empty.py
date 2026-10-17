"""An organization that could not be read is not an organization with nothing in it.

connectors/aws_org.py caught its own failures and returned []: AWS
Organizations denied, Cost Explorer denied, the client not built. Each
caller then answered as if the org were empty. Reproduced with the reads
denied:

  list_org_accounts()        []  -> the tool: "No accounts found"
  account_anomalies()        []  -> the tool: total_anomalies 0, spikes 0
  ou_cost_breakdown()        []  -> the tool: {"ous": []}
  per-account rollup         "No accounts found in organization"

Each failure now raises OrgReadError, which the callers report as a failed
read. An organization that answers with no accounts is still [].
"""
from __future__ import annotations

import boto3
import pytest

from finops import cache
from finops.connectors import aws_org


@pytest.fixture(autouse=True)
def _cold(monkeypatch, tmp_path):
    from finops import demo_data, server

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    monkeypatch.setattr(demo_data, "_real_provider_connected", lambda: True)
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cache, "_DISK_DISABLED", True)
    monkeypatch.setattr(aws_org, "_load_account_names", dict)
    monkeypatch.setattr(aws_org, "_sync_accounts_to_db", lambda *a, **k: None)
    monkeypatch.setattr(server, "require_pro", lambda *a, **k: None)
    cache.clear()
    yield
    cache.clear()


class _Denied(Exception):
    def __init__(self, op):
        super().__init__(f"An error occurred (AccessDeniedException) when calling the {op} "
                         "operation: not authorized")
        self.response = {"Error": {"Code": "AccessDeniedException"}}


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self):
        for page in self._pages:
            if isinstance(page, Exception):
                raise page
            yield page


class _Org:
    def __init__(self, pages):
        self._pages = pages

    def get_paginator(self, name):
        assert name == "list_accounts"
        return _Paginator(self._pages)

    def describe_organization(self):
        return {"Organization": {"MasterAccountId": "999999999999"}}

    def list_parents(self, ChildId):
        return {"Parents": [{"Id": "ou-1", "Type": "ORGANIZATIONAL_UNIT"}]}

    def describe_organizational_unit(self, OrganizationalUnitId):
        return {"OrganizationalUnit": {"Name": "Platform"}}


def _org(monkeypatch, *pages):
    monkeypatch.setattr(aws_org, "_org_client", lambda: _Org(list(pages)))


class _DeniedCE:
    def get_cost_and_usage(self, **kw):
        raise _Denied("GetCostAndUsage")


def _ce_denied(monkeypatch):
    real = boto3.client

    def _client(service, *a, **k):
        if service == "ce":
            return _DeniedCE()
        return real(service, *a, **k)
    monkeypatch.setattr(boto3, "client", _client)


_ACCOUNTS = {"Accounts": [
    {"Id": "111111111111", "Name": "prod", "Status": "ACTIVE", "Email": "p@x"},
    {"Id": "999999999999", "Name": "mgmt", "Status": "ACTIVE", "Email": "m@x"},
]}


# ── the connector ────────────────────────────────────────────────────────────

def test_a_denied_account_listing_raises_instead_of_returning_no_accounts(monkeypatch):
    _org(monkeypatch, _Denied("ListAccounts"))
    with pytest.raises(aws_org.OrgReadError, match="AccessDenied"):
        aws_org.list_org_accounts(sync_to_db=False)


def test_a_listing_that_fails_on_a_later_page_raises(monkeypatch):
    _org(monkeypatch, _ACCOUNTS, _Denied("ListAccounts"))
    with pytest.raises(aws_org.OrgReadError):
        aws_org.list_org_accounts(sync_to_db=False)


def test_an_organizations_client_that_cannot_be_built_raises(monkeypatch):
    def _no_client():
        raise RuntimeError("Unable to locate credentials")
    monkeypatch.setattr(aws_org, "_org_client", _no_client)
    with pytest.raises(aws_org.OrgReadError, match="credentials"):
        aws_org.list_org_accounts(sync_to_db=False)


def test_an_organization_with_no_active_accounts_is_still_empty(monkeypatch):
    _org(monkeypatch, {"Accounts": [{"Id": "1", "Name": "gone", "Status": "SUSPENDED"}]})
    assert aws_org.list_org_accounts(sync_to_db=False) == []


def test_a_listing_that_answers_is_unchanged(monkeypatch):
    _org(monkeypatch, _ACCOUNTS)
    out = aws_org.list_org_accounts(sync_to_db=False)
    assert [a["account_id"] for a in out] == ["111111111111", "999999999999"]
    assert [a["is_management_account"] for a in out] == [False, True]


def test_account_anomalies_over_a_denied_cost_explorer_raises(monkeypatch):
    _ce_denied(monkeypatch)
    with pytest.raises(aws_org.OrgReadError, match="AccessDenied"):
        aws_org.account_anomalies(days_back=30)


def test_ou_breakdown_over_a_denied_listing_raises(monkeypatch):
    _org(monkeypatch, _Denied("ListAccounts"))
    with pytest.raises(aws_org.OrgReadError):
        aws_org.ou_cost_breakdown(days_back=30)


def test_ou_breakdown_over_a_rollup_that_failed_raises(monkeypatch):
    _org(monkeypatch, _ACCOUNTS)
    monkeypatch.setattr(aws_org, "org_cost_summary", lambda **k: {
        "error": "Could not read spend for any account in the organization"})
    with pytest.raises(aws_org.OrgReadError, match="Could not read spend"):
        aws_org.ou_cost_breakdown(days_back=30)


def test_the_per_account_rollup_says_the_listing_failed(monkeypatch):
    _org(monkeypatch, _Denied("ListAccounts"))
    out = aws_org._fallback_per_account_rollup("2026-09-01", "2026-10-01", 30)
    assert out["error"] != "No accounts found in organization", out
    assert "AccessDenied" in out["error"]


# ── the tools that report it ─────────────────────────────────────────────────

def _tool(name: str, **args) -> dict:
    from tests.mcp_support import call
    return call(name, args)


def test_the_list_tool_reports_a_failed_read_not_an_empty_org(monkeypatch):
    _org(monkeypatch, _Denied("ListAccounts"))
    out = _tool("list_org_accounts")
    assert "error" in out and "AccessDenied" in out["error"], out
    assert out.get("accounts") is None


def test_the_anomaly_tool_reports_a_failed_read_not_zero_anomalies(monkeypatch):
    _ce_denied(monkeypatch)
    out = _tool("get_account_anomalies", days_back=30)
    assert "error" in out and "total_anomalies" not in out, out


def test_the_ou_tool_reports_a_failed_read_not_zero_ous(monkeypatch):
    _org(monkeypatch, _Denied("ListAccounts"))
    out = _tool("get_ou_cost_breakdown", days_back=30)
    assert "error" in out and "ous" not in out, out
