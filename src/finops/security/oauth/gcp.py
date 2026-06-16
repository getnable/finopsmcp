"""
GCP OAuth: imports a service account key JSON into the vault.
Service account keys are the standard machine auth method for GCP billing APIs.
We store the full JSON encrypted — never on disk in plaintext.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def import_service_account_key(key_path: str | Path) -> None:
    """
    Read a service account JSON key file, validate it, then store it
    encrypted in the vault. Deletes the plaintext file if the user wants.
    """
    from ..vault import Vault

    path = Path(key_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Key file not found: {path}")

    raw = path.read_text()
    try:
        key_data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in key file: {e}") from e

    if key_data.get("type") != "service_account":
        raise ValueError("Not a service account key (expected type=service_account)")

    project_id = key_data.get("project_id", "")
    sa_email = key_data.get("client_email", "")

    # Validate: try to fetch a token
    try:
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(
            key_data,
            scopes=["https://www.googleapis.com/auth/cloud-billing.readonly"],
        )
        creds.refresh(__import__("google.auth.transport.requests", fromlist=["Request"]).Request())
    except Exception as e:
        raise RuntimeError(f"Service account key validation failed: {e}") from e

    vault = Vault.default()
    vault.store("_GCP_SERVICE_ACCOUNT_JSON", raw)
    vault.store("GOOGLE_CLOUD_PROJECT", project_id)

    # Write a temp credentials file that the GCP SDK can pick up
    # This file is re-created at startup from vault; never kept long-term
    creds_path = Path(os.environ.get("FINOPS_DATA_DIR", Path.home() / ".finops")) / "gcp_sa.json"
    vault.store("GOOGLE_APPLICATION_CREDENTIALS", str(creds_path))

    print(f"  ✓ GCP service account stored in vault")
    print(f"    Project: {project_id}")
    print(f"    Account: {sa_email}")

    answer = input("\n  Delete the plaintext key file? [y/N]: ").strip().lower()
    if answer == "y":
        path.unlink()
        print(f"  ✓ Deleted {path}")
    else:
        print(f"  ⚠  Key file still exists at {path} — consider removing it manually")


def restore_credentials_file() -> str | None:
    """
    At startup: decrypt the stored service account JSON and write it to a
    temp file so the GCP SDK can find it via GOOGLE_APPLICATION_CREDENTIALS.
    Returns the path, or None if not configured.
    """
    from ..vault import Vault
    import stat

    vault = Vault.default()
    raw_json = vault.get("_GCP_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        return None

    creds_path = vault.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path:
        from ...storage.db import data_dir
        creds_path = str(data_dir() / "gcp_sa.json")

    path = Path(creds_path)
    path.write_text(raw_json)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)
    return str(path)


def store_billing_accounts(
    billing_account_ids: list[str],
    bq_table: str | None = None,
    project_ids: list[str] | None = None,
) -> None:
    from ..vault import Vault
    vault = Vault.default()
    vault.store("GCP_BILLING_ACCOUNT_IDS", ",".join(billing_account_ids))
    if bq_table:
        vault.store("GCP_BQ_BILLING_TABLE", bq_table)
    if project_ids:
        # Resource scans (the Compute/Monitoring waste audit) are per-project, not
        # per-billing-account. Stored so audit_gcp_waste works without env setup.
        vault.store("GCP_PROJECT_IDS", ",".join(project_ids))
    print(f"  ✓ GCP billing accounts stored: {', '.join(billing_account_ids)}")
