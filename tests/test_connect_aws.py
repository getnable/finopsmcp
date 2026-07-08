"""connect_aws: connect an AWS account from inside the MCP client.

The 74->5 activation leak was that connecting a provider required leaving the
MCP client for the terminal wizard, and unconnected cost tools silently returned
demo data that looked real. These tests lock in the in-client connect path
(detect -> propose -> confirm, local-only, never mutating AWS) and the demo
"sample data" hint that steers the model to connect_aws.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import finops.server as server
from finops.accounts import AccountConfig


def _run(coro):
    return asyncio.run(coro)


def _candidate(account_id="111122223333", profile="prod", alias="acme-prod"):
    return {
        "profile": profile,
        "account_id": account_id,
        "alias": alias,
        "region": "us-east-1",
        "label": f"profile '{profile}'" if profile else "default credentials",
    }


# ── connect_aws: no credentials on the machine ────────────────────────────────

def test_connect_aws_no_credentials():
    with patch("finops.setup_wizard._detect_aws_candidates", return_value=[]), \
         patch("finops.accounts.list_accounts", return_value=[]):
        out = _run(server.connect_aws())
    assert out["connected"] is False
    assert out["candidates"] == []
    # Points them at a real way to get connected, and stays local-only.
    assert any("CloudShell" in s or "access key" in s for s in out["how_to_connect"])
    assert "never" in out["note"].lower()


# ── connect_aws with no account_id: propose, store nothing ────────────────────

def test_connect_aws_lists_candidates_without_connecting():
    cands = [_candidate("111122223333"), _candidate("444455556666", profile="dev", alias="acme-dev")]
    with patch("finops.setup_wizard._detect_aws_candidates", return_value=cands), \
         patch("finops.accounts.list_accounts", return_value=[]), \
         patch("finops.accounts.add_account") as add:
        out = _run(server.connect_aws())
    assert out["connected"] is False
    assert {c["account_id"] for c in out["candidates"]} == {"111122223333", "444455556666"}
    # Proposing must not write anything.
    add.assert_not_called()


# ── connect_aws with an account_id: connect it ────────────────────────────────

def test_connect_aws_connects_chosen_account():
    cands = [_candidate("111122223333", profile="prod", alias="acme-prod")]
    with patch("finops.setup_wizard._detect_aws_candidates", return_value=cands), \
         patch("finops.accounts.list_accounts", return_value=[]), \
         patch("finops.accounts.add_account") as add, \
         patch("finops.setup_wizard._emit_provider_connected") as emit:
        out = _run(server.connect_aws(account_id="111122223333"))
    assert out["connected"] is True
    assert out["account_id"] == "111122223333"
    assert out["auth_method"] == "profile"
    # It actually persisted the account and fired the activation event.
    add.assert_called_once()
    saved = add.call_args[0][0]
    assert isinstance(saved, AccountConfig)
    assert saved.account_id == "111122223333"
    assert saved.profile == "prod"
    emit.assert_called_once_with("profile")


def test_connect_aws_unknown_account_id_is_rejected():
    cands = [_candidate("111122223333")]
    with patch("finops.setup_wizard._detect_aws_candidates", return_value=cands), \
         patch("finops.accounts.list_accounts", return_value=[]), \
         patch("finops.accounts.add_account") as add:
        out = _run(server.connect_aws(account_id="999999999999"))
    assert out["connected"] is False
    assert "999999999999" in out["error"]
    add.assert_not_called()


def test_connect_aws_already_connected_is_idempotent():
    cands = [_candidate("111122223333")]
    existing = [AccountConfig(name="acme-prod", account_id="111122223333", region="us-east-1", profile="prod")]
    with patch("finops.setup_wizard._detect_aws_candidates", return_value=cands), \
         patch("finops.accounts.list_accounts", return_value=existing), \
         patch("finops.accounts.add_account") as add:
        out = _run(server.connect_aws(account_id="111122223333"))
    assert out["connected"] is True
    add.assert_not_called()  # no duplicate write


# ── the demo "sample data" hint steers unconnected cost tools to connect_aws ──

def test_cost_tool_injects_connect_hint_when_unconnected(monkeypatch):
    monkeypatch.setenv("FINOPS_DEMO", "1")
    monkeypatch.setattr(server, "_unconnected_hint_fired", False)
    with patch("finops.demo_data._real_provider_connected", return_value=False), \
         patch("finops.server._telemetry._send_event") as ev:
        out = _run(server.get_cost_summary())
    assert "connect_aws" in out["_connect_hint"]["actions"]
    assert out["_connect_hint"]["sample_data"] is True
    # The wall is recorded once so the funnel can see "used a tool, never connected".
    assert any(c.args[1] == "unconnected_cost_tool" for c in ev.call_args_list)


def test_cost_tool_no_hint_when_connected(monkeypatch):
    monkeypatch.setenv("FINOPS_DEMO", "1")
    with patch("finops.demo_data._real_provider_connected", return_value=True):
        out = _run(server.get_cost_summary())
    assert "_connect_hint" not in out


# ── a configured account counts as connected (turns demo off) ─────────────────

def test_configured_account_counts_as_connected(monkeypatch):
    import finops.demo_data as dd
    dd._real_provider_cache = None
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    with patch("finops.security.vault.Vault") as V, \
         patch("finops.accounts.list_accounts",
               return_value=[AccountConfig(name="acme", account_id="111122223333",
                                           region="us-east-1", profile="prod")]):
        V.default.return_value.list_keys.return_value = []
        assert dd._real_provider_connected() is True
    dd._real_provider_cache = None
