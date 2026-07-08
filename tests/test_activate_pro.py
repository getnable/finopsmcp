"""activate_pro: paste a license key in the editor, Pro unlocks in-session.

The upgrade friction was that activating happened in a separate terminal process,
so the running MCP server couldn't see the new license without an editor restart.
activate_pro runs inside the server process: store_license clears the cached
status and get_status re-reads, so the paid plan is live from the next call with
no restart. These tests lock in that behavior and the no-key / bad-key paths.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import finops.server as server
from finops.license import LicenseStatus


def _run(coro):
    return asyncio.run(coro)


def test_activate_pro_no_key_prompts():
    out = _run(server.activate_pro())
    assert out["activated"] is False
    assert "FINOPS-2-" in out["message"]
    assert "get_pro" in out


def test_activate_pro_valid_key_unlocks_in_session():
    stored = LicenseStatus(mode="pro", email="dev@acme.com", issued="2026-07-08", message="")
    live = LicenseStatus(mode="pro", email="dev@acme.com", issued="2026-07-08", message="")
    with patch("finops.license.store_license", return_value=stored) as store, \
         patch("finops.license.get_status", return_value=live):
        out = _run(server.activate_pro(license_key="FINOPS-2-abc-def"))
    assert out["activated"] is True
    assert out["plan"] == "pro"
    assert out["email"] == "dev@acme.com"
    assert "No restart" in out["message"]
    # It actually stored the key (that's what hot-flips the running server).
    store.assert_called_once_with("FINOPS-2-abc-def")


def test_activate_pro_team_key():
    stored = LicenseStatus(mode="team", email="lead@acme.com", issued="2026-07-08", message="")
    with patch("finops.license.store_license", return_value=stored), \
         patch("finops.license.get_status", return_value=stored):
        out = _run(server.activate_pro(license_key="FINOPS-2-team"))
    assert out["activated"] is True
    assert out["plan"] == "team"


def test_activate_pro_invalid_key_is_rejected():
    bad = LicenseStatus(mode="invalid", email="", issued="", message="License key signature invalid.")
    with patch("finops.license.store_license", return_value=bad) as store, \
         patch("finops.license.get_status"):
        out = _run(server.activate_pro(license_key="FINOPS-2-forged"))
    assert out["activated"] is False
    assert out["plan"] == "invalid"
    assert "signature invalid" in out["error"]
