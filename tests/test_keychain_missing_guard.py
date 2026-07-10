"""On macOS, calling keyring with no login keychain pops a blocking 'a keychain
cannot be found' modal that catching the Python exception cannot suppress. That
hit a real user (Reddit, 2026-07-10) and shows up in any altered-$HOME / sandbox
run. Both the trial store (license.py) and the credential vault (vault.py) must
detect the missing keychain and fall back to their file stores instead of making
the call.
"""
from __future__ import annotations

import pytest

from finops import license as lic
from finops.security import vault as vlt


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("FINOPS_NO_KEYRING", raising=False)
    monkeypatch.delenv("FINOPS_AIRGAP", raising=False)


@pytest.mark.parametrize("mod", [lic, vlt])
def test_missing_login_keychain_disables_keyring_on_macos(mod, monkeypatch, tmp_path):
    # macOS + a HOME with no Library/Keychains/login.keychain* => keyring off.
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    monkeypatch.setattr(mod.Path, "home", classmethod(lambda cls: tmp_path))
    assert mod._macos_keychain_missing() is True
    assert mod._keyring_disabled() is True


@pytest.mark.parametrize("mod", [lic, vlt])
def test_present_login_keychain_allows_keyring_on_macos(mod, monkeypatch, tmp_path):
    kc = tmp_path / "Library" / "Keychains"
    kc.mkdir(parents=True)
    (kc / "login.keychain-db").write_bytes(b"")
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    monkeypatch.setattr(mod.Path, "home", classmethod(lambda cls: tmp_path))
    assert mod._macos_keychain_missing() is False
    assert mod._keyring_disabled() is False


@pytest.mark.parametrize("mod", [lic, vlt])
def test_non_macos_never_flags_missing(mod, monkeypatch, tmp_path):
    # On Linux/Windows there is no such modal; the guard must not disable keyring.
    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.setattr(mod.Path, "home", classmethod(lambda cls: tmp_path))
    assert mod._macos_keychain_missing() is False


@pytest.mark.parametrize("mod", [lic, vlt])
def test_env_disable_still_wins(mod, monkeypatch, tmp_path):
    # Present keychain but explicit opt-out => still disabled.
    kc = tmp_path / "Library" / "Keychains"
    kc.mkdir(parents=True)
    (kc / "login.keychain-db").write_bytes(b"")
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    monkeypatch.setattr(mod.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("FINOPS_NO_KEYRING", "1")
    assert mod._keyring_disabled() is True
