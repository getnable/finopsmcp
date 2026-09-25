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


def _block(ts_epoch, msg_id, request_id, tout, tin=2, cwrite=1000, cread=40_000,
           model="claude-opus-5-5", session="sess-a"):
    # One transcript line per content block, as Claude Code writes them: every
    # block of a response repeats the response's usage, output growing as it streams.
    rec = _assistant(ts_epoch, tin=tin, tout=tout, cwrite=cwrite, cread=cread, model=model)
    rec["message"]["id"] = msg_id
    rec["requestId"] = request_id
    rec["sessionId"] = session
    return rec


def test_a_response_logged_as_several_blocks_counts_once(tmp_path):
    now = time.time()
    _write_session(tmp_path / "claude", [
        _block(now - 60, "msg_1", "req_1", tout=8),     # thinking block
        _block(now - 60, "msg_1", "req_1", tout=120),   # text block
        _block(now - 60, "msg_1", "req_1", tout=351),   # tool_use, final count
        _block(now - 30, "msg_2", "req_2", tout=40),    # a second response
    ])
    u = ab.read_agent_usage(now - 3600)
    assert u["messages"] == 2
    assert u["input_tokens"] == 4                     # 2 per response, not per line
    assert u["output_tokens"] == 351 + 40             # the last line of each response
    assert u["cache_creation_tokens"] == 2000
    assert u["cache_read_tokens"] == 80_000
    assert u["billable_tokens"] == 4 + 391 + 2000


def test_each_response_is_priced_at_its_own_models_rate(tmp_path):
    # One blended $3/$15 rate read Opus 5.5 low and Haiku 4.5 high. Same tokens,
    # three models, three prices.
    now = time.time()
    _write_session(tmp_path / "claude", [
        _assistant(now - 90, tin=1_000_000, tout=1_000_000, model="claude-opus-5-5"),
        _assistant(now - 60, tin=1_000_000, tout=1_000_000, model="claude-haiku-4-5-20251001"),
        _assistant(now - 30, tin=1_000_000, tout=1_000_000, model="claude-sonnet-4-6"),
    ])
    u = ab.read_agent_usage(now - 3600)
    assert u["cost_by_model"] == {"claude-opus-5-5": 24.0, "claude-sonnet-4-6": 18.0,
                                  "claude-haiku-4-5-20251001": 6.0}
    assert u["usd_equivalent"] == 48.0
    assert u["unpriced_models"] == {}
    assert "unpriced_note" not in u


def test_cache_reads_and_both_cache_write_durations_have_their_own_rates(tmp_path):
    now = time.time()
    rec = _assistant(now - 60, cwrite=3_000_000, cread=10_000_000, model="claude-opus-5-5")
    # Claude Code writes most of its cache with the 1-hour TTL (2x input); the
    # remainder is 5-minute (1.25x). Opus 5.5 reads at 0.05x, not 0.1x.
    rec["message"]["usage"]["cache_creation"] = {
        "ephemeral_1h_input_tokens": 2_000_000, "ephemeral_5m_input_tokens": 1_000_000}
    _write_session(tmp_path / "claude", [rec])
    u = ab.read_agent_usage(now - 3600)
    assert u["usd_equivalent"] == pytest.approx(2 * 8.00 + 1 * 5.00 + 10 * 0.20)


def test_fast_mode_responses_bill_at_the_fast_rate(tmp_path):
    now = time.time()
    rec = _assistant(now - 60, tin=1_000_000, tout=1_000_000, model="claude-opus-5")
    rec["message"]["usage"]["speed"] = "fast"
    _write_session(tmp_path / "claude", [rec])
    assert ab.read_agent_usage(now - 3600)["usd_equivalent"] == 60.0   # $10 + $50


def test_an_unknown_model_uses_the_fallback_and_is_named_unpriced(tmp_path):
    now = time.time()
    _write_session(tmp_path / "claude", [
        _assistant(now - 60, tin=1_000_000, tout=1_000_000, model="claude-nova-9"),
        _assistant(now - 30, tin=1_000_000, model="claude-haiku-4-5"),
    ])
    u = ab.read_agent_usage(now - 3600)
    assert u["unpriced_models"] == {"claude-nova-9": 2_000_000}
    assert u["cost_by_model"]["claude-nova-9"] == 18.0     # the $3/$15 fallback
    assert u["usd_equivalent"] == 19.0
    assert "claude-nova-9" in u["unpriced_note"] and "$3/$15" in u["unpriced_note"]


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
    ab.set_budget(mode="flat", plan_cost=200)         # arbitrary plan cost, any number
    st = ab.status()
    assert st["mode"] == "flat"
    assert st["verdict"] == ab.BUDGET_OK              # never OVER off a list-price estimate
    assert st["subsidy"] is not None
    assert st["subsidy"]["plan_cost_usd"] == 200.0
    assert st["subsidy"]["multiple"] and st["subsidy"]["multiple"] > 1
    assert st["cost_per_1m_effective"] is not None    # $200 spread over the tokens pulled
    # the gate also must not cry "over budget" on a subscription
    g = ab.check()
    assert g["verdict"] == ab.BUDGET_OK
    assert g["mode"] == "flat"


def test_plan_cost_infers_flat_mode(tmp_path):
    # Passing a plan cost alone is enough; mode falls out of it (matches the wizard).
    ab.set_budget(plan_cost=100)
    assert ab.get_budget()["mode"] == "flat"
    ab.reset_budget()
    ab.set_budget(spend_cap=2500)
    assert ab.get_budget()["mode"] == "metered"


def test_metered_gates_on_spend_cap(tmp_path):
    # Metered API lens: an estimated-dollar spend cap does drive the verdict, and we
    # surface a cost-per-1M rate. (For metered, the list-price estimate IS the basis
    # until an Admin key is wired for exact spend.)
    now = time.time()
    _write_session(tmp_path / "claude", [
        _assistant(now - 60, tin=2_000_000, tout=1_000_000),   # ~$ of list-price compute
    ])
    ab.set_budget(mode="metered", spend_cap=1)        # tiny cap so any usage trips it
    st = ab.status()
    assert st["mode"] == "metered"
    assert st["verdict_basis"] == "spend"
    assert st["verdict"] == ab.BUDGET_OVER
    assert st["cost_per_1m_list"] is not None
    assert st["subsidy"] is None                      # no subsidy story on metered


def test_reset_forgets_budget(tmp_path):
    ab.set_budget(mode="flat", plan_cost=100)
    assert ab.get_budget()["mode"] == "flat"
    ab.reset_budget()
    assert ab.get_budget()["mode"] == ""


def test_welcome_teaser_is_the_front_door(tmp_path):
    # The first-run banner leads with the agent's own usage when Claude Code logs
    # exist, and shows nothing (no empty block) when they don't.
    from finops import welcome
    assert welcome._agent_usage_teaser() is None       # no logs -> no teaser
    now = time.time()
    _write_session(tmp_path / "claude", [_assistant(now - 60, tin=500_000, tout=250_000)])
    line = welcome._agent_usage_teaser()
    assert line and "last 5h" in line[0] and "/hour" in line[0]


def test_no_logs_is_graceful(tmp_path):
    # No claude dir at all: empty, source_present False, no crash.
    u = ab.read_agent_usage(time.time() - 3600)
    assert u["billable_tokens"] == 0
    assert u["source_present"] is False
    assert ab.status()["verdict"] == ab.BUDGET_OK


def test_configured_budget_confirms_even_with_zero_usage(tmp_path):
    # DevEx regression: after setting a budget with no agent usage yet, the status
    # must confirm the budget, never nag you to set the budget you just set.
    # Asserted on the wording-independent core ("to set a budget") so rebranding
    # the command in the copy cannot turn this into a vacuous pass.
    ab.set_budget(mode="flat", plan_cost=100)
    st = ab.status()
    assert st["billable_tokens_mtd"] == 0
    assert "to set a budget" not in st["summary"]
    assert "$100/mo plan is set" in st["summary"]

    ab.reset_budget()
    ab.set_budget(mode="metered", spend_cap=2500)
    # metered already confirms via the spend-cap branch (never said "set a budget")
    smy = ab.status()["summary"]
    assert "$2,500 spend cap" in smy and "to set a budget" not in smy


def test_empty_state_names_a_next_step_for_non_claude_code_users(capsys):
    """The launch command's empty state must not be a dead end.

    Claude Code is the only provider nable reads without a key, so anyone on
    Cursor / Windsurf / Zed / a plain API key sees nothing but zeros. With no
    pointer to `nable connect`, that reads as "this tool does nothing" on the
    exact command the Product Hunt post tells people to run.

    Drives the real `run()` against the empty sandbox the autouse fixture
    already provides: no stubs, so it fails if the wiring changes and not just
    if the string does.
    """
    import argparse
    from finops import cli_ai_budget as cli

    cli.run(argparse.Namespace(plan_cost=None, spend_cap=None, tokens=None,
                               reset=False, json=False))
    out = capsys.readouterr().out

    assert "no Claude Code usage found" in out
    assert "nable connect openai" in out, "empty state offers no next step"
    for provider in ("anthropic", "openrouter", "litellm", "mistral"):
        assert provider in out, f"{provider} missing from the connect hint"


def test_the_fallback_cache_rates_follow_the_fallback_input_rate(tmp_path, monkeypatch):
    """Someone who sets FINOPS_AI_USD_PER_MTOK_IN to what their model costs
    should not have its cache writes and reads priced at Sonnet's rates."""
    monkeypatch.setenv("FINOPS_AI_USD_PER_MTOK_IN", "10")
    now = time.time()
    _write_session(tmp_path / "claude", [
        _assistant(now - 60, cwrite=1_000_000, cread=1_000_000, model="claude-nova-9"),
    ])
    # 1.25x input to write, 0.1x input to read: $12.50 + $1.00.
    assert ab.read_agent_usage(now - 3600)["usd_equivalent"] == 13.5
    monkeypatch.setenv("FINOPS_AI_USD_PER_MTOK_CACHE_READ", "0.5")
    assert ab.read_agent_usage(now - 3600)["usd_equivalent"] == 13.0


def test_the_unpriced_note_names_every_rate_it_used(tmp_path):
    now = time.time()
    _write_session(tmp_path / "claude", [
        _assistant(now - 60, tin=1_000_000, model="claude-nova-9"),
    ])
    note = ab.read_agent_usage(now - 3600)["unpriced_note"]
    assert "FINOPS_AI_USD_PER_MTOK_IN/OUT" in note
    assert "FINOPS_AI_USD_PER_MTOK_CACHE_WRITE" in note
    assert "FINOPS_AI_USD_PER_MTOK_CACHE_READ" in note


def test_the_headline_says_how_much_rests_on_a_fallback_rate(tmp_path):
    now = time.time()
    _write_session(tmp_path / "claude", [
        _assistant(now - 60, tin=1_000_000, tout=1_000_000, model="claude-nova-9"),  # $18
        _assistant(now - 30, tin=1_000_000, model="claude-haiku-4-5"),               # $1
    ])
    u = ab.read_agent_usage(now - 3600)
    assert u["unpriced_usd"] == 18.0
    st = ab.status()
    assert "includes ~$18.00 priced at a fallback rate" in st["summary"]
    ab.set_budget(spend_cap=100)
    assert "includes ~$18.00 priced at a fallback rate" in ab.status()["summary"]
    assert "includes ~$18.00 priced at a fallback rate" in ab.check()["reason"]


def test_no_fallback_note_when_every_model_is_priced(tmp_path):
    now = time.time()
    _write_session(tmp_path / "claude", [_assistant(now - 30, tin=1_000_000,
                                                    model="claude-haiku-4-5")])
    assert ab.read_agent_usage(now - 3600)["unpriced_usd"] == 0.0
    assert "fallback" not in ab.status()["summary"]
