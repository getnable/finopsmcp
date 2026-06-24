"""GitHub AI engineering attribution: who shipped what, sized, joined to spend."""
from __future__ import annotations

import asyncio

import pytest

from finops.connectors import github_contributions as gc


# ── attribution ──────────────────────────────────────────────────────────────
def test_claude_trailer_resolves_to_the_model():
    text = "A PR.\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
    assert gc.attribute(author_login="alice", author_is_bot=False, text=text) == "Claude Opus 4.8"


def test_model_named_trailer_beats_a_bare_tool_trailer():
    text = ("Co-authored-by: Cursor <bot@cursor.com>\n"
            "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>")
    assert gc.attribute(author_login="bob", author_is_bot=False, text=text) == "Claude Sonnet 4.6"


def test_known_ai_bot_authors_resolve_to_the_tool():
    assert gc.attribute(author_login="copilot-swe-agent[bot]", author_is_bot=True, text="") == "GitHub Copilot"
    assert gc.attribute(author_login="openai-codex[bot]", author_is_bot=True, text="") == "OpenAI Codex"
    assert gc.attribute(author_login="devin-ai-integration[bot]", author_is_bot=True, text="") == "Devin"
    assert gc.attribute(author_login="cursor[bot]", author_is_bot=True, text="") == "Cursor"


def test_generated_with_marker_resolves():
    text = "Generated with [Claude Code](https://claude.com/claude-code)"
    assert gc.attribute(author_login="x", author_is_bot=False, text=text).startswith("Claude")


def test_no_ai_signal_is_human():
    assert gc.attribute(author_login="alice", author_is_bot=False, text="just a normal PR") == "Human"


def test_unknown_bot_is_a_generic_ai_agent():
    assert gc.attribute(author_login="mystery[bot]", author_is_bot=True, text="") == "AI agent"


# ── magnitude ────────────────────────────────────────────────────────────────
def test_magnitude_thresholds():
    assert gc.magnitude(5) == "low"
    assert gc.magnitude(100) == "medium"
    assert gc.magnitude(500) == "high"


# ── aggregation ──────────────────────────────────────────────────────────────
def test_summarize_counts_by_label_and_magnitude():
    prs = [
        {"label": "Claude Opus 4.8", "magnitude": "high", "lines": 400, "title": "a", "url": "", "repo": "o/r"},
        {"label": "Claude Opus 4.8", "magnitude": "low", "lines": 10, "title": "b", "url": "", "repo": "o/r"},
        {"label": "GitHub Copilot", "magnitude": "medium", "lines": 100, "title": "c", "url": "", "repo": "o/r"},
        {"label": "Human", "magnitude": "low", "lines": 5, "title": "d", "url": "", "repo": "o/r"},
    ]
    s = gc.summarize(prs)
    assert s["total_pr_count"] == 4
    assert s["ai_pr_count"] == 3
    assert s["human_pr_count"] == 1
    assert s["ai_share_pct"] == 75.0
    opus = s["by_label"]["Claude Opus 4.8"]
    assert opus["pr_count"] == 2 and opus["high"] == 1 and opus["low"] == 1


# ── spend join ───────────────────────────────────────────────────────────────
def test_join_llm_spend_matches_model_and_computes_share_and_cost_per_pr():
    s = gc.summarize([
        {"label": "Claude Opus 4.8", "magnitude": "high", "lines": 400, "title": "a", "url": "", "repo": "o/r"},
        {"label": "Claude Opus 4.8", "magnitude": "low", "lines": 10, "title": "b", "url": "", "repo": "o/r"},
    ])
    gc.join_llm_spend(s, {"claude-opus-4-8": 49.0, "claude-sonnet-4-6": 51.0}, total_llm_spend=100.0)
    opus = s["by_label"]["Claude Opus 4.8"]
    assert opus["llm_spend_usd"] == 49.0
    assert opus["spend_share_pct"] == 49.0
    assert opus["cost_per_pr_usd"] == 24.5  # 49 / 2 PRs


def test_join_leaves_spend_none_for_a_tool_without_model_spend():
    s = gc.summarize([
        {"label": "GitHub Copilot", "magnitude": "low", "lines": 5, "title": "a", "url": "", "repo": "o/r"},
    ])
    gc.join_llm_spend(s, {"claude-opus-4-8": 49.0}, total_llm_spend=49.0)
    cop = s["by_label"]["GitHub Copilot"]
    assert cop["llm_spend_usd"] is None and cop["spend_share_pct"] is None


# ── I/O guards + report assembly ─────────────────────────────────────────────
def test_fetch_is_not_configured_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_ORGS", raising=False)
    out = asyncio.run(gc.fetch_ai_contributions(days=30))
    assert out["configured"] is False


def test_build_report_joins_fetched_prs_with_llm_spend(monkeypatch):
    async def fake_fetch(*, days, repos=None):
        return {"configured": True, "window_days": days, "prs": [
            {"label": "Claude Opus 4.8", "magnitude": "high", "lines": 400, "title": "x", "url": "", "repo": "o/r"},
        ]}

    monkeypatch.setattr(gc, "fetch_ai_contributions", fake_fetch)
    monkeypatch.setattr(
        "finops.connectors.llm_costs.get_all_llm_costs",
        lambda **kw: {"by_model": {"claude-opus-4-8": 49.0}, "total_usd": 100.0},
    )
    rep = asyncio.run(gc.build_report(days=30))
    assert rep["unit"] == "pr"
    assert rep["by_label"]["Claude Opus 4.8"]["spend_share_pct"] == 49.0


# ── commit path (teams that push straight to main, no PRs) ────────────────────
def test_fetch_ai_commits_is_not_configured_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_ORGS", raising=False)
    out = asyncio.run(gc.fetch_ai_commits(days=30))
    assert out["configured"] is False


def test_build_report_falls_back_to_commits_when_there_are_no_prs(monkeypatch):
    async def no_prs(*, days, repos=None):
        return {"configured": True, "window_days": days, "prs": []}

    async def some_commits(*, days, repos=None):
        return {"configured": True, "window_days": days, "commits": [
            {"label": "Claude Opus 4.8", "magnitude": "high", "lines": 400, "title": "c", "url": "", "repo": "o/r"},
            {"label": "Human", "magnitude": "low", "lines": 5, "title": "h", "url": "", "repo": "o/r"},
        ]}

    monkeypatch.setattr(gc, "fetch_ai_contributions", no_prs)
    monkeypatch.setattr(gc, "fetch_ai_commits", some_commits)
    monkeypatch.setattr(
        "finops.connectors.llm_costs.get_all_llm_costs",
        lambda **kw: {"by_model": {"claude-opus-4-8": 40.0}, "total_usd": 100.0},
    )
    rep = asyncio.run(gc.build_report(days=30))
    assert rep["unit"] == "commit"
    assert rep["total_pr_count"] == 2 and rep["ai_pr_count"] == 1
    assert rep["by_label"]["Claude Opus 4.8"]["cost_per_pr_usd"] == 40.0  # 40 / 1 commit


def test_unit_commit_forces_commits_even_when_prs_exist(monkeypatch):
    called = {"prs": False}

    async def prs(*, days, repos=None):
        called["prs"] = True
        return {"configured": True, "window_days": days, "prs": [
            {"label": "Human", "magnitude": "low", "lines": 1, "title": "p", "url": "", "repo": "o/r"}]}

    async def commits(*, days, repos=None):
        return {"configured": True, "window_days": days, "commits": [
            {"label": "Claude Sonnet 4.6", "magnitude": "medium", "lines": 50, "title": "c", "url": "", "repo": "o/r"}]}

    monkeypatch.setattr(gc, "fetch_ai_contributions", prs)
    monkeypatch.setattr(gc, "fetch_ai_commits", commits)
    monkeypatch.setattr(
        "finops.connectors.llm_costs.get_all_llm_costs",
        lambda **kw: {"by_model": {}, "total_usd": 0},
    )
    rep = asyncio.run(gc.build_report(days=30, unit="commit"))
    assert rep["unit"] == "commit"
    assert called["prs"] is False                      # PR path skipped entirely
    assert "Claude Sonnet 4.6" in rep["by_label"]
