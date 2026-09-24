"""FINOPS_VAULT_KEY accepts the legacy base64(Fernet key) form and the documented token_urlsafe(32)."""
from __future__ import annotations

import base64
import secrets

import pytest
from cryptography.fernet import Fernet

from finops.security.vault import _fernet_key_from_env


def _roundtrip(key: bytes) -> None:
    f = Fernet(key)
    assert f.decrypt(f.encrypt(b"x")) == b"x"


def test_documented_token_urlsafe_recipe_is_a_usable_key():
    _roundtrip(_fernet_key_from_env(secrets.token_urlsafe(32)))


def test_plain_fernet_key_is_accepted():
    k = Fernet.generate_key()
    assert _fernet_key_from_env(k.decode()) == k


def test_legacy_base64_of_fernet_key_still_decodes_to_the_same_key():
    k = Fernet.generate_key()
    legacy = base64.urlsafe_b64encode(k).decode()
    assert _fernet_key_from_env(legacy) == k


def test_wrong_length_is_a_clear_error():
    with pytest.raises(ValueError, match="FINOPS_VAULT_KEY"):
        _fernet_key_from_env(secrets.token_urlsafe(16))
