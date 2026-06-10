"""
Team tier ($1,000/mo flat) tests: plan validation, gating, and the bot's
hard gate. The conversational Slack bot and chat remediation are Team-only;
trial passes so demos feel the full product; no free questions.
"""
from __future__ import annotations

import pytest

import finops.license as lic
from finops.license import LicenseStatus
from finops.slack_bot import app as slack_app


@pytest.fixture(autouse=True)
def _reset_status_cache():
    lic._status = None
    yield
    lic._status = None


def _status(mode):
    return LicenseStatus(mode=mode, email="x@y.z", issued="2026-06-01", message="")


def test_tier_inclusion_matrix():
    assert not _status("free").is_team
    assert not _status("pro").is_team        # $40 solo keys do not get the bot
    assert _status("team").is_team
    assert _status("enterprise").is_team
    assert _status("trial").is_team          # demos feel the full product
    # Higher tiers include pro features (this also fixes the enterprise hole)
    assert _status("team").is_pro
    assert _status("enterprise").is_pro


def test_team_plan_key_roundtrip(monkeypatch):
    # Test keypair from test_license_v2: the public half matches the bundled key.
    monkeypatch.setenv("FINOPS_LICENSE_PRIVATE_KEY", "8HW1kTWT2OIuBRaBY-YcfmweY9hoECjF7uedaJfzID4")
    key = lic.generate_key("buyer@acme.com", plan="team")
    status = lic.validate_key(key)
    assert status.mode == "team"
    assert status.is_team and status.is_pro


def test_require_team_blocks_free_and_pro(monkeypatch):
    for mode in ("free", "pro"):
        monkeypatch.setattr(lic, "get_status", lambda m=mode: _status(m))
        err = lic.require_team("slack_conversational_bot")
        assert err is not None
        assert "$1,000/mo" in err["upgrade"]


def test_require_team_allows_team_and_trial(monkeypatch):
    for mode in ("team", "enterprise", "trial"):
        monkeypatch.setattr(lic, "get_status", lambda m=mode: _status(m))
        assert lic.require_team("slack_conversational_bot") is None


def test_require_team_unknown_feature_fails_open(monkeypatch):
    monkeypatch.setattr(lic, "get_status", lambda: _status("free"))
    assert lic.require_team("not_a_team_feature") is None


def test_bot_gate_blocks_free(monkeypatch):
    monkeypatch.setattr("finops.license.check_license", lambda: _status("free"))
    msg = slack_app._license_gate()
    assert msg is not None
    assert "Team" in msg and "trial" in msg


def test_bot_gate_allows_team_and_trial(monkeypatch):
    for mode in ("team", "trial", "enterprise"):
        monkeypatch.setattr("finops.license.check_license", lambda m=mode: _status(m))
        assert slack_app._license_gate() is None


def test_bot_gate_fails_open_on_license_error(monkeypatch):
    """A license-system bug must never take the bot down for paying users."""
    def boom():
        raise RuntimeError("keyring exploded")

    monkeypatch.setattr("finops.license.check_license", boom)
    assert slack_app._license_gate() is None
