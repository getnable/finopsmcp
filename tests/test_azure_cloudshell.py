"""Tests for the Azure Cloud Shell one-paste connect.

Azure has no CloudFormation equivalent (an App Registration lives at the
tenant level, not a resource group an ARM template can target), so the
one-click parity with AWS is a generated Cloud Shell script instead. These
tests guard: the script only assigns the three documented read-only roles,
and the combined-paste parser is strict enough to fall back to manual entry
on anything that doesn't look like real Cloud Shell output.
"""
from unittest.mock import patch

from finops.security import azure_cloudshell as C


def test_script_creates_the_three_documented_readonly_roles():
    script = C.generate_cloudshell_script()
    assert "Cost Management Reader" in script
    assert "Reader" in script
    assert "Monitoring Reader" in script
    assert "az ad sp create-for-rbac" in script
    # No role beyond the three read roles is ever assigned.
    assert "Owner" not in script
    assert "Contributor" not in script


def test_script_loops_over_every_subscription():
    script = C.generate_cloudshell_script()
    assert 'az account list --query "[].id"' in script
    assert "for SUB in $SUBS" in script


def test_script_prints_one_combined_line():
    script = C.generate_cloudshell_script()
    assert '"$TENANT_ID:$CLIENT_ID:$CLIENT_SECRET:$SUB_CSV"' in script


def test_parse_valid_combined_paste():
    tenant = "11111111-1111-1111-1111-111111111111"
    client = "22222222-2222-2222-2222-222222222222"
    parsed = C.parse_combined_azure_paste(f"{tenant}:{client}:supersecretvalue:sub-a,sub-b")
    assert parsed == (tenant, client, "supersecretvalue", ["sub-a", "sub-b"])


def test_parse_rejects_wrong_field_count():
    assert C.parse_combined_azure_paste("") is None
    assert C.parse_combined_azure_paste("not-enough-colons") is None
    assert C.parse_combined_azure_paste("a:b:c:d:e") is None  # too many


def test_parse_rejects_non_guid_tenant_or_client():
    assert C.parse_combined_azure_paste("not-a-guid:22222222-2222-2222-2222-222222222222:secretvalue:sub1") is None


def test_parse_rejects_short_secret():
    tenant = "11111111-1111-1111-1111-111111111111"
    client = "22222222-2222-2222-2222-222222222222"
    assert C.parse_combined_azure_paste(f"{tenant}:{client}:short:sub1") is None


def test_parse_rejects_empty_subscription_list():
    tenant = "11111111-1111-1111-1111-111111111111"
    client = "22222222-2222-2222-2222-222222222222"
    assert C.parse_combined_azure_paste(f"{tenant}:{client}:supersecretvalue:") is None


# ── wizard wiring ─────────────────────────────────────────────────────────────

def test_setup_azure_accepts_cloudshell_paste_end_to_end(capsys):
    from finops import setup_wizard as W

    tenant = "11111111-1111-1111-1111-111111111111"
    client = "22222222-2222-2222-2222-222222222222"
    combined = f"{tenant}:{client}:supersecretvalue:sub-a,sub-b"
    # Prompts in order: 1) auth method choice, 2) the Cloud Shell paste.
    answers = iter(["1", combined])

    stored = {}

    def _fake_store_service_principal(tenant_id, client_id, client_secret, subscription_ids):
        stored.update(tenant_id=tenant_id, client_id=client_id,
                      client_secret=client_secret, subscription_ids=subscription_ids)

    # _prompt() routes secret=True through getpass.getpass, everything else
    # through input(); both need to draw from the same ordered sequence.
    with patch("builtins.input", lambda *a, **k: next(answers)), \
         patch("getpass.getpass", lambda *a, **k: next(answers)), \
         patch("finops.security.oauth.azure.store_service_principal", _fake_store_service_principal), \
         patch("webbrowser.open", lambda *a, **k: True):
        W.setup_azure()

    assert stored == {
        "tenant_id": tenant, "client_id": client,
        "client_secret": "supersecretvalue", "subscription_ids": ["sub-a", "sub-b"],
    }
    assert "Cost Management Reader" in capsys.readouterr().out


def test_setup_azure_falls_back_to_manual_on_bad_paste():
    from finops import setup_wizard as W

    # 1) Cloud Shell, 2) garbage paste -> falls through to manual entry,
    # which then needs sub ids, tenant id, client id, secret.
    answers = iter(["1", "not-a-valid-paste", "sub-a", "tenant-x", "client-x", "secret-x"])

    stored = {}

    def _fake_store_service_principal(tenant_id, client_id, client_secret, subscription_ids):
        stored.update(tenant_id=tenant_id, client_id=client_id,
                      client_secret=client_secret, subscription_ids=subscription_ids)

    # _prompt() routes secret=True through getpass.getpass, everything else
    # through input(); both need to draw from the same ordered sequence.
    with patch("builtins.input", lambda *a, **k: next(answers)), \
         patch("getpass.getpass", lambda *a, **k: next(answers)), \
         patch("finops.security.oauth.azure.store_service_principal", _fake_store_service_principal), \
         patch("webbrowser.open", lambda *a, **k: True):
        W.setup_azure()

    assert stored == {
        "tenant_id": "tenant-x", "client_id": "client-x",
        "client_secret": "secret-x", "subscription_ids": ["sub-a"],
    }
