"""When an AWS session expires, nable must tell the user to log back in (and which
profile), not to re-run setup. The account config persists as a profile reference,
so a fresh `aws sso login` is all that's needed. First-user feedback (2026-07-10):
"it's ok for my credentials to be expired, just tell me to log into my ssos!"
"""
from __future__ import annotations

from finops.connectors.aws import _reauth_hint, _EXPIRED_CREDENTIAL_MARKERS


class _Sess:
    def __init__(self, profile):
        self.profile_name = profile


def test_hint_names_the_profile_and_gives_the_command():
    msg = _reauth_hint(_Sess("crosstx-mfa"))
    assert "crosstx-mfa" in msg
    assert "aws sso login --profile crosstx-mfa" in msg
    assert "reconfigure" in msg.lower()  # reassures: nothing to redo
    # It must NOT tell them to re-run setup.
    assert "finops setup" not in msg


def test_hint_generic_when_no_named_profile():
    for sess in (_Sess(""), _Sess("default"), _Sess(None), None):
        msg = _reauth_hint(sess)
        assert "aws sso login" in msg
        assert "finops setup" not in msg


def test_markers_cover_sso_mfa_and_empty_chain():
    for m in ("SSOTokenLoadError", "ExpiredToken", "NoCredentialsError",
              "TokenRetrievalError", "UnauthorizedSSOTokenError"):
        assert m in _EXPIRED_CREDENTIAL_MARKERS
