"""SEC-2: require_role must honor an explicitly attached identity even in
permissive (single-tenant SQLite) mode, while staying wide open when no identity
is attached, so the box owner is never locked out.

Before this fix, require_role returned None unconditionally whenever auth was not
required (no Postgres / FINOPS_REQUIRE_AUTH unset), so a sub-admin dashboard
session ran every tool as admin. Now an attached identity is enforced in both
modes; absence of one keeps the owner unrestricted.
"""
import pytest

from finops.auth.rbac import Identity, require_role, set_current_identity


def _ident(role: str) -> Identity:
    return Identity(key_id=0, name="t", email="", role=role,
                    scope_team=None, scope_provider=None)


@pytest.fixture(autouse=True)
def _permissive_env(monkeypatch):
    # Force permissive single-tenant mode and a clean identity for each test.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINOPS_REQUIRE_AUTH", raising=False)
    set_current_identity(None)
    yield
    set_current_identity(None)


def test_permissive_no_identity_is_open():
    # The single-tenant owner: no identity attached -> unrestricted, as before.
    assert require_role("viewer") is None
    assert require_role("analyst") is None
    assert require_role("admin") is None


def test_permissive_attached_viewer_blocks_admin_tool():
    set_current_identity(_ident("viewer"))
    err = require_role("admin")
    assert err is not None
    assert err["error"] == "insufficient_role"
    assert err["your_role"] == "viewer"
    assert err["required_role"] == "admin"
    # A viewer can still run viewer-level tools.
    assert require_role("viewer") is None


def test_permissive_attached_analyst_respects_hierarchy():
    set_current_identity(_ident("analyst"))
    assert require_role("viewer") is None     # below
    assert require_role("analyst") is None    # equal
    assert require_role("admin") is not None  # above -> blocked


def test_permissive_explicit_ident_argument_enforced():
    # Passing ident explicitly enforces too, even with nothing in the context.
    assert require_role("admin", _ident("viewer")) is not None
    assert require_role("viewer", _ident("viewer")) is None


def test_strict_mode_unchanged_requires_auth(monkeypatch):
    # Shared Postgres + FINOPS_REQUIRE_AUTH=1, no identity -> auth_required, as before.
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("FINOPS_REQUIRE_AUTH", "1")
    set_current_identity(None)
    err = require_role("viewer")
    assert err is not None
    assert err["error"] == "auth_required"
