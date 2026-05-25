"""
Drop-in replacement for os.getenv() that checks the vault first.
Connectors import get_env() instead of os.getenv() — no other changes needed.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("finops.security.env")

_vault = None
_loaded = False


def _get_vault():
    global _vault
    if _vault is None:
        try:
            from .vault import Vault
            _vault = Vault.default()
        except Exception as e:
            log.warning("Vault unavailable: %s. Credentials will be read from environment only.", e)
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
        except Exception as e:
            log.debug("Vault read failed for %s: %s", key, e)
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
        # Vault is unavailable; server will rely on env vars only
        log.info("No vault available at startup. Using environment variables only.")
        _loaded = True
        return 0
    try:
        count = v.load_to_env()
    except Exception as e:
        log.warning("Failed to load vault credentials into environment: %s", e)
        _loaded = True
        return 0
    _loaded = True
    log.debug("Loaded %d credentials from vault into environment", count)
    return count
