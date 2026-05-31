"""
OIDC / OAuth2 SSO for the nable dashboard.

Required env vars (all three must be set to enable SSO):
    FINOPS_SSO_ISSUER          OIDC issuer URL
                               Okta:     https://dev-xyz.okta.com
                               Azure AD: https://login.microsoftonline.com/{tenant}/v2.0
                               Google:   https://accounts.google.com
    FINOPS_SSO_CLIENT_ID       OAuth2 client ID
    FINOPS_SSO_CLIENT_SECRET   OAuth2 client secret

Optional:
    FINOPS_SSO_REDIRECT_URI    defaults to http://localhost:8080/sso/callback
    FINOPS_SSO_ALLOWED_DOMAINS comma-separated; if set, only emails from these domains may sign in
                               e.g. acme.com,acme-corp.com
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("finops.auth.sso")

SSO_ISSUER: str = os.environ.get("FINOPS_SSO_ISSUER", "").rstrip("/")
SSO_CLIENT_ID: str = os.environ.get("FINOPS_SSO_CLIENT_ID", "")
SSO_CLIENT_SECRET: str = os.environ.get("FINOPS_SSO_CLIENT_SECRET", "")
SSO_REDIRECT_URI: str = os.environ.get(
    "FINOPS_SSO_REDIRECT_URI", "http://localhost:8080/sso/callback"
)
SSO_ALLOWED_DOMAINS: list[str] = [
    d.strip().lower()
    for d in os.environ.get("FINOPS_SSO_ALLOWED_DOMAINS", "").split(",")
    if d.strip()
]

SSO_ENABLED: bool = bool(SSO_ISSUER and SSO_CLIENT_ID and SSO_CLIENT_SECRET)

# Cached OIDC discovery document and JWKS — TTL-bounded to handle key rotation
_DISCOVERY: dict[str, Any] | None = None
_DISCOVERY_EXP: float = 0.0
_JWKS: dict[str, Any] | None = None
_JWKS_EXP: float = 0.0
_DISCOVERY_TTL: int = 3600      # 1 hour
_JWKS_TTL: int = 3600           # 1 hour (IdPs rotate keys every 6-24h)

# Pending auth states: state_value -> expiry (unix ts)
_SSO_STATES: dict[str, float] = {}
_STATE_TTL: int = 600  # 10 minutes


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _fetch_json(url: str, data: bytes | None = None, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode()
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc


# ── OIDC discovery ────────────────────────────────────────────────────────────

def discovery() -> dict[str, Any]:
    global _DISCOVERY, _DISCOVERY_EXP
    if _DISCOVERY is None or time.time() > _DISCOVERY_EXP:
        url = f"{SSO_ISSUER}/.well-known/openid-configuration"
        try:
            _DISCOVERY = _fetch_json(url)
            _DISCOVERY_EXP = time.time() + _DISCOVERY_TTL
        except Exception as exc:
            raise RuntimeError(f"OIDC discovery failed for {SSO_ISSUER!r}: {exc}") from exc
    return _DISCOVERY


def _jwks() -> dict[str, Any]:
    global _JWKS, _JWKS_EXP
    if _JWKS is None or time.time() > _JWKS_EXP:
        jwks_uri = discovery()["jwks_uri"]
        _JWKS = _fetch_json(jwks_uri)
        _JWKS_EXP = time.time() + _JWKS_TTL
    return _JWKS


# ── JWT helpers ───────────────────────────────────────────────────────────────

def _b64_decode(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _find_jwk(kid: str | None) -> dict[str, Any]:
    """Find the signing key matching kid. Refreshes JWKS once on miss."""
    global _JWKS, _JWKS_EXP
    for attempt in range(2):
        keys = _jwks().get("keys", [])
        for k in keys:
            if kid and k.get("kid") == kid:
                return k
            if not kid:
                # Accept any RSA sig key; fall back to any RSA key if use field is absent
                if k.get("use") == "sig" or (k.get("kty") == "RSA" and "use" not in k):
                    return k
        if attempt == 0:
            _JWKS = None   # force re-fetch on next _jwks() call
            _JWKS_EXP = 0.0
    raise ValueError(f"No matching JWK found for kid={kid!r}")


def _verify_jwt(token: str) -> dict[str, Any]:
    """Verify RS256/384/512 JWT signature and return claims. Raises on failure."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed JWT")

    header = json.loads(_b64_decode(parts[0]))
    alg = header.get("alg", "RS256")
    if alg not in ("RS256", "RS384", "RS512"):
        raise ValueError(f"Unsupported JWT algorithm: {alg}")

    key_data = _find_jwk(header.get("kid"))

    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

    n = int.from_bytes(_b64_decode(key_data["n"]), "big")
    e = int.from_bytes(_b64_decode(key_data["e"]), "big")
    pub_key = RSAPublicNumbers(e, n).public_key(default_backend())

    signing_input = f"{parts[0]}.{parts[1]}".encode()
    sig = _b64_decode(parts[2])
    hash_map = {"RS256": hashes.SHA256(), "RS384": hashes.SHA384(), "RS512": hashes.SHA512()}
    pub_key.verify(sig, signing_input, padding.PKCS1v15(), hash_map[alg])

    claims: dict[str, Any] = json.loads(_b64_decode(parts[1]))

    now = time.time()
    if claims.get("exp", 0) < now:
        raise ValueError("Token expired")
    if claims.get("nbf", now) > now + 30:
        raise ValueError("Token not yet valid")

    # Validate issuer
    iss = claims.get("iss", "").rstrip("/")
    if SSO_ISSUER and iss != SSO_ISSUER:
        raise ValueError(f"Issuer mismatch: {iss!r}")

    # Validate audience
    aud = claims.get("aud", "")
    if isinstance(aud, list):
        if SSO_CLIENT_ID not in aud:
            raise ValueError("Client ID not in token audience")
    elif aud != SSO_CLIENT_ID:
        raise ValueError(f"Audience mismatch: {aud!r}")

    return claims


# ── Public API ────────────────────────────────────────────────────────────────

def build_auth_url() -> str:
    """Build the IdP authorization URL and store the state nonce."""
    state = secrets.token_urlsafe(32)
    now = time.time()
    _SSO_STATES[state] = now + _STATE_TTL
    # Prune expired states
    for s in [k for k, exp in _SSO_STATES.items() if now > exp]:
        _SSO_STATES.pop(s, None)

    auth_ep = discovery()["authorization_endpoint"]
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": SSO_CLIENT_ID,
        "redirect_uri": SSO_REDIRECT_URI,
        "scope": "openid email profile",
        "state": state,
    })
    return f"{auth_ep}?{params}"


def exchange_code(code: str, state: str) -> dict[str, str]:
    """Exchange authorization code for identity. Returns {email, name, sub}."""
    exp = _SSO_STATES.pop(state, None)
    if exp is None or time.time() > exp:
        raise ValueError("Invalid or expired state parameter")

    token_ep = discovery()["token_endpoint"]
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": SSO_REDIRECT_URI,
        "client_id": SSO_CLIENT_ID,
        "client_secret": SSO_CLIENT_SECRET,
    }).encode()
    token_resp = _fetch_json(
        token_ep,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    id_token = token_resp.get("id_token")
    if not id_token:
        raise ValueError("No id_token in token response")

    claims = _verify_jwt(id_token)

    email: str = claims.get("email", "")
    if claims.get("email_verified") is False:
        raise ValueError("Email address is not verified by the IdP")

    if SSO_ALLOWED_DOMAINS:
        domain = email.split("@")[-1].lower() if "@" in email else ""
        if domain not in SSO_ALLOWED_DOMAINS:
            raise ValueError(f"Email domain not allowed: {domain!r}")

    name: str = claims.get("name") or claims.get("preferred_username") or email
    return {"email": email, "name": name, "sub": claims.get("sub", "")}
