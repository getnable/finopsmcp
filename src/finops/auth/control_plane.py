"""
Control-plane login for the hosted dashboard.

A nable instance can let getnable.com act as its identity provider. The control
plane signs a short-lived, single-use token scoped to ONE instance, then
redirects the browser to /auth/cp on that instance. The instance verifies the
signature against its own per-instance secret, checks the token is for itself
and not expired or replayed, then mints a normal dashboard session. No password
or per-instance SSO setup is needed: the getnable.com login is the auth.

Security model:
  - Per-instance HMAC secret (FINOPS_CONTROL_PLANE_SECRET). The control plane
    holds the same secret for this one customer, so a token minted for instance
    A cannot open instance B. There is no shared key across tenants.
  - Short-lived (the control plane sets exp, the instance enforces it) and
    single-use (a random jti is recorded until it expires, so a captured token
    cannot be replayed inside its window).
  - Bound to this instance (FINOPS_INSTANCE_ID) and carries a role the dashboard
    maps to a full or read-only session.
  - Off by default: disabled unless BOTH the secret and the instance id are set,
    so every existing password or SSO deploy is untouched.

The control plane never holds the instance's raw cloud credentials. This token
only grants access. The bills and keys stay on the instance.

Token format (the same shape as the getnable.com account token, so the control
plane signs it the same way in JavaScript):

    base64url(json_payload) + "." + hex(HMAC-SHA256(secret, base64url_payload))

Payload: {"email", "instance_id", "role", "exp", "jti"}.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time

_VALID_ROLES = ("viewer", "analyst", "admin")

# Single-use guard: jti -> expiry (unix). A token is redeemable once inside its
# short lifetime; a replay within the window is rejected. The dashboard runs on a
# threaded server, so a lock guards the store.
_SEEN_JTIS: dict[str, float] = {}
_JTI_LOCK = threading.Lock()


def _secret() -> str:
    return os.environ.get("FINOPS_CONTROL_PLANE_SECRET", "").strip()


def _instance_id() -> str:
    return os.environ.get("FINOPS_INSTANCE_ID", "").strip()


def is_enabled() -> bool:
    """Control-plane login is on only when both the per-instance secret and the
    instance id are set. Either one alone fails closed (stays disabled)."""
    return bool(_secret()) and bool(_instance_id())


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(secret: str, payload_b64: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()


def mint_token(
    secret: str,
    *,
    email: str,
    instance_id: str,
    role: str,
    ttl_seconds: int = 60,
    jti: str | None = None,
    now: float | None = None,
) -> str:
    """Sign a control-plane access token. The getnable.com control plane produces
    the same shape in JavaScript. This exists for tests and to pin the format."""
    now = time.time() if now is None else now
    if jti is None:
        jti = _b64url_encode(os.urandom(16))
    payload = {
        "email": email,
        "instance_id": instance_id,
        "role": role,
        "exp": int(now + ttl_seconds),
        "jti": jti,
    }
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    return f"{payload_b64}.{_sign(secret, payload_b64)}"


def verify_token(
    secret: str, token: str, expected_instance_id: str, *, now: float | None = None
) -> dict | None:
    """Verify a control-plane token. Return the payload dict on success, or None
    on any failure (bad signature, expired, wrong instance, bad role, missing
    field, replay). Constant-time signature compare; single-use via the jti."""
    if not secret or not token or not expected_instance_id:
        return None
    try:
        payload_b64, sig_hex = token.rsplit(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(secret, payload_b64), sig_hex):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    now = time.time() if now is None else now
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or now > exp:
        return None
    if payload.get("instance_id") != expected_instance_id:
        return None
    role = payload.get("role")
    if role not in _VALID_ROLES:
        return None
    email = payload.get("email")
    if not email or not isinstance(email, str):
        return None
    jti = payload.get("jti")
    if not jti or not isinstance(jti, str):
        return None

    # Single-use: reject a replay, otherwise record the jti until it expires.
    with _JTI_LOCK:
        for old in [k for k, e in _SEEN_JTIS.items() if e < now]:
            _SEEN_JTIS.pop(old, None)
        if jti in _SEEN_JTIS:
            return None
        _SEEN_JTIS[jti] = exp

    return {
        "email": email,
        "instance_id": expected_instance_id,
        "role": role,
        "exp": exp,
        "jti": jti,
    }


def verify_request_token(token: str, *, now: float | None = None) -> dict | None:
    """Verify a token against this instance's configured secret and id. The
    dashboard route uses this so it never handles the raw secret directly.
    Returns None when control-plane login is disabled."""
    if not is_enabled():
        return None
    return verify_token(_secret(), token, _instance_id(), now=now)
