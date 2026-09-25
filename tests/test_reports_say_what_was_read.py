"""A report says what was read and what was delivered, never a guess.

Reports read only the local cost_snapshots table, which an open install fills
only when a snapshot is taken on request. On a fresh install every report said
"Total spend: $0 (+0.0%)", "Anomalies: None detected" and "Budgets: All within
limits": three findings about data nobody had read and budgets nobody had set.
The weekly digest email led with "$0 tracked spend".

send_report_now skipped an unconfigured Slack in silence, returned email
{"ok": false} with no reason, and stamped last_sent_at even when nothing was
delivered. A subscription naming three channels on one incoming webhook posted
the same report three times to the webhook's one channel.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from finops.notifications import reports


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("FINOPS_AIRGAP", raising=False)
    for v in ("SLACK_WEBHOOK_URL", "SLACK_BOT_TOKEN", "FINOPS_SMTP_HOST", "FINOPS_SMTP_USER",
              "FINOPS_SMTP_PASSWORD", "FINOPS_DIGEST_TO"):
        monkeypatch.delenv(v, raising=False)
    import finops.storage.db as db_mod
    db_mod._ENGINE = None
    yield db_mod
    db_mod._ENGINE = None


def _seed_spend(db_mod, amount=120.0):
    from finops.storage.db import cost_snapshots, get_engine
    with get_engine().begin() as conn:
        conn.execute(cost_snapshots.insert().values(
            provider="aws", service="Amazon EC2", account_id="1", region="us-east-1",
            snapshot_date=(date.today() - timedelta(days=1)).isoformat(), amount_usd=amount,
            captured_at=datetime.now(timezone.utc)))


def _text(blocks) -> str:
    return json.dumps(blocks)


# ── sections ──────────────────────────────────────────────────────────────────

def test_spend_with_nothing_read_says_no_cost_data(db):
    blocks, text = asyncio.run(reports._section_spend(filters={}, lookback_days=7))
    assert "$0" not in text and "+0.0%" not in text
    assert "no cost data yet" in text.lower()
    assert "no cost data yet" in _text(blocks).lower()


def test_spend_with_no_prior_period_does_not_claim_zero_change(db):
    _seed_spend(db)
    blocks, text = asyncio.run(reports._section_spend(filters={}, lookback_days=7))
    assert "$120" in text
    assert "+0.0%" not in text and "+0.0%" not in _text(blocks)


def test_anomalies_with_nothing_read_is_not_none_detected(db):
    blocks, text = asyncio.run(reports._section_anomalies(filters={}, lookback_days=7))
    assert "None detected" not in _text(blocks)
    assert "no cost data" in _text(blocks).lower()


def test_budgets_with_none_set_is_not_all_within_limits(db):
    blocks, text = asyncio.run(reports._section_budgets(filters={}, lookback_days=7))
    assert "All within limits" not in _text(blocks)
    assert "no budgets set" in _text(blocks).lower()


# ── the digest email ──────────────────────────────────────────────────────────

def test_the_digest_email_with_no_data_says_so():
    from finops.notifications.email_digest import _build_html, _subject
    html = _build_html("Sep 1 - Sep 7", 0.0, 0.0, [], [], [], has_data=False)
    assert "$0" not in html
    assert "No cost data yet" in html
    assert "$0 tracked spend" not in _subject(0.0, "Sep 1 - Sep 7", has_data=False)


# ── delivery ──────────────────────────────────────────────────────────────────

def _sub(**kw) -> dict:
    base = {"name": "Weekly", "slack_channels": "[]", "email_addresses": "[]",
            "teams_webhook": ""}
    base.update(kw)
    return base


def test_an_unconfigured_slack_is_reported_not_skipped(db):
    out = asyncio.run(reports.deliver_report(
        _sub(slack_channels=json.dumps(["#finops"])), [], "x"))
    assert out["slack"], "the unconfigured Slack channel vanished from the result"
    assert out["slack"][0]["ok"] is False
    assert "SLACK_WEBHOOK_URL" in out["slack"][0]["reason"]


def test_an_unconfigured_email_says_why(db):
    out = asyncio.run(reports.deliver_report(
        _sub(email_addresses=json.dumps(["a@example.com"])), [], "x"))
    assert out["email"][0]["ok"] is False
    assert "FINOPS_SMTP_HOST" in out["email"][0]["reason"]


def test_several_channels_on_one_webhook_post_once(db, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
    from finops.notifications import slack
    posts: list = []

    async def _send_webhook(blocks, text=""):
        posts.append(text)
        return True
    monkeypatch.setattr(slack, "send_webhook", _send_webhook)
    out = asyncio.run(reports.deliver_report(
        _sub(slack_channels=json.dumps(["#a", "#b", "#c"])), [], "x"))
    assert len(posts) == 1
    assert len(out["slack"]) == 1
    assert "one channel" in out["slack"][0]["note"]


def _make_sub(db_mod, **kw) -> int:
    from finops.notifications.reports import create_subscription
    return create_subscription(name="Weekly", sections=["spend"], frequency="weekly", **kw)["id"]


def _last_sent(sub_id):
    from finops.notifications.reports import list_subscriptions
    return next(s for s in list_subscriptions() if s["id"] == sub_id)["last_sent_at"]


def test_last_sent_at_moves_only_on_a_real_delivery(db, monkeypatch):
    sid = _make_sub(db, slack_channels=["#finops"])
    before = _last_sent(sid)
    out = asyncio.run(reports.run_subscription(sid))
    assert out["delivered"] is False
    assert _last_sent(sid) == before

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
    from finops.notifications import slack

    async def _ok(blocks, text=""):
        return True
    monkeypatch.setattr(slack, "send_webhook", _ok)
    out = asyncio.run(reports.run_subscription(sid))
    assert out["delivered"] is True
    assert _last_sent(sid) != before


def test_list_report_subscriptions_says_on_request_on_an_open_install(db, monkeypatch):
    import inspect

    import finops.server  # noqa: F401
    from finops.tools import notifications as nt
    _make_sub(db, slack_channels=["#finops"])
    out = nt.list_report_subscriptions()
    if inspect.isawaitable(out):
        out = asyncio.run(out)
    assert out["subscriptions"][0]["delivery"] == "on_request"


def test_send_digest_now_with_no_snapshot_posts_nothing(db, monkeypatch):
    import inspect

    import finops.server  # noqa: F401
    from finops.notifications import slack
    from finops.tools import notifications as nt
    monkeypatch.setattr(nt._srv, "require_pro", lambda *a, **k: None)
    monkeypatch.setattr(nt._srv, "require_role", lambda *a, **k: None)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
    posts: list = []

    async def _send(blocks, text=""):
        posts.append(text)
        return True
    monkeypatch.setattr(slack, "send_webhook", _send)
    out = nt.send_digest_now()
    if inspect.isawaitable(out):
        out = asyncio.run(out)
    assert out["sent"] is False
    assert "No cost data" in out["message"]
    assert posts == []
