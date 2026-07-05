"""SEC-2: SSO group -> role mapping for hosted-box sessions.

Before this, every SSO login minted an admin session and the dashboard agent
would run admin-only tools for anyone in the company directory. The mapping is
lock-out-safe: with no group envs configured, behavior is exactly historical
(admin), so an upgrade can never lock a deployment out; configuring any group
env opts into enforcement with viewer as the unmapped default.
"""
from __future__ import annotations

import pytest

from finops.auth.sso import resolve_role
from finops.auth.rbac import Identity, require_role


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("FINOPS_SSO_ADMIN_GROUPS", "FINOPS_SSO_ANALYST_GROUPS",
              "FINOPS_SSO_VIEWER_GROUPS", "FINOPS_SSO_GROUPS_CLAIM",
              "FINOPS_SSO_DEFAULT_ROLE", "DATABASE_URL", "FINOPS_REQUIRE_AUTH"):
        monkeypatch.delenv(v, raising=False)


def test_no_mapping_configured_is_admin_lockout_safety():
    assert resolve_role({"groups": ["whatever"]}) == "admin"
    assert resolve_role({}) == "admin"


def test_groups_map_to_roles(monkeypatch):
    monkeypatch.setenv("FINOPS_SSO_ADMIN_GROUPS", "platform-admins")
    monkeypatch.setenv("FINOPS_SSO_ANALYST_GROUPS", "eng, finance-eng")
    monkeypatch.setenv("FINOPS_SSO_VIEWER_GROUPS", "everyone")
    assert resolve_role({"groups": ["platform-admins"]}) == "admin"
    assert resolve_role({"groups": ["finance-eng"]}) == "analyst"
    assert resolve_role({"groups": ["everyone"]}) == "viewer"


def test_highest_matching_role_wins(monkeypatch):
    monkeypatch.setenv("FINOPS_SSO_ADMIN_GROUPS", "admins")
    monkeypatch.setenv("FINOPS_SSO_ANALYST_GROUPS", "eng")
    assert resolve_role({"groups": ["eng", "admins"]}) == "admin"


def test_unmapped_user_gets_default_viewer(monkeypatch):
    monkeypatch.setenv("FINOPS_SSO_ADMIN_GROUPS", "admins")
    assert resolve_role({"groups": ["marketing"]}) == "viewer"
    monkeypatch.setenv("FINOPS_SSO_DEFAULT_ROLE", "analyst")
    assert resolve_role({"groups": ["marketing"]}) == "analyst"
    # garbage default falls back to viewer, never to something wider
    monkeypatch.setenv("FINOPS_SSO_DEFAULT_ROLE", "superuser")
    assert resolve_role({"groups": ["marketing"]}) == "viewer"


def test_group_matching_is_case_insensitive_and_shape_tolerant(monkeypatch):
    monkeypatch.setenv("FINOPS_SSO_ANALYST_GROUPS", "Finance-Eng")
    assert resolve_role({"groups": ["finance-eng"]}) == "analyst"
    # some IdPs send a space or comma separated string instead of a list
    assert resolve_role({"groups": "finance-eng other"}) == "analyst"
    assert resolve_role({"groups": "other,finance-eng"}) == "analyst"


def test_custom_groups_claim(monkeypatch):
    monkeypatch.setenv("FINOPS_SSO_ANALYST_GROUPS", "eng")
    monkeypatch.setenv("FINOPS_SSO_GROUPS_CLAIM", "https://corp/claims/roles")
    assert resolve_role({"https://corp/claims/roles": ["eng"]}) == "analyst"
    assert resolve_role({"groups": ["eng"]}) == "viewer"  # wrong claim ignored


def test_end_to_end_analyst_session_cannot_run_admin_tools():
    """The full SEC-2 chain: a mapped analyst identity is denied by require_role
    even on a permissive single-tenant box, while the ownerless local path
    (ident None) stays wide open, that is the lock-out guarantee."""
    analyst = Identity(key_id=0, name="dashboard", email="", role="analyst",
                       scope_team=None, scope_provider=None)
    denied = require_role("admin", analyst)
    assert denied and denied["error"] == "insufficient_role"
    assert require_role("analyst", analyst) is None   # own level still works
    assert require_role("admin", None) is None        # box owner untouched
