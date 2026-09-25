"""Offline, nable stays fast and says only what happened.

On a blackholed network (packets dropped, no refusal), dogfooding found:
- opted-in telemetry added ~9.6 s to every command: a 5 s httpx POST, then a
  4 s urllib retry of the same unreachable host;
- `nable upgrade` sat ~10 s on PyPI, then suggested "finops upgrade 0.8.57",
  a version far older than the one installed;
- the email-capture prompt printed "Got it. We'll follow up soon." when the
  POST had failed and nothing was recorded.
"""
from __future__ import annotations

import time

import pytest

import finops.setup_wizard as W
import finops.telemetry as tel


# ── telemetry ─────────────────────────────────────────────────────────────────

@pytest.fixture
def opted_in(monkeypatch):
    monkeypatch.setattr(tel, "_is_opted_out", lambda: False)
    monkeypatch.setattr(tel, "_runtime_props", lambda: {})


def test_an_unreachable_host_costs_a_command_about_a_second(opted_in, monkeypatch):
    import httpx

    def _hang(*a, **k):
        time.sleep(5)
        raise httpx.ConnectTimeout("blackholed")
    monkeypatch.setattr(httpx, "post", _hang)
    t0 = time.monotonic()
    tel._send_event("id", "evt", {})
    assert time.monotonic() - t0 < 2.0


def test_a_failed_httpx_send_is_not_retried_over_urllib(opted_in, monkeypatch):
    import urllib.request

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(
        httpx.ConnectError("down")))
    called: list = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: called.append(1))
    tel._send_event("id", "evt", {})
    time.sleep(0.1)
    assert called == []


def test_a_fast_send_still_lands(opted_in, monkeypatch):
    import httpx
    sent: list = []
    monkeypatch.setattr(httpx, "post", lambda url, **k: sent.append(url))
    tel._send_event("id", "evt", {})
    assert sent and sent[0].endswith("/capture/")


# ── nable upgrade ─────────────────────────────────────────────────────────────

def test_upgrade_offline_is_bounded_and_suggests_no_old_version(monkeypatch, capsys):
    import httpx

    def _hang(*a, **k):
        time.sleep(3)
        raise httpx.ConnectTimeout("blackholed")
    monkeypatch.setattr(httpx, "get", _hang)
    monkeypatch.setattr(W, "_PYPI_WAIT_S", 0.3)
    monkeypatch.setattr(W, "_installed_version", lambda: "0.9.400")
    t0 = time.monotonic()
    W._run_upgrade("")
    assert time.monotonic() - t0 < 1.5
    out = capsys.readouterr().out
    assert "0.8.57" not in out
    assert "0.9.400" in out


def test_upgrade_never_downgrades_to_an_older_latest(monkeypatch, capsys):
    monkeypatch.setattr(W, "_installed_version", lambda: "0.9.400")
    monkeypatch.setattr(W, "_latest_pypi_version", lambda: "0.9.300")
    called: list = []
    monkeypatch.setattr(W, "_upgrade_running_cli", lambda *a: called.append(a))
    W._run_upgrade("")
    out = capsys.readouterr().out
    assert called == []
    assert "newer" in out


# ── email capture ─────────────────────────────────────────────────────────────

def test_email_capture_says_nothing_was_recorded_when_the_post_fails(monkeypatch, tmp_path, capsys):
    import urllib.request
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FINOPS_AIRGAP", raising=False)
    monkeypatch.setattr("builtins.input", lambda *a: "me@example.com")

    def _fail(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", _fail)
    W._offer_email_signup()
    out = capsys.readouterr().out
    assert "Got it" not in out and "follow up" not in out
    assert "not sent" in out.lower() or "could not" in out.lower()
