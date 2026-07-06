"""Suite-wide guards.

The OS keychain is per-macOS-user, not per-$HOME or per-FINOPS_DATA_DIR, so any
test that reaches the real `keyring` module reads (and on first-run generation,
rewrites) the developer's actual keychain. On macOS that is a password dialog
per suite run, and a rewrite resets the item ACL so "Always Allow" never sticks.
This is exactly how `test_setup_scan.py`'s Vault.default() calls turned every
pytest run into a keychain prompt.

Every test gets an in-memory keyring stub. Tests that assert keyring behavior
(test_keychain_prompts.py, test_keyring_cache.py) install their own counting
stubs on top via monkeypatch, which override this one for their duration.
"""
import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    store: dict = {}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service, user: store.get((service, user))
    fake.set_password = lambda service, user, value: store.__setitem__((service, user), value)
    fake.delete_password = lambda service, user: store.pop((service, user), None)
    monkeypatch.setitem(sys.modules, "keyring", fake)

    # The vault and trial caches are module-global; clear them so a key cached
    # by one test never satisfies (or poisons) another.
    from finops.security.vault import Vault
    import finops.license as license_mod
    Vault._key_cache.clear()
    monkeypatch.setattr(license_mod, "_kr_cached_date", None)
    yield
    Vault._key_cache.clear()
