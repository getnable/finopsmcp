"""A named account that does not exist must not quietly become the default one.

`get_account(account) or get_default_account()` turned a typo into the default
account's spend, reported as if it were the account the user asked about. For
a cost tool that is the worst kind of wrong answer: a plausible number that
belongs to somebody else. These tests drive the real tools with a fake
connector standing where Cost Explorer sits.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

import finops.accounts as accounts
from finops.connectors.base import CostEntry, CostSummary

_YAML = """\
default_account: prod
accounts:
  - name: prod
    account_id: "111111111111"
  - name: staging
    account_id: "222222222222"
"""


@pytest.fixture
def two_accounts(tmp_path, monkeypatch):
    path = tmp_path / "accounts.yaml"
    path.write_text(_YAML)
    monkeypatch.setattr(accounts, "_ACCOUNTS_FILE", path)
    monkeypatch.setenv("FINOPS_CACHE_DISABLED", "1")
    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    monkeypatch.delenv("FINOPS_DEMO_FORCE", raising=False)

    import finops.cache as cache_mod
    import finops.connectors.aws as aws_mod
    import finops.server as srv

    monkeypatch.setattr(cache_mod, "_DISABLED", True, raising=False)
    monkeypatch.setattr(accounts, "get_boto3_session", lambda acct: acct.name)

    class _FakeAWS:
        """Bills whichever account its session names: prod $9,000, staging $40."""

        def __init__(self, session=None):
            self.session = session

        async def is_configured(self):
            return True

        def cache_identity(self):
            return f"fake:{self.session}"

        async def get_costs(self, start, end, granularity="MONTHLY"):
            amt = {"prod": 9_000.0, "staging": 40.0}[self.session]
            return CostSummary(
                provider="aws", start_date=start, end_date=end, total_usd=amt,
                by_service={"EC2": amt}, by_account={self.session: amt}, by_region={},
                entries=[CostEntry(provider="aws", account_id=self.session,
                                   account_name=self.session, service="EC2",
                                   region="", amount=amt)])

    monkeypatch.setattr(aws_mod, "AWSConnector", _FakeAWS)

    async def _no_credit(*a, **kw):
        return None

    monkeypatch.setattr(srv, "_credit_context", _no_credit)
    monkeypatch.setattr(srv, "_team_nudge", lambda *a, **kw: None)
    return srv


def test_resolve_named_account_lists_valid_names(two_accounts):
    acct, err = accounts.resolve_named_account("prodd")
    assert acct is None
    assert err["valid_accounts"] == ["prod", "staging"]
    assert "prodd" in err["error"]
    acct, err = accounts.resolve_named_account("staging")
    assert (acct.name, err) == ("staging", None)


def test_cost_summary_refuses_a_misspelled_account(two_accounts):
    out = asyncio.run(two_accounts.get_cost_summary(account="stagingg"))
    assert "grand_total_usd" not in out, "a typo was answered with the default account's spend"
    assert out["valid_accounts"] == ["prod", "staging"]


def test_cost_summary_names_the_account_it_answered_for(two_accounts):
    out = asyncio.run(two_accounts.get_cost_summary(account="staging"))
    assert out["grand_total_usd"] == 40.0
    assert out["account"] == "staging"


def test_costs_by_service_refuses_a_misspelled_account(two_accounts):
    out = asyncio.run(two_accounts.get_costs_by_service(account="stagingg"))
    assert "total_usd" not in out, "a typo was answered with the default account's spend"
    assert out["valid_accounts"] == ["prod", "staging"]


def test_costs_by_service_names_the_account_it_answered_for(two_accounts):
    out = asyncio.run(two_accounts.get_costs_by_service(account="staging"))
    assert out["total_usd"] == 40.0
    assert out["account"] == "staging"
