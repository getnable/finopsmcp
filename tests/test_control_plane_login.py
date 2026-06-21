"""
Tests for the control-plane login primitive (auth/control_plane.py).

The dashboard lets getnable.com sign a short-lived, single-use, per-instance
token that mints a session. These pin the token format and the security
properties: signature, expiry, instance binding, role, single-use, and the
off-by-default gating.
"""
from __future__ import annotations

import json

import finops.auth.control_plane as cp

SECRET = "test-instance-secret-abc123"
INSTANCE = "inst_acme"


def _raw_token(secret: str, payload: dict) -> str:
    """Assemble a token from an arbitrary payload, for edge cases mint_token will
    not produce (missing fields, bad role). The signature is always valid for the
    payload, so the rejection comes from the field check, not a bad signature."""
    payload_b64 = cp._b64url_encode(json.dumps(payload).encode("utf-8"))
    return f"{payload_b64}.{cp._sign(secret, payload_b64)}"


def test_valid_token_verifies():
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE,
                      role="analyst", now=1000)
    out = cp.verify_token(SECRET, t, INSTANCE, now=1000)
    assert out is not None
    assert out["email"] == "a@acme.com"
    assert out["role"] == "analyst"
    assert out["instance_id"] == INSTANCE


def test_tampered_signature_rejected():
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="admin", now=1000)
    body, sig = t.rsplit(".", 1)
    assert cp.verify_token(SECRET, body + "." + ("0" * len(sig)), INSTANCE, now=1000) is None


def test_tampered_payload_rejected():
    # Swap the payload but keep the original signature: it no longer matches.
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="viewer", now=1000)
    _, sig = t.rsplit(".", 1)
    evil = cp._b64url_encode(json.dumps(
        {"email": "evil@x.com", "instance_id": INSTANCE, "role": "admin",
         "exp": 9999999999, "jti": "x"}).encode())
    assert cp.verify_token(SECRET, f"{evil}.{sig}", INSTANCE, now=1000) is None


def test_expired_token_rejected():
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="analyst",
                      ttl_seconds=60, now=1000)
    assert cp.verify_token(SECRET, t, INSTANCE, now=1000 + 61) is None


def test_wrong_instance_rejected():
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="analyst", now=1000)
    assert cp.verify_token(SECRET, t, "inst_other", now=1000) is None


def test_wrong_secret_rejected():
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="analyst", now=1000)
    assert cp.verify_token("different-secret", t, INSTANCE, now=1000) is None


def test_invalid_role_rejected():
    t = _raw_token(SECRET, {"email": "a@acme.com", "instance_id": INSTANCE,
                            "role": "superuser", "exp": 9999999999, "jti": "j-role"})
    assert cp.verify_token(SECRET, t, INSTANCE, now=1000) is None


def test_missing_email_rejected():
    t = _raw_token(SECRET, {"instance_id": INSTANCE, "role": "admin",
                            "exp": 9999999999, "jti": "j-email"})
    assert cp.verify_token(SECRET, t, INSTANCE, now=1000) is None


def test_missing_jti_rejected():
    t = _raw_token(SECRET, {"email": "a@acme.com", "instance_id": INSTANCE,
                            "role": "admin", "exp": 9999999999})
    assert cp.verify_token(SECRET, t, INSTANCE, now=1000) is None


def test_replay_rejected():
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="admin",
                      jti="unique-jti-replay", ttl_seconds=300, now=1000)
    assert cp.verify_token(SECRET, t, INSTANCE, now=1000) is not None
    # The same token again: the single-use guard rejects it.
    assert cp.verify_token(SECRET, t, INSTANCE, now=1000) is None


def test_malformed_or_empty_token_rejected():
    assert cp.verify_token(SECRET, "no-dot-here", INSTANCE, now=1000) is None
    assert cp.verify_token(SECRET, "", INSTANCE, now=1000) is None


def test_empty_secret_or_instance_rejected():
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="admin", now=1000)
    assert cp.verify_token("", t, INSTANCE, now=1000) is None
    assert cp.verify_token(SECRET, t, "", now=1000) is None


def test_viewer_role_round_trips():
    t = cp.mint_token(SECRET, email="v@acme.com", instance_id=INSTANCE, role="viewer", now=1000)
    out = cp.verify_token(SECRET, t, INSTANCE, now=1000)
    assert out is not None and out["role"] == "viewer"


def test_is_enabled_requires_both(monkeypatch):
    monkeypatch.delenv("FINOPS_CONTROL_PLANE_SECRET", raising=False)
    monkeypatch.delenv("FINOPS_INSTANCE_ID", raising=False)
    assert cp.is_enabled() is False
    monkeypatch.setenv("FINOPS_CONTROL_PLANE_SECRET", "s")
    assert cp.is_enabled() is False  # instance id still missing
    monkeypatch.setenv("FINOPS_INSTANCE_ID", "i")
    assert cp.is_enabled() is True


def test_verify_request_token_uses_env(monkeypatch):
    monkeypatch.setenv("FINOPS_CONTROL_PLANE_SECRET", SECRET)
    monkeypatch.setenv("FINOPS_INSTANCE_ID", INSTANCE)
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="viewer",
                      jti="env-jti-ok", now=1000)
    out = cp.verify_request_token(t, now=1000)
    assert out is not None and out["role"] == "viewer"


def test_verify_request_token_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("FINOPS_CONTROL_PLANE_SECRET", raising=False)
    monkeypatch.delenv("FINOPS_INSTANCE_ID", raising=False)
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="admin",
                      jti="env-jti-disabled", now=1000)
    assert cp.verify_request_token(t, now=1000) is None
