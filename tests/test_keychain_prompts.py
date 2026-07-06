"""Steady state never touches the OS keychain.

macOS shows a permission dialog whenever an unsigned interpreter reads a keychain
item it does not own, and uvx creates a new interpreter per release, so any
per-session keychain access becomes a recurring "python wants to access ..."
prompt. Worse, rewriting an item recreates it and resets its ACL, so "Always
Allow" never sticks.

Regression guards for the fix:
- The trial check resolves from the signed file without importing keyring, and
  never rewrites the keychain item once created.
- The keychain is recovery only: it restores a deleted trial file, and its value
  is re-cached to the vault key file after a read.
- The legacy disguised trial entry (system.cache.prefs) migrates to nable-trial.
- The vault resolves from FINOPS_VAULT_KEY, then vault.key, then the keyring.
"""
import base64
import sys
from datetime import date

import pytest
from cryptography.fernet import Fernet

import finops.license as L
from finops.security import vault as V


class _CountingKeyring:
    """keyring stand-in backed by a dict, counting every touch."""

    def __init__(self, store=None):
        self.store = dict(store or {})
        self.gets = 0
        self.sets = 0
        self.deletes = 0

    def get_password(self, service, user):
        self.gets += 1
        return self.store.get((service, user))

    def set_password(self, service, user, val):
        self.sets += 1
        self.store[(service, user)] = val

    def delete_password(self, service, user):
        self.deletes += 1
        self.store.pop((service, user), None)

    @property
    def touches(self):
        return self.gets + self.sets + self.deletes


@pytest.fixture
def kr(monkeypatch):
    fake = _CountingKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return fake


@pytest.fixture
def trial_file(monkeypatch, tmp_path):
    monkeypatch.setattr(L, "_TRIAL_FILE", tmp_path / "trial_start")
    monkeypatch.setattr(L, "_kr_cached_date", None)
    return L._TRIAL_FILE


def _signed(d: date) -> str:
    iso = d.isoformat()
    return f"{iso}:{L._sign_date(iso)}"


# ── Trial store ───────────────────────────────────────────────────────────────

def test_trial_steady_state_never_touches_keychain(kr, trial_file):
    d = date(2026, 6, 1)
    L._file_set(d)
    assert L._get_or_create_trial_start() == d
    assert kr.touches == 0


def test_trial_recovers_deleted_file_from_keychain_without_rewrite(kr, trial_file):
    d = date(2026, 6, 1)
    kr.store[(L._KR_SERVICE, L._KR_USERNAME)] = _signed(d)
    assert not trial_file.exists()
    assert L._get_or_create_trial_start() == d
    assert trial_file.exists() and L._file_get() == d  # file restored
    assert kr.sets == 0  # the keychain item is never rewritten


def test_trial_creation_writes_keychain_exactly_once(kr, trial_file):
    today = date.today()
    assert L._get_or_create_trial_start() == today
    assert kr.sets == 1
    assert L._file_get() == today
    # Subsequent checks resolve from the file: zero further keychain touches.
    touches_before = kr.touches
    L._kr_cached_date = None
    assert L._get_or_create_trial_start() == today
    assert kr.touches == touches_before


def test_trial_legacy_disguised_entry_migrates(kr, trial_file):
    d = date(2026, 5, 20)
    kr.store[(L._KR_LEGACY_SERVICE, L._KR_LEGACY_USERNAME)] = _signed(d)
    assert L._get_or_create_trial_start() == d
    assert kr.store.get((L._KR_SERVICE, L._KR_USERNAME)) == _signed(d)
    assert (L._KR_LEGACY_SERVICE, L._KR_LEGACY_USERNAME) not in kr.store


def test_trial_tampered_file_falls_back_to_keychain(kr, trial_file):
    d = date(2026, 6, 1)
    kr.store[(L._KR_SERVICE, L._KR_USERNAME)] = _signed(d)
    trial_file.parent.mkdir(parents=True, exist_ok=True)
    trial_file.write_text("2030-01-01\nforged-signature\n")
    assert L._get_or_create_trial_start() == d


# ── Vault master key ──────────────────────────────────────────────────────────

@pytest.fixture
def vault_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    monkeypatch.delenv("FINOPS_VAULT_KEY", raising=False)
    monkeypatch.delenv("FINOPS_VAULT_KEYCHAIN_ONLY", raising=False)
    V.Vault._key_cache.clear()
    yield tmp_path
    V.Vault._key_cache.clear()


def test_vault_key_file_short_circuits_keychain(kr, vault_env):
    key = Fernet.generate_key()
    (vault_env / "vault.key").write_bytes(key)
    v = V.Vault.default()
    assert v._key == key
    assert kr.touches == 0


def test_vault_keychain_read_recaches_to_file(kr, vault_env):
    key = Fernet.generate_key()
    kr.store[(V._keyring_service(), V._KEYRING_USER)] = base64.urlsafe_b64encode(key).decode()
    v = V.Vault.default()
    assert v._key == key
    assert (vault_env / "vault.key").read_bytes() == key  # re-cached
    # Next open resolves from the file, even with a cold in-process cache.
    V.Vault._key_cache.clear()
    gets_before = kr.gets
    v2 = V.Vault.default()
    assert v2._key == key and kr.gets == gets_before


def test_vault_env_var_beats_file(kr, vault_env, monkeypatch):
    env_key = Fernet.generate_key()
    file_key = Fernet.generate_key()
    (vault_env / "vault.key").write_bytes(file_key)
    monkeypatch.setenv("FINOPS_VAULT_KEY", base64.urlsafe_b64encode(env_key).decode())
    assert V.Vault.default()._key == env_key


def test_vault_first_run_stores_key_in_both(kr, vault_env):
    v = V.Vault.default()
    stored = kr.store.get((V._keyring_service(), V._KEYRING_USER))
    assert stored and base64.urlsafe_b64decode(stored.encode()) == v._key
    assert (vault_env / "vault.key").read_bytes() == v._key


def test_vault_keychain_only_flag_skips_file(kr, vault_env, monkeypatch):
    monkeypatch.setenv("FINOPS_VAULT_KEYCHAIN_ONLY", "1")
    key = Fernet.generate_key()
    kr.store[(V._keyring_service(), V._KEYRING_USER)] = base64.urlsafe_b64encode(key).decode()
    stale = Fernet.generate_key()
    (vault_env / "vault.key").write_bytes(stale)
    v = V.Vault.default()
    assert v._key == key
    assert (vault_env / "vault.key").read_bytes() == stale  # file left untouched


def test_vault_malformed_key_file_falls_through(kr, vault_env):
    (vault_env / "vault.key").write_bytes(b"not-a-key")
    key = Fernet.generate_key()
    kr.store[(V._keyring_service(), V._KEYRING_USER)] = base64.urlsafe_b64encode(key).decode()
    v = V.Vault.default()
    assert v._key == key
    assert (vault_env / "vault.key").read_bytes() == key  # healed by the re-cache


# ── FINOPS_NO_KEYRING kill switch ─────────────────────────────────────────────
# Ephemeral runs (demos, CI, scratch-HOME cold runs) miss the key/trial files
# but still share the developer's real OS keychain. The kill switch must keep
# every path off the keychain entirely, even on a file miss.

@pytest.mark.parametrize("env_var", ["FINOPS_NO_KEYRING", "FINOPS_AIRGAP"])
def test_no_keyring_vault_first_run_never_touches_keychain(kr, vault_env, monkeypatch, env_var):
    monkeypatch.setenv(env_var, "1")
    v = V.Vault.default()
    assert kr.touches == 0
    # Still fully functional: key generated and cached to the file.
    assert (vault_env / "vault.key").read_bytes() == v._key
    v.store("X", "y")
    assert V.Vault.default().get("X") == "y"


@pytest.mark.parametrize("env_var", ["FINOPS_NO_KEYRING", "FINOPS_AIRGAP"])
def test_no_keyring_trial_never_touches_keychain(kr, trial_file, monkeypatch, env_var):
    monkeypatch.setenv(env_var, "1")
    # Existing keychain entries must not even be read, let alone the legacy
    # disguised item probed.
    kr.store[(L._KR_SERVICE, L._KR_USERNAME)] = _signed(date(2026, 1, 1))
    kr.store[(L._KR_LEGACY_SERVICE, L._KR_LEGACY_USERNAME)] = _signed(date(2026, 1, 1))
    assert L._get_or_create_trial_start() == date.today()
    assert kr.touches == 0
    assert L._file_get() == date.today()  # trial still works, file-only


def test_no_keyring_beats_keychain_only_flag(kr, vault_env, monkeypatch):
    monkeypatch.setenv("FINOPS_NO_KEYRING", "1")
    monkeypatch.setenv("FINOPS_VAULT_KEYCHAIN_ONLY", "1")
    V.Vault.default()
    assert kr.touches == 0
