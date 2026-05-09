"""
AWS OAuth via IAM Identity Center (SSO) device authorization flow.
No long-lived keys stored on disk — gets short-lived tokens via SSO,
then stores the refresh token encrypted in the vault.
"""
from __future__ import annotations

import json
import time
import webbrowser
from typing import Any


def _sso_client(region: str):
    import boto3
    return boto3.client("sso-oidc", region_name=region)


def start_device_flow(
    start_url: str,
    region: str = "us-east-1",
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    """
    Step 1: register client and get device code.
    Returns the full OIDC registration + authorization response.
    """
    client = _sso_client(region)
    scopes = scopes or ["sso:account:access"]

    reg = client.register_client(
        clientName="finops-mcp",
        clientType="public",
        scopes=scopes,
    )

    auth = client.start_device_authorization(
        clientId=reg["clientId"],
        clientSecret=reg["clientSecret"],
        startUrl=start_url,
    )

    return {
        "client_id": reg["clientId"],
        "client_secret": reg["clientSecret"],
        "device_code": auth["deviceCode"],
        "verification_uri_complete": auth["verificationUriComplete"],
        "expires_in": auth["expiresIn"],
        "interval": auth.get("interval", 5),
        "region": region,
    }


def poll_for_token(flow_state: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
    """
    Step 2: poll until the user approves in the browser.
    Returns the token response on success.
    Opens the verification URL in the default browser.
    """
    import boto3
    from botocore.exceptions import ClientError

    client = _sso_client(flow_state["region"])
    url = flow_state["verification_uri_complete"]

    print(f"\n  Open this URL to authorize (or it should open automatically):\n  {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    deadline = time.time() + timeout
    interval = flow_state["interval"]

    while time.time() < deadline:
        time.sleep(interval)
        try:
            tokens = client.create_token(
                clientId=flow_state["client_id"],
                clientSecret=flow_state["client_secret"],
                grantType="urn:ietf:params:oauth:grant-type:device_code",
                deviceCode=flow_state["device_code"],
            )
            return tokens
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "AuthorizationPendingException":
                continue
            if code == "SlowDownException":
                interval += 5
                continue
            raise

    raise TimeoutError("AWS SSO authorization timed out after %ds" % timeout)


def store_sso_credentials(tokens: dict, region: str, account_id: str, role_name: str) -> None:
    """
    Exchange SSO token for temporary IAM credentials and store in vault.
    Also stores the SSO access token for refresh.
    """
    import boto3
    from ..vault import Vault

    sso = boto3.client("sso", region_name=region)
    creds = sso.get_role_credentials(
        accountId=account_id,
        roleName=role_name,
        accessToken=tokens["accessToken"],
    )["roleCredentials"]

    vault = Vault.default()
    vault.store("AWS_ACCESS_KEY_ID", creds["accessKeyId"])
    vault.store("AWS_SECRET_ACCESS_KEY", creds["secretAccessKey"])
    vault.store("AWS_SESSION_TOKEN", creds["sessionCredentials"] if "sessionCredentials" in creds else creds.get("sessionToken", ""))
    vault.store("AWS_DEFAULT_REGION", region)
    vault.store("_AWS_SSO_ACCESS_TOKEN", tokens["accessToken"])
    vault.store("_AWS_SSO_ACCOUNT_ID", account_id)
    vault.store("_AWS_SSO_ROLE_NAME", role_name)
    vault.store("_AWS_SSO_REGION", region)
    print(f"  ✓ AWS credentials stored in vault (account {account_id}, role {role_name})")
