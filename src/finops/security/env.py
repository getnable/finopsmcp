"""
Drop-in replacement for os.getenv() that checks the vault first.
Connectors import get_env() instead of os.getenv() — no other changes needed.
"""
from __future__ import annotations

import os
from typing import Optional

_vault = None
_loaded = False


def _get_vault():
    global _vault
    if _vault is None:
        try:
            from .vault import Vault
            _vault = Vault.default()
        except Exception:
            _vault = None
    return _vault


def get_env(key: str, default: str = "") -> str:
    """Check vault first, then os.environ, then default."""
    v = _get_vault()
    if v is not None:
        try:
            val = v.get(key)
            if val is not None:
                return val
        except Exception:
            pass
    return os.environ.get(key, default)


def load_vault_to_env() -> int:
    """
    At server startup: decrypt all vault credentials into os.environ.
    This lets existing connectors that use os.getenv() work without modification.
    Returns number of credentials loaded.
    """
    global _loaded
    if _loaded:
        return 0
    v = _get_vault()
    if v is None:
        return 0
    count = v.load_to_env()
    _loaded = True
    return count
