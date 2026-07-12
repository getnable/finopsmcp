"""
Tests for the finops serve web dashboard.

Tests server startup, /health, /api/data, dashboard HTML structure,
and port-conflict handling — without requiring live cloud credentials.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from http.server import HTTPServer
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _free_port() -> int:
    """Return an available port on localhost."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode()


def _post(url: str, payload: dict | None = None, raw: bytes | None = None,
          timeout: float = 5.0) -> tuple[int, str]:
    import urllib.error
    data = raw if raw is not None else json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def mock_dashboard_data():
    """Mock out _fetch_dashboard_data so tests never call AWS."""
    data = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "account_id": "123456789012",
        "total_spend_mtd": 1234.56,
        "total_spend_last_month": 1000.00,
        "projected_month_total": 1543.20,
        "delta_pct": 23.5,
        "finops_grade": "B",
        "finops_score": 82.0,
        "top_services": [
            {"service": "Amazon EC2", "amount": 800.0, "pct": 64.8},
            {"service": "Amazon S3", "amount": 200.0, "pct": 16.2},
        ],
        "opportunities_count": 3,
        "opportunities_total_saving": 420.0,
        "savings_achieved_mtd": 150.0,
        "anomalies_open": 1,
        "budget_pct_used": 62.0,
        "recent_opportunities": [
            {
                "description": "Downsize m5.2xlarge",
                "monthly_saving": 200.0,
                "resource": "i-abc123",
                "effort": "LOW",
                "impact": "high",
                "service": "Amazon EC2",
            },
        ],
        "recent_savings": [],
        "error": None,
        "connected_providers": ["aws"],
        "trend": [
            {"month": "March", "actual": 10200.0, "projected": None},
            {"month": "April", "actual": 11800.0, "projected": None},
            {"month": "May (partial)", "actual": 13703.0, "projected": None},
            {"month": "May (projected)", "actual": None, "projected": 15742.0},
        ],
        "scorecard": {
            "overall_grade": "B",
            "overall_score": 82.0,
            "dimensions": [
                {"name": "Waste Reduction", "score": 100, "grade": "A"},
                {"name": "Anomaly Response", "score": 80, "grade": "B"},
                {"name": "Compute Efficiency", "score": 50, "grade": "C"},
                {"name": "Commitment Coverage", "score": 0, "grade": "F"},
                {"name": "Tag Hygiene", "score": 0, "grade": "F"},
            ],
        },
    }
    with patch(
        "finops.server_web._fetch_dashboard_data",
        new=AsyncMock(return_value=data),
    ):
        yield data


@pytest.fixture()
def running_server(mock_dashboard_data):
    """Start the server on a free port, yield (server, port), then shut down."""
    import finops.server_web as server_web
    from finops.server_web import _make_server

    # Dashboard auth is on by default; disable it for the in-process test server
    # so requests don't 401. (Production behavior is covered by auth-specific tests.)
    _auth_was = server_web._AUTH_DISABLED
    server_web._AUTH_DISABLED = True

    port = _free_port()
    server = _make_server("127.0.0.1", port)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    time.sleep(0.1)
    yield server, port
    server.shutdown()
    server_web._AUTH_DISABLED = _auth_was


# ── Tests: health check ───────────────────────────────────────────────────────

def test_health_returns_ok(running_server):
    _, port = running_server
    status, body = _get(f"http://127.0.0.1:{port}/health")
    assert status == 200
    data = json.loads(body)
    assert data["status"] == "ok"


# ── Tests: /api/data ──────────────────────────────────────────────────────────

def test_api_data_returns_valid_json(running_server):
    _, port = running_server
    status, body = _get(f"http://127.0.0.1:{port}/api/data")
    assert status == 200
    data = json.loads(body)
    # Required top-level keys
    for key in (
        "generated_at",
        "total_spend_mtd",
        "top_services",
        "opportunities_count",
        "savings_achieved_mtd",
        "anomalies_open",
    ):
        assert key in data, f"Missing key: {key}"


def test_api_data_values(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/api/data")
    data = json.loads(body)
    assert data["total_spend_mtd"] == 1234.56
    assert data["delta_pct"] == 23.5
    assert data["opportunities_count"] == 3
    assert len(data["top_services"]) == 2
    assert data["top_services"][0]["service"] == "Amazon EC2"


# ── Tests: dashboard HTML ─────────────────────────────────────────────────────

def test_dashboard_html_served(running_server):
    _, port = running_server
    status, body = _get(f"http://127.0.0.1:{port}/")
    assert status == 200
    assert "text/html" in body or "<!doctype" in body.lower()


def test_dashboard_html_structure(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/")
    assert "nable" in body
    assert "Spend" in body
    assert "getnable.com" in body  # footer link
    assert "'Geist'" in body  # correct font (DESIGN.md: Geist Sans, retired Bricolage 2026-07-02)


def test_dashboard_auto_refresh_script(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/")
    # The page should auto-refresh every 60 seconds
    assert "60000" in body


def test_dashboard_has_chartjs(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/")
    # Chart.js is self-hosted (no CDN dependency) and rendered into <canvas> elements.
    assert "chart.min.js" in body and "<canvas" in body


def test_dashboard_has_spend_by_service(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/")
    assert "Spend by service" in body


def test_dashboard_has_trend_chart(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/")
    assert "Spend over time" in body


def test_dashboard_has_efficiency_scorecard(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/")
    assert "Efficiency scorecard" in body


def test_dashboard_has_savings_opportunities(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/")
    assert "Savings opportunities" in body


def test_api_data_has_trend_key(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/api/data")
    data = json.loads(body)
    assert "trend" in data
    assert isinstance(data["trend"], list)
    assert len(data["trend"]) > 0
    # Each entry has month and actual/projected
    first = data["trend"][0]
    assert "month" in first
    assert "actual" in first or "projected" in first


def test_api_data_has_scorecard_key(running_server):
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/api/data")
    data = json.loads(body)
    assert "scorecard" in data
    sc = data["scorecard"]
    assert "overall_grade" in sc
    assert "overall_score" in sc
    assert "dimensions" in sc
    assert isinstance(sc["dimensions"], list)


# ── Tests: 404 ───────────────────────────────────────────────────────────────

def test_unknown_path_returns_404(running_server):
    _, port = running_server
    try:
        _get(f"http://127.0.0.1:{port}/nonexistent")
        assert False, "Expected HTTP error"
    except urllib.error.HTTPError as e:
        assert e.code == 404


# ── Tests: port conflict handling ─────────────────────────────────────────────

def test_port_conflict_uses_next_port():
    """If the requested port is taken, _make_server should bind to the next one."""
    from finops.server_web import _make_server

    # Occupy a port by actively LISTENING on it. A bound-but-not-listening
    # socket with SO_REUSEADDR does NOT block a second bind on Linux (it does
    # on macOS), which made this test pass locally but fail in CI. An active
    # listener reliably triggers EADDRINUSE on the next bind on both platforms.
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocked_port = blocker.getsockname()[1]
    blocker.listen(1)

    try:
        server = _make_server("127.0.0.1", blocked_port)
        actual_port = server.server_address[1]
        server.server_close()
        # Should have moved to a different port
        assert actual_port != blocked_port
    finally:
        blocker.close()


# ── Tests: data fetcher returns error flag when no providers configured ────────

@pytest.mark.asyncio
async def test_fetch_data_no_providers():
    """When no connectors are configured, the data dict should have an error flag."""
    # Connectors are lazily imported inside _fetch_dashboard_data, so patch at source
    mock_instance = MagicMock()
    mock_instance.is_configured = AsyncMock(return_value=False)
    mock_cls = MagicMock(return_value=mock_instance)

    with patch("finops.connectors.aws.AWSConnector", mock_cls), \
         patch("finops.connectors.azure.AzureConnector", mock_cls), \
         patch("finops.connectors.gcp.GCPConnector", mock_cls):

        # Patch the connector classes as they're imported inside _fetch_dashboard_data
        import finops.connectors.aws as _aws_mod
        import finops.connectors.azure as _azure_mod
        import finops.connectors.gcp as _gcp_mod
        _orig_aws = _aws_mod.AWSConnector
        _orig_azure = _azure_mod.AzureConnector
        _orig_gcp = _gcp_mod.GCPConnector
        try:
            _aws_mod.AWSConnector = mock_cls  # type: ignore[assignment]
            _azure_mod.AzureConnector = mock_cls  # type: ignore[assignment]
            _gcp_mod.GCPConnector = mock_cls  # type: ignore[assignment]

            from finops.server_web import _fetch_dashboard_data
            result = await _fetch_dashboard_data()
        finally:
            _aws_mod.AWSConnector = _orig_aws
            _azure_mod.AzureConnector = _orig_azure
            _gcp_mod.GCPConnector = _orig_gcp

    assert result["error"] is not None
    assert result["total_spend_mtd"] == 0.0


# ── Tests: local IP detection ─────────────────────────────────────────────────

def test_local_ip_returns_string():
    from finops.server_web import _local_ip

    ip = _local_ip()
    assert isinstance(ip, str)
    parts = ip.split(".")
    assert len(parts) == 4


# ── Tests: finance services (scheduler + Slack bot) gating ────────────────────

def test_finance_services_off_by_default(monkeypatch):
    """With no env set, neither the scheduler nor the Slack bot starts, and the
    status lines say so. A solo `finops serve` must stay a quiet dashboard."""
    for var in ("FINOPS_ENABLE_SCHEDULER", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    with patch("finops.scheduler.jobs.start_scheduler") as start_sched, \
         patch("finops.slack_bot.app.main") as slack_main:
        from finops.server_web import _start_finance_services
        status = _start_finance_services()

    start_sched.assert_not_called()
    slack_main.assert_not_called()
    joined = " ".join(status).lower()
    assert "scheduler:  off" in joined
    assert "slack bot:  off" in joined


def test_finance_services_scheduler_opt_in(monkeypatch):
    """FINOPS_ENABLE_SCHEDULER=1 starts the digest/alert scheduler."""
    monkeypatch.setenv("FINOPS_ENABLE_SCHEDULER", "1")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

    with patch("finops.scheduler.jobs.start_scheduler") as start_sched:
        from finops.server_web import _start_finance_services
        status = _start_finance_services()

    start_sched.assert_called_once()
    assert any("scheduler:  on" in s.lower() for s in status)


def test_finance_services_slack_starts_only_with_both_tokens(monkeypatch):
    """The Slack bot needs both tokens. One alone must not launch the thread."""
    monkeypatch.delenv("FINOPS_ENABLE_SCHEDULER", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

    with patch("threading.Thread") as thread_cls:
        from finops.server_web import _start_finance_services
        status = _start_finance_services()
    thread_cls.assert_not_called()
    assert any("slack bot:  off" in s.lower() for s in status)

    # Both tokens set. The bot thread starts only if slack_bolt is importable.
    # CI does not install the [slack] extra, so pin find_spec to a present spec
    # to test the token-gating + thread-start path deterministically.
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    with patch("importlib.util.find_spec", return_value=object()), \
         patch("threading.Thread") as thread_cls:
        from finops.server_web import _start_finance_services
        status = _start_finance_services()
    thread_cls.assert_called_once()
    started = thread_cls.return_value
    started.start.assert_called_once()
    assert any("slack bot:  on" in s.lower() for s in status)


def test_finance_services_slack_off_when_dependency_missing(monkeypatch):
    """Both tokens set but slack_bolt not installed: the banner reports OFF with a
    reason and does not start the thread (the 0.8.42 honest-banner behavior). This
    is the exact case CI hits, since it does not install the [slack] extra."""
    monkeypatch.delenv("FINOPS_ENABLE_SCHEDULER", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")

    with patch("importlib.util.find_spec", return_value=None), \
         patch("threading.Thread") as thread_cls:
        from finops.server_web import _start_finance_services
        status = _start_finance_services()

    thread_cls.assert_not_called()
    assert any("slack bot:  off" in s.lower() for s in status)
    assert any("slack_bolt" in s.lower() for s in status)


def test_finance_services_never_raises_when_scheduler_broken(monkeypatch):
    """A broken scheduler degrades to an OFF line; the dashboard must still serve."""
    monkeypatch.setenv("FINOPS_ENABLE_SCHEDULER", "1")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

    with patch("finops.scheduler.jobs.start_scheduler", side_effect=RuntimeError("boom")):
        from finops.server_web import _start_finance_services
        status = _start_finance_services()  # must not raise

    assert any("scheduler:  off" in s.lower() for s in status)


def test_readonly_and_full_sessions_use_separate_stores():
    """Regression: a read-only share token must never validate as a full-access
    session. The two live in separate stores, so a nable_view value copied into
    nable_session cannot pass the full-access check (privilege escalation)."""
    import finops.server_web as sw

    full = sw._create_session()
    ro = sw._create_ro_session()

    # Each token is valid only in its own store.
    assert sw._session_valid(full) is True
    assert sw._ro_session_valid(full) is False      # full token is not a RO token
    assert sw._ro_session_valid(ro) is True
    assert sw._session_valid(ro) is False            # RO token is NOT full access

    # The escalation attempt: replaying the RO token as a full session fails.
    assert sw._session_valid(ro) is False


def test_session_mint_is_thread_safe():
    """Regression: under ThreadingHTTPServer, _prune iterating the session dict
    while another thread minted a token raised 'dictionary changed size during
    iteration'. Hammer concurrent mints and assert no exception escapes."""
    import threading
    import finops.server_web as sw

    errors = []

    def worker():
        try:
            for _ in range(200):
                sw._create_session()
                sw._create_ro_session()
        except Exception as e:  # the race surfaced as RuntimeError here
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"session mint raced: {errors[:2]}"


# ── Tests: /api/agent (the in-browser cost copilot) ──────────────────────────

def test_api_agent_happy_path(running_server):
    """POST /api/agent returns the shared agent loop's answer as JSON."""
    _, port = running_server
    from finops.slack_bot.llm import LoopResult
    with patch("finops.slack_bot.llm.ask",
               return_value=LoopResult("Your top driver is EC2 at $800/mo.")):
        status, body = _post(
            f"http://127.0.0.1:{port}/api/agent",
            {"question": "what drove our bill up?"},
        )
    assert status == 200
    assert json.loads(body)["answer"].startswith("Your top driver")


def test_api_agent_passes_stripped_question_to_loop(running_server):
    """The user's question reaches the agent loop trimmed."""
    _, port = running_server
    from finops.slack_bot.llm import LoopResult
    mock_ask = MagicMock(return_value=LoopResult("ok"))
    with patch("finops.slack_bot.llm.ask", mock_ask):
        _post(f"http://127.0.0.1:{port}/api/agent", {"question": "  hello cost  "})
    assert mock_ask.called
    assert mock_ask.call_args.args[0] == "hello cost"


def test_api_agent_empty_question_400(running_server):
    _, port = running_server
    status, _ = _post(f"http://127.0.0.1:{port}/api/agent", {"question": "   "})
    assert status == 400


def test_api_agent_invalid_json_400(running_server):
    _, port = running_server
    status, _ = _post(f"http://127.0.0.1:{port}/api/agent", raw=b"not json at all")
    assert status == 400


def test_api_agent_degrades_when_loop_errors(running_server):
    """If the agent loop raises, the endpoint returns 200 with answer=None and an
    error field (never a 500), so the chat UI can show a friendly message."""
    _, port = running_server
    with patch("finops.slack_bot.llm.ask", side_effect=RuntimeError("boom")):
        status, body = _post(f"http://127.0.0.1:{port}/api/agent", {"question": "x"})
    assert status == 200
    data = json.loads(body)
    assert data["answer"] is None
    assert "error" in data


# ── Tests: ask-to-build-a-view (live pin loop + metering) ────────────────────

def test_api_agent_signals_views_changed_when_view_pinned(running_server):
    """When the agent pins a view this turn (a 'view_pinned' side effect), the
    /api/agent response sets views_changed=true and carries the new view id, so the
    dashboard JS knows to re-fetch /api/views and slide the new card in live."""
    _, port = running_server
    from finops.slack_bot.llm import LoopResult
    result = LoopResult(
        "Pinned spend by team for this quarter.",
        side_effects=[{"type": "view_pinned", "id": 42, "title": "Spend by team"}],
        input_tokens=1200, output_tokens=300,
    )
    with patch("finops.slack_bot.llm.ask", return_value=result):
        status, body = _post(
            f"http://127.0.0.1:{port}/api/agent",
            {"question": "show me spend by team this quarter and keep it on my dashboard"},
        )
    assert status == 200
    data = json.loads(body)
    assert data["views_changed"] is True
    assert data["new_view_ids"] == [42]


def test_api_agent_no_views_changed_when_nothing_pinned(running_server):
    """A plain question that pins nothing must report views_changed=false with no
    ids, so the dashboard never re-renders the pinned grid for no reason."""
    _, port = running_server
    from finops.slack_bot.llm import LoopResult
    with patch("finops.slack_bot.llm.ask",
               return_value=LoopResult("You spent $1,234 this month.")):
        status, body = _post(
            f"http://127.0.0.1:{port}/api/agent",
            {"question": "what did we spend this month?"},
        )
    assert status == 200
    data = json.loads(body)
    assert data["views_changed"] is False
    assert data["new_view_ids"] == []


def test_api_agent_records_managed_ai_usage(running_server):
    """Every /api/agent turn fires the managed-AI metering hook with the loop's
    input/output token counts, so credit billing can consume it later."""
    _, port = running_server
    from finops.slack_bot.llm import LoopResult
    result = LoopResult("ok", input_tokens=900, output_tokens=120)
    with patch("finops.slack_bot.llm.ask", return_value=result), \
         patch("finops.slack_bot.llm.record_managed_ai_usage") as meter:
        status, _ = _post(f"http://127.0.0.1:{port}/api/agent", {"question": "hi"})
    assert status == 200
    assert meter.called
    kwargs = meter.call_args.kwargs
    assert kwargs["surface"] == "dashboard_ask"
    assert kwargs["input_tokens"] == 900
    assert kwargs["output_tokens"] == 120


def test_loop_flags_pin_side_effect_and_sums_tokens():
    """Unit test of the agent loop's plumbing: a pin_view tool call produces a
    'view_pinned' side effect carrying the new id, and token usage is summed across
    model calls. The LLM and the bridge tools are mocked, no cloud is touched."""
    import finops.slack_bot.llm as llm

    class _Usage:
        def __init__(self, i, o):
            self.input_tokens, self.output_tokens = i, o

    class _Block:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    # Turn 1: model asks to call pin_view. Turn 2: model ends the turn.
    pin_call = _Block(type="tool_use", id="t1", name="pin_view", input={"title": "Spend by team"})
    resp1 = _Block(stop_reason="tool_use", content=[pin_call], usage=_Usage(1000, 50))
    resp2 = _Block(stop_reason="end_turn",
                   content=[_Block(type="text", text="Pinned it.")], usage=_Usage(200, 30))

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [resp1, resp2]
    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = fake_client

    with patch.dict("sys.modules", {"anthropic": fake_anthropic}), \
         patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}), \
         patch("finops.slack_bot.bridge.get_bridge_tools", return_value=[]), \
         patch("finops.slack_bot.bridge.execute_bridge_tool",
               return_value='{"pinned": true, "id": 7, "title": "Spend by team"}'), \
         patch("finops.slack_bot.remediation.role_can_draft", return_value=False):
        result = llm._run_agent_loop_sync("pin spend by team", tier="chat")

    pinned = [se for se in result.side_effects if se.get("type") == "view_pinned"]
    assert pinned and pinned[0]["id"] == 7
    # Tokens summed across both model calls.
    assert result.input_tokens == 1200
    assert result.output_tokens == 80


def test_record_managed_ai_usage_logs_structured_event(caplog):
    """The metering hook writes one parseable managed_ai_usage log line that credit
    billing can tail. It never raises into the caller."""
    import logging
    import finops.slack_bot.llm as llm
    with caplog.at_level(logging.INFO, logger="finops.slack_bot.llm"):
        llm.record_managed_ai_usage(
            surface="dashboard_ask", tier="chat", model="claude-sonnet-4-6",
            input_tokens=500, output_tokens=80,
        )
    line = next((r.getMessage() for r in caplog.records if "managed_ai_usage" in r.getMessage()), "")
    assert line
    payload = json.loads(line.split("managed_ai_usage ", 1)[1])
    assert payload["surface"] == "dashboard_ask"
    assert payload["total_tokens"] == 580


def test_dashboard_html_has_build_a_view_starter(running_server):
    """The Ask tab leads with a 'Build a view' starter chip and the JS re-renders
    the pinned grid live when views_changed is signaled."""
    _, port = running_server
    _, body = _get(f"http://127.0.0.1:{port}/")
    assert "Build a view:" in body
    assert "views_changed" in body
    assert "starter-build" in body
    assert "cc-justpinned" in body
