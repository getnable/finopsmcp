"""Tests for v2 (Ed25519) license keys.

The whole point of v2: a client validates a key with the bundled PUBLIC key and
needs no shared secret, and nobody can forge a key without the private key.
"""
import importlib
import json as _json
from datetime import date, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Throwaway test keypair, generated solely for tests and unrelated to any
# production key. The public half is genuinely injected into the module (see
# _license) so these tests verify with the test key, never the shipped one.
# (A prior version pasted the real production private key here, which leaked it;
# this pair derives a public key that is NOT the bundled one, by design.)
_TEST_PRIV = "8fbe8En53x3KhJ93ZwEmE3L0IVLHQm6yI-gn3FGIpeg"
_TEST_PUB = "sxzvFKJjtkqH4xZWXQZLvrYhRxQFVoaJ5YRiEu18dMw"


def _license(monkeypatch, secret=None, priv=_TEST_PRIV):
    """Reload the license module with a controlled environment, then inject the
    test public key so verification does not depend on the bundled production key."""
    if secret is None:
        monkeypatch.delenv("FINOPS_LICENSE_SECRET", raising=False)
    else:
        monkeypatch.setenv("FINOPS_LICENSE_SECRET", secret)
    if priv is None:
        monkeypatch.delenv("FINOPS_LICENSE_PRIVATE_KEY", raising=False)
    else:
        monkeypatch.setenv("FINOPS_LICENSE_PRIVATE_KEY", priv)
    import finops.license as L
    importlib.reload(L)
    L._PUBLIC_KEY_B64 = _TEST_PUB  # verify against the test key, not production
    return L


def test_v2_validates_with_no_secret(monkeypatch):
    """A v2 key validates as pro using only the bundled public key — no secret."""
    L = _license(monkeypatch, secret=None)
    assert not L._SECRET  # validating side has no shared secret
    key = L.generate_key("user@example.com")
    assert key.startswith("FINOPS-2-")
    st = L.validate_key(key)
    assert st.mode == "pro"
    assert st.is_pro
    assert st.email == "user@example.com"


def test_v2_tampered_payload_rejected(monkeypatch):
    L = _license(monkeypatch, secret=None)
    key = L.generate_key("user@example.com")
    flipped = "A" if key[12] != "A" else "B"
    bad = key[:12] + flipped + key[13:]
    assert L.validate_key(bad).mode == "invalid"


# Compromised seed that was once committed in this repo (public GitHub history).
# Production rotated away from it on 2026-06-09. This test fails loudly if the
# bundled public key is ever reverted to the one that seed signs for.
_LEAKED_SEED = "8HW1kTWT2OIuBRaBY-YcfmweY9hoECjF7uedaJfzID4"


def test_leaked_seed_cannot_forge_against_bundled_key(monkeypatch):
    """A key signed by the historically-leaked private seed must NOT validate
    against the bundled production public key. Guards the key rotation."""
    monkeypatch.setenv("FINOPS_LICENSE_PRIVATE_KEY", _LEAKED_SEED)
    import finops.license as L
    importlib.reload(L)
    # Note: no public-key injection — verify against the REAL bundled key.
    forged = L.generate_key("attacker@evil.com", plan="enterprise")
    assert L.validate_key(forged).mode == "invalid"
    importlib.reload(L)  # restore clean module state for later tests


def test_v2_forged_signature_rejected(monkeypatch):
    L = _license(monkeypatch, secret=None)
    key = L.generate_key("user@example.com")
    forged = "-".join(key.split("-")[:3]) + "-Zm9yZ2Vkc2ln"
    assert L.validate_key(forged).mode == "invalid"


def test_generating_v2_requires_private_key(monkeypatch):
    """Without the private key, no v2 key can be minted (signing stays server-side)."""
    L = _license(monkeypatch, secret=None, priv=None)
    with pytest.raises(RuntimeError):
        L.generate_key("user@example.com")


def test_v1_keys_are_retired(monkeypatch):
    """The v1 HMAC secret leaked in public git history, so v1 keys are
    forgeable: generation refuses and validation rejects them."""
    import pytest as _pytest
    L = _license(monkeypatch, secret="unit-test-secret")
    with _pytest.raises(ValueError):
        L.generate_key("user@example.com", version=1)
    fake_v1 = "FINOPS-1-eyJlIjoiYSJ9-c2ln"
    status = L.validate_key(fake_v1)
    assert status.mode == "invalid"
    assert "retired" in status.message


# ── explicit per-cycle expiry ("x") ───────────────────────────────────────────
# Keys now carry an explicit expiry the webhook derives from the billing cycle,
# so a lapsed monthly subscription stops within the cycle instead of 366 days
# later. Keys without "x" keep the legacy TTL behavior (backward compatible).


def _mint(L, fields):
    """Sign an arbitrary v2 payload the way the Stripe webhook emits it: compact
    JSON (no spaces, like JS JSON.stringify), Ed25519 over "2:{payload}". Lets a
    test control the issue date "d" and explicit expiry "x" precisely."""
    priv = Ed25519PrivateKey.from_private_bytes(L._unb64(_TEST_PRIV))
    payload = L._b64(_json.dumps(fields, separators=(",", ":")).encode())
    sig = L._b64(priv.sign(f"2:{payload}".encode()))
    return f"FINOPS-2-{payload}-{sig}"


def test_explicit_expiry_future_is_valid(monkeypatch):
    L = _license(monkeypatch)
    key = L.generate_key("user@example.com", expiry_days=30)
    st = L.validate_key(key)
    assert st.mode == "pro" and st.is_pro


def test_explicit_expiry_past_is_expired(monkeypatch):
    L = _license(monkeypatch)
    key = L.generate_key("user@example.com", expiry_days=-1)
    st = L.validate_key(key)
    assert st.mode == "invalid"
    assert "expired" in st.message.lower()


def test_explicit_expiry_overrides_legacy_ttl(monkeypatch):
    """Issued today but with "x" in the past: expired, even though the 366-day
    TTL window is wide open. This is what makes a monthly key die with the cycle."""
    L = _license(monkeypatch)
    today = date.today().strftime("%Y%m%d")
    past = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    key = _mint(L, {"e": "user@example.com", "d": today, "p": "pro", "x": past})
    st = L.validate_key(key)
    assert st.mode == "invalid"
    assert "expired" in st.message.lower()


def test_webhook_wire_format_with_expiry_validates(monkeypatch):
    """Cross-language parity: a key minted exactly the way the JS webhook emits
    it (compact JSON, fields e/d/p/x) validates and honors the embedded expiry."""
    L = _license(monkeypatch)
    today = date.today().strftime("%Y%m%d")
    future = (date.today() + timedelta(days=41)).strftime("%Y%m%d")
    key = _mint(L, {"e": "team@acme.com", "d": today, "p": "team", "x": future})
    st = L.validate_key(key)
    assert st.mode == "team"
    assert st.email == "team@acme.com"


def test_legacy_key_without_expiry_uses_ttl(monkeypatch):
    """No "x": a key issued well within the 366-day window stays valid. The
    legacy fallback path is unchanged for keys minted before this field existed."""
    L = _license(monkeypatch)
    recent = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
    key = _mint(L, {"e": "user@example.com", "d": recent, "p": "pro"})
    assert L.validate_key(key).mode == "pro"


def test_legacy_key_beyond_ttl_is_expired(monkeypatch):
    """No "x", issued more than 366 days ago: still expires via the legacy TTL."""
    L = _license(monkeypatch)
    old = (date.today() - timedelta(days=400)).strftime("%Y%m%d")
    key = _mint(L, {"e": "user@example.com", "d": old, "p": "pro"})
    st = L.validate_key(key)
    assert st.mode == "invalid"
    assert "expired" in st.message.lower()
