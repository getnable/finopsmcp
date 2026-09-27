"""The workload classifier asks the org model first.

What has to stay true:
  - an environment a human confirmed for the account or namespace is the
    answer, before any tag or name
  - a proposed environment fact may move a result toward prod or unknown,
    never toward nonprod
  - with no account or namespace, or no org model, the heuristics answer
    exactly as before
"""
from __future__ import annotations

import pytest

from finops import org
from finops.context.workload import classify

ACCT = "123456789012"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_TAG_RULES", "FINOPS_REQUIRED_TAGS", "FINOPS_PROTECTED_TAGS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "none.yaml"))


def confirmed(subject, env):
    org.set_fact(org.make_fact("environment", subject, {"env": env}, source="human"), "maria")


def proposed(subject, env):
    org.propose(org.make_fact("environment", subject, {"env": env}, source="inference",
                              confidence=0.95))


def test_a_confirmed_environment_beats_the_tags():
    confirmed(f"aws_account:{ACCT}", "nonprod")
    ctx = classify(tags={"Environment": "production"}, account_id=ACCT)
    assert ctx.kind == "nonprod" and ctx.is_nonprod
    assert ctx.evidence == [f"org model: aws account {ACCT} is nonprod (confirmed)"]
    confirmed(f"aws_account:{ACCT}", "prod")
    assert classify(tags={"env": "dev"}, account_id=ACCT).kind == "prod"


def test_the_namespace_is_asked_before_the_account():
    confirmed(f"aws_account:{ACCT}", "prod")
    confirmed("k8s_namespace:load-tests", "sandbox")
    ctx = classify(namespace="load-tests", cluster="eu-1", account_id=ACCT)
    assert ctx.kind == "nonprod"
    assert ctx.reason() == "org model: k8s namespace eu-1/load-tests is sandbox (confirmed)"


def test_dr_and_shared_are_production_here():
    confirmed(f"aws_account:{ACCT}", "dr")
    assert classify(resource_name="standby-dev-copy", account_id=ACCT).kind == "prod"


def test_a_proposed_nonprod_never_makes_anything_nonprod():
    proposed(f"aws_account:{ACCT}", "nonprod")
    assert classify(account_id=ACCT).kind == "unknown"
    assert classify(tags={"env": "production"}, account_id=ACCT).kind == "prod"
    proposed("k8s_namespace:ci", "sandbox")
    assert classify(namespace="ci").kind == "unknown"


def test_a_proposed_prod_moves_toward_prod_only():
    proposed(f"aws_account:{ACCT}", "prod")
    ctx = classify(account_id=ACCT)
    assert ctx.kind == "prod" and "(proposed, not confirmed)" in ctx.reason()
    # The tags say dev and a guess says prod: neither wins.
    ctx = classify(tags={"env": "dev"}, account_id=ACCT)
    assert ctx.kind == "unknown" and not ctx.is_nonprod
    assert classify(tags={"env": "production"}, account_id=ACCT).kind == "prod"


def test_a_confirmed_unknown_leaves_it_to_the_heuristics():
    confirmed(f"aws_account:{ACCT}", "unknown")
    assert classify(tags={"env": "staging"}, account_id=ACCT).kind == "nonprod"


def test_without_a_subject_or_a_model_nothing_changes(monkeypatch):
    confirmed(f"aws_account:{ACCT}", "nonprod")
    assert classify(tags={"env": "prod"}).kind == "prod"
    assert classify(tags={"env": "prod"}, account_id=ACCT, org=False).kind == "prod"

    def boom(*_a, **_k):
        raise OSError("unreadable")
    monkeypatch.setattr(org, "load", boom)
    assert classify(tags={"env": "prod"}, account_id=ACCT).kind == "prod"


def test_a_loaded_model_is_used_as_given():
    confirmed("gcp_project:shop-sbx", "sandbox")
    m = org.load()
    assert classify(account_id="shop-sbx", provider="gcp", org=m).kind == "nonprod"
    assert classify(account_id="shop-sbx", provider="aws", org=m).kind == "unknown"
