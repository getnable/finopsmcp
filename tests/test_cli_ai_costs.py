# SPDX-License-Identifier: Apache-2.0
"""`nable ai-costs --by team`: AI spend per group from the terminal.

The CLI view of get_ai_cost_attribution. It has to carry the same honesty as
the tool: a provider that was not read is printed as not read, never as a
$0.00 line, and a provider without the field says so.
"""
from __future__ import annotations

import json

import pytest

from finops import cli_ai_costs, setup_wizard
from finops.connectors import ai_attribution

READ = {
    "dimension": "team", "period": "2026-08-26 to 2026-09-25",
    "groups": [
        {"group": "Search", "id": "t1", "provider": "litellm", "source": "api", "cost_usd": 15.0},
        {"group": "(no team)", "id": None, "provider": "litellm", "source": "api",
         "cost_usd": 1.5},
    ],
    "by_provider": {"litellm": {"source": "api", "total_usd": 16.5, "group_count": 2}},
    "total_usd": 16.5,
    "not_available": {"openai": "Not available from openai: OpenAI has no team field."},
    "failed_providers": {"langfuse": "HTTP 503"},
    "partial": True,
    "note": "Not read: langfuse. Their spend is missing from these groups, not zero.",
    "not_covered": ai_attribution.NOT_COVERED,
}


@pytest.fixture
def stub(monkeypatch):
    seen = {}

    def fake(dimension, provider=None, days=30, start_date=None, end_date=None):
        seen.update(dimension=dimension, provider=provider, days=days)
        return seen.get("answer", READ)
    monkeypatch.setattr(ai_attribution, "get_ai_cost_attribution", fake)
    return seen


def test_json_is_the_attribution_result(stub, capsys):
    rc = setup_wizard_main(["ai-costs", "--by", "team", "--json", "--days", "7"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["groups"][0]["group"] == "Search"
    assert stub == {"dimension": "team", "provider": None, "days": 7}


def test_table_names_every_group_and_never_zeroes_an_unread_provider(stub, capsys):
    rc = setup_wizard_main(["ai-costs", "--by", "team"])
    text = capsys.readouterr().out
    assert rc == 0
    assert "Search" in text and "$15.00" in text and "litellm" in text
    assert "Not available from openai" in text
    assert "Not read: langfuse (HTTP 503)" in text
    assert "langfuse" not in [ln.split()[-2] for ln in text.splitlines()
                              if ln.strip().endswith("$0.00")]
    assert "$0.00" not in text
    assert "partial" in text.lower()


def test_provider_and_alias_pass_through(stub, capsys):
    setup_wizard_main(["ai-costs", "--by", "customer", "--provider", "litellm", "--json"])
    assert stub["dimension"] == "customer"
    assert stub["provider"] == "litellm"


def test_nothing_read_exits_nonzero_with_the_reason(stub, capsys):
    stub["answer"] = {"dimension": "team", "groups": [], "by_provider": {},
                      "total_usd": None, "error": "No AI provider is connected, so ..."}
    rc = setup_wizard_main(["ai-costs", "--by", "team"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "No AI provider is connected" in captured.err


def test_withheld_total_is_explained_not_printed_as_a_sum(stub, capsys):
    stub["answer"] = {**READ, "total_usd": None, "failed_providers": {}, "partial": False,
                      "note": "LiteLLM and Langfuse record calls that OpenAI and Anthropic "
                              "also bill."}
    setup_wizard_main(["ai-costs", "--by", "team"])
    text = capsys.readouterr().out
    assert "Total" not in text
    assert "also bill" in text


def test_ai_costs_is_listed_with_the_answer_commands(capsys):
    setup_wizard_main(["--help"])
    text = capsys.readouterr().out.lower()
    answers = text[text.index("get answers"):text.index("start here")]
    assert "ai-costs" in answers


def test_unknown_dimension_is_an_argparse_error(stub, capsys):
    with pytest.raises(SystemExit) as e:
        setup_wizard.main(["ai-costs", "--by", "region"])
    assert e.value.code == 2


@pytest.mark.parametrize("days", ["0", "-3", "seven"])
def test_days_below_one_is_an_argparse_error_not_thirty(stub, capsys, days):
    """--days 0 used to become 30 through `or 30`."""
    with pytest.raises(SystemExit) as e:
        setup_wizard.main(["ai-costs", "--days", days])
    assert e.value.code == 2 and "days" not in stub


def test_the_header_names_the_window_that_was_read(stub, capsys):
    stub["answer"] = {**READ, "period": "2026-09-25 to 2026-09-25", "days": 1}
    setup_wizard_main(["ai-costs", "--by", "team", "--days", "1"])
    assert "last 1 day (2026-09-25 to 2026-09-25)" in capsys.readouterr().out


# helpers ------------------------------------------------------------------

def setup_wizard_main(argv):
    with pytest.raises(SystemExit) as e:
        setup_wizard.main(argv)
    return e.value.code


def test_module_exposes_the_parser_hooks():
    assert callable(cli_ai_costs.add_parser) and callable(cli_ai_costs.run)
