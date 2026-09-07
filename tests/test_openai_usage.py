# SPDX-License-Identifier: Apache-2.0
"""A revoked or typo'd OpenAI key must not read as a clean zero.

The bug: get_costs() and its estimate fallback caught every failure the same
way (a bare `except Exception`), so a bad admin key came back
total_usd=0.0, source="none"/"estimated", exactly like an account with no AI
spend this period. This pins the fix: a 401/403 from OpenAI produces a typed,
distinguishable result (source="error", reason="credential_invalid"), while
a transient failure (5xx, network) still falls through exactly as before, and
a genuine zero-spend account still reports a clean source="api" zero.
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from finops.connectors.saas import openai_usage as oai

COSTS_URL = "https://api.openai.com/v1/organization/costs"
USAGE_URL = "https://api.openai.com/v1/organization/usage/completions"
PROJECTS_URL = "https://api.openai.com/v1/organization/projects"


def _env(mapping):
    return lambda k, d="": mapping.get(k, d)


class _FakeResp:
    """Raises a real httpx.HTTPStatusError, carrying the status code, on
    raise_for_status(). _is_auth_error reads exc.response.status_code, which
    a generic stub exception (a bare RuntimeError, say) would not have, so
    this has to be the real thing rather than a shortcut.
    """

    def __init__(self, status_code=200, payload=None, url=COSTS_URL):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", self._url)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response)

    def json(self):
        return self._payload


def _by_url(mapping):
    """Route a fake httpx.get by a substring of the URL, so a test can give
    the costs endpoint and the usage/projects endpoints different answers."""
    def fake_get(url, **kw):
        for needle, resp in mapping.items():
            if needle in url:
                return resp
        raise AssertionError(f"unexpected URL in this test: {url}")
    return fake_get


# ── _is_auth_error: the classifier the fix hinges on ────────────────────────

@pytest.mark.parametrize("status", [401, 403])
def test_is_auth_error_true_for_401_and_403(status):
    resp = _FakeResp(status)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        resp.raise_for_status()
    assert oai._is_auth_error(exc_info.value) is True


def test_is_auth_error_false_for_a_server_error():
    resp = _FakeResp(500)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        resp.raise_for_status()
    assert oai._is_auth_error(exc_info.value) is False


def test_is_auth_error_false_for_a_plain_network_exception():
    assert oai._is_auth_error(ConnectionError("no route to host")) is False


# ── get_costs(): the credential-failure path must not read as a zero ───────

def test_get_costs_surfaces_a_credential_error_not_a_zero(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env",
                        _env({"OPENAI_ADMIN_KEY": "sk-admin-revoked"}))
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResp(401, url=COSTS_URL))

    out = oai.get_costs(date(2026, 6, 1), date(2026, 6, 2))

    assert out["total_usd"] == 0.0
    assert out["source"] == "error"
    assert out["reason"] == "credential_invalid"
    assert out.get("error"), "the typed result must carry what OpenAI said"
    # The whole point: this must not be mistakable for "not configured" or a
    # clean zero from a real, successful call.
    assert out["source"] not in ("none", "estimated", "api")


def test_get_costs_a_403_is_also_a_credential_error(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env",
                        _env({"OPENAI_ADMIN_KEY": "sk-admin-no-billing-scope"}))
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResp(403, url=COSTS_URL))

    out = oai.get_costs(date(2026, 6, 1), date(2026, 6, 2))
    assert out["source"] == "error"
    assert out["reason"] == "credential_invalid"


def test_get_costs_standard_key_falls_back_to_usage_after_costs_401(monkeypatch):
    """The regression this pins: /v1/organization/costs is admin-only, so a
    standard OPENAI_API_KEY (no OPENAI_ADMIN_KEY set at all) legitimately
    401s there even though the key is perfectly good, since OPENAI_ADMIN_KEY
    is optional by design (setup_wizard prompts it as such) and this
    module's own docs say the usage fallback works with standard keys. A
    costs-endpoint 401 must not be read as a bad key: get_costs() has to
    fall through to the usage-based estimate, which a standard key IS
    entitled to call, and hand back the real data from there rather than a
    false credential error."""
    monkeypatch.setattr("finops.security.env.get_env",
                        _env({"OPENAI_API_KEY": "sk-a-perfectly-good-standard-key"}))
    usage_payload = {"data": [{"start_time": 1717200000, "results": [
        {"model_id": "gpt-4o-mini", "input_tokens": 1_000_000, "output_tokens": 500_000},
    ]}]}
    monkeypatch.setattr(httpx, "get", _by_url({
        "organization/costs": _FakeResp(401, url=COSTS_URL),
        "organization/usage/completions": _FakeResp(200, usage_payload, url=USAGE_URL),
    }))

    out = oai.get_costs(date(2026, 6, 1), date(2026, 6, 2))

    assert out["source"] == "estimated"
    assert out["total_usd"] > 0.0
    assert out["source"] != "error"


def test_get_costs_a_bad_key_fails_both_endpoints_and_still_errors(monkeypatch):
    """This test used to be named '...never_reaches_the_estimate_endpoint'
    and asserted get_costs() stopped at the very first 401, from the
    admin-only costs endpoint. That assertion WAS the regression: a
    perfectly good standard key also 401s that admin-only endpoint (see
    test_get_costs_standard_key_falls_back_to_usage_after_costs_401), so a
    costs-endpoint 401 alone is not proof of a bad key and get_costs must
    not stop there. The real signal is a 401/403 from the usage endpoint,
    which any valid key, standard or admin, is entitled to reach. This key
    fails THAT endpoint too, so it is genuinely bad and must still end up as
    a credential error, just by trying the usage fallback first rather than
    skipping straight to it."""
    monkeypatch.setattr("finops.security.env.get_env",
                        _env({"OPENAI_ADMIN_KEY": "sk-admin-revoked"}))
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _FakeResp(401, url=url)

    monkeypatch.setattr(httpx, "get", fake_get)
    out = oai.get_costs(date(2026, 6, 1), date(2026, 6, 2))

    assert len(calls) == 2, f"expected the costs call AND the usage fallback, got {calls}"
    assert out["source"] == "error"
    assert out["reason"] == "credential_invalid"


def test_get_costs_a_5xx_still_falls_back_to_the_estimate_as_before(monkeypatch):
    """Unchanged behaviour: a transient failure is not a credential problem.
    It still tries the token-usage estimate, and a successful estimate still
    reports real numbers."""
    monkeypatch.setattr("finops.security.env.get_env",
                        _env({"OPENAI_ADMIN_KEY": "sk-admin-good"}))
    usage_payload = {"data": [{"start_time": 1717200000, "results": [
        {"model_id": "gpt-4o-mini", "input_tokens": 1_000_000, "output_tokens": 500_000},
    ]}]}
    monkeypatch.setattr(httpx, "get", _by_url({
        "organization/costs": _FakeResp(500, url=COSTS_URL),
        "organization/usage/completions": _FakeResp(200, usage_payload, url=USAGE_URL),
    }))

    out = oai.get_costs(date(2026, 6, 1), date(2026, 6, 2))
    assert out["source"] == "estimated"
    assert out["total_usd"] > 0.0


def test_get_costs_a_genuine_zero_spend_account_stays_a_clean_zero(monkeypatch):
    """The other half of the distinction this bug is about: a real 200 with no
    line items is a genuine zero, not an error, and must stay source='api'."""
    monkeypatch.setattr("finops.security.env.get_env",
                        _env({"OPENAI_ADMIN_KEY": "sk-admin-good"}))
    monkeypatch.setattr(httpx, "get", _by_url({
        "organization/costs": _FakeResp(200, {"data": []}, url=COSTS_URL),
        "organization/usage/completions": _FakeResp(200, {"data": []}, url=USAGE_URL),
        "organization/projects": _FakeResp(200, {"data": []}, url=PROJECTS_URL),
    }))

    out = oai.get_costs(date(2026, 6, 1), date(2026, 6, 2))
    assert out["source"] == "api"
    assert out["total_usd"] == 0.0


def test_get_costs_without_a_key_is_not_configured_not_an_error(monkeypatch):
    """A third state that must stay distinct: no key at all is 'not_configured',
    never 'credential_invalid' (which means a key WAS sent and was rejected)."""
    monkeypatch.setattr("finops.security.env.get_env", _env({}))
    out = oai.get_costs(date(2026, 6, 1), date(2026, 6, 2))
    assert out["source"] == "none"
    assert out["reason"] == "not_configured"


def test_get_costs_a_working_key_is_unaffected(monkeypatch):
    """Regression guard: the happy path is untouched by this fix."""
    monkeypatch.setattr("finops.security.env.get_env",
                        _env({"OPENAI_ADMIN_KEY": "sk-admin-good"}))
    payload = {"data": [{"start_time": 1717200000, "results": [
        {"amount": {"value": 12.5}, "model_id": "gpt-4o", "project_id": "proj_1"},
    ]}]}
    monkeypatch.setattr(httpx, "get", _by_url({
        "organization/costs": _FakeResp(200, payload, url=COSTS_URL),
        "organization/usage/completions": _FakeResp(200, {"data": []}, url=USAGE_URL),
        "organization/projects": _FakeResp(200, {"data": []}, url=PROJECTS_URL),
    }))

    out = oai.get_costs(date(2026, 6, 1), date(2026, 6, 2))
    assert out["source"] == "api"
    assert out["total_usd"] == 12.5


# ── _estimate_from_usage(): the second bug site, directly ──────────────────

def test_estimate_fallback_itself_surfaces_a_credential_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResp(401, url=USAGE_URL))

    out = oai._estimate_from_usage(date(2026, 6, 1), date(2026, 6, 2),
                                    "sk-admin-revoked", None)

    assert out["source"] == "error"
    assert out["reason"] == "credential_invalid"
    assert out["total_usd"] == 0.0


def test_estimate_fallback_a_5xx_is_still_the_old_empty_api_error(monkeypatch):
    """Unchanged behaviour for a non-credential failure at this second site."""
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResp(500, url=USAGE_URL))

    out = oai._estimate_from_usage(date(2026, 6, 1), date(2026, 6, 2),
                                    "sk-admin-good", None)

    assert out["source"] == "none"
    assert out["reason"] == "api_error"


# ── the typed result itself ──────────────────────────────────────────────

def test_credential_error_result_shape_matches_every_other_result():
    """Same keys as _empty_result, so a caller that only reads total_usd /
    by_model / daily is not broken; source and reason are what carry the
    distinction for a caller that looks."""
    out = oai._credential_error_result("OpenAI said no")
    for key in ("total_usd", "by_model", "by_project", "by_model_tokens", "daily"):
        assert key in out
    assert out["source"] == "error"
    assert out["reason"] == "credential_invalid"
    assert out["error"] == "OpenAI said no"
