"""
Encrypted credential vault using Fernet symmetric encryption.

- Master key is stored in the OS keyring (preferred) or in a 0600 key file
- All credentials are encrypted at rest in a SQLite database
- Every read/write is appended to an audit log (key names only, never values)
- The vault DB and key file are chmod 600 on creation
"""
from __future__ import annotations

import base64
import getpass
import logging
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("finops.vault")

_KEYRING_SERVICE_DEFAULT = "finops-mcp"
_KEYRING_USER = "master-key"


def _active_profile() -> str:
    """Return the active profile name from FINOPS_PROFILE env var, or empty string."""
    return os.environ.get("FINOPS_PROFILE", "").strip()


def _keyring_service() -> str:
    """Return the keyring service name, scoped to the active profile if set."""
    profile = _active_profile()
    if profile:
        return f"nable-{profile}-mcp"
    return _KEYRING_SERVICE_DEFAULT


class VaultError(Exception):
    pass


def _data_dir() -> Path:
    raw = os.environ.get("FINOPS_DATA_DIR", "")
    d = Path(raw).expanduser() if raw else Path.home() / ".finops"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(stat.S_IRWXU)
    return d


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480_000)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def _machine_id() -> str:
    for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
        try:
            return Path(path).read_text().strip()
        except OSError:
            pass
    import uuid
    return str(uuid.getnode())


class Vault:
    def __init__(self, db_path: Path, key: bytes) -> None:
        self._db_path = db_path
        self._key = key
        self._fernet = self._make_fernet(key)
        self._init_db()
        self._secure_file(db_path)

    @staticmethod
    def _make_fernet(key: bytes):
        from cryptography.fernet import Fernet
        return Fernet(key)

    def _init_db(self) -> None:
        import sqlite3
        con = sqlite3.connect(str(self._db_path))
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS credentials (
                    key_name TEXT PRIMARY KEY,
                    encrypted_value BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    key_name TEXT NOT NULL,
                    client_pid INTEGER,
                    client_user TEXT
                )
            """)
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _secure_file(path: Path) -> None:
        if path.exists():
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def _audit(self, operation: str, key_name: str) -> None:
        import sqlite3
        try:
            con = sqlite3.connect(str(self._db_path))
            try:
                con.execute(
                    "INSERT INTO audit_log (ts, operation, key_name, client_pid, client_user) VALUES (?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), operation, key_name, os.getpid(), getpass.getuser()),
                )
                con.commit()
            finally:
                con.close()
        except Exception:
            pass  # audit failure must never block operations

    # ── Public API ────────────────────────────────────────────────────────────

    def store(self, key_name: str, value: str) -> None:
        if not key_name or not value:
            raise VaultError("key_name and value must be non-empty")
        encrypted = self._fernet.encrypt(value.encode())
        now = datetime.now(timezone.utc).isoformat()
        import sqlite3
        con = sqlite3.connect(str(self._db_path))
        try:
            con.execute(
                "INSERT INTO credentials (key_name, encrypted_value, created_at, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(key_name) DO UPDATE SET encrypted_value=excluded.encrypted_value, updated_at=excluded.updated_at",
                (key_name, encrypted, now, now),
            )
            con.commit()
        finally:
            con.close()
        self._audit("WRITE", key_name)
        log.debug("Vault: stored %s", key_name)

    def get(self, key_name: str) -> str | None:
        import sqlite3
        con = sqlite3.connect(str(self._db_path))
        try:
            row = con.execute(
                "SELECT encrypted_value FROM credentials WHERE key_name = ?", (key_name,)
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        self._audit("READ", key_name)
        try:
            return self._fernet.decrypt(row[0]).decode()
        except Exception as e:
            # Do not include key_name or the underlying exception in the public
            # message to avoid leaking internal credential names or crypto details.
            log.debug("Vault decrypt failed for key %r: %s", key_name, e)
            raise VaultError("Failed to decrypt credential - check vault key") from e

    def delete(self, key_name: str) -> bool:
        import sqlite3
        con = sqlite3.connect(str(self._db_path))
        try:
            cur = con.execute("DELETE FROM credentials WHERE key_name = ?", (key_name,))
            con.commit()
        finally:
            con.close()
        deleted = cur.rowcount > 0
        if deleted:
            self._audit("DELETE", key_name)
        return deleted

    def list_keys(self) -> list[str]:
        import sqlite3
        con = sqlite3.connect(str(self._db_path))
        try:
            rows = con.execute("SELECT key_name FROM credentials ORDER BY key_name").fetchall()
        finally:
            con.close()
        return [r[0] for r in rows]

    def load_to_env(self) -> int:
        """Decrypt all credentials and set them in os.environ. Returns count loaded."""
        import sqlite3
        con = sqlite3.connect(str(self._db_path))
        try:
            rows = con.execute("SELECT key_name, encrypted_value FROM credentials").fetchall()
        finally:
            con.close()
        count = 0
        for key_name, encrypted_value in rows:
            try:
                value = self._fernet.decrypt(encrypted_value).decode()
                os.environ[key_name] = value
                count += 1
            except Exception:
                log.warning("Vault: could not decrypt %s, skipping", key_name)
        log.debug("Vault: loaded %d credentials into environment", count)
        return count

    def rotate_key(self, new_key: bytes) -> None:
        """
        Re-encrypt all credentials with a new key.
        Uses a single transaction: either all rows are rotated or none are.
        Skips corrupt entries with a warning rather than aborting the whole rotation.
        """
        import sqlite3
        new_fernet = self._make_fernet(new_key)
        con = sqlite3.connect(str(self._db_path))
        try:
            rows = con.execute("SELECT key_name, encrypted_value FROM credentials").fetchall()
            now = datetime.now(timezone.utc).isoformat()
            skipped = 0
            for key_name, enc_val in rows:
                try:
                    plaintext = self._fernet.decrypt(enc_val)
                except Exception:
                    log.warning("Vault: could not decrypt %s during rotation, skipping", key_name)
                    skipped += 1
                    continue
                re_encrypted = new_fernet.encrypt(plaintext)
                con.execute(
                    "UPDATE credentials SET encrypted_value=?, updated_at=? WHERE key_name=?",
                    (re_encrypted, now, key_name),
                )
            con.commit()
        finally:
            con.close()
        self._key = new_key
        self._fernet = new_fernet
        rotated = len(rows) - skipped
        log.info("Vault: key rotation complete (%d rotated, %d skipped)", rotated, skipped)

    # ── Factory methods ───────────────────────────────────────────────────────

    @classmethod
    def _load_or_create_key(cls, key_path: Path) -> bytes:
        if key_path.exists():
            raw = key_path.read_bytes()
        else:
            from cryptography.fernet import Fernet
            raw = Fernet.generate_key()
            key_path.write_bytes(raw)
            key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            log.info("Vault: generated new master key at %s", key_path)
        return raw

    @classmethod
    def _try_keyring(cls) -> bytes | None:
        try:
            import keyring  # type: ignore[import]
            val = keyring.get_password(_keyring_service(), _KEYRING_USER)
            if val:
                return base64.urlsafe_b64decode(val.encode())
        except Exception:
            pass
        return None

    @classmethod
    def _save_keyring(cls, key: bytes) -> bool:
        try:
            import keyring  # type: ignore[import]
            keyring.set_password(_keyring_service(), _KEYRING_USER, base64.urlsafe_b64encode(key).decode())
            return True
        except Exception:
            return False

    @classmethod
    def default(cls) -> "Vault":
        """
        Open (or create) the default vault. Key priority:
        1. OS keyring (scoped to active profile if FINOPS_PROFILE is set)
        2. FINOPS_VAULT_KEY env var (for CI/container deployments)
        3. ~/.finops/vault.key file (chmod 600), or profile dir equivalent

        When FINOPS_PROFILE is set, the vault DB lives in
        ~/.finops/profiles/{profile}/vault.db and the keyring service is
        prefixed with "nable-{profile}-".
        """
        profile = _active_profile()
        if profile:
            import stat as _stat
            data = Path.home() / ".finops" / "profiles" / profile
            data.mkdir(parents=True, exist_ok=True)
            data.chmod(_stat.S_IRWXU)
        else:
            data = _data_dir()
        db_path = data / "vault.db"

        # 1. OS keyring
        key = cls._try_keyring()

        # 2. Env var (base64-encoded Fernet key, for CI/container use)
        if key is None:
            raw_env = os.environ.get("FINOPS_VAULT_KEY", "")
            if raw_env:
                key = base64.urlsafe_b64decode(raw_env.encode())

        # 3. Key file
        if key is None:
            key_path = data / "vault.key"
            key = cls._load_or_create_key(key_path)
            # Try to promote to keyring for better security; delete the file
            # once the key is safely stored so it doesn't sit world-readable.
            if cls._save_keyring(key):
                log.info("Vault: promoted master key to OS keyring")
                try:
                    key_path.unlink()
                    log.info("Vault: removed vault.key file after keyring promotion")
                except OSError:
                    pass

        return cls(db_path, key)
