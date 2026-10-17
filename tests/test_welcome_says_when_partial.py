"""The first number a new user sees says when it is only part of the bill.

get_cost_summary already marks a total `partial` when a provider could not be
read, with a warning that real spend is higher. The welcome screen printed
"Total spend $12,000" from it with nothing else, so the first figure nable ever
showed someone could be one cloud out of two, presented as the bill. The idle
line had the same gap once the idle scan began reporting unread regions.

The suggested question also said "Why is Amazon EC2 $7,000 this month" about a
figure covering the last 30 days; asked that way, the answer comes back for a
different window and a different number.
"""
from __future__ import annotations

import pytest

import finops.welcome as w


@pytest.fixture
def partial_scans(monkeypatch):
    from finops import server

    for k in ("FINOPS_DEMO", "FINOPS_DEMO_MODE", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)

    async def _no():
        return False

    monkeypatch.setattr(w, "_any_llm_configured", _no)
    monkeypatch.setattr(w, "_aws_connected", lambda: False)
    from finops.anomaly import backfill
    monkeypatch.setattr(backfill, "backfill_from_cost_explorer", lambda *a, **k: None)

    async def _summary():
        return {"grand_total_usd": 12000.0,
                "grand_by_service": {"Amazon EC2": 7000.0, "Amazon S3": 5000.0},
                "partial": True, "failed_providers": {"gcp": "billing export not found"},
                "partial_warning": ("This total covers aws only. gcp could not be read, so "
                                    "real spend is higher than the figure above.")}

    async def _idle():
        return {"total_monthly_waste_usd": 400.0, "partial": True,
                "regions_not_read": {"eu-west-1": "UnauthorizedOperation"}}

    async def _ai():
        return None

    monkeypatch.setattr(server, "get_cost_summary", _summary)
    monkeypatch.setattr(server, "list_idle_resources", _idle)
    monkeypatch.setattr(server, "optimize_ai_spend", _ai)


def test_a_partial_total_is_labelled_partial(partial_scans, capsys):
    assert w._show_value_moment(demo=False) is True
    out = capsys.readouterr().out
    assert "$12,000" in out
    line = next(ln for ln in out.splitlines() if "Total spend" in ln)
    rest = out.split(line, 1)[1].splitlines()[0:2]
    assert any("gcp" in ln.lower() and "not" in ln.lower() for ln in rest), out


def test_a_partial_idle_figure_is_labelled_partial(partial_scans, capsys):
    w._show_value_moment(demo=False)
    out = capsys.readouterr().out
    idle_line = next(ln for ln in out.splitlines() if "Idle / wasted" in ln)
    assert "partial" in idle_line.lower() or "eu-west-1" in idle_line, out


def test_the_suggested_question_names_the_window_of_the_figure():
    qs = w._suggest_questions({"Amazon EC2": 7000.0}, 7000.0, 0)
    assert "this month" not in qs[0], qs
    assert "last 30 days" in qs[0], qs
