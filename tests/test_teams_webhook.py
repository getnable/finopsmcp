"""Regression: reports.py calls teams.send_to_webhook, which did not exist,
so every Teams report subscription silently failed at send time."""
from __future__ import annotations

import asyncio
from unittest.mock import patch


def test_send_to_webhook_exists_and_posts():
    from finops.notifications import teams

    posted = {}

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            posted["url"] = url
            posted["json"] = json
            return _FakeResp()

    with patch("httpx.AsyncClient", _FakeClient):
        ok = asyncio.run(teams.send_to_webhook(
            "https://example.webhook.office.com/webhookb2/abc", "weekly report"))
    assert ok is True
    assert "webhookb2" in posted["url"]
    assert "weekly report" in str(posted["json"])


def test_send_to_webhook_refuses_non_office_hosts():
    from finops.notifications import teams

    called = {"n": 0}

    class _FakeClient:
        def __init__(self, *a, **k):
            called["n"] += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise AssertionError("must not post")

    with patch("httpx.AsyncClient", _FakeClient):
        ok = asyncio.run(teams.send_to_webhook("https://evil.example.com/hook", "x"))
    assert ok is False
    assert called["n"] == 0
