"""Tests for onboarding-funnel instrumentation (_emit_step) and the parallel,
time-capped ambient AWS credential detection."""
from __future__ import annotations

import pytest


# ── _emit_step ───────────────────────────────────────────────────────────────

def test_emit_step_dispatches_setup_step(monkeypatch):
    from finops import setup_wizard, telemetry
    captured = {}

    def fake_send(install_id, event, props):
        captured["event"] = event
        captured["props"] = props

    monkeypatch.setattr(telemetry, "_send_event", fake_send)
    setup_wizard._emit_step("connect_attempted", auth_method="access_key")

    assert captured["event"] == "setup_step"
    assert captured["props"]["step"] == "connect_attempted"
    assert captured["props"]["provider"] == "aws"
    assert captured["props"]["auth_method"] == "access_key"


def test_emit_step_defaults_provider_aws(monkeypatch):
    from finops import setup_wizard, telemetry
    captured = {}
    monkeypatch.setattr(telemetry, "_send_event",
                        lambda i, e, p: captured.update(props=p))
    setup_wizard._emit_step("manual_opened")
    assert captured["props"] == {"step": "manual_opened", "provider": "aws"}


def test_emit_step_swallows_errors(monkeypatch):
    from finops import setup_wizard, telemetry

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(telemetry, "_send_event", boom)
    # Must not propagate — telemetry can never break onboarding.
    setup_wizard._emit_step("manual_opened")


# ── _detect_aws_candidates (parallel + dedupe + resilience) ──────────────────

class _FakeClient:
    def __init__(self, acct):
        self._acct = acct

    def get_caller_identity(self):
        return {"Account": self._acct}

    def list_account_aliases(self):
        return {"AccountAliases": []}


def _fake_session_factory(acct_for: dict, profiles: list, raise_for=frozenset()):
    class _FakeSession:
        def __init__(self, profile_name=None):
            self.profile_name = profile_name
            self.region_name = "us-east-1"

        @property
        def available_profiles(self):
            return profiles

        def client(self, svc, config=None):
            if self.profile_name in raise_for:
                raise RuntimeError("expired SSO token")
            return _FakeClient(acct_for.get(self.profile_name, "000"))

    return _FakeSession


def test_detect_prefers_named_and_dedupes_default_chain(monkeypatch):
    import boto3
    from finops import setup_wizard

    # default chain (None) resolves to the same account as the 'prod' profile.
    acct_for = {None: "111", "prod": "111", "dev": "222"}
    monkeypatch.setattr(boto3, "Session",
                        _fake_session_factory(acct_for, ["prod", "dev"]))

    cands = setup_wizard._detect_aws_candidates()
    accts = sorted(c["account_id"] for c in cands)
    assert accts == ["111", "222"]  # default-chain 111 deduped against named 'prod'
    by_acct = {c["account_id"]: c for c in cands}
    assert by_acct["111"]["profile"] == "prod"  # named profile preferred over default


def test_detect_skips_failing_probe(monkeypatch):
    import boto3
    from finops import setup_wizard

    acct_for = {None: "999", "good": "999", "bad": "ignored"}
    monkeypatch.setattr(boto3, "Session",
                        _fake_session_factory(acct_for, ["good", "bad"], raise_for={"bad"}))

    cands = setup_wizard._detect_aws_candidates()
    # 'bad' raised and is skipped; 'good' (999) wins; default-chain 999 deduped.
    assert [c["account_id"] for c in cands] == ["999"]
    assert cands[0]["profile"] == "good"


def test_detect_no_profiles_uses_default_chain(monkeypatch):
    import boto3
    from finops import setup_wizard

    acct_for = {None: "555"}
    monkeypatch.setattr(boto3, "Session", _fake_session_factory(acct_for, []))

    cands = setup_wizard._detect_aws_candidates()
    assert len(cands) == 1
    assert cands[0]["account_id"] == "555"
    assert cands[0]["profile"] == ""  # default chain
    assert cands[0]["label"] == "default credentials"


def test_detect_empty_when_nothing_resolves(monkeypatch):
    import boto3
    from finops import setup_wizard

    monkeypatch.setattr(boto3, "Session",
                        _fake_session_factory({}, ["x"], raise_for={"x", None}))
    assert setup_wizard._detect_aws_candidates() == []
