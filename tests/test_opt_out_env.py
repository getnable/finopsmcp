"""Every opt-out variable uses one truthiness rule, and DO_NOT_TRACK is honoured."""
from __future__ import annotations

import pytest

from finops import telemetry, update_check


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for v in ("FINOPS_AIRGAP", "FINOPS_NO_UPDATE_CHECK", "NABLE_NO_TELEMETRY",
              "DO_NOT_TRACK", "NABLE_TELEMETRY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(telemetry, "is_ci", lambda: False)


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "anything"])
def test_update_check_respects_any_on_value(monkeypatch, value):
    monkeypatch.setenv("NABLE_NO_TELEMETRY", value)
    assert update_check._disabled() is True


def test_do_not_track_disables_update_check(monkeypatch):
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert update_check._disabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_off_values_do_not_disable(monkeypatch, value):
    monkeypatch.setenv("NABLE_NO_TELEMETRY", value)
    assert update_check._disabled() is False


def test_do_not_track_beats_an_explicit_opt_in(monkeypatch):
    if not telemetry._POSTHOG_KEY:
        pytest.skip("no ingest key in this build")
    monkeypatch.setenv(telemetry._OPT_IN_ENV, "1")
    assert telemetry._is_opted_out() is False
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert telemetry._is_opted_out() is True
