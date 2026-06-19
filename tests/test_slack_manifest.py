"""The Slack app manifest is the 2-minute, local-first install path. These tests
keep the committed manifest in sync with the wizard and assert it grants exactly
what the bot's handlers use, so the install can't silently break."""
from __future__ import annotations

from pathlib import Path

from finops.setup_wizard import _SLACK_APP_MANIFEST

_MANIFEST_FILE = Path(__file__).resolve().parent.parent / "docs" / "slack-app-manifest.yaml"


def test_doc_manifest_matches_wizard():
    """docs/slack-app-manifest.yaml must match what `finops setup slack` prints."""
    assert _MANIFEST_FILE.read_text().strip() == _SLACK_APP_MANIFEST.strip()


def test_manifest_covers_every_handler_scope():
    """Codifies the scope audit. Each scope maps to a real handler:
    app_mentions:read -> @app.event('app_mention'); chat:write -> chat_postMessage;
    im:* -> the message.im DM handler; users:read -> users_info; reactions -> ack."""
    m = _SLACK_APP_MANIFEST
    for scope in ("app_mentions:read", "chat:write", "im:history", "im:write",
                  "users:read", "reactions:write"):
        assert scope in m, f"manifest is missing bot scope {scope!r}"
    for event in ("app_mention", "message.im"):
        assert event in m, f"manifest is missing event subscription {event!r}"
    assert "is_enabled: true" in m          # interactivity, for the anomaly/approve buttons
    assert "socket_mode_enabled: true" in m


def test_manifest_is_local_first_not_a_hosted_marketplace_app():
    """Socket Mode + no hosted OAuth redirect = runs on the customer's machine,
    not a publicly-hosted Marketplace app that would hold their workspace token."""
    m = _SLACK_APP_MANIFEST
    assert "socket_mode_enabled: true" in m
    assert "redirect_urls" not in m
