"""AWS Identity Center (SSO) profiles that are configured but not logged in must
be surfaced, not silently dropped.

Real user feedback (Reddit, 2026-07-10): nable detected access-key profiles but
not Identity-Center SSO profiles, because the STS probe throws for an unlogged
SSO profile and the exception was swallowed. The user concluded nable "only
checks ~/.aws/credentials". These tests pin that:
  - SSO profiles in ~/.aws/config are found (both the sso_session and legacy
    sso_start_url formats);
  - the configured sso_account_id and an `aws sso login --profile X` command
    come back;
  - plain access-key profiles are not misreported as SSO.
"""
from __future__ import annotations

import textwrap

import pytest


@pytest.fixture
def aws_home(tmp_path, monkeypatch):
    """Point botocore at a synthetic ~/.aws with one key profile and two SSO
    profiles (modern + legacy), none logged in."""
    aws = tmp_path / ".aws"
    aws.mkdir()
    (aws / "config").write_text(textwrap.dedent("""
        [profile keyprof]
        region = us-east-1

        [profile prod-sso]
        sso_session = mycorp
        sso_account_id = 111122223333
        sso_role_name = ReadOnly
        region = us-west-2

        [profile legacy-sso]
        sso_start_url = https://mycorp.awsapps.com/start
        sso_region = us-east-1
        sso_account_id = 444455556666
        sso_role_name = Billing
        region = us-east-1

        [sso-session mycorp]
        sso_start_url = https://mycorp.awsapps.com/start
        sso_region = us-east-1
        sso_registration_scopes = sso:account:access
    """))
    (aws / "credentials").write_text(textwrap.dedent("""
        [keyprof]
        aws_access_key_id = AKIAEXAMPLE
        aws_secret_access_key = secret
    """))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(aws / "credentials"))
    # A stray SSO cache dir from the dev's real machine must not leak in.
    monkeypatch.setenv("HOME", str(tmp_path))
    return aws


def test_sso_profiles_surfaced_with_login_command(aws_home):
    from finops.setup_wizard import _detect_sso_profiles_needing_login

    found = {s["profile"]: s for s in _detect_sso_profiles_needing_login()}
    assert set(found) == {"prod-sso", "legacy-sso"}, found

    assert found["prod-sso"]["account_id"] == "111122223333"
    assert found["prod-sso"]["region"] == "us-west-2"
    assert found["prod-sso"]["login_command"] == "aws sso login --profile prod-sso"

    # Legacy sso_start_url format is detected too.
    assert found["legacy-sso"]["account_id"] == "444455556666"
    assert found["legacy-sso"]["login_command"] == "aws sso login --profile legacy-sso"


def test_key_profile_not_reported_as_sso(aws_home):
    from finops.setup_wizard import _detect_sso_profiles_needing_login

    profiles = {s["profile"] for s in _detect_sso_profiles_needing_login()}
    assert "keyprof" not in profiles


def test_no_config_returns_empty(tmp_path, monkeypatch):
    from finops.setup_wizard import _detect_sso_profiles_needing_login

    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "nope"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _detect_sso_profiles_needing_login() == []
