"""
Azure OAuth via device code flow (no browser required on the server).
Uses azure-identity's DeviceCodeCredential, then stores the refresh token
encrypted in the vault.
"""
from __future__ import annotations


# Azure public multi-tenant app ID for device code flows
# Use your own app registration for production (set AZURE_CLIENT_ID env var)
_DEFAULT_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"  # Azure CLI public client


def start_device_flow(
    tenant_id: str,
    client_id: str | None = None,
    scopes: list[str] | None = None,
) -> dict:
    """
    Initiate a device code flow. Returns state needed to poll.
    Prints the user code and URL to stdout.
    """
    import msal  # type: ignore[import]

    app = msal.PublicClientApplication(
        client_id or _DEFAULT_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )
    scopes = scopes or ["https://management.azure.com/.default"]
    flow = app.initiate_device_flow(scopes=scopes)

    if "user_code" not in flow:
        raise RuntimeError("Failed to create device flow: " + str(flow.get("error_description", flow)))

    print(f"\n  {flow['message']}\n")
    return {"app": app, "flow": flow, "scopes": scopes, "tenant_id": tenant_id}


def poll_for_token(state: dict, timeout: int = 300) -> dict:
    """Poll until the user completes auth in the browser. Returns MSAL token cache."""
    import msal
    app: msal.PublicClientApplication = state["app"]
    result = app.acquire_token_by_device_flow(state["flow"])
    if "error" in result:
        raise RuntimeError(f"Azure auth failed: {result.get('error_description', result['error'])}")
    return result


def store_credentials(token_result: dict, tenant_id: str, subscription_ids: list[str]) -> None:
    """Store Azure credentials in the vault."""
    from ..vault import Vault

    vault = Vault.default()
    vault.store("AZURE_TENANT_ID", tenant_id)
    vault.store("AZURE_SUBSCRIPTION_IDS", ",".join(subscription_ids))
    # Store the access token for immediate use
    # For long-lived access, use a service principal — device code tokens expire
    vault.store("_AZURE_ACCESS_TOKEN", token_result.get("access_token", ""))
    vault.store("_AZURE_REFRESH_TOKEN", token_result.get("refresh_token", ""))
    print(f"  ✓ Azure credentials stored in vault (tenant {tenant_id})")
    print("    Note: For long-lived access, create a service principal with Cost Management Reader role.")
    print("    Run: finops setup azure --service-principal")


def store_service_principal(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    subscription_ids: list[str],
) -> None:
    """Store service principal credentials (preferred for production)."""
    from ..vault import Vault

    # Validate before storing
    from azure.identity import ClientSecretCredential
    cred = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    token = cred.get_token("https://management.azure.com/.default")
    if not token.token:
        raise RuntimeError("Service principal validation failed — check credentials")

    vault = Vault.default()
    vault.store("AZURE_TENANT_ID", tenant_id)
    vault.store("AZURE_CLIENT_ID", client_id)
    vault.store("AZURE_CLIENT_SECRET", client_secret)
    vault.store("AZURE_SUBSCRIPTION_IDS", ",".join(subscription_ids))
    print(f"  ✓ Azure service principal stored in vault (tenant {tenant_id})")
