"""The org rollup and the account-anomaly period totals are read-through cached.

Before this, org_cost_summary re-hit Cost Explorer on every call, and
top_spending_accounts (which delegates to it) plus the weekly digest each fired
another full CE query. Cost Explorer bills per request, so an agent asking for the
org summary and then the top spenders paid twice for identical data. These lock
the cache-once behavior on the reporting path.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from finops import cache
from finops.connectors import aws_org


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch, tmp_path):
    # Isolate the on-disk cache under tmp and start every test cold.
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cache, "_DISK_DISABLED", True)  # memory-only, deterministic
    cache.clear()
    yield
    cache.clear()


def test_org_cost_summary_is_cached_and_reused():
    calls = {"n": 0}

    def _uncached(days_back=30, include_zero_spend=False):
        calls["n"] += 1
        return {"org_total_usd": 1234.0, "account_count": 2, "accounts": [], "method": "test"}

    with patch.object(aws_org, "_org_cost_summary_uncached", side_effect=_uncached):
        a = aws_org.org_cost_summary(days_back=30)
        b = aws_org.org_cost_summary(days_back=30)

    assert a == b
    assert calls["n"] == 1  # second call served from cache, no re-fetch


def test_top_spending_accounts_reuses_the_org_summary_fetch():
    calls = {"n": 0}

    def _uncached(days_back=30, include_zero_spend=False):
        calls["n"] += 1
        return {
            "org_total_usd": 300.0,
            "account_count": 3,
            "accounts": [
                {"account_id": "a", "total_usd": 200.0},
                {"account_id": "b", "total_usd": 70.0},
                {"account_id": "c", "total_usd": 30.0},
            ],
            "method": "test",
        }

    with patch.object(aws_org, "_org_cost_summary_uncached", side_effect=_uncached):
        aws_org.org_cost_summary(days_back=30)          # primes the cache
        top = aws_org.top_spending_accounts(limit=2, days_back=30)  # should hit cache

    assert [x["account_id"] for x in top] == ["a", "b"]
    assert calls["n"] == 1  # top_spending_accounts did not trigger a second CE query


def test_different_params_are_cached_separately():
    calls = {"n": 0}

    def _uncached(days_back=30, include_zero_spend=False):
        calls["n"] += 1
        return {"org_total_usd": float(days_back), "accounts": [], "method": "test"}

    with patch.object(aws_org, "_org_cost_summary_uncached", side_effect=_uncached):
        aws_org.org_cost_summary(days_back=30)
        aws_org.org_cost_summary(days_back=7)   # different window -> separate fetch
        aws_org.org_cost_summary(days_back=30)  # back to first -> cached

    assert calls["n"] == 2


def test_error_payloads_are_not_cached():
    seq = iter([
        {"error": "throttled", "org_total_usd": 0},
        {"org_total_usd": 500.0, "accounts": [], "method": "test"},
    ])

    with patch.object(aws_org, "_org_cost_summary_uncached", side_effect=lambda *a, **k: next(seq)):
        first = aws_org.org_cost_summary(days_back=30)
        second = aws_org.org_cost_summary(days_back=30)

    assert "error" in first          # transient failure, not cached
    assert second["org_total_usd"] == 500.0  # retry fetched fresh and succeeded


def test_cached_result_is_isolated_from_mutation():
    def _uncached(days_back=30, include_zero_spend=False):
        return {"org_total_usd": 10.0, "accounts": [{"account_id": "a", "total_usd": 10.0}]}

    with patch.object(aws_org, "_org_cost_summary_uncached", side_effect=_uncached):
        a = aws_org.org_cost_summary(days_back=30)
        a["accounts"].append({"account_id": "poison"})   # mutate the returned copy
        b = aws_org.org_cost_summary(days_back=30)

    assert len(b["accounts"]) == 1  # cache handed back a clean deep copy, not the mutated one
