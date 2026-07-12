"""Azure and GCP get the same "log back in" treatment AWS got (0.8.169): an
expired token tells the user how to re-authenticate for that provider instead of
surfacing a raw SDK error or "re-run setup". The account config is not lost, only
the session lapsed.
"""
from __future__ import annotations

import pytest

from finops.connectors._reauth import is_auth_expiry, reauth_message


class _AzErr(Exception):
    pass


class ClientAuthenticationError(Exception):
    pass


class RefreshError(Exception):
    pass


def test_azure_expiry_markers_detected():
    assert is_auth_expiry(Exception("AADSTS700082: token expired"), "azure")
    assert is_auth_expiry(ClientAuthenticationError("auth failed"), "azure")
    assert is_auth_expiry(Exception("InvalidAuthenticationToken"), "azure")
    # a plain data error is NOT an auth expiry
    assert not is_auth_expiry(Exception("BadRequest: invalid timeframe"), "azure")


def test_gcp_expiry_markers_detected():
    assert is_auth_expiry(RefreshError("invalid_grant: token expired"), "gcp")
    assert is_auth_expiry(Exception("google.auth.exceptions.DefaultCredentialsError"), "gcp")
    assert is_auth_expiry(Exception("Reauthentication is needed"), "gcp")
    assert not is_auth_expiry(Exception("403 permission denied on bigquery"), "gcp")


def test_provider_isolation():
    # An Azure marker must not trip the GCP matcher and vice-versa.
    assert not is_auth_expiry(Exception("AADSTS700082"), "gcp")
    assert not is_auth_expiry(Exception("invalid_grant"), "azure")


def test_azure_message_matches_real_auth_path():
    # AzureConnector authenticates via ClientSecretCredential (service principal),
    # so the remediation is a NEW CLIENT SECRET + finops setup azure. `az login`
    # would not help and must not be suggested.
    msg = reauth_message("azure", "subscription 1234")
    assert "Azure" in msg
    assert "subscription 1234" in msg
    assert "client secret" in msg
    assert "finops setup azure" in msg
    assert "az login" not in msg
    assert "not lost" in msg  # reassures: account setup persists


def test_gcp_message_matches_real_auth_path():
    # GCPConnector needs a service-account key (GOOGLE_APPLICATION_CREDENTIALS);
    # gcloud ADC is the secondary local path. Mention both, key first.
    msg = reauth_message("gcp", "billing account 01ABCD-XYZ")
    assert "GCP" in msg
    assert "billing account 01ABCD-XYZ" in msg
    assert "service-account key" in msg
    assert "gcloud auth application-default login" in msg


def test_azure_connector_translates_expiry(monkeypatch):
    import asyncio
    from finops.connectors.azure import AzureConnector
    from datetime import date

    c = AzureConnector()
    c._subscription_ids = ["sub-1"]

    def _boom(*a, **k):
        raise ClientAuthenticationError("AADSTS700082 token expired")

    monkeypatch.setattr(c, "_query_costs", _boom)
    monkeypatch.setattr("finops.cache.get", lambda *a, **k: None)
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(c.get_costs(date(2026, 6, 1), date(2026, 6, 30)))
    assert "client secret" in str(ei.value) and "Azure" in str(ei.value)


def test_gcp_connector_translates_expiry(monkeypatch):
    import asyncio
    from finops.connectors.gcp import GCPConnector
    from datetime import date

    c = GCPConnector()
    c._billing_account_ids = ["01ABCD-XYZ"]

    def _boom(*a, **k):
        raise RefreshError("invalid_grant: token has been expired or revoked")

    monkeypatch.setattr(c, "_query_bigquery", _boom)
    monkeypatch.setattr("finops.cache.get", lambda *a, **k: None)
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(c.get_costs(date(2026, 6, 1), date(2026, 6, 30)))
    assert "gcloud auth application-default login" in str(ei.value)
