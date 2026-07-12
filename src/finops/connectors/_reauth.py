"""Shared "your session expired, log back in" translation across cloud connectors.

An expired SSO / MFA / OAuth token should tell the user how to re-authenticate for
THAT provider, not surface a raw SDK exception or a misleading "re-run setup". The
account stays configured; only the session lapsed. This mirrors the AWS reauth hint
(see connectors/aws.py::_reauth_hint) for Azure and GCP, which previously let an
expired credential surface as an opaque error.
"""
from __future__ import annotations

# Per-provider substrings (matched against the error code and the exception text /
# type name) that mean "credentials expired or are no longer valid".
_MARKERS: dict[str, tuple[str, ...]] = {
    "azure": (
        "ExpiredAuthenticationToken", "InvalidAuthenticationToken",
        "AuthenticationRequiredError", "ClientAuthenticationError",
        "CredentialUnavailableError", "invalid_client",
        # Azure AD (Entra) sign-in error codes: expired/revoked token, expired
        # secret, password expired, MFA required.
        "AADSTS700082", "AADSTS7000215", "AADSTS50173", "AADSTS50076", "AADSTS900023",
    ),
    "gcp": (
        "RefreshError", "DefaultCredentialsError", "ReauthError", "ReauthFailError",
        "invalid_grant", "invalid_rapt", "Reauthentication",
        "could not automatically determine credentials",
    ),
}

# Remediation must match how each connector ACTUALLY authenticates.
# Azure: ClientSecretCredential from AZURE_* env vars only, so `az login` would
# NOT help; the fix is a fresh client secret. GCP: a service-account key file
# (GOOGLE_APPLICATION_CREDENTIALS) for billing queries, with gcloud ADC as the
# secondary local path.
_LOGIN: dict[str, str] = {
    "azure": ("create a new client secret for your app registration (Azure Portal > "
              "App registrations > Certificates & secrets), then re-run "
              "`finops setup azure` with it"),
    "gcp": ("generate a new service-account key and update "
            "GOOGLE_APPLICATION_CREDENTIALS, or if you use gcloud ADC, run "
            "`gcloud auth application-default login`"),
}

_DISPLAY: dict[str, str] = {"azure": "Azure", "gcp": "GCP"}


def is_auth_expiry(exc: Exception, provider: str) -> bool:
    """True when this exception means the provider's credentials have expired or
    are no longer valid (as opposed to a data/permission error)."""
    if hasattr(exc, "response"):
        try:
            err_code = exc.response.get("Error", {}).get("Code", "")  # type: ignore[attr-defined]
        except Exception:
            err_code = type(exc).__name__
    else:
        err_code = type(exc).__name__
    text = f"{err_code} {exc}"
    return any(m in text for m in _MARKERS.get(provider, ()))


def reauth_message(provider: str, context: str = "") -> str:
    """A friendly re-auth instruction naming the provider and (optionally) the
    subscription / billing account, with the remediation that matches how the
    connector actually authenticates."""
    fix = _LOGIN.get(provider, "re-authenticate")
    where = f" for {context}" if context else ""
    prov = _DISPLAY.get(provider, provider)
    return (
        f"Your {prov} credentials{where} have expired or are no longer valid. "
        f"Your nable account setup is not lost. To fix it: {fix}. "
        f"Then ask again; nable picks up the refreshed credentials automatically."
    )
