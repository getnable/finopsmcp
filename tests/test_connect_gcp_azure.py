"""connect_gcp and connect_azure: in-client provider connect parity with AWS.

GCP mirrors the AWS detect-then-confirm shape (it has ambient credentials). Azure
has no local ambient creds, so connect_azure returns the Cloud Shell one-paste
script and accepts the pasted line back. Both are propose-only and never mutate
the cloud. Also covers the heartbeat 'surface' tag that splits the ran-but-never-
used cliff into CLI vs wired-MCP-server.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import finops.server as server

_TENANT = "11111111-1111-1111-1111-111111111111"
_CLIENT = "22222222-2222-2222-2222-222222222222"
_SUB = "33333333-3333-3333-3333-333333333333"
_AZURE_PASTE = f"{_TENANT}:{_CLIENT}:secret1234:{_SUB}"


def _run(coro):
    return asyncio.run(coro)


# ── connect_gcp ───────────────────────────────────────────────────────────────

def test_connect_gcp_no_credentials():
    with patch("finops.setup_wizard._detect_gcp_ambient", return_value=None):
        out = _run(server.connect_gcp())
    assert out["connected"] is False
    assert any("application-default login" in s for s in out["how_to_connect"])


def test_connect_gcp_lists_billing_without_connecting():
    amb = {"source": "gcloud ADC", "project": "proj-1",
           "billing": [("AAAAAA-BBBBBB-CCCCCC", "Acme Billing")], "billing_error": None}
    with patch("finops.setup_wizard._detect_gcp_ambient", return_value=amb), \
         patch("finops.security.oauth.gcp.store_billing_accounts") as store:
        out = _run(server.connect_gcp())
    assert out["connected"] is False
    assert out["candidates"][0]["billing_account_id"] == "AAAAAA-BBBBBB-CCCCCC"
    store.assert_not_called()


def test_connect_gcp_connects_chosen_billing_account():
    amb = {"source": "gcloud ADC", "project": "proj-1",
           "billing": [("AAAAAA-BBBBBB-CCCCCC", "Acme Billing")], "billing_error": None}
    with patch("finops.setup_wizard._detect_gcp_ambient", return_value=amb), \
         patch("finops.setup_wizard._discover_bq_export", return_value=None), \
         patch("finops.setup_wizard._gcp_emit_connected") as emit, \
         patch("finops.security.oauth.gcp.store_billing_accounts") as store, \
         patch("finops.setup_scan.gcloud_adc_path", return_value=None), \
         patch("finops.security.vault.Vault"):
        out = _run(server.connect_gcp(billing_account_id="AAAAAA-BBBBBB-CCCCCC"))
    assert out["connected"] is True
    assert out["billing_account_id"] == "AAAAAA-BBBBBB-CCCCCC"
    store.assert_called_once()
    assert store.call_args[0][0] == ["AAAAAA-BBBBBB-CCCCCC"]
    emit.assert_called_once()


def test_connect_gcp_rejects_unknown_billing_account():
    amb = {"source": "gcloud ADC", "project": "proj-1",
           "billing": [("AAAAAA-BBBBBB-CCCCCC", "Acme")], "billing_error": None}
    with patch("finops.setup_wizard._detect_gcp_ambient", return_value=amb), \
         patch("finops.security.oauth.gcp.store_billing_accounts") as store:
        out = _run(server.connect_gcp(billing_account_id="ZZZZZZ-ZZZZZZ-ZZZZZZ"))
    assert out["connected"] is False
    assert "error" in out
    store.assert_not_called()


# ── connect_azure ─────────────────────────────────────────────────────────────

def test_connect_azure_returns_script_and_routes_secret_local(monkeypatch):
    # Not connected yet: returns the script but explicitly refuses the secret in-chat.
    with patch("finops.security.vault.Vault") as V:
        V.default.return_value.list_keys.return_value = []
        out = _run(server.connect_azure())
    assert out["connected"] is False
    assert out["script"]
    # It must tell the user to finish in their own terminal via finops setup azure.
    assert any("finops setup azure" in s for s in out["steps"])
    assert "why_not_paste_here" in out


def test_connect_azure_never_accepts_a_secret_argument():
    # The whole point of the fix: no parameter can carry the client secret through
    # the model. connect_azure must take zero arguments.
    import inspect
    sig = inspect.signature(server.connect_azure)
    assert list(sig.parameters) == []


def test_connect_azure_reports_already_connected(monkeypatch):
    with patch("finops.security.vault.Vault") as V:
        V.default.return_value.list_keys.return_value = ["AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_IDS"]
        out = _run(server.connect_azure())
    assert out["connected"] is True
    assert out["provider"] == "azure"


# ── heartbeat surface tag (the cliff split) ───────────────────────────────────

def test_ping_startup_tags_surface_mcp_server():
    import finops.telemetry as tel
    with patch("finops.telemetry.ping") as ping, \
         patch("sys.stdin") as stdin:
        stdin.isatty.return_value = False  # piped stdin = MCP client launched us
        tel.ping_startup(provider_count=3, plan="free")
    assert ping.call_args[0][0]["surface"] == "mcp_server"


def test_ping_startup_tags_surface_cli():
    import finops.telemetry as tel
    with patch("finops.telemetry.ping") as ping, \
         patch("sys.stdin") as stdin:
        stdin.isatty.return_value = True  # a TTY = ran in a terminal
        tel.ping_startup(provider_count=3, plan="free")
    assert ping.call_args[0][0]["surface"] == "cli"
