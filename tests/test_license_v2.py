"""Tests for v2 (Ed25519) license keys.

The whole point of v2: a client validates a key with the bundled PUBLIC key and
needs no shared secret, and nobody can forge a key without the private key.
"""
import importlib

import pytest

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


def test_v1_still_works_with_secret(monkeypatch):
    """Legacy HMAC keys still validate when the secret is present (backward compat)."""
    L = _license(monkeypatch, secret="unit-test-secret")
    key = L.generate_key("user@example.com", version=1)
    assert key.startswith("FINOPS-1-")
    assert L.validate_key(key).mode == "pro"
