"""The commands around the scan: doctor, setup, help, and bad flags.

Found by dogfooding:
- `nable doctor` showed an expired SSO session as an info dot, "No AWS
  credentials configured", no fix, exit 0.
- `nable setup`, typing `aws` at "Enter numbers": configured nothing, then
  printed "Done".
- piped input (EOF) at "Connect this account?" connected the account.
- `setup` was missing from `nable --help`; `nable help` was an unknown command.
- `nable scan --jsn` printed the top-level usage, not the scan usage.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from botocore.exceptions import SSOTokenLoadError

from finops import setup_wizard as SW

# ── doctor ───────────────────────────────────────────────────────────────────

def test_doctor_names_an_expired_sso_session_and_the_fix(monkeypatch):
    from finops import doctor
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.setenv("AWS_PROFILE", "acme")
    with patch("boto3.client", side_effect=SSOTokenLoadError(error_msg="Token for acme does not exist")):
        res = doctor._check_aws_scope()
    assert "expired" in res["detail"]
    assert "No AWS credentials configured" not in res["detail"]
    assert res["recommendation"] == "Run: aws sso login --profile acme"
    assert res["warnings"], "an expired session must count as a warning"


def test_doctor_counts_the_expired_session_as_a_warning(monkeypatch, capsys):
    from finops import doctor
    expired = {"name": "AWS credential scope", "ok": None, "detail": "AWS credentials expired",
               "warnings": ["AWS SSO session for profile acme has expired"],
               "recommendation": "Run: aws sso login --profile acme"}
    for name in ("_check_python_version", "_check_path_and_install", "_check_license",
                 "_check_keyring_storage", "_check_azure_permissions", "_check_database",
                 "_check_telemetry", "_check_network", "_check_audit_log"):
        monkeypatch.setattr(doctor, name, lambda: {"name": "x", "ok": True, "detail": ""})
    monkeypatch.setattr(doctor, "_check_aws_scope", lambda: expired)
    doctor.run_doctor()
    out = capsys.readouterr().out
    assert "aws sso login --profile acme" in out
    assert "warnings only" in out


def test_doctor_env_keys_expired_says_so(monkeypatch):
    from botocore.exceptions import ClientError

    from finops import doctor
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIAEXAMPLE")
    err = ClientError({"Error": {"Code": "ExpiredToken", "Message": "x"}}, "GetCallerIdentity")
    with patch("boto3.client", side_effect=err):
        res = doctor._check_aws_scope()
    assert "environment" in res["detail"]
    assert "AWS_SESSION_TOKEN" in res["recommendation"]


# ── setup provider menu ──────────────────────────────────────────────────────

_PROVIDERS = ["aws", "azure", "gcp", "openai"]


def _answers(monkeypatch, *answers):
    it = iter(answers)

    def fake_input(msg=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)


def test_provider_names_are_accepted(monkeypatch):
    _answers(monkeypatch, "aws, gcp")
    assert SW._select_providers(_PROVIDERS) == ["aws", "gcp"]


def test_numbers_and_names_mix(monkeypatch):
    _answers(monkeypatch, "2 openai")
    assert SW._select_providers(_PROVIDERS) == ["azure", "openai"]


def test_an_unknown_name_is_refused_and_asked_again(monkeypatch, capsys):
    _answers(monkeypatch, "awz", "1")
    assert SW._select_providers(_PROVIDERS) == ["aws"]
    out = capsys.readouterr().out
    assert "1 entry was not on the list" in out


def test_an_unrecognised_entry_is_not_echoed(monkeypatch, capsys):
    """A key pasted into the wrong prompt must not be printed back."""
    _answers(monkeypatch, "sk-live-abc123secret", "1")  # pragma: allowlist secret
    assert SW._select_providers(_PROVIDERS) == ["aws"]
    assert "sk-live-abc123secret" not in capsys.readouterr().out  # pragma: allowlist secret


def test_nothing_valid_selects_nothing(monkeypatch):
    _answers(monkeypatch, "x", "y", "z")
    assert SW._select_providers(_PROVIDERS) == []


# ── "Connect this account?" on a closed input ────────────────────────────────

def test_a_closed_input_does_not_connect_the_account(monkeypatch, capsys):
    _answers(monkeypatch)                       # EOF at the first prompt
    monkeypatch.setattr(SW, "_emit_step", lambda *a, **k: None)
    monkeypatch.setattr("finops.accounts.list_accounts", list)
    added = []
    monkeypatch.setattr("finops.accounts.add_account", lambda *a, **k: added.append(a))
    monkeypatch.setattr(SW, "_detect_aws_candidates", lambda: [
        {"account_id": "123456789012", "label": "default credentials", "alias": "",
         "profile": ""}])
    SW._DECLINED[0] = False
    SW.setup_aws_account()
    assert added == []
    assert "No answer, so not connecting" in capsys.readouterr().out
    assert SW._DECLINED[0] is True
    SW._DECLINED[0] = False


def test_the_default_still_applies_to_ordinary_prompts(monkeypatch):
    _answers(monkeypatch)
    assert SW._prompt("Region", default="us-east-1") == "us-east-1"
    assert SW._prompt("Connect? [Y/n]", default="y", on_eof="n") == "n"


# ── help and bad flags ───────────────────────────────────────────────────────

def test_setup_is_in_the_help(capsys):
    with pytest.raises(SystemExit):
        SW.main(["--help"])
    assert "\n  setup " in capsys.readouterr().out


def test_help_is_a_command(capsys):
    with pytest.raises(SystemExit) as e:
        SW.main(["help"])
    assert e.value.code == 0
    assert "usage: nable" in capsys.readouterr().out


def test_help_for_a_subcommand(capsys):
    with pytest.raises(SystemExit):
        SW.main(["help", "scan"])
    assert "usage: nable scan" in capsys.readouterr().out


def test_a_bad_flag_prints_the_subcommand_usage(capsys):
    with pytest.raises(SystemExit) as e:
        SW.main(["scan", "--jsn"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "usage: nable scan" in err
    assert "did you mean --json?" in err
