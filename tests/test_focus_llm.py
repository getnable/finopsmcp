"""FOCUS normalization for LLM / AI providers.

The AI spend narrative is the moat: every LLM connector returns the same normalized
dict (by_model + by_model_tokens), and one translator maps it to FOCUS 2.0 records
(one per model, "AI and Machine Learning", ResourceType "Model") with token counts
preserved as Tags. get_all_llm_costs_as_focus rolls this across every provider.
"""
from datetime import date

from finops.focus.schema import FocusRecord, SERVICE_CATEGORIES
from finops.focus.translators.llm import llm_result_to_focus

_START = date(2026, 6, 1)
_END = date(2026, 6, 30)


def _result():
    return {
        "total_usd": 1234.5,
        "by_model": {"gpt-4o": 1000.0, "gpt-4o-mini": 234.5},
        "by_model_tokens": {
            "gpt-4o": {"input_tokens": 5_000_000, "output_tokens": 1_200_000,
                       "request_count": 4200},
            "gpt-4o-mini": {"input_tokens": 9_000_000, "output_tokens": 800_000,
                            "request_count": 15000},
        },
        "source": "api",
    }


def test_llm_result_maps_per_model():
    recs = llm_result_to_focus(_result(), provider="OpenAI", start_date=_START, end_date=_END)
    assert len(recs) == 2
    assert all(isinstance(r, FocusRecord) for r in recs)
    assert all(r.ProviderName == "OpenAI" and r.PublisherName == "OpenAI" for r in recs)
    assert all(r.ServiceCategory == "AI and Machine Learning" for r in recs)
    assert all(r.ServiceCategory in SERVICE_CATEGORIES for r in recs)
    assert all(r.ResourceType == "Model" for r in recs)

    big = next(r for r in recs if r.ServiceName == "gpt-4o")
    assert big.BilledCost == 1000.0
    assert big.Tags.get("input_tokens") == "5000000"
    assert big.Tags.get("request_count") == "4200"


def test_llm_tokens_without_cost_are_kept():
    # A gateway can report usage but no dollars; the model must still appear.
    res = {
        "by_model": {},
        "by_model_tokens": {"llama-3-70b": {"input_tokens": 100, "output_tokens": 50}},
        "source": "limited",
    }
    recs = llm_result_to_focus(res, provider="OpenRouter", start_date=_START, end_date=_END)
    assert len(recs) == 1
    assert recs[0].ServiceName == "llama-3-70b"
    assert recs[0].BilledCost == 0.0
    assert recs[0].Tags.get("input_tokens") == "100"


def test_llm_publisher_override_and_empty():
    recs = llm_result_to_focus(
        {"by_model": {"claude-3-5-sonnet": 42.0}, "by_model_tokens": {}},
        provider="AWS Bedrock", publisher="Anthropic", start_date=_START, end_date=_END,
    )
    assert recs[0].ProviderName == "AWS Bedrock" and recs[0].PublisherName == "Anthropic"
    assert llm_result_to_focus({}, provider="OpenAI", start_date=_START, end_date=_END) == []
    assert llm_result_to_focus(None, provider="OpenAI", start_date=_START, end_date=_END) == []


def _fake_all():
    return {
        "provider_results": {
            "openai": {"by_model": {"gpt-4o": 10.0}, "by_model_tokens": {}},
            "openrouter": {"by_model": {"llama-3": 5.0}, "by_model_tokens": {}},
            "bedrock": {"by_model": {"claude-3-5-sonnet": 20.0}, "by_model_tokens": {}},
            "vertex": {"by_model": {"gemini-1.5-pro": 8.0}, "by_model_tokens": {}},
        }
    }


def test_get_all_llm_costs_as_focus_rolls_providers(monkeypatch):
    import finops.connectors.llm_costs as llm

    monkeypatch.setattr(llm, "get_all_llm_costs", lambda *a, **k: _fake_all())
    recs = llm.get_all_llm_costs_as_focus(_START, _END)
    providers = {r.ProviderName for r in recs}
    assert providers == {"OpenAI", "OpenRouter", "AWS Bedrock", "Google Vertex AI"}
    assert all(r.ServiceCategory == "AI and Machine Learning" for r in recs)


def test_get_all_llm_costs_as_focus_excludes_cloud_native(monkeypatch):
    # Bedrock/Vertex already appear in AWS/GCP FOCUS; exclude to avoid double count.
    import finops.connectors.llm_costs as llm

    monkeypatch.setattr(llm, "get_all_llm_costs", lambda *a, **k: _fake_all())
    recs = llm.get_all_llm_costs_as_focus(_START, _END, exclude_cloud_native=True)
    providers = {r.ProviderName for r in recs}
    assert providers == {"OpenAI", "OpenRouter"}
    assert "AWS Bedrock" not in providers and "Google Vertex AI" not in providers
