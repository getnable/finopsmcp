"""`nable license` says the plan the key is for and links the checkout for it.

Found dogfooding a paid install: the Pro activation screen and the invalid-key
screen both linked the Team ($1,000/mo) Stripe checkout, so a user who came to
activate a $25 Pro key was one click from the wrong purchase. A Team key was
greeted with "unexpected plan: team" followed by "Pro plan active", and an
invalid key exited 0, so a script activating a key could not tell it failed.
"""
from __future__ import annotations

import pytest

import finops.license as L
import finops.setup_wizard as W

# The throwaway keypair from tests/test_license_v2.py, unrelated to production.
_TEST_PRIV = "8fbe8En53x3KhJ93ZwEmE3L0IVLHQm6yI-gn3FGIpeg"  # pragma: allowlist secret
_TEST_PUB = "sxzvFKJjtkqH4xZWXQZLvrYhRxQFVoaJ5YRiEu18dMw"  # pragma: allowlist secret


class _FakeVault:
    def __init__(self):
        self.data: dict = {}

    def store(self, k, v):
        self.data[k] = v

    def get(self, k):
        return self.data.get(k)

    def delete(self, k):
        self.data.pop(k, None)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FINOPS_LICENSE_PRIVATE_KEY", _TEST_PRIV)
    monkeypatch.delenv("FINOPS_LICENSE_KEY", raising=False)
    monkeypatch.setattr(L, "_PUBLIC_KEY_B64", _TEST_PUB)
    monkeypatch.setattr(L, "_status", None)
    vault = _FakeVault()
    from finops.security import vault as vault_mod
    monkeypatch.setattr(vault_mod.Vault, "default", classmethod(lambda cls: vault))
    import finops.telemetry as tel
    monkeypatch.setattr(tel, "_send_event", lambda *a, **k: None)
    return vault


def test_pro_activation_prompt_links_the_pro_checkout(env, monkeypatch, capsys):
    monkeypatch.setattr(W, "_prompt", lambda *a, **k: "")
    assert W._run_license_setup("") == 1, "no key entered is not a success"
    out = capsys.readouterr().out
    assert L._PRO_CHECKOUT_URL in out
    assert L._CHECKOUT_URL not in out, "the Pro activation screen links the $1,000 Team checkout"


def test_invalid_key_exits_nonzero_and_links_pro(env, capsys):
    with pytest.raises(SystemExit) as e:
        W._run_license_setup("FINOPS-2-garbage-garbage")
    assert e.value.code not in (0, None)
    out = capsys.readouterr().out
    assert L._PRO_CHECKOUT_URL in out
    assert L._CHECKOUT_URL not in out


def test_team_key_says_team_plan_active(env, capsys):
    key = L.generate_key("buyer@example.com", plan="team")
    W._run_license_setup(key)
    out = capsys.readouterr().out
    assert "Team plan active" in out
    assert "unexpected plan" not in out
    assert "Pro plan active" not in out
    assert env.data["FINOPS_LICENSE_KEY"] == key


def test_pro_key_says_pro_plan_active(env, capsys):
    key = L.generate_key("buyer@example.com", plan="pro")
    assert W._run_license_setup(key) == 0
    out = capsys.readouterr().out
    assert "Pro plan active" in out
    assert "Team features" not in out


def test_the_plan_table_is_what_the_screens_read():
    assert L.plan_name("pro") == "Pro"
    assert L.plan_name("team") == "Team"
    assert L.checkout_url("pro") == L._PRO_CHECKOUT_URL
    assert L.checkout_url("team") == L._CHECKOUT_URL
    assert L.plan_label("pro") == "Pro ($25/mo)"
    assert L.plan_label("team") == "Team ($1,000/mo flat, unlimited seats)"


def test_nable_license_with_no_key_exits_nonzero(env, monkeypatch):
    """The CLI dispatch turns the return into the exit code, so a script
    activating a key can tell an empty paste did not take."""
    monkeypatch.setattr(W, "_prompt", lambda *a, **k: "")
    with pytest.raises(SystemExit) as e:
        W.main(["license"])
    assert e.value.code == 1
