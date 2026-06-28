"""The OS keychain is read at most once per process.

Vault.default() and the license trial check cache the master key / trial date
in-process, so a long-running server does not re-read the keychain on every
call. Before this, macOS re-prompted the user ("python wants to use your
confidential information") every few minutes. Regression guard for that fix.
"""
import base64
import sys
from datetime import date

from cryptography.fernet import Fernet


def _install_fake_keyring(monkeypatch, value):
    """Replace the `keyring` module with a counting stub that returns `value`."""
    calls = {"n": 0}

    class _FakeKeyring:
        @staticmethod
        def get_password(service, user):
            calls["n"] += 1
            return value

        @staticmethod
        def set_password(service, user, val):
            pass

    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring)
    return calls


def test_vault_master_key_read_is_cached(monkeypatch):
    from finops.security import vault as V

    V.Vault._key_cache.clear()
    key = Fernet.generate_key()
    calls = _install_fake_keyring(monkeypatch, base64.urlsafe_b64encode(key).decode())
    try:
        assert V.Vault._try_keyring() == key
        assert V.Vault._try_keyring() == key  # second call served from the cache
        assert calls["n"] == 1
    finally:
        V.Vault._key_cache.clear()


def test_license_trial_date_read_is_cached(monkeypatch):
    import finops.license as L

    L._kr_cached_date = None
    d = date(2026, 1, 1)
    signed = f"{d.isoformat()}:{L._sign_date(d.isoformat())}"
    calls = _install_fake_keyring(monkeypatch, signed)
    try:
        assert L._kr_get() == d
        assert L._kr_get() == d  # second call served from the cache
        assert calls["n"] == 1
    finally:
        L._kr_cached_date = None
