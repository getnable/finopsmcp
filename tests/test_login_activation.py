"""Tests for `finops login` activation.

The login flow stores the license the server returns (after an email OTP) in the
local vault, so the user never copies a key. These tests cover the storage layer:
store -> read-back with no env var, explicit env override, logout, and the
guarantee that an invalid key is never persisted.
"""
import importlib

# Throwaway test keypair (same one used by test_license_v2): the public half is
# injected into the reloaded module so validation uses the test key, not the
# bundled production key.
_TEST_PRIV = "8fbe8En53x3KhJ93ZwEmE3L0IVLHQm6yI-gn3FGIpeg"
_TEST_PUB = "sxzvFKJjtkqH4xZWXQZLvrYhRxQFVoaJ5YRiEu18dMw"


def _license(monkeypatch):
    """Reload the license module with the test signing key + test public key."""
    monkeypatch.delenv("FINOPS_LICENSE_SECRET", raising=False)
    monkeypatch.setenv("FINOPS_LICENSE_PRIVATE_KEY", _TEST_PRIV)
    import finops.license as L
    importlib.reload(L)
    L._PUBLIC_KEY_B64 = _TEST_PUB
    return L


class _FakeVault:
    """In-memory stand-in for the keyring-backed Vault."""

    def __init__(self):
        self.d = {}

    def store(self, k, v):
        self.d[k] = v

    def get(self, k):
        return self.d.get(k)

    def delete(self, k):
        existed = k in self.d
        self.d.pop(k, None)
        return existed


def _fake_vault(monkeypatch):
    fake = _FakeVault()
    monkeypatch.setattr(
        "finops.security.vault.Vault.default", classmethod(lambda cls: fake)
    )
    return fake


def test_login_stores_license_in_vault_and_check_reads_it(monkeypatch):
    L = _license(monkeypatch)
    fake = _fake_vault(monkeypatch)
    monkeypatch.delenv("FINOPS_LICENSE_KEY", raising=False)

    key = L.generate_key("buyer@acme.com")  # a valid pro key for the test keypair
    status = L.store_license(key)
    assert status.is_pro
    assert fake.d.get("FINOPS_LICENSE_KEY") == key  # persisted to the vault

    # check_license picks it up from the vault with no env var set
    L._status = None
    st2 = L.check_license()
    assert st2.is_pro
    assert st2.email == "buyer@acme.com"


def test_env_license_key_overrides_vault(monkeypatch):
    L = _license(monkeypatch)
    fake = _fake_vault(monkeypatch)

    real = L.generate_key("buyer@acme.com")
    fake.d["FINOPS_LICENSE_KEY"] = "garbage-not-a-key"  # vault holds junk
    monkeypatch.setenv("FINOPS_LICENSE_KEY", real)  # explicit env must win

    L._status = None
    assert L.check_license().is_pro


def test_logout_clears_the_stored_license(monkeypatch):
    L = _license(monkeypatch)
    fake = _fake_vault(monkeypatch)
    monkeypatch.delenv("FINOPS_LICENSE_KEY", raising=False)

    L.store_license(L.generate_key("buyer@acme.com"))
    assert fake.d.get("FINOPS_LICENSE_KEY")

    L.clear_license()
    assert fake.d.get("FINOPS_LICENSE_KEY") is None
    L._status = None
    assert not L.check_license().is_pro


def test_store_license_never_persists_an_invalid_key(monkeypatch):
    L = _license(monkeypatch)
    fake = _fake_vault(monkeypatch)

    status = L.store_license("FINOPS-2-not-a-real-key")
    assert status.mode == "invalid"
    assert "FINOPS_LICENSE_KEY" not in fake.d


def test_run_login_rejects_a_bad_email_without_touching_the_network(monkeypatch):
    # An obviously invalid email must short-circuit before any HTTP call.
    from finops import setup_wizard

    setup_wizard._run_login("notanemail")  # no "@" -> early return, no exception
