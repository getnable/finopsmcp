"""Tests for the telemetry-signal fixes: countable installs, honest doctor
report, and cert-safe delivery.

Note: another test file (test_airgap) deletes finops.telemetry from sys.modules
and re-imports it, so we always resolve the module fresh via importlib and patch
by string path (which resolves from sys.modules at call time) rather than holding
a top-level reference that can go stale.
"""
import importlib
import sys
import types

from unittest.mock import MagicMock

import finops.welcome as welcome


def _tel():
    return importlib.import_module("finops.telemetry")


def test_welcome_uses_per_install_uuid_not_a_constant(monkeypatch):
    # Regression: welcome fired with distinct_id="install" (constant), collapsing
    # every install into one PostHog person so installs were uncountable. It must
    # now delegate to telemetry with the per-install UUID.
    monkeypatch.setattr("finops.telemetry._is_opted_out", lambda: False)

    captured = {}

    def fake_send(install_id, event, props):
        captured.update(install_id=install_id, event=event, props=props)

    monkeypatch.setattr("finops.telemetry._send_event", fake_send)
    # Run the fire-and-forget thread body synchronously for a deterministic assert.
    monkeypatch.setattr(welcome.threading, "Thread",
                        lambda target, args, daemon: MagicMock(start=lambda: target(*args)))

    welcome._fire_telemetry("install_completed", {"source": "finops_welcome"})

    assert captured["install_id"] == _tel()._get_install_id()
    assert captured["install_id"] != "install"
    assert len(captured["install_id"]) == 36  # a UUID, not a constant
    assert captured["event"] == "install_completed"
    assert "version" in captured["props"]  # live version injected


def test_welcome_respects_opt_out(monkeypatch):
    monkeypatch.setattr("finops.telemetry._is_opted_out", lambda: True)
    called = {"n": 0}
    monkeypatch.setattr("finops.telemetry._send_event",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    welcome._fire_telemetry("install_completed", {"source": "finops_welcome"})
    assert called["n"] == 0


def test_send_event_prefers_httpx_for_cert_safety(monkeypatch):
    # Regression: urllib relies on the platform trust store, which is empty on
    # python.org macOS builds, so events silently dropped. httpx (certifi) must be
    # the primary path.
    monkeypatch.setenv("NABLE_POSTHOG_KEY", "phc_test")
    # _send_event now checks opt-out itself; neutralize CI/air-gap signals so
    # this test exercises the transport path.
    monkeypatch.setattr(_tel(), "_is_opted_out", lambda: False)
    fake_httpx = types.ModuleType("httpx")
    posted = {}
    fake_httpx.post = lambda url, json, timeout: posted.update({"url": url, "json": json})
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    _tel()._send_event("abc-install-id", "unit_test_event", {"k": "v"})

    assert "us.i.posthog.com/capture/" in posted["url"]
    assert posted["json"]["distinct_id"] == "abc-install-id"
    assert posted["json"]["event"] == "unit_test_event"


def test_doctor_reports_telemetry_on_by_default(monkeypatch):
    from finops.doctor import _check_telemetry
    monkeypatch.setattr("finops.telemetry._is_opted_out", lambda: False)
    out = _check_telemetry()
    assert "on" in out["detail"].lower()
    assert "NABLE_NO_TELEMETRY" in out["detail"]


def test_doctor_reports_telemetry_off_when_opted_out(monkeypatch):
    from finops.doctor import _check_telemetry
    monkeypatch.setattr("finops.telemetry._is_opted_out", lambda: True)
    out = _check_telemetry()
    assert "off" in out["detail"].lower()
