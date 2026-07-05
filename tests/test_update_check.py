"""Staleness self-check. Born from a real incident: the nable launcher shim
allowed Python 3.10 while finops-mcp 0.8.90+ requires 3.11, so 3.10 machines
silently resolved a five-week-old build and nothing ever told the user. The
product now notices on its own; these tests pin the guardrails around that.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from finops import update_check


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    update_check._checked.clear()
    for var in ("FINOPS_AIRGAP", "FINOPS_NO_UPDATE_CHECK", "NABLE_NO_TELEMETRY"):
        monkeypatch.delenv(var, raising=False)
    yield
    update_check._checked.clear()


def test_stale_version_produces_the_note():
    with patch.object(update_check, "latest_version", return_value="9.9.9"):
        note = update_check.staleness_note()
    assert note and "9.9.9" in note and "finops upgrade" in note


def test_current_version_is_silent():
    from finops import __version__
    with patch.object(update_check, "latest_version", return_value=__version__):
        assert update_check.staleness_note() is None


def test_network_failure_is_silent():
    with patch.object(update_check, "latest_version", return_value=None):
        assert update_check.staleness_note() is None


@pytest.mark.parametrize("var", ["FINOPS_AIRGAP", "FINOPS_NO_UPDATE_CHECK", "NABLE_NO_TELEMETRY"])
def test_opt_outs_disable_the_network_call(monkeypatch, var):
    monkeypatch.setenv(var, "1")
    # latest_version must return None WITHOUT attempting the request
    with patch("httpx.get", side_effect=AssertionError("network attempted")) as g:
        assert update_check.latest_version() is None


def test_memoized_one_network_call_per_process():
    calls = {"n": 0}
    def _fake(timeout=2.0):
        calls["n"] += 1
        return "9.9.9"
    with patch.object(update_check, "latest_version", side_effect=_fake):
        update_check.staleness_note()
        update_check.staleness_note()
        update_check.staleness_note()
    assert calls["n"] == 1


def test_version_parse_tolerates_garbage():
    assert update_check._parse("not.a.version") == ()
    # garbage running version must never produce a note
    with patch.object(update_check, "latest_version", return_value="9.9.9"), \
         patch("finops.__version__", "garbage"):
        assert update_check.staleness_note() is None
