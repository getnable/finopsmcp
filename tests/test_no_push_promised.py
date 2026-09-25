"""The open install never promises a message it will not send on its own.

An open install is an MCP server and a CLI: it answers when asked and runs
nothing on a timer. Scheduled delivery is nable Cloud. Yet the free-user tips
said "Pro auto-posts anomalies to Slack or Teams the moment they fire",
`nable slack` asked for a "Daily digest time" nothing would ever read and
finished with "alerts and digests will post to Slack", and it said "Slack
configured" without anything having been posted, so a wrong webhook stayed
invisible until the day someone wondered where the alerts were.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import finops.license as L
import finops.setup_wizard as W
from finops import server

_PUSH_PROMISES = (
    "auto-posts", "the moment they fire", "the moment these fire",
    "the moment a cost spike fires", "automatic Slack alerts",
    "sends automatically at 09:00", "alerts and digests will post",
    "Daily digest time", "scheduled weekly digest",
)

_SRC = Path(server.__file__).resolve().parent


@pytest.mark.parametrize("rel", ["server.py", "setup_wizard.py", "tools/anomalies.py",
                                 "tools/cost_queries.py", "tools/notifications.py"])
def test_no_surface_promises_push(rel):
    text = (_SRC / rel).read_text(encoding="utf-8")
    for phrase in _PUSH_PROMISES:
        assert phrase not in text, f"{rel} promises push: {phrase!r}"


class _Lic:
    def __init__(self, mode):
        self.mode = mode


def test_tips_name_scheduled_delivery_as_hosted(monkeypatch):
    monkeypatch.setattr(server, "get_status", lambda: _Lic("free"))
    for tool in ("get_anomalies", "get_costs_by_team", "get_org_cost_summary"):
        server._team_tips_shown.clear()
        tip = server._maybe_team_tip(tool)
        assert tip is not None, tool
        text = tip["missing_with_team"]
        if "Slack" in text or "schedule" in text or "digest" in text:
            assert "nable Cloud" in text or "on request" in text or "when you ask" in text, text


def test_the_tip_price_line_is_the_pro_price_not_a_team_promise(monkeypatch):
    server._team_tips_shown.clear()
    monkeypatch.setattr(server, "get_status", lambda: _Lic("free"))
    tip = server._maybe_team_tip("get_anomalies")
    assert "one price for your whole team" not in tip["upgrade"]
    assert L.plan_label("pro") in tip["upgrade"]
    assert L.checkout_url("pro") in tip["upgrade"]


def test_a_team_user_gets_no_upsell(monkeypatch):
    server._team_tips_shown.clear()
    monkeypatch.setattr(server, "get_status", lambda: _Lic("team"))
    assert server._maybe_team_tip("get_anomalies") is None


def test_no_tip_sells_a_feature_that_is_free_today(monkeypatch):
    monkeypatch.setattr(server, "get_status", lambda: _Lic("free"))
    if not L._HOLD_AI_UNGATE:
        pytest.skip("the hold is off")
    for tool in ("get_rightsizing_recommendations", "get_commitment_analysis"):
        server._team_tips_shown.clear()
        assert server._maybe_team_tip(tool) is None, tool


# ── nable slack / nable teams ─────────────────────────────────────────────────

class _FakeVault:
    def __init__(self):
        self.data: dict = {}

    def store(self, k, v):
        self.data[k] = v

    def get(self, k):
        return self.data.get(k)


@pytest.fixture
def vault(monkeypatch):
    v = _FakeVault()
    from finops.security import vault as vault_mod
    monkeypatch.setattr(vault_mod.Vault, "default", classmethod(lambda cls: v))
    return v


def _answers(monkeypatch, answers: list[str]) -> list[str]:
    asked: list[str] = []
    it = iter(answers)

    def _prompt(msg, secret=False, default=""):
        asked.append(msg)
        try:
            return next(it)
        except StopIteration:
            return default
    monkeypatch.setattr(W, "_prompt", _prompt)
    return asked


class _Resp:
    def __init__(self, code=200, body=None):
        self.status_code = code
        self._body = body or {"ok": True}

    def json(self):
        return self._body


def test_slack_webhook_setup_asks_no_digest_time_and_sends_nothing_unasked(
        vault, monkeypatch, capsys):
    import httpx
    posted: list = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: posted.append(url) or _Resp())
    asked = _answers(monkeypatch, ["1", "https://hooks.slack.com/services/T/B/x", "n"])
    W.setup_slack()
    out = capsys.readouterr().out
    assert not any("digest" in a.lower() for a in asked), asked
    assert "FINOPS_DIGEST_CRON" not in vault.data
    assert posted == []
    assert "No test message was sent" in out
    assert "Slack configured" not in out
    assert "nable Cloud" in out


def test_slack_webhook_test_post_goes_to_the_webhook_just_given(vault, monkeypatch, capsys):
    import httpx
    posted: list = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: posted.append(url) or _Resp())
    _answers(monkeypatch, ["1", "https://hooks.slack.com/services/T/B/x", "y"])
    W.setup_slack()
    out = capsys.readouterr().out
    assert posted == ["https://hooks.slack.com/services/T/B/x"]
    assert "Test message posted" in out


def test_a_rejected_test_post_is_reported(vault, monkeypatch, capsys):
    import httpx
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _Resp(404))
    _answers(monkeypatch, ["1", "https://hooks.slack.com/services/T/B/x", "y"])
    W.setup_slack()
    out = capsys.readouterr().out
    assert "Test message posted" not in out
    assert "404" in out


def test_teams_setup_asks_no_digest_time(vault, monkeypatch, capsys):
    asked = _answers(monkeypatch, ["https://x.webhook.office.com/abc", "n"])
    W.setup_teams()
    out = capsys.readouterr().out
    assert not any("digest" in a.lower() for a in asked), asked
    assert "No test message was sent" in out


def test_the_post_connect_line_for_a_channel_does_not_promise_posts():
    line = W._post_connect_message("slack")
    assert "will post" not in line
    assert "ask" in line.lower()


def test_the_setup_footer_does_not_advertise_a_dashboard_that_is_not_installed(capsys):
    W._print_setup_footer("slack")
    out = capsys.readouterr().out
    import importlib.util
    if importlib.util.find_spec("finops.server_web") is None:
        assert "serve" not in out


def test_check_notification_config_on_an_open_install_says_on_request(monkeypatch):
    from finops.tools import notifications as nt
    monkeypatch.setattr(nt, "_scheduler_installed", lambda: False)
    import asyncio
    import inspect
    out = nt.check_notification_config()
    if inspect.isawaitable(out):
        out = asyncio.run(out)
    assert out["delivery"] == "on_request"
    assert "schedule" not in out
