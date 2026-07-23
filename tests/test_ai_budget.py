"""Tests for the local AI-agent budget meter (finops.ai_budget)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from finops import ai_budget as ab


def _write_session(claude_dir, records):
    proj = claude_dir / "projects" / "-Users-x-proj"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / "sess.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return f


def _assistant(ts_epoch, tin=0, tout=0, cwrite=0, cread=0, model="claude-sonnet-5"):
    return {
        "timestamp": datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
        .isoformat().replace("+00:00", "Z"),
        "message": {"model": model, "usage": {
            "input_tokens": tin, "output_tokens": tout,
            "cache_creation_input_tokens": cwrite, "cache_read_input_tokens": cread,
        }},
    }


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    yield


def test_billable_excludes_cache_read(tmp_path):
    now = time.time()
    _write_session(tmp_path / "claude", [
        _assistant(now - 60, tin=1000, tout=500, cwrite=2000, cread=9_000_000),
    ])
    u = ab.read_agent_usage(now - 3600)
    assert u["billable_tokens"] == 3500          # 1000 + 500 + 2000, NOT the 9M cache read
    assert u["cache_read_tokens"] == 9_000_000
    assert u["messages"] == 1


def test_window_filters_old_records(tmp_path):
    now = time.time()
    _write_session(tmp_path / "claude", [
        _assistant(now - 100, tin=10, tout=10),          # inside 1h window
        _assistant(now - 10 * 24 * 3600, tin=999, tout=999),  # 10 days ago, excluded
    ])
    u = ab.read_agent_usage(now - 3600)
    assert u["billable_tokens"] == 20                # only the recent record counted
    assert u["messages"] == 1


def test_budget_roundtrip_and_verdicts(tmp_path, monkeypatch):
    # 90 tokens billable this month, budget of 100 tokens -> WARN (>=80%)
    now = time.time()
    _write_session(tmp_path / "claude", [_assistant(now - 60, tin=60, tout=30)])
    ab.set_budget(monthly_tokens=100)
    assert ab.get_budget()["monthly_tokens"] == 100
    st = ab.status()
    assert st["billable_tokens_mtd"] == 90
    assert st["verdict"] == ab.BUDGET_WARN

    ab.set_budget(monthly_tokens=50)                 # now 90/50 -> OVER
    assert ab.status()["verdict"] == ab.BUDGET_OVER

    ab.set_budget(monthly_tokens=100000)             # plenty -> OK
    assert ab.status()["verdict"] == ab.BUDGET_OK


def test_gate_is_advice_only_and_projects_next_task(tmp_path):
    now = time.time()
    _write_session(tmp_path / "claude", [_assistant(now - 60, tin=40, tout=40)])
    ab.set_budget(monthly_tokens=100)                # 80/100 already -> WARN
    g = ab.check()
    assert g["advice_only"] is True
    assert g["verdict"] == ab.BUDGET_WARN
    # a next task that blows the token budget escalates the verdict + reason
    g2 = ab.check(estimated_next_tokens=1000)
    assert g2["verdict"] in (ab.BUDGET_WARN, ab.BUDGET_OVER)
    assert "token budget" in g2["reason"]


def test_subscription_never_over_on_dollar_estimate(tmp_path):
    # A Max user pulls far more compute than their flat fee. That must NOT read as
    # "over budget" — the flat fee is what they actually pay. This is the bug fix.
    now = time.time()
    _write_session(tmp_path / "claude", [
        _assistant(now - 60, tin=1_000_000, tout=500_000, cwrite=100_000_000),
    ])
    ab.set_budget(monthly_usd=200, plan="claude-max-20x")
    st = ab.status()
    assert st["plan_kind"] == "subscription"
    assert st["verdict"] == ab.BUDGET_OK              # never OVER off a list-price estimate
    assert st["subsidy"] is not None
    assert st["subsidy"]["plan_price_usd"] == 200.0
    assert st["subsidy"]["multiple"] and st["subsidy"]["multiple"] > 1
    # the gate also must not cry "over budget" on a subscription
    g = ab.check()
    assert g["verdict"] == ab.BUDGET_OK
    assert g["plan_kind"] == "subscription"


def test_no_logs_is_graceful(tmp_path):
    # No claude dir at all: empty, source_present False, no crash.
    u = ab.read_agent_usage(time.time() - 3600)
    assert u["billable_tokens"] == 0
    assert u["source_present"] is False
    assert ab.status()["verdict"] == ab.BUDGET_OK
