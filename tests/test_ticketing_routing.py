"""Tickets routed to the owner the org model names.

What has to stay true:
  - the owner and their channel are in the body; a proposed owner is "likely"
  - a team:<team> label only for a confirmed owner
  - an assignee only from a confirmed fact whose people name one for this
    tracker ("github:login", "jira:<id>", "linear:<id>"); never a guess
  - routing changes fields, never the destination, and a tracker that
    refuses the assignee still gets the ticket
  - with no org model the ticket is exactly what it was
"""
from __future__ import annotations

import httpx
import pytest

from finops import org
from finops.integrations import ticketing as T
from finops.org.cli import _who as human

ACCT = "123456789012"
REC = {"resource_id": "i-0abc", "resource_type": "ec2", "current_type": "m5.2xlarge",
       "recommended_type": "m5.large", "monthly_savings_usd": 210.0, "account_id": ACCT}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for var in ("JIRA_BASE_URL", "JIRA_API_TOKEN", "JIRA_USER_EMAIL", "JIRA_PROJECT_KEY",
                "JIRA_ASSIGNEE_ID", "LINEAR_API_KEY", "LINEAR_TEAM_ID", "LINEAR_ASSIGNEE_ID",
                "GITHUB_FINOPS_ASSIGNEES", "FINOPS_TICKET_PROVIDER", "FINOPS_TAG_RULES",
                "FINOPS_REQUIRED_TAGS", "FINOPS_PROTECTED_TAGS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "none.yaml"))
    monkeypatch.setattr(T.time, "sleep", lambda *_: None)


@pytest.fixture
def github(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_FINOPS_REPO", "acme/finops")
    return _capture(monkeypatch, {"html_url": "https://github.com/acme/finops/issues/1"})


@pytest.fixture
def jira(monkeypatch):
    for k, v in (("JIRA_BASE_URL", "https://acme.atlassian.net"), ("JIRA_API_TOKEN", "t"),
                 ("JIRA_USER_EMAIL", "bot@acme.com"), ("JIRA_PROJECT_KEY", "OPS")):
        monkeypatch.setenv(k, v)
    return _capture(monkeypatch, {"key": "OPS-1"})


def _capture(monkeypatch, reply, refuse_assignee=False):
    calls: list[dict] = []

    def fake(method, url, **kw):
        calls.append({"method": method, "url": url, "json": kw.get("json")})
        req = httpx.Request(method, url)
        payload = kw.get("json") or {}
        fields = payload.get("fields") or {}
        if refuse_assignee and fields.get("assignee", {}).get("id") == "acct-maria":
            return httpx.Response(400, request=req, json={"errors": {"assignee": "bad"}})
        return httpx.Response(201, request=req, json=reply)
    monkeypatch.setattr(T.httpx, "request", fake)
    return calls


def confirmed(kind, subject, value):
    org.set_fact(org.make_fact(kind, subject, value, source="human"), human("maria"))


def proposed(kind, subject, value):
    org.propose(org.make_fact(kind, subject, value, source="codeowners:x", confidence=0.9))


def test_no_org_model_leaves_the_ticket_as_it_was(github):
    title, body, _, labels = T._rightsizing_ticket(REC)
    assert T.create_rightsizing_ticket(REC)
    sent = github[-1]["json"]
    assert sent["title"] == title and sent["body"] == body
    assert sent["labels"] == labels and "assignees" not in sent


def test_a_confirmed_owner_labels_mentions_and_assigns(github, monkeypatch):
    monkeypatch.setenv("GITHUB_FINOPS_ASSIGNEES", "finops-bot")
    confirmed("owner", f"aws_account:{ACCT}",
              {"team": "payments", "channel": "#payments-oncall",
               "people": ["github:maria", "jira:acct-maria"]})
    assert T.create_rightsizing_ticket(REC) == "https://github.com/acme/finops/issues/1"
    call = github[-1]
    assert call["url"] == "https://api.github.com/repos/acme/finops/issues"
    sent = call["json"]
    assert "team:payments" in sent["labels"]
    assert "**Owner:** payments (#payments-oncall)" in sent["body"]
    assert sent["body"].index("**Owner:**") < sent["body"].index("*Created automatically")
    assert sent["assignees"] == ["maria"]


def test_a_proposed_owner_is_mentioned_as_likely_and_nothing_else(github, monkeypatch):
    monkeypatch.setenv("GITHUB_FINOPS_ASSIGNEES", "finops-bot")
    proposed("owner", f"aws_account:{ACCT}",
             {"team": "payments", "channel": "#pay", "people": ["github:maria"]})
    T.create_rightsizing_ticket(REC)
    sent = github[-1]["json"]
    assert "**Likely owner:** payments (#pay), not confirmed in the org model" in sent["body"]
    assert not any(lbl.startswith("team:") for lbl in sent["labels"])
    assert sent["assignees"] == ["finops-bot"]


def test_people_without_the_trackers_prefix_assign_nobody(github):
    confirmed("owner", f"aws_account:{ACCT}", {"team": "payments", "people": ["@maria",
                                                                              "maria@acme.com"]})
    T.create_rightsizing_ticket(REC)
    assert "assignees" not in github[-1]["json"]


def test_people_from_a_proposed_team_fact_assign_nobody(github):
    confirmed("owner", f"aws_account:{ACCT}", {"team": "payments"})
    proposed("team", "team:payments", {"name": "payments", "people": ["github:bob"]})
    T.create_rightsizing_ticket(REC)
    sent = github[-1]["json"]
    assert "team:payments" in sent["labels"] and "assignees" not in sent
    confirmed("team", "team:payments", {"name": "payments", "people": ["github:bob"],
                                        "channel": "#pay"})
    T.create_rightsizing_ticket(REC)
    sent = github[-1]["json"]
    assert sent["assignees"] == ["bob"] and "(#pay)" in sent["body"]


def test_jira_gets_the_label_and_the_account_id(jira):
    confirmed("owner", f"aws_account:{ACCT}", {"team": "data platform",
                                               "people": ["jira:acct-maria"]})
    assert T.create_rightsizing_ticket(REC) == "https://acme.atlassian.net/browse/OPS-1"
    fields = jira[-1]["json"]["fields"]
    assert "team:data-platform" in fields["labels"]
    assert fields["assignee"] == {"id": "acct-maria"}
    assert fields["project"] == {"key": "OPS"}


def test_a_refused_assignee_still_creates_the_ticket(jira, monkeypatch):
    monkeypatch.setenv("JIRA_ASSIGNEE_ID", "acct-default")
    calls = _capture(monkeypatch, {"key": "OPS-2"}, refuse_assignee=True)
    confirmed("owner", f"aws_account:{ACCT}", {"team": "payments",
                                               "people": ["jira:acct-maria"]})
    assert T.create_rightsizing_ticket(REC) == "https://acme.atlassian.net/browse/OPS-2"
    assert [c["json"]["fields"].get("assignee") for c in calls] == [
        {"id": "acct-maria"}, {"id": "acct-default"}]
    assert "team:payments" in calls[-1]["json"]["fields"]["labels"]


def test_linear_is_assigned_from_a_linear_person(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    monkeypatch.setenv("LINEAR_TEAM_ID", "team-uuid")
    calls = _capture(monkeypatch, {"data": {"issueCreate": {"issue": {"url": "https://l/1"}}}})
    confirmed("owner", f"aws_account:{ACCT}", {"team": "payments",
                                               "people": ["linear:user-uuid"]})
    assert T.create_rightsizing_ticket(REC) == "https://l/1"
    inp = calls[-1]["json"]["variables"]["input"]
    assert inp["assigneeId"] == "user-uuid" and inp["teamId"] == "team-uuid"
    assert "**Owner:** payments" in inp["description"]


def test_a_kubernetes_finding_routes_by_namespace(github):
    confirmed("owner", "k8s_namespace:checkout", {"team": "checkout", "channel": "#co"})
    T.create_kubernetes_waste_ticket({"kind": "over_requested", "cluster": "prod-eu",
                                      "namespace": "checkout", "name": "web",
                                      "monthly_waste_usd": 90.0})
    sent = github[-1]["json"]
    assert "team:checkout" in sent["labels"] and "**Owner:** checkout (#co)" in sent["body"]


def test_tags_route_through_the_orgs_aliases(github):
    confirmed("tag_alias", "tag_value:pay", {"canonical_key": "team",
                                             "canonical_value": "payments"})
    confirmed("team", "team:payments", {"name": "payments", "channel": "#pay"})
    confirmed("tag_key", "org:org", {"canonical": "team", "keys": ["Team"]})
    T.create_rightsizing_ticket({**REC, "account_id": "", "tags": {"Team": "pay"}})
    assert "**Owner:** payments (#pay)" in github[-1]["json"]["body"]


def test_a_broken_org_model_never_costs_the_ticket(github, monkeypatch):
    from finops import org_owner
    monkeypatch.setattr(org_owner, "load_model", lambda: 1 / 0)
    assert T.create_rightsizing_ticket(REC)
    assert "Owner" not in github[-1]["json"]["body"]


def test_a_custom_ticket_routes_only_when_given_a_subject(github):
    confirmed("owner", f"aws_account:{ACCT}", {"team": "payments"})
    T.create_custom_ticket("t", "b")
    assert github[-1]["json"]["body"] == "b"
    T.create_custom_ticket("t", "b", subject={"account_id": ACCT})
    assert "**Owner:** payments" in github[-1]["json"]["body"]
