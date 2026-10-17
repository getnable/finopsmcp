"""Under FINOPS_AIRGAP nothing nable sends leaves for Slack, Teams or email.

Air-gap promises traffic to the cost providers you connect and nothing else.
Report subscriptions honoured it (notifications/reports.py skips delivery),
but `send_digest_now` posted the digest to Slack and Teams, and `nable brief
--deliver` to Slack, Teams and SMTP: cost figures leaving an air-gapped
network. The senders themselves now refuse, so no path around one of them
can send.
"""
from __future__ import annotations

import asyncio
import smtplib

import httpx
import pytest

from finops import config


@pytest.fixture
def airgapped(monkeypatch):
    monkeypatch.setattr(config, "AIRGAP", True)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T0/B0/x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-not-a-real-token")
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://example.webhook.office.com/x")
    for k, v in (("FINOPS_SMTP_HOST", "smtp.example.com"), ("FINOPS_SMTP_USER", "u"),
                 ("FINOPS_SMTP_PASSWORD", "p"), ("FINOPS_DIGEST_TO", "a@example.com")):
        monkeypatch.setenv(k, v)

    tried: list = []

    def no_network(*a, **k):
        tried.append(a)                 # senders catch exceptions, so record, then fail
        raise ConnectionRefusedError("no network in this test")
    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    monkeypatch.setattr(smtplib, "SMTP", no_network)
    return tried


def test_slack_and_teams_send_nothing(airgapped):
    from finops.notifications import slack, teams
    for send in (lambda: slack.send_webhook([], "x"), lambda: slack.send_bot([], "x"),
                 lambda: slack.send([], "x"), lambda: teams._post({}),
                 lambda: teams.send_to_webhook("https://example.webhook.office.com/x", "x")):
        try:
            assert asyncio.run(send()) is False
        except ConnectionRefusedError:
            pass
    assert airgapped == [], "opened a connection under FINOPS_AIRGAP"


def test_email_sends_nothing(airgapped):
    from finops.notifications import email_digest
    assert email_digest.send_message("a@example.com", "s", "t") is False
    assert email_digest.send_custom_digest("a@example.com", "s", "t") is False
    result = email_digest.send_weekly_digest_result(1000.0, 900.0, [], [], [])
    assert result["sent"] is False and "FINOPS_AIRGAP" in result["error"]
    assert airgapped == [], "opened an SMTP connection under FINOPS_AIRGAP"
