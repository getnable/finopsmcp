"""Approval chains on nable's pull requests and tickets.

What has to stay true:
  - a remediation PR requests reviews from the confirmed approval chain for
    its action class (GitHub logins as reviewers, team: slugs as team
    reviewers), through the same PR call path, on the same repository; a
    refusal of the request never costs the PR
  - the PR body names the chain, and carries a place for the change ticket's
    link when the chain requires one
  - a ticket names the chain, carries a needs-approval label, and adds the
    approvers as watchers where the tracker has them (Jira watchers, Linear
    subscribers), on the tracker it was going to anyway
  - a proposed chain, or one from an untrusted repo, names nobody; with no
    chain a PR and a ticket are exactly what they were
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from finops import org
from finops.integrations import ticketing as T
from finops.org.cli import _who as human
from finops.remediation import rightsizing_pr as R

ACCT = "123456789012"
REC = {"resource_id": "i-0abc", "resource_type": "ec2", "current_type": "m5.2xlarge",
       "recommended_type": "m5.large", "monthly_savings_usd": 210.0, "account_id": ACCT}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for var in ("JIRA_BASE_URL", "JIRA_API_TOKEN", "JIRA_USER_EMAIL", "JIRA_PROJECT_KEY",
                "JIRA_ASSIGNEE_ID", "LINEAR_API_KEY", "LINEAR_TEAM_ID", "LINEAR_ASSIGNEE_ID",
                "GITHUB_FINOPS_ASSIGNEES", "FINOPS_TICKET_PROVIDER", "FINOPS_TAG_RULES",
                "FINOPS_REQUIRED_TAGS", "FINOPS_PROTECTED_TAGS", "GITHUB_FINOPS_REPO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "none.yaml"))
    monkeypatch.setattr(T.time, "sleep", lambda *_: None)
    # `nable org trust` writes the trusted-repo list under HOME: never the
    # developer's real one.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    monkeypatch.delenv("FINOPS_DATA_DIR", raising=False)
    db = sys.modules.get("finops.storage.db")
    if db is not None:                      # it caches the data dir it first saw
        monkeypatch.setattr(db, "_DATA_DIR", None)


def _capture(monkeypatch, reply, refuse=None):
    """Every HTTP call the ticketing module makes, answered with `reply`;
    a URL containing `refuse` gets a 422."""
    calls: list[dict] = []

    def fake(method, url, **kw):
        calls.append({"method": method, "url": url, "json": kw.get("json")})
        req = httpx.Request(method, url)
        if refuse and refuse in url:
            return httpx.Response(422, request=req, json={"message": "not a collaborator"})
        return httpx.Response(201, request=req, json=reply)
    monkeypatch.setattr(T.httpx, "request", fake)
    return calls


def confirmed(kind, subject, value):
    org.set_fact(org.make_fact(kind, subject, value, source="human"), human("maria"))


def proposed(kind, subject, value):
    org.propose(org.make_fact(kind, subject, value, source="agent:x", confidence=0.9))


def payments_chain(**extra):
    confirmed("owner", f"aws_account:{ACCT}", {"team": "payments"})
    confirmed("approval", "team:payments",
              {"action_classes": ["rightsizing"],
               "approvers": ["github:alice", "team:platform", "jira:acct-alice",
                             "linear:user-alice", "email:alice@example.com"], **extra})


# ── the PR call ───────────────────────────────────────────────────────────────

def test_create_github_pr_requests_reviews_on_the_same_repo(monkeypatch):
    calls = _capture(monkeypatch, {"number": 7, "html_url": "https://github.com/a/b/pull/7"})
    pr = T.create_github_pr(repo="acme/infra", title="t", body="b", head="h", token="x",
                            reviewers=["alice"], team_reviewers=["platform"])
    assert [c["url"] for c in calls] == [
        "https://api.github.com/repos/acme/infra/pulls",
        "https://api.github.com/repos/acme/infra/pulls/7/requested_reviewers"]
    assert calls[1]["json"] == {"reviewers": ["alice"], "team_reviewers": ["platform"]}
    assert pr["nable_review_request"] == {"reviewers": ["alice"],
                                          "team_reviewers": ["platform"], "error": None}


def test_a_refused_review_request_keeps_the_pr(monkeypatch):
    calls = _capture(monkeypatch, {"number": 7, "html_url": "u"}, refuse="requested_reviewers")
    pr = T.create_github_pr(repo="acme/infra", title="t", body="b", head="h", token="x",
                            reviewers=["mallory"])
    assert pr["html_url"] == "u" and len(calls) == 2
    assert "422" in pr["nable_review_request"]["error"]


def test_no_reviewers_is_one_call_as_before(monkeypatch):
    calls = _capture(monkeypatch, {"number": 7, "html_url": "u"})
    pr = T.create_github_pr(repo="acme/infra", title="t", body="b", head="h", token="x")
    assert len(calls) == 1 and "nable_review_request" not in pr


# ── a rightsizing PR end to end ───────────────────────────────────────────────

def _row():
    cfg = {"tf_resource_type": "aws_instance", "tf_resource_name": "web",
           "instance_type": "m5.large", "from_instance_type": "m5.xlarge"}
    return SimpleNamespace(id=1, resource_id="i-abc", resource_name="web",
                           estimated_monthly_savings_usd=100.0,
                           recommended_config=json.dumps(cfg), account_id=ACCT,
                           provider="aws", current_config="{}")


def _open_pr(tmp_path: Path) -> tuple[dict, dict]:
    tf = tmp_path / "main.tf"
    tf.write_text('resource "aws_instance" "web" {\n  instance_type = "m5.xlarge"\n}\n')
    conn = MagicMock()
    conn.__enter__ = lambda s: conn
    conn.__exit__ = MagicMock(return_value=False)
    conn.execute.return_value.fetchall.return_value = [_row()]
    engine = MagicMock()
    engine.connect.return_value = conn
    sent: dict = {}

    def fake_pr(**kw):
        sent.update(kw)
        return {"html_url": "https://github.com/acme/infra/pull/9",
                **({"nable_review_request": {"reviewers": kw["reviewers"],
                                             "team_reviewers": kw["team_reviewers"],
                                             "error": None}} if "reviewers" in kw else {})}
    with patch.object(R, "get_engine", return_value=engine), \
            patch.object(R, "build_id_map", return_value={}), \
            patch.object(R, "resolve_recommendation", return_value=None), \
            patch.object(R, "find_resource_file", return_value=str(tf)), \
            patch.object(R, "mark_acted_on", return_value=True), \
            patch.object(R, "run_git", return_value=""), \
            patch("finops.remediation.gate.remediation_pr_enabled", return_value=True), \
            patch.object(R, "create_github_pr", side_effect=fake_pr):
        out = R.open_rightsizing_pr(tf_dir=str(tmp_path), github_repo="acme/infra")
    return out, sent


def test_a_rightsizing_pr_requests_the_confirmed_chain(tmp_path):
    payments_chain(change_ticket=True)
    proposed("approval", "team:payments", {"action_classes": ["rightsizing"],
                                           "approvers": ["github:mallory"]})
    out, sent = _open_pr(tmp_path)
    assert out["pr_url"].endswith("/pull/9")
    assert sent["reviewers"] == ["alice"] and sent["team_reviewers"] == ["platform"]
    assert out["reviews_requested"]["reviewers"] == ["alice"]
    body = sent["body"]
    assert "### Approval" in body and "`github:alice`" in body and "mallory" not in body
    assert "team:payments, from the org model" in body
    assert "**Change ticket:** _add the link here_" in body
    assert body.index("### Approval") < body.index("---\nGenerated by")


def test_an_environment_chain_and_the_repo_owners_chain_apply(tmp_path):
    confirmed("environment", f"aws_account:{ACCT}", {"env": "prod"})
    confirmed("approval", "environment:prod", {"action_classes": ["*"],
                                               "approvers": ["github:sre-lead"]})
    (tmp_path / ".git").mkdir()
    confirmed("owner", f"repo_path:{tmp_path.name.lower()}//.", {"team": "platform"})
    confirmed("approval", "team:platform", {"action_classes": ["rightsizing"],
                                            "approvers": ["team:infra-reviewers"]})
    _, sent = _open_pr(tmp_path)
    assert sent["reviewers"] == ["sre-lead"]
    assert sent["team_reviewers"] == ["infra-reviewers"]


def test_no_chain_leaves_the_pr_as_it_was(tmp_path):
    confirmed("owner", f"aws_account:{ACCT}", {"team": "payments"})
    confirmed("approval", "team:payments", {"action_classes": ["delete_resource"],
                                            "approvers": ["github:alice"]})
    out, sent = _open_pr(tmp_path)
    assert "reviewers" not in sent and "### Approval" not in sent["body"]
    assert "reviews_requested" not in out
    assert sent["body"] == R._pr_body([{
        "estimated_monthly_savings_usd": 100.0, "resource_name": "web",
        "recommended_config": json.loads(_row().recommended_config)}])


def test_an_untrusted_repos_chain_names_nobody(tmp_path, monkeypatch):
    monkeypatch.delenv("FINOPS_ORG_DIR")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    d = repo / "nable.org"
    d.mkdir()
    for kind, subject, value in (
            ("owner", f"aws_account:{ACCT}", {"team": "payments"}),
            ("approval", "team:payments", {"action_classes": ["rightsizing"],
                                           "approvers": ["github:eve"]})):
        org.set_fact(org.make_fact(kind, subject, value, source="human"), human("maria"),
                     dir=d)
    monkeypatch.chdir(repo)
    assert R._pr_approvals([{"subject": {"account_id": ACCT}}], str(repo)) is None
    org.trust(repo, human("maria"))
    assert R._pr_approvals([{"subject": {"account_id": ACCT}}], str(repo)).github == ["eve"]


# ── tickets ───────────────────────────────────────────────────────────────────

def test_a_jira_ticket_names_the_chain_and_adds_watchers(monkeypatch):
    for k, v in (("JIRA_BASE_URL", "https://acme.atlassian.net"), ("JIRA_API_TOKEN", "x"),
                 ("JIRA_USER_EMAIL", "bot@acme.com"), ("JIRA_PROJECT_KEY", "OPS")):
        monkeypatch.setenv(k, v)
    calls = _capture(monkeypatch, {"key": "OPS-3"})
    payments_chain()
    assert T.create_rightsizing_ticket(REC) == "https://acme.atlassian.net/browse/OPS-3"
    created = calls[0]["json"]["fields"]
    assert "needs-approval" in created["labels"]
    text = created["description"]["content"][0]["content"][0]["text"]
    assert "**Approval:** 1 of github:alice, team:platform" in text
    assert [(c["url"], c["json"]) for c in calls[1:]] == [
        ("https://acme.atlassian.net/rest/api/3/issue/OPS-3/watchers", "acct-alice")]
    # Nothing went anywhere but the Jira the ticket was for.
    assert all(c["url"].startswith("https://acme.atlassian.net/") for c in calls)


def test_a_refused_jira_watcher_keeps_the_ticket(monkeypatch):
    for k, v in (("JIRA_BASE_URL", "https://acme.atlassian.net"), ("JIRA_API_TOKEN", "x"),
                 ("JIRA_USER_EMAIL", "bot@acme.com"), ("JIRA_PROJECT_KEY", "OPS")):
        monkeypatch.setenv(k, v)
    _capture(monkeypatch, {"key": "OPS-4"}, refuse="/watchers")
    payments_chain()
    assert T.create_rightsizing_ticket(REC) == "https://acme.atlassian.net/browse/OPS-4"


def test_a_linear_ticket_subscribes_the_approvers(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    monkeypatch.setenv("LINEAR_TEAM_ID", "team-uuid")
    calls = _capture(monkeypatch, {"data": {"issueCreate": {"issue": {"url": "https://l/1"}}}})
    payments_chain()
    assert T.create_rightsizing_ticket(REC) == "https://l/1"
    inp = calls[-1]["json"]["variables"]["input"]
    assert inp["subscriberIds"] == ["user-alice"]
    assert "**Approval:** 1 of github:alice" in inp["description"]


def test_a_github_issue_gets_the_label_and_the_line_but_no_watchers(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_FINOPS_REPO", "acme/finops")
    calls = _capture(monkeypatch, {"html_url": "https://github.com/acme/finops/issues/1"})
    payments_chain()
    assert T.create_rightsizing_ticket(REC)
    assert len(calls) == 1
    sent = calls[0]["json"]
    assert "needs-approval" in sent["labels"] and "**Approval:**" in sent["body"]
    assert "assignees" not in sent


def test_a_ticket_for_another_class_or_a_proposed_chain_is_unchanged(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_FINOPS_REPO", "acme/finops")
    calls = _capture(monkeypatch, {"html_url": "https://github.com/acme/finops/issues/1"})
    confirmed("owner", f"aws_account:{ACCT}", {"team": "payments"})
    confirmed("approval", "team:payments", {"action_classes": ["purchase_commitment"],
                                            "approvers": ["github:alice"]})
    proposed("approval", "team:payments", {"action_classes": ["rightsizing"],
                                           "approvers": ["github:mallory"]})
    T.create_rightsizing_ticket(REC)
    sent = calls[-1]["json"]
    assert "needs-approval" not in sent["labels"] and "**Approval:**" not in sent["body"]
    # The commitment chain applies to a commitment ticket.
    T.create_commitment_gap_ticket({"coverage_pct": 20, "uncovered_on_demand_usd": 9000,
                                    "recommendation": "buy", "account_id": ACCT})
    assert "needs-approval" in calls[-1]["json"]["labels"]
