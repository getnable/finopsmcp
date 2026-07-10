"""Practitioner-driven guard + preflight refinements (2026-07-10 thread quote):
'auto-execute in staging, human approval in production' -> the guard now asks
before reversible mutations aimed at prod; 'agent tags resources from deploy
context' -> the Terraform preflight flags newly created resources that support
tags but carry no owner/cost attribution.
"""
from __future__ import annotations

import pytest

import finops.guard as g
from finops.connectors.terraform_estimate import estimate_plan


@pytest.fixture(autouse=True)
def _pro(monkeypatch):
    monkeypatch.setattr("finops.license.feature_available", lambda f: True)
    monkeypatch.delenv("FINOPS_GUARD_STRICT", raising=False)
    monkeypatch.delenv("FINOPS_GUARD_PROD_PATTERNS", raising=False)


# ── production-context confirmation ────────────────────────────────────────────

def test_prod_mutation_asks():
    v = g.gate_command("kubectl scale deploy api --replicas=10 -n prod")
    assert v is not None and v["decision"] == "ask"
    assert "production" in v["reason"].lower()


def test_staging_mutation_stays_silent():
    assert g.gate_command("kubectl scale deploy api --replicas=10 -n staging") is None


def test_product_word_does_not_trip_it():
    # word boundary: 'product' is not 'prod'
    assert g.gate_command("kubectl scale deploy product-api --replicas=2 -n staging") is None


def test_prod_check_can_be_disabled(monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_PROD_PATTERNS", "off")
    assert g.gate_command("terraform apply -auto-approve -var env=prod") is None


def test_custom_pattern(monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_PROD_PATTERNS", r"\blive\b")
    v = g.gate_command("kubectl scale deploy api --replicas=3 -n live")
    assert v is not None and v["decision"] == "ask"


def test_one_way_doors_unchanged():
    v = g.gate_command("terraform destroy -auto-approve")
    assert v is not None and v["decision"] == "ask"  # still asks, as before


# ── untagged-resource flag in the plan preflight ───────────────────────────────

def _plan(resources):
    return {"resource_changes": resources}


def _create(address, rtype, after):
    return {"address": address, "type": rtype,
            "change": {"actions": ["create"], "before": None, "after": after}}


def test_untagged_create_is_flagged():
    plan = _plan([_create("aws_instance.web", "aws_instance",
                          {"instance_type": "t3.micro", "tags": None})])
    out = estimate_plan(plan)
    assert out["untagged_resources"][0]["address"] == "aws_instance.web"
    assert "missing owner/cost tags" in out["summary"]
    assert "untagged_note" in out


def test_tagged_with_owner_not_flagged():
    plan = _plan([_create("aws_instance.web", "aws_instance",
                          {"instance_type": "t3.micro",
                           "tags": {"Owner": "platform", "Name": "web"}})])
    assert "untagged_resources" not in estimate_plan(plan)


def test_tags_without_attribution_flagged_differently():
    plan = _plan([_create("aws_instance.web", "aws_instance",
                          {"instance_type": "t3.micro", "tags": {"Name": "web"}})])
    out = estimate_plan(plan)
    assert out["untagged_resources"][0]["missing"] == "no owner/cost_center/team tag"


def test_untaggable_resource_is_not_noise():
    # No tags/labels key in the schema at all -> never flagged.
    plan = _plan([_create("aws_s3_bucket_policy.p", "aws_s3_bucket_policy",
                          {"bucket": "b", "policy": "{}"})])
    assert "untagged_resources" not in estimate_plan(plan)


def test_deletes_not_flagged():
    plan = _plan([{"address": "aws_instance.old", "type": "aws_instance",
                   "change": {"actions": ["delete"],
                               "before": {"tags": None}, "after": None}}])
    assert "untagged_resources" not in estimate_plan(plan)
