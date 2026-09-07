# SPDX-License-Identifier: Apache-2.0
"""Every not-connected provider must name its own fix, not AWS's.

The bug: list_connected_providers told every not-configured provider to "call
connect_aws", including Azure, GCP, and every SaaS/AI connector, reproduced
live across all 19 non-AWS providers this tool enumerates. connect_aws only
ever fixes AWS. _remediation() is the one place that decides the real
per-provider answer, so this pins both the mapping itself and that the tool
actually uses it, rather than a hardcoded string that happened to be right
for one provider and wrong for the other nineteen.
"""
from __future__ import annotations

import pytest

from finops import server as _srv
from finops.tools import meta


class _FakeConnector:
    async def is_configured(self) -> bool:
        return False


def _async_false():
    async def f():
        return False
    return f


# ── _remediation() in isolation ─────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("aws", "not connected: call connect_aws, or run 'uvx nable'"),
    ("azure", "not connected: call connect_azure, or run 'uvx nable'"),
    ("gcp", "not connected: call connect_gcp, or run 'uvx nable'"),
    ("datadog", "not connected: run 'finops setup datadog' to add your key, or run 'uvx nable'"),
    ("snowflake", "not connected: run 'finops setup snowflake' to add your key, or run 'uvx nable'"),
    ("openai", "not connected: run 'finops setup openai' to add your key, or run 'uvx nable'"),
    ("anthropic", "not connected: run 'finops setup anthropic' to add your key, or run 'uvx nable'"),
    ("modal", "not connected: run 'finops setup modal' to add your key, or run 'uvx nable'"),
    # registry key and CLI slug diverge for these two
    ("new_relic", "not connected: run 'finops setup newrelic' to add your key, or run 'uvx nable'"),
    ("mongodb_atlas", "not connected: run 'finops setup mongodb' to add your key, or run 'uvx nable'"),
    # vertex has no setup command of its own; it rides on GCP's credentials
    ("vertex", "not connected: run 'finops setup gcp' to add your key, or run 'uvx nable'"),
])
def test_remediation_names_the_right_path(name, expected):
    assert meta._remediation(name) == expected


def test_remediation_never_tells_a_non_aws_provider_to_call_connect_aws():
    """Reproduces the bug across the whole non-AWS surface at once."""
    non_aws = (set(_srv.CLOUD_CONNECTORS) | set(_srv.SAAS_CONNECTORS) | {
        "openai", "anthropic", "vertex", "openrouter", "litellm",
        "modal", "together", "replicate",
    }) - {"aws"}
    assert len(non_aws) >= 19, f"expected the full non-AWS surface, got {sorted(non_aws)}"
    for name in non_aws:
        msg = meta._remediation(name)
        assert "connect_aws" not in msg, f"{name} was told to call connect_aws: {msg}"


# ── wired into the tool a user (or agent) actually calls ────────────────────

async def test_list_connected_providers_reports_the_real_remediation(monkeypatch):
    """End to end: a not-connected non-AWS provider must carry its own fix in
    what list_connected_providers actually returns, not the AWS one."""
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: False)
    monkeypatch.setattr(_srv, "CLOUD_CONNECTORS", {
        "aws": _FakeConnector(), "azure": _FakeConnector(), "gcp": _FakeConnector(),
    })
    monkeypatch.setattr(_srv, "SAAS_CONNECTORS", {"datadog": _FakeConnector()})

    from finops.connectors.saas import (
        openai_usage, anthropic_usage, vertex_costs, openrouter, litellm, gpu_infra,
    )
    for mod in (openai_usage, anthropic_usage, vertex_costs, openrouter, litellm):
        monkeypatch.setattr(mod, "is_configured", _async_false())
    monkeypatch.setattr(gpu_infra, "modal_configured", lambda: False)
    monkeypatch.setattr(gpu_infra, "together_configured", lambda: False)
    monkeypatch.setattr(gpu_infra, "replicate_configured", lambda: False)

    out = await meta.list_connected_providers()

    assert out["aws"]["status"] == "not connected: call connect_aws, or run 'uvx nable'"
    assert out["azure"]["status"] == "not connected: call connect_azure, or run 'uvx nable'"
    assert out["gcp"]["status"] == "not connected: call connect_gcp, or run 'uvx nable'"
    assert out["datadog"]["status"] == (
        "not connected: run 'finops setup datadog' to add your key, or run 'uvx nable'")
    assert out["openai"]["status"] == (
        "not connected: run 'finops setup openai' to add your key, or run 'uvx nable'")
    assert out["modal"]["status"] == (
        "not connected: run 'finops setup modal' to add your key, or run 'uvx nable'")

    # The property that matters more than any one string: nobody not-AWS is
    # ever pointed at connect_aws.
    for name, entry in out.items():
        if name.startswith("_") or entry.get("configured") or name == "aws":
            continue
        assert "connect_aws" not in entry["status"], (name, entry["status"])
