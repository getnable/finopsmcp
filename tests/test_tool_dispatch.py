"""Regression tests for bugs that shipped in 0.8.51 and were invisible to the
existing suite because those tests call tool functions directly, never through
the MCP dispatch wrapper (_instrumented_tool) that the live server uses.

Covers:
  1. Sync @mcp.tool() functions (whoami, *_api_key) broke under dispatch because
     the wrapper did `await fn(...)` unconditionally -> "object dict can't be
     used in 'await' expression".
  2. explain_recent_cost_drivers unpacked _gather_costs()[0] (a float total)
     and called .keys() on it -> "'float' object has no attribute 'keys'".
  3. get_llm_costs reported $0 for Bedrock because Cost Explorer now labels
     model spend under SKU service names like "Claude Sonnet 4.5 (Amazon
     Bedrock Edition)", which the hardcoded SERVICE=="Amazon Bedrock" filter
     missed.
"""
import asyncio
import json
from datetime import date

from finops import server


def _payload(res):
    """call_tool returns content; pull the JSON dict out of it."""
    if isinstance(res, tuple):
        res = res[0]
    if isinstance(res, list):
        res = res[0]
    txt = getattr(res, "text", None)
    return json.loads(txt) if txt else res


def test_sync_tool_dispatches_without_await_error():
    # whoami is a sync `def`; dispatching it through the wrapper must not raise.
    res = asyncio.run(server.mcp.call_tool("whoami", {}))
    d = _payload(res)
    assert isinstance(d, dict)
    assert d.get("mode")  # real result, not an await error
    assert "await" not in str(d.get("error", ""))


def test_explain_recent_cost_drivers_diffs_service_breakdown(monkeypatch):
    calls = {"n": 0}

    async def fake_active(*a, **k):
        return ["aws"]

    async def fake_gather(*a, **k):
        # (grand_total, by_provider, grand_by_service)
        calls["n"] += 1
        if calls["n"] == 1:  # current period
            return (5000.0, {}, {"Amazon Textract": 3000.0, "Amazon EC2": 2000.0})
        return (4000.0, {}, {"Amazon Textract": 2000.0, "Amazon EC2": 2000.0})  # prior

    monkeypatch.setattr(server, "_active", fake_active)
    monkeypatch.setattr(server, "_gather_costs", fake_gather)

    d = asyncio.run(server.explain_recent_cost_drivers(days=30))
    assert "error" not in d, d
    assert d["total_current_usd"] == 5000.0
    assert d["total_previous_usd"] == 4000.0
    # Textract (+$1000) is the driver; EC2 is flat and should not appear.
    assert d["top_increases"][0]["key"] == "Amazon Textract"
    assert all(x["key"] != "Amazon EC2" for x in d["top_increases"])


def test_bedrock_costs_captures_sku_named_services(monkeypatch):
    from finops.connectors import llm_costs

    class FakeCE:
        def get_cost_and_usage(self, **kw):
            group_keys = [g["Key"] for g in kw.get("GroupBy", [])]
            if group_keys == ["SERVICE"]:  # discovery pass
                return {"ResultsByTime": [{"Groups": [
                    {"Keys": ["Claude Sonnet 4.5 (Amazon Bedrock Edition)"],
                     "Metrics": {"UnblendedCost": {"Amount": "3473.66"}}},
                    {"Keys": ["Amazon Textract"],
                     "Metrics": {"UnblendedCost": {"Amount": "5327.88"}}},
                ]}]}
            # detail pass: should be filtered to the bedrock service only
            vals = kw["Filter"]["Dimensions"]["Values"]
            assert vals == ["Claude Sonnet 4.5 (Amazon Bedrock Edition)"]
            return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-05-07"}, "Groups": [
                {"Keys": ["Claude Sonnet 4.5 (Amazon Bedrock Edition)", "USE1-InputTokens"],
                 "Metrics": {"UnblendedCost": {"Amount": "3473.66"}}},
            ]}]}

    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeCE())

    out = llm_costs.get_bedrock_costs(date(2026, 5, 7), date(2026, 6, 6))
    assert out["total_usd"] == 3473.66
    # SKU service name becomes the model label; non-bedrock service excluded.
    assert "Claude Sonnet 4.5" in out["by_model"]
    assert "Textract" not in json.dumps(out["by_model"])


def _record_events(monkeypatch):
    """Capture telemetry event names fired synchronously through the dispatch
    wrapper. first_cost_query_success is sent inline (not threaded), so it lands
    deterministically; threaded tool_called pings are irrelevant to these asserts."""
    seen = []
    monkeypatch.setattr(
        server._telemetry, "_send_event",
        lambda install_id, event, properties=None: seen.append(event),
    )
    return seen


def test_first_cost_query_success_not_fired_in_demo_mode(monkeypatch):
    # Demo cost responses are non-error dicts. Without the is_demo() guard in the
    # dispatch wrapper, the activation metric would count people who only ever saw
    # the demo dataset, never their own real cost number.
    import finops.demo_data as demo_data
    monkeypatch.setattr(demo_data, "DEMO_MODE", True)
    server._first_cost_query_fired = False
    seen = _record_events(monkeypatch)

    res = asyncio.run(server.mcp.call_tool("get_cost_summary", {}))
    d = _payload(res)
    assert isinstance(d, dict) and "error" not in d  # demo returned real-looking data
    assert "first_cost_query_success" not in seen


def test_first_cost_query_success_fires_on_real_data(monkeypatch):
    # Real (non-demo) cost answer must fire the activation event exactly once.
    import finops.demo_data as demo_data
    monkeypatch.setattr(demo_data, "DEMO_MODE", False)

    async def fake_active(pool):
        return {"aws": object()}

    async def fake_gather(targets, sd, ed, granularity):
        return (1234.0, {"aws": {"total": 1234.0}}, {"Amazon EC2": 1234.0})

    monkeypatch.setattr(server, "_active", fake_active)
    monkeypatch.setattr(server, "_gather_costs", fake_gather)
    server._first_cost_query_fired = False
    seen = _record_events(monkeypatch)

    res = asyncio.run(server.mcp.call_tool("get_cost_summary", {}))
    d = _payload(res)
    assert "error" not in d, d
    assert d["grand_total_usd"] == 1234.0
    assert seen.count("first_cost_query_success") == 1
