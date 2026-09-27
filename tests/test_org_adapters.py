"""Org-context adapters: CODEOWNERS, Terraform, AWS Organizations, tags and
workload propose facts with dollars, and `nable org init` turns them into a
week-one list of ten questions or fewer.

What has to stay true:

  - adapters only propose: whatever they return is written as proposed, and
    nothing an adapter does confirms a fact;
  - they read and never act: no subprocess (terraform is never run), no
    cloud call, no write outside the org dir;
  - CODEOWNERS is read as GitHub reads it (last match wins, anchoring,
    escapes, invalid lines skipped);
  - unknown stays unknown, and nonprod is never proposed from weak evidence;
  - an alias needs strong similarity, and two values with large spend of
    their own are flagged by a lower confidence;
  - init twice proposes nothing new;
  - on the fixture org, confirming the bulk owner questions reaches 80% of
    spend with a confirmed owner, in ten questions or fewer.
"""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from finops import org
from finops.org import cli, store
from finops.org.adapters import AdapterContext, _hcl
from finops.org.adapters import aws_org as aws_org_adapter
from finops.org.adapters import codeowners as co
from finops.org.adapters import tags as tags_adapter
from finops.org.adapters import terraform as tf
from finops.org.adapters import workload as wl
from finops.org.adapters._common import bend, env_of_name, team_norm
from finops.org.adapters.data import CostData
from finops.org.cli import _who as human
from finops.org.model import OrgModel

MONTH = "2026-08"
SPEND = {"111111111111": 12000.0, "222222222222": 2000.0, "333333333333": 8000.0,
         "444444444444": 10000.0, "555555555555": 3000.0, "666666666666": 1500.0,
         "777777777777": 1000.0}
TOTAL = sum(SPEND.values())


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in ("FINOPS_ORG_DIR", "FINOPS_TAG_RULES", "FINOPS_ACCOUNTS_FILE",
                "FINOPS_REQUIRED_TAGS", "FINOPS_PROTECTED_TAGS", "FINOPS_PROFILE",
                "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "no-such.db"))
    monkeypatch.setattr(store, "_data_dir", lambda: tmp_path / "data")
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(cli, "_is_tty", lambda: False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return h


@pytest.fixture
def odir(home, tmp_path, monkeypatch):
    d = tmp_path / "orgdir"
    monkeypatch.setenv("FINOPS_ORG_DIR", str(d))
    return d


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    import finops.storage.db as db_mod
    prev_engine, prev_dir = db_mod._ENGINE, db_mod._DATA_DIR
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "finops.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    db_mod._ENGINE = None
    db_mod._DATA_DIR = None
    yield db_mod
    if db_mod._ENGINE is not None and db_mod._ENGINE is not prev_engine:
        db_mod._ENGINE.dispose()
    db_mod._ENGINE = prev_engine
    db_mod._DATA_DIR = prev_dir


def w(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


CODEOWNERS = r"""# Default reviewers for everything
*                         @acme/platform

# Infrastructure. The last matching rule wins.
/infra/                   @acme/platform alice@acme.io
/infra/payments/          @acme/payments @bob   # payments owns its stack
/infra/search/            @acme/search
/infra/search/legacy/     @carol
/infra/payments/shared/   @acme/platform
/infra/weird\ name/       @acme/data
/infra/ghost/             @acme/data !not-an-owner
/infra/unowned/
*.md                      @acme/docs
"""


def build_repo(root: Path) -> Path:
    """A platform repo: CODEOWNERS, Terraform with default_tags, assume_role
    ARNs, allowed_account_ids, a kubernetes_namespace, env directories, a
    selected workspace and a local tfstate; a Helm chart, a CloudFormation
    stack and a Kubernetes manifest; and code that is not infrastructure."""
    (root / ".git").mkdir(parents=True)
    w(root, ".github/CODEOWNERS", CODEOWNERS)
    w(root, "infra/network/main.tf", 'resource "aws_vpc" "main" { cidr_block = "10.0.0.0/16" }\n')
    w(root, "infra/ghost/main.tf", 'resource "aws_s3_bucket" "b" { bucket = "ghost" }\n')
    w(root, "infra/unowned/main.tf", 'resource "aws_s3_bucket" "b" { bucket = "orphan" }\n')
    w(root, "infra/payments/envs/prod/main.tf", """
# Production payments. A brace in a comment: {
provider "aws" {
  region = "us-east-1"
  assume_role {
    role_arn = "arn:aws:iam::111111111111:role/terraform"  # the deploy role
  }
  default_tags {
    tags = {
      Team        = "payments"
      Environment = "prod"
    }
  }
}

module "app" {
  source = "../../modules/app"
  name   = "payments-#1 {not a block}"
}
""")
    w(root, "infra/payments/envs/dev/main.tf", """
provider "aws" {
  region              = "us-east-1"
  allowed_account_ids = ["222222222222"]
  default_tags {
    tags = { Team = "payments", Environment = "dev" }
  }
}

module "app" {
  source = "../../modules/app"
}
""")
    w(root, "infra/payments/envs/dev/terraform.tfstate", json.dumps({
        "version": 4, "resources": [
            {"mode": "managed", "type": "aws_instance", "name": "worker", "instances": [
                {"attributes": {"id": "i-0dev1", "arn":
                                "arn:aws:ec2:us-east-1:222222222222:instance/i-0dev1"}}]},
            {"mode": "managed", "type": "aws_db_instance", "name": "db",
             "module": "module.app", "instances": [
                 {"attributes": {"id": "payments-dev-db", "arn":
                                 "arn:aws:rds:us-east-1:222222222222:db:payments-dev-db"}}]},
            {"mode": "managed", "type": "aws_iam_role", "name": "r", "instances": [
                {"attributes": {"id": "role-x", "arn": "arn:aws:iam::222222222222:role/x"}}]},
            {"mode": "data", "type": "aws_caller_identity", "name": "me", "instances": [
                {"attributes": {"id": "333333333333"}}]},
        ]}))
    w(root, "infra/payments/modules/app/main.tf", """
resource "aws_db_instance" "db" {
  instance_class = "db.t3.medium"
  user_data = <<EOF
  } # not the end of the block
EOF
}
""")
    w(root, "infra/payments/k8s/namespace.tf", """
resource "kubernetes_namespace" "payments" {
  metadata {
    name = "payments"
  }
}
""")
    w(root, "infra/payments/shared/main.tf", 'resource "aws_sns_topic" "t" { name = "alerts" }\n')
    w(root, "infra/search/main.tf", """
variable "deploy_role" {
  default = "arn:aws:iam::333333333333:role/terraform"
}

locals {
  tags = {
    Team       = "search"
    CostCenter = "cc-200"
  }
}

provider "aws" {
  assume_role {
    role_arn = var.deploy_role
  }
  default_tags {
    tags = local.tags
  }
}
""")
    w(root, "infra/search/.terraform/environment", "prod\n")
    w(root, "infra/search/charts/search-api/Chart.yaml", "apiVersion: v2\nname: search-api\n")
    w(root, "infra/search/charts/search-api/templates/deployment.yaml",
      "apiVersion: apps/v1\nkind: Deployment\n")
    w(root, "infra/search/legacy/stack.yaml",
      "AWSTemplateFormatVersion: '2010-09-09'\nResources:\n  Q:\n    Type: AWS::SQS::Queue\n")
    w(root, "infra/weird name/main.tf", """
resource "kubernetes_namespace_v1" "jobs" {
  metadata { name = "data-jobs" }
}
""")
    w(root, "k8s/base/deployment.yaml", "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n"
      "  name: web\n")
    w(root, "docs/README.md", "# docs\n")
    w(root, "src/app.py", "print('hi')\n")
    w(root, ".github/workflows/ci.yaml", "on: push\njobs: {}\n")
    return root


def seed_db(db) -> None:
    """The fixture org's cost history: seven accounts, one shared account
    whose spend is split by team tags, synced org accounts, an inventory
    and Kubernetes namespaces."""
    now = datetime.now(UTC)
    with db.get_engine().begin() as conn:
        for acct, usd in SPEND.items():
            for day in ("01", "15"):
                conn.execute(db.cost_snapshots.insert().values(
                    provider="aws", service="EC2", account_id=acct, region="us-east-1",
                    snapshot_date=f"{MONTH}-{day}", amount_usd=usd / 2, granularity="DAILY",
                    captured_at=now))
        conn.execute(db.cost_snapshots.insert().values(
            provider="aws", service="EC2", account_id="777777777777", region="",
            snapshot_date="2026-07-10", amount_usd=50000.0, granularity="DAILY",
            captured_at=now))
        for team, env, usd in (("payments", "production", 3000.0),
                               ("Payments", "production", 1000.0),
                               ("payments-svc", "production", 500.0),
                               ("search", "production", 2500.0),
                               ("data", "production", 2000.0),
                               ("unattributed", "", 1000.0)):
            conn.execute(db.attributed_costs.insert().values(
                provider="aws", service="EC2", account_id="444444444444", team=team,
                environment=env, snapshot_date=f"{MONTH}-15", amount_usd=usd,
                captured_at=now))
        for acct, name, parent, status, tags in (
                ("111111111111", "payments-prod", "Root/Payments/Prod", "ACTIVE", {}),
                ("222222222222", "payments-dev", "Root/Payments/NonProd", "ACTIVE", {}),
                ("333333333333", "search-prod", "ou-ab12-34567890", "ACTIVE", {}),
                ("444444444444", "shared-services", "Root/Infrastructure", "ACTIVE", {}),
                ("555555555555", "data-sandbox", "Root/Data/Sandbox", "ACTIVE", {}),
                ("666666666666", "security", "Root/Security", "ACTIVE",
                 {"CostCenter": "cc-900"}),
                ("777777777777", "legacy-misc", "", "ACTIVE", {}),
                ("888888888888", "old-prod", "", "SUSPENDED", {})):
            conn.execute(db.org_accounts.insert().values(
                cloud_provider="aws", account_id=acct, account_name=name, parent_id=parent,
                status=status, tags=json.dumps(tags), assume_role_arn="",
                last_synced=f"{MONTH}-15", is_management_account=False))
        for acct, rid, rtype, usd, tags in (
                ("222222222222", "i-0dev1", "ec2:instance", 150.0,
                 {"Team": "payments", "Environment": "dev", "Owner": "bob@acme.io"}),
                ("111111111111", "i-0prod1", "ec2:instance", 900.0,
                 {"Team": "payments", "Environment": "production", "Owner": "bob@acme.io",
                  "CostCenter": "cc-100"}),
                ("333333333333", "i-search", "ec2:instance", 700.0,
                 {"team": "search", "env": "prod", "Owner": "carol@acme.io"}),
                ("444444444444", "bucket-x", "s3:bucket", 50.0, {"Name": "logs-bucket"})):
            conn.execute(db.resource_inventory.insert().values(
                provider="aws", account_id=acct, region="us-east-1", resource_id=rid,
                resource_type=rtype, resource_name=rid, tags=json.dumps(tags),
                monthly_cost_usd=usd, first_seen=f"{MONTH}-01", last_seen=f"{MONTH}-15",
                is_active=True, metadata="{}"))
        for cluster, ns, usd, labels in (("prod-eks", "payments", 900.0, {"env": "production"}),
                                         ("dev-eks", "ci-runners", 300.0, {}),
                                         ("dev-eks", "feature-login", 120.0, {})):
            conn.execute(db.kubernetes_costs.insert().values(
                cluster=cluster, namespace=ns, snapshot_date=f"{MONTH}-15",
                monthly_cost_usd=usd, labels=json.dumps(labels), captured_at=now))


@pytest.fixture
def fixture_org(odir, fresh_db, tmp_path, monkeypatch):
    repo = build_repo(tmp_path / "platform")
    seed_db(fresh_db)
    monkeypatch.chdir(repo)
    return repo


def ctx_for(repo: Path | None = None, data: CostData | None = None,
            facts: list | None = None) -> AdapterContext:
    return AdapterContext(model=OrgModel(list(facts or [])), repos=[repo] if repo else [],
                          data=data or CostData())


def by_subject(facts, kind="owner"):
    return {str(f.subject): f for f in facts if f.fact == kind}


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# ── CODEOWNERS ────────────────────────────────────────────────────────────────

def test_codeowners_is_read_as_github_reads_it():
    rules = co.parse(CODEOWNERS + "[Section]\n^[Optional] @x\n!negated @acme/x\n"
                     "src/[ab].py @acme/x\ndocs/* @acme/docs-top\n**/logs @acme/logs\n"
                     "apps/ @acme/apps\n/scripts/\\#run.sh @acme/ops\n")

    def owners(path):
        r = co.owning_rule(rules, path)
        return r.owners if r else None

    assert owners("README.md") == ["@acme/docs"]
    assert owners("infra/payments/envs/prod/main.tf") == ["@acme/payments", "@bob"]
    assert owners("infra/payments/shared/main.tf") == ["@acme/platform"]   # a later rule
    assert owners("infra/weird name/main.tf") == ["@acme/data"]            # escaped space
    assert owners("infra/ghost/main.tf") == ["@acme/platform", "alice@acme.io"]  # invalid line
    assert owners("infra/unowned/main.tf") == []                           # unowned
    assert owners("infra/search/legacy/stack.yaml") == ["@carol"]
    assert owners("src/app.py") == ["@acme/platform"]                      # catch-all
    assert owners("docs/guide.txt") == ["@acme/docs-top"]
    assert owners("docs/deep/guide.txt") == ["@acme/platform"]             # docs/*: not nested
    assert owners("build/logs/x.log") == ["@acme/logs"]
    assert owners("a/b/apps/c/d.js") == ["@acme/apps"]
    assert owners("scripts/#run.sh") == ["@acme/ops"]
    assert not any(r.pattern.startswith(("!", "[", "^")) or "[" in r.pattern for r in rules)
    team_rule = next(r for r in rules if r.pattern == "/infra/payments/")
    assert (team_rule.teams, team_rule.people, team_rule.root) == \
        (["payments"], ["@bob"], "infra/payments")


def test_codeowners_proposes_iac_directories_not_files(tmp_path):
    repo = build_repo(tmp_path / "r")
    facts = co.propose(ctx_for(repo))
    got = {str(f.subject): (f.value, f.confidence) for f in facts}
    assert got == {
        "repo_path:infra/network": ({"team": "platform", "people": ["alice@acme.io"]}, 0.85),
        "repo_path:infra/ghost": ({"team": "platform", "people": ["alice@acme.io"]}, 0.85),
        "repo_path:infra/payments": ({"team": "payments", "people": ["@bob"]}, 0.85),
        "repo_path:infra/payments/shared": ({"team": "platform"}, 0.85),
        "repo_path:infra/search": ({"team": "search"}, 0.85),
        "repo_path:infra/search/legacy": ({"team": "@carol", "people": ["@carol"]}, 0.51),
        "repo_path:infra/weird name": ({"team": "data"}, 0.85),
        "repo_path:k8s/base": ({"team": "platform"}, 0.5),
    }
    # /infra/ is not proposed once for the whole tree: infra/unowned is
    # explicitly unowned, and a prefix fact would have owned it.
    assert all(f.status == "proposed" for f in facts)
    assert all(f.source.startswith("codeowners:.github/CODEOWNERS:") for f in facts)
    pay = next(f for f in facts if f.subject.id == "infra/payments")
    assert pay.source == "codeowners:.github/CODEOWNERS:6"
    m = OrgModel(facts)
    assert m.owner_of("repo_path:infra/payments/envs/prod").team == "payments"
    assert m.owner_of("repo_path:infra/payments/shared/x").team == "platform"
    assert m.owner_of("repo_path:infra/unowned") is None


def test_codeowners_second_repo_and_disagreeing_repos(tmp_path):
    a = build_repo(tmp_path / "a")
    b = tmp_path / "b"
    (b / ".git").mkdir(parents=True)
    w(b, "CODEOWNERS", "/infra/search/ @acme/relevance\n")
    w(b, "infra/search/main.tf", "")
    ctx = AdapterContext(model=OrgModel([]), repos=[a, b], data=CostData())
    facts = [f for f in co.propose(ctx) if f.subject.id == "infra/search"]
    assert {f.value["team"] for f in facts} == {"search", "relevance"}
    assert all(f.confidence == 0.4 for f in facts)
    assert any(f.source == "codeowners:b/CODEOWNERS:1" for f in facts)


def test_no_codeowners_no_repo_no_proposals(tmp_path):
    bare = tmp_path / "bare"
    (bare / ".git").mkdir(parents=True)
    w(bare, "main.tf", "")
    assert co.propose(ctx_for(bare)) == []
    assert co.propose(ctx_for(None)) == []


# ── HCL and Terraform ─────────────────────────────────────────────────────────

def test_hcl_reads_blocks_past_comments_strings_and_heredocs():
    root = _hcl.parse("""
# a comment with { a brace
/* block { comment */
provider "aws" {
  // another }
  x = "a # b { c"
  y = <<-EOT
    } still text
  EOT
  assume_role {
    role_arn = "arn:aws:iam::${var.acct}:role/x"
  }
  list = ["a", var.b, "c"]
  obj = {
    "K1" = "v1"
    K2   = local.k2
    K3   = lower("X")
  }
}
resource "aws_instance" "web" { ami = "ami-1" }
""")
    prov, res = root.blocks
    assert (prov.type, prov.labels, res.labels) == ("provider", ["aws"], ["aws_instance", "web"])
    scope = _hcl.Scope({"acct": '"123456789012"', "b": '"B"'}, {"k2": '"v2"'})
    assert _hcl.string(prov.attrs["x"]) == "a # b { c"
    assert _hcl.string(prov.find("assume_role")[0].attrs["role_arn"], scope) == \
        "arn:aws:iam::123456789012:role/x"
    assert _hcl.string(prov.find("assume_role")[0].attrs["role_arn"]) is None
    assert _hcl.strings(prov.attrs["list"], scope) == ["a", "B", "c"]
    assert _hcl.mapping(prov.attrs["obj"], scope) == {"K1": "v1", "K2": "v2"}
    assert res.attrs["ami"] == '"ami-1"'


def _codeowners_facts(repo):
    return co.propose(ctx_for(repo))


def test_terraform_maps_accounts_namespaces_and_state_to_path_owners(tmp_path, monkeypatch):
    def no_subprocess(*a, **k):
        raise AssertionError("an adapter must never run a subprocess")
    monkeypatch.setattr(subprocess, "run", no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", no_subprocess)
    repo = build_repo(tmp_path / "r")
    data = CostData(month=MONTH, accounts={("aws", a): u for a, u in SPEND.items()},
                    namespaces={("prod-eks", "payments"): {"usd": 900.0, "labels": {}}},
                    inventory=[{"provider": "aws", "account_id": "222222222222",
                                "resource_id": "i-0dev1", "arn": None, "type": "ec2",
                                "name": "", "tags": {}, "usd": 150.0}])
    ctx = AdapterContext(model=OrgModel([]), repos=[repo], data=data,
                         prior=_codeowners_facts(repo))
    facts = tf.propose(ctx)
    owners = by_subject(facts)
    got = {s: (f.value["team"], f.confidence, f.dollars_monthly) for s, f in owners.items()}
    assert got == {
        # assume_role ARN; CODEOWNERS says payments and default_tags agree.
        "aws_account:111111111111": ("payments", 0.85, 12000.0),
        # allowed_account_ids, and the ARN of a resource in local state.
        "aws_account:222222222222": ("payments", 0.85, 2000.0),
        # var.deploy_role resolved from the variable's default; local.tags.
        "aws_account:333333333333": ("search", 0.85, 8000.0),
        "k8s_namespace:payments": ("payments", 0.75, 900.0),
        "k8s_namespace:data-jobs": ("data", 0.75, None),
        # default_tags cover the provider's resources too.
        "resource:i-0dev1": ("payments", 0.85, 150.0),
        # A module's resource belongs to the module directory's owner.
        "resource:payments-dev-db": ("payments", 0.85, None),
    }
    assert owners["aws_account:111111111111"].value["people"] == ["@bob"]
    assert owners["aws_account:111111111111"].source == \
        "terraform:infra/payments/envs/prod/main.tf"
    assert "resource:role-x" not in owners               # not a cost-bearing type
    assert "aws_account:333333333333" in owners           # ...but never from a data source
    envs = {str(f.subject): (f.value["env"], f.confidence) for f in facts
            if f.fact == "environment"}
    assert envs == {
        "repo_path:infra/payments/envs/prod": ("prod", 0.8),
        "repo_path:infra/payments/envs/dev": ("nonprod", 0.75),
        "repo_path:infra/search": ("prod", 0.7),              # the selected workspace
        "aws_account:111111111111": ("prod", 0.75),
        "aws_account:222222222222": ("nonprod", 0.7),
        "aws_account:333333333333": ("prod", 0.65),
        "resource:i-0dev1": ("nonprod", 0.7),
        "resource:payments-dev-db": ("nonprod", 0.7),
    }
    keys = {f.value["canonical"]: f.value["keys"] for f in facts if f.fact == "tag_key"}
    assert keys == {"team": ["Team"], "environment": ["Environment"],
                    "cost_center": ["CostCenter"]}
    assert all(f.status == "proposed" for f in facts)


def test_terraform_without_path_owners_uses_default_tags_only(tmp_path):
    repo = build_repo(tmp_path / "r")
    owners = by_subject(tf.propose(ctx_for(repo)))
    assert {s: (f.value["team"], f.confidence) for s, f in owners.items()} == {
        "aws_account:111111111111": ("payments", 0.6),
        "aws_account:222222222222": ("payments", 0.6),
        "aws_account:333333333333": ("search", 0.6),
        "resource:i-0dev1": ("payments", 0.6),
        "resource:payments-dev-db": ("payments", 0.6),
    }


def test_terraform_shared_account_and_disagreeing_evidence(tmp_path):
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    w(repo, "CODEOWNERS", "/a/ @acme/alpha\n/b/ @acme/beta\n")
    arn = 'provider "aws" {\n assume_role {\n role_arn = "arn:aws:iam::999999999999:role/t"\n}\n}\n'
    w(repo, "a/main.tf", arn)
    w(repo, "b/main.tf", arn)
    # A directory whose name says prod and whose tags say dev says nothing.
    w(repo, "c/envs/prod/main.tf", 'provider "aws" {\n default_tags {\n tags = '
      '{ Environment = "dev" }\n}\n}\n')
    ctx = AdapterContext(model=OrgModel([]), repos=[repo], data=CostData())
    ctx.prior.extend(co.propose(ctx))
    facts = tf.propose(ctx)
    assert "aws_account:999999999999" not in by_subject(facts)    # half alpha, half beta
    assert not any(f.fact == "environment" and f.subject.id.startswith("c/") for f in facts)


def test_terraform_tag_keys_only_add_to_known_keys(tmp_path):
    repo = build_repo(tmp_path / "r")
    known = org.make_fact("tag_key", "org:org", {"canonical": "team", "keys": ["team"]},
                          source="legacy:tag_rules.yaml")
    known.status = "confirmed"
    facts = tf.propose(ctx_for(repo, facts=[known]))
    assert not any(f.fact == "tag_key" and f.value["canonical"] == "team" for f in facts)
    squad = org.make_fact("tag_key", "org:org", {"canonical": "team", "keys": ["squad"]},
                          source="x:y")
    facts = tf.propose(ctx_for(repo, facts=[squad]))
    team = next(f for f in facts if f.fact == "tag_key" and f.value["canonical"] == "team")
    assert team.value["keys"] == ["squad", "Team"]


def test_terraform_json_and_other_clouds(tmp_path):
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    w(repo, "CODEOWNERS", "/stacks/ @acme/infra\n")
    w(repo, "stacks/aws/main.tf.json", json.dumps({
        "provider": {"aws": [{"assume_role": {
            "role_arn": "arn:aws:iam::101010101010:role/deploy"}}]},
        "resource": {"kubernetes_namespace": {"ns": {"metadata": [{"name": "batch"}]}}}}))
    w(repo, "stacks/gcp/main.tf", 'provider "google" {\n  project = "acme-data-prod"\n}\n'
      'provider "azurerm" {\n  features {}\n  subscription_id = "00000000-1111-2222-3333-'
      '444444444444"\n}\n')
    ctx = AdapterContext(model=OrgModel([]), repos=[repo], data=CostData())
    ctx.prior.extend(co.propose(ctx))
    owners = {s: f.value["team"] for s, f in by_subject(tf.propose(ctx)).items()}
    assert owners == {"aws_account:101010101010": "infra", "k8s_namespace:batch": "infra",
                      "gcp_project:acme-data-prod": "infra",
                      "azure_subscription:00000000-1111-2222-3333-444444444444": "infra"}


# ── the cost history ──────────────────────────────────────────────────────────

def test_cost_data_reads_the_seeded_history_and_never_creates_a_db(home, fresh_db, tmp_path,
                                                                  monkeypatch):
    missing = tmp_path / "missing.db"
    monkeypatch.setenv("FINOPS_DB_PATH", str(missing))
    empty = CostData.from_local()
    assert not missing.exists() and empty.not_read and not empty.accounts
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "finops.db"))
    seed_db(fresh_db)
    data = CostData.from_local()
    assert data.month == MONTH
    assert data.accounts[("aws", "111111111111")] == 12000.0
    assert data.teams["444444444444"]["payments-svc"] == 500.0
    assert data.envs["444444444444"]["production"] == 9000.0
    assert {r["account_id"] for r in data.org_accounts} >= {"111111111111", "888888888888"}
    assert data.namespace_usd("payments") == 900.0
    assert data.resource_usd("i-0dev1") == 150.0
    assert data.account_name("aws_account", "555555555555") == "data-sandbox"


# ── AWS Organizations ─────────────────────────────────────────────────────────

def _org_data():
    rows = []
    for acct, name, parent, status, tags in (
            ("111111111111", "payments-prod", "Root/Payments/Prod", "ACTIVE", {}),
            ("333333333333", "search-prod", "ou-ab12-34567890", "ACTIVE", {}),
            ("444444444444", "shared-services", "Root/Infrastructure", "ACTIVE", {}),
            ("555555555555", "data-sandbox", "Root/Data/Sandbox", "ACTIVE", {}),
            ("666666666666", "security", "Root/Security", "ACTIVE", {"CostCenter": "cc-900"}),
            ("676767676767", "Log Archive", "", "ACTIVE", {}),
            ("686868686868", "billing", "", "ACTIVE",
             {"Team": "finance", "Environment": "production"}),
            ("696969696969", "staging-prod", "", "ACTIVE", {}),
            ("777777777777", "legacy-misc", "", "ACTIVE", {}),
            ("888888888888", "old-prod", "", "SUSPENDED", {})):
        rows.append({"provider": "aws", "account_id": acct, "name": name, "parent_id": parent,
                     "status": status, "tags": tags, "is_management_account": False})
    return CostData(month=MONTH, accounts={("aws", a): u for a, u in SPEND.items()},
                    teams={"444444444444": {"data": 2000.0}}, org_accounts=rows)


def test_aws_org_accounts_business_units_and_environments():
    facts = aws_org_adapter.propose(ctx_for(data=_org_data()))
    accounts = {f.subject.id: (f.value, f.confidence, f.dollars_monthly) for f in facts
                if f.fact == "account"}
    assert accounts["111111111111"] == (
        {"name": "payments-prod", "business_unit": "Payments"}, 0.6, 12000.0)
    assert accounts["333333333333"] == ({"name": "search-prod"}, 0.9, 8000.0)  # opaque OU id
    assert accounts["444444444444"][0] == {"name": "shared-services"}          # structural OU
    assert accounts["555555555555"][0] == {"name": "data-sandbox", "business_unit": "Data"}
    assert accounts["666666666666"][0] == {"name": "security", "cost_center": "cc-900"}
    assert "888888888888" not in accounts                                    # suspended
    envs = {f.subject.id: (f.value["env"], f.confidence) for f in facts
            if f.fact == "environment"}
    assert envs == {
        "111111111111": ("prod", 0.7), "333333333333": ("prod", 0.65),
        "444444444444": ("shared", 0.65), "555555555555": ("sandbox", 0.7),
        "666666666666": ("shared", 0.7), "676767676767": ("shared", 0.65),
        "686868686868": ("prod", 0.85),
    }
    # "staging-prod" says two things, "legacy-misc" nothing: unknown stays unknown.
    assert "696969696969" not in envs and "777777777777" not in envs
    owners = {f.subject.id: (f.value["team"], f.confidence) for f in facts if f.fact == "owner"}
    assert owners == {"555555555555": ("data", 0.5), "686868686868": ("finance", 0.8)}


def test_aws_org_reads_nothing_from_aws(monkeypatch):
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the adapter must not call AWS")))
    assert aws_org_adapter.propose(ctx_for(data=CostData())) == []
    assert aws_org_adapter.propose(ctx_for(data=_org_data()))


def test_aws_org_leaves_a_confirmed_account_record_alone():
    known = org.make_fact("account", "aws_account:333333333333", {"name": "search-prod"},
                          source="legacy:accounts.yaml")
    known.status = "confirmed"
    facts = aws_org_adapter.propose(ctx_for(data=_org_data(), facts=[known]))
    assert not any(f.fact == "account" and f.subject.id == "333333333333" for f in facts)


# ── tags ──────────────────────────────────────────────────────────────────────

def test_team_normal_forms_are_strict():
    assert bend("payments", "Payments") == "case"
    assert bend("payments_team", "Payments-Team") == "separator"
    assert bend("payments-svc", "payments") == "suffix"
    assert bend("payment", "payments") == "suffix"
    assert bend("Payments Team", "payments") == "suffix"
    for a, b in (("pay", "payments"), ("search", "research"), ("data", "data-eng"),
                 ("ml", "ml-platform"), ("ops", "op"), ("class", "clas"), ("", "x")):
        assert bend(a, b) is None, (a, b)
    assert team_norm("svc") == "svc"


def _tag_data(teams, inventory=(), envs=None, rules=()):
    return CostData(month=MONTH, teams={"444444444444": dict(teams)},
                    envs={"444444444444": dict(envs or {})}, inventory=list(inventory),
                    tag_rules=list(rules))


def test_tag_aliases_cluster_near_duplicates_only():
    anchor = org.make_fact("owner", "aws_account:111111111111", {"team": "payments"},
                           source="terraform:x")
    data = _tag_data({"payments": 3000.0, "Payments": 1000.0, "payments-svc": 500.0,
                      "Payments_Team": 200.0, "pay": 400.0, "search": 2500.0,
                      "research": 300.0, "unattributed": 999.0, "bob@acme.io": 50.0})
    facts = tags_adapter.propose(ctx_for(data=data, facts=[anchor]))
    aliases = {f.subject.id: (f.value["canonical_value"], f.confidence, f.dollars_monthly)
               for f in facts if f.fact == "tag_alias"}
    assert aliases == {"payments-svc": ("payments", 0.75, 500.0),
                       "Payments_Team": ("payments", 0.75, 200.0)}
    teams = {f.subject.id: f.dollars_monthly for f in facts if f.fact == "team"}
    # "Payments" is the same value to the model (tag values are case-blind).
    assert teams == {"payments": 4700.0, "pay": 400.0, "search": 2500.0, "research": 300.0}
    assert all(f.confidence == 0.6 for f in facts if f.fact == "team")


def test_two_large_independent_spends_are_flagged_not_merged_quietly():
    data = _tag_data({"core": 5000.0, "core-team": 4000.0, "core_svc": 100.0})
    facts = tags_adapter.propose(ctx_for(data=data))
    aliases = {f.subject.id: f.confidence for f in facts if f.fact == "tag_alias"}
    assert aliases == {"core-team": 0.45, "core_svc": 0.75}


def test_two_named_teams_are_never_merged():
    named = [org.make_fact("owner", f"repo_path:{t}", {"team": t}, source="codeowners:x")
             for t in ("payments", "payments-team")]
    data = _tag_data({"payments": 100.0, "payments-team": 100.0})
    facts = tags_adapter.propose(ctx_for(data=data, facts=named))
    assert not [f for f in facts if f.fact == "tag_alias"]


def test_tag_keys_from_names_and_values():
    inv = [
        {"resource_id": f"i-{i}", "usd": 100.0, "tags": t, "account_id": "1", "provider": "aws",
         "type": "ec2", "name": "", "arn": None}
        for i, t in enumerate([
            {"Team": "payments", "Owner": "bob@acme.io", "Env": "production", "squadron": "x",
             "CostCenter": "cc1", "group": "payments"},
            {"team": "search", "Owner": "carol@acme.io", "Env": "dev", "group": "search"},
            {"Team": "payments", "Owner": "bob@acme.io", "Env": "prod", "Name": "web-1"},
        ])]
    anchor = org.make_fact("owner", "aws_account:1", {"team": "payments"}, source="x:y")
    anchor2 = org.make_fact("owner", "aws_account:2", {"team": "search"}, source="x:y")
    data = _tag_data({}, inventory=inv, envs={"production": 100.0, "dev": 50.0},
                     rules=[{"tag_key": "cost-centre", "tag_value_pattern": "*",
                             "maps_to_field": "service", "maps_to_value": "", "priority": 1},
                            {"tag_key": "team", "tag_value_pattern": "paymnts",
                             "maps_to_field": "team", "maps_to_value": "payments",
                             "priority": 2}])
    facts = tags_adapter.propose(ctx_for(data=data, facts=[anchor, anchor2]))
    keys = {(f.value["canonical"], f.source): (f.value["keys"], f.confidence)
            for f in facts if f.fact == "tag_key"}
    assert keys[("team", "tags:resource_inventory")] == (["group", "Team"], 0.5)
    assert keys[("owner", "tags:resource_inventory")] == (["Owner"], 0.8)
    assert keys[("environment", "tags:resource_inventory")] == (["Env"], 0.85)
    assert keys[("cost_center", "tags:resource_inventory")] == (["CostCenter"], 0.8)
    assert keys[("service", "tags:tag_rules")] == (["cost-centre"], 0.9)
    aliases = {f.subject.id: (f.value, f.confidence) for f in facts if f.fact == "tag_alias"}
    assert aliases["paymnts"] == ({"canonical_key": "team", "canonical_value": "payments"}, 0.9)
    assert aliases["production"] == ({"canonical_key": "environment",
                                      "canonical_value": "prod"}, 0.75)
    assert aliases["dev"][0]["canonical_value"] == "nonprod"
    assert "prod" not in aliases                          # already an environment


# ── workload ──────────────────────────────────────────────────────────────────

def test_workload_unknown_stays_unknown_and_weak_evidence_never_says_nonprod():
    rows = [{"provider": "aws", "account_id": a, "name": n, "parent_id": "", "status": "ACTIVE",
             "tags": {}, "is_management_account": False}
            for a, n in (("100000000001", "acme-dev"), ("100000000002", "legacy-misc"),
                         ("100000000003", "mixed"), ("100000000004", "untagged"))]
    data = CostData(
        month=MONTH,
        accounts={("aws", "100000000001"): 100.0, ("aws", "100000000002"): 100.0,
                  ("aws", "100000000003"): 100.0, ("aws", "100000000004"): 100.0},
        envs={"100000000003": {"production": 50.0, "dev": 50.0},
              "100000000004": {"production": 95.0}},
        org_accounts=rows,
        namespaces={("prod-eks", "payments"): {"usd": 900.0, "labels": {"env": "production"}},
                    ("dev-eks", "ci-runners"): {"usd": 300.0, "labels": {}},
                    ("dev-eks", "feature-login"): {"usd": 120.0, "labels": {}},
                    ("a", "api"): {"usd": 10.0, "labels": {"env": "prod"}},
                    ("b", "api"): {"usd": 10.0, "labels": {"env": "dev"}}})
    facts = wl.propose(ctx_for(data=data))
    envs = {str(f.subject): (f.value["env"], f.confidence) for f in facts}
    assert envs == {
        "aws_account:100000000001": ("nonprod", 0.55),     # the account name
        "aws_account:100000000004": ("prod", 0.8),         # 95% of its spend tagged production
        "k8s_namespace:payments": ("prod", 0.8),
        "k8s_namespace:feature-login": ("nonprod", 0.55),  # an ephemeral namespace name
        "k8s_namespace:a/api": ("prod", 0.8),              # one name, two clusters, two answers
        "k8s_namespace:b/api": ("nonprod", 0.75),
    }
    # legacy-misc says nothing; "mixed" is half prod half dev; ci-runners
    # is nonprod only by its cluster's name, which is weak evidence.
    assert all(f.status == "proposed" for f in facts)


def test_workload_leaves_subjects_another_adapter_answered():
    other = org.make_fact("environment", "aws_account:100000000001", {"env": "shared"},
                          source="aws_org:x")
    rows = [{"provider": "aws", "account_id": "100000000001", "name": "acme-dev",
             "parent_id": "", "status": "ACTIVE", "tags": {}, "is_management_account": False}]
    assert wl.propose(ctx_for(data=CostData(org_accounts=rows), facts=[other])) == []


def test_env_words_disagreeing_is_unknown():
    assert env_of_name("payments-prod") == ("prod", "prod")
    assert env_of_name("non-prod") == ("nonprod", "non-prod")
    assert env_of_name("prod-dr") == ("dr", "dr")
    assert env_of_name("dev-sandbox") == ("sandbox", "sandbox")
    assert env_of_name("staging-prod") is None
    assert env_of_name("security") == ("shared", "security")
    assert env_of_name("security", accounts=False) is None
    assert env_of_name("production-ish") is None or env_of_name("production-ish")[0] == "prod"


# ── the run: proposals only ───────────────────────────────────────────────────

def test_adapters_never_write_confirmed_facts(odir, monkeypatch):
    def rogue(ctx):
        f = org.make_fact("owner", "aws_account:123456789012", {"team": "payments"},
                          source="rogue:x", confidence=1.0)
        f.status, f.confirmed_by, f.confirmed_at = "confirmed", "@mallory", "2026-01-01"
        return [f, "not a fact"]

    def broken(ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "ADAPTERS", [rogue, broken])
    runs = org.run_adapters()
    assert [(r.id, r.error) for r in runs] == [("rogue", None), ("broken", "RuntimeError: boom")]
    assert runs[0].counts == {"added": 1}
    saved = org.load().facts
    assert [(f.status, f.confirmed_by, f.confirmed_at) for f in saved] == \
        [("proposed", None, None)]


def test_propose_many_matches_propose(odir):
    a = org.make_fact("owner", "aws_account:111111111111", {"team": "a"}, source="x:y")
    b = org.make_fact("owner", "aws_account:222222222222", {"team": "b"}, source="x:y")
    org.propose(b)
    org.reject(b.key, human("@maria"))
    bad = org.make_fact("owner", "aws_account:333333333333", {"team": "c"}, source="x:y")
    bad.value = {}
    assert org.propose_many([a, a, b, bad]) == ["added", "duplicate", "suppressed_rejected",
                                                "invalid"]
    org.confirm(a.key, human("@maria"))
    rival = org.make_fact("owner", "aws_account:111111111111", {"team": "z"}, source="x:y")
    assert org.propose_many([rival]) == ["conflict"]


def test_confirm_many_is_all_or_nothing(odir):
    a = org.make_fact("owner", "aws_account:111111111111", {"team": "a"}, source="x:y")
    org.propose(a)
    with pytest.raises(org.OrgError):
        org.confirm_many([a.key, "ffffffffff"], human("@maria"))
    assert org.load().facts[0].status == "proposed"
    assert [f.status for f in org.confirm_many([a.key, a.key], human("@maria"))] == ["confirmed"]
    with pytest.raises(org.OrgError):
        org.confirm_many([a.key], human(""))


# ── init on the fixture org ───────────────────────────────────────────────────

def _snapshot(d: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(d.iterdir()) if p.suffix == ".yaml"}


def test_init_runs_the_adapters_and_prints_what_they_proposed(fixture_org, odir, capsys):
    code, out, _ = run(capsys, "init")
    assert code == 0
    for name in ("codeowners", "terraform", "aws_org", "tags", "workload"):
        assert f"    {name}: " in out, name
    assert "aws_account:111111111111 is owned by team payments, people @bob  $12,000/mo" in out
    # The model is not in the repo, so each repo path names its repo.
    assert "repo_path:platform//infra/payments is owned by team payments" in out
    m = org.load()
    assert m.facts and all(f.status == "proposed" for f in m.facts)
    assert {f.source.split(":")[0] for f in m.facts} == \
        {"codeowners", "terraform", "aws_org", "tags", "workload"}
    # Every dollar figure an adapter attached is this month's spend, never more.
    for f in m.facts:
        if f.subject.kind == "aws_account" and f.dollars_monthly:
            assert f.dollars_monthly == SPEND[f.subject.id]


def test_init_twice_proposes_nothing_new(fixture_org, odir, capsys):
    assert run(capsys, "init")[0] == 0
    before = _snapshot(odir)
    code, out, _ = run(capsys, "init")
    assert code == 0
    assert _snapshot(odir) == before
    lines = [ln for ln in out.splitlines() if ln.startswith("    ") and ": " in ln
             and ln.split(":")[0].strip() in ("codeowners", "terraform", "aws_org", "tags",
                                              "workload")]
    assert len(lines) == 5
    assert all("nothing to propose" in ln or ("already there" in ln and " new" not in ln)
               for ln in lines), lines
    keys = [f.key for f in org.load().facts]
    assert len(keys) == len(set(keys))


def test_init_without_adapters_and_with_an_extra_repo(odir, fresh_db, tmp_path, monkeypatch,
                                                      capsys):
    seed_db(fresh_db)
    extra = build_repo(tmp_path / "platform")
    here = tmp_path / "app"
    (here / ".git").mkdir(parents=True)
    w(here, "CODEOWNERS", "/deploy/ @acme/app\n")
    w(here, "deploy/main.tf", "")
    monkeypatch.chdir(here / "deploy")
    code, out, _ = run(capsys, "init", "--no-adapters")
    assert code == 0 and "  adapters (" not in out
    assert org.load().facts == []
    code, out, _ = run(capsys, "init", "--repo", str(extra / "infra"))
    assert code == 0
    sources = {f.source for f in org.load().facts}
    # The repo nable runs in is read first, unprefixed; the extra one is
    # named in the source. The model is outside both repos, so every repo path
    # names the repo it is in.
    assert "codeowners:CODEOWNERS:1" in sources
    assert any(s.startswith("codeowners:platform/.github/CODEOWNERS:") for s in sources)
    m = org.load()
    assert m.owner_of("repo_path:platform//infra/payments/envs/prod").team == "payments"
    assert m.owner_of("repo_path:app//deploy").team == "app"
    assert m.owner_of("repo_path:app//infra/payments/envs/prod") is None


def test_week_one_questions_are_few_bulk_and_cover_most_spend(fixture_org, odir, capsys):
    run(capsys, "init")
    qs = org.questions()
    assert 1 <= len(qs) <= 10
    first = qs[0]
    assert first.kind == "bulk" and first.group == "owner:payments"
    assert first.command == f"nable org confirm --owner-bulk payments@{first.digest}"
    assert first.digest == org.bulk_digest(first.keys)
    assert first.text.startswith("payments owns these ")
    assert "$18,500/mo" in first.text          # 111 + 222 + payments-tagged spend in 444
    covered: set[str] = set()
    for q in qs:
        for s in q.subjects + ([q.subject] if q.subject else []):
            if s.startswith("aws_account:"):
                covered.add(s.split(":", 1)[1])
    assert sum(SPEND[a] for a in covered) > 0.5 * TOTAL
    # The owner questions come first; unowned accounts are asked, not guessed.
    kinds = [q.kind for q in qs]
    assert "unowned" in kinds
    assert {q.subject for q in qs if q.kind == "unowned"} >= \
        {"aws_account:666666666666", "aws_account:777777777777"}
    code, out, _ = run(capsys, "questions", "--json")
    assert code == 0 and len(json.loads(out)["questions"]) <= 10


def test_bulk_confirm_is_a_human_decision(fixture_org, odir, capsys):
    run(capsys, "init")
    code, _, err = run(capsys, "confirm", "--owner-bulk", "payments")
    assert code == 2 and "--as" in err
    assert all(f.status == "proposed" for f in org.load().facts)
    code, _, err = run(capsys, "confirm", "--as", "@maria")
    assert code == 2
    code, _, _ = run(capsys, "confirm", "--owner-bulk", "nobody", "--as", "@maria")
    assert code == 1


def test_confirming_the_bulk_owner_questions_reaches_80_percent(fixture_org, odir, capsys):
    """The Phase 1 exit on the fixture org: init, answer yes to the bulk owner
    questions (as a person, through the CLI), and 80% of the month's spend
    has a confirmed owner."""
    run(capsys, "init")
    before = org.coverage()
    assert before["spend_total"] == TOTAL and before["pct_confirmed"] == 0.0
    qs = org.questions()
    asked = 0
    for q in qs:
        if q.kind == "bulk" and q.group.startswith("owner:"):
            argv = q.command.split()[2:] + ["--as", "@maria"]
            code, _, err = run(capsys, *argv)
            assert code == 0, err
            asked += 1
    assert asked <= 10
    cov = org.coverage()
    assert cov["spend_total"] == TOTAL
    assert cov["pct_confirmed"] >= 80.0, cov
    assert cov["by_team"]["payments"]["confirmed"] == 18500.0
    m = org.load()
    assert m.owner_of("aws_account:111111111111").confirmed
    assert m.owner_of("repo_path:platform//infra/payments/envs/prod/x.tf").team == "payments"
    assert m.team_for_tags({"team": "payments-svc"}).team == "payments"
    # Confirmed by the person, never by an adapter.
    assert {f.confirmed_by for f in m.facts if f.confirmed} == {"@maria"}
    # And the list shrinks: what was answered is not asked again.
    assert not any(q.group == "owner:payments" for q in org.questions())


def test_a_contested_subject_is_asked_on_its_own(odir):
    for team, conf in (("payments", 0.8), ("search", 0.4)):
        org.propose(org.make_fact("owner", "aws_account:111111111111", {"team": team},
                                  source="x:y", confidence=conf, dollars_monthly=100.0))
    for acct in ("222222222222", "333333333333"):
        org.propose(org.make_fact("owner", f"aws_account:{acct}", {"team": "payments"},
                                  source="x:y", dollars_monthly=50.0))
    qs = org.questions(include_spend=False)
    bulk = [q for q in qs if q.kind == "bulk"]
    assert len(bulk) == 1 and set(bulk[0].subjects) == {"aws_account:222222222222",
                                                        "aws_account:333333333333"}
    singles = {(q.subject, q.fact["value"]["team"]) for q in qs if q.kind == "confirm"}
    assert singles == {("aws_account:111111111111", "payments"),
                       ("aws_account:111111111111", "search")}


def test_bulk_interview_yes_no_and_edit(odir, monkeypatch, capsys):
    for acct, team in (("111111111111", "payments"), ("222222222222", "payments"),
                       ("333333333333", "search"), ("444444444444", "search"),
                       ("555555555555", "data"), ("666666666666", "data")):
        org.propose(org.make_fact("owner", f"aws_account:{acct}", {"team": team},
                                  source="x:y", dollars_monthly=float(acct[0]) * 100))
    monkeypatch.setattr(cli, "_is_tty", lambda: True)
    answers = iter(["e", "y", "n", "n", ""])     # data: edit (y, n); search: no; payments: yes
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    code, _, _ = run(capsys, "init", "--as", "@maria", "--no-adapters")
    assert code == 0
    st = {f.subject.id: f.status for f in org.load().facts}
    assert st == {"555555555555": "confirmed", "666666666666": "rejected",
                  "333333333333": "rejected", "444444444444": "rejected",
                  "111111111111": "confirmed", "222222222222": "confirmed"}


def test_installed_pack_adapters_run_after_the_built_in_ones(monkeypatch, tmp_path):
    """Adapters from installed packs (finops.packs.org_adapters) run in the
    same init, and what they return is written as a proposal like any other."""
    from finops import org, packs
    from finops.org import store

    calls = []

    class FakePackAdapter:
        name = "pack:io.example/owners/csv"

        def __call__(self, ctx=None):
            calls.append(ctx)
            f = org.make_fact("owner", {"kind": "aws_account", "id": "999999999999"},
                              {"team": "research"}, source="pack:io.example/owners:csv",
                              confidence=0.7)
            return [f]

    monkeypatch.setattr(store, "ADAPTERS", [])
    monkeypatch.setattr(packs, "org_adapters", lambda: [FakePackAdapter()])
    runs = store.run_adapters(tmp_path / "org")
    assert [r.id for r in runs] == ["pack:io.example/owners/csv"]
    assert calls and runs[0].results == ["added"]
    fact = org.load(tmp_path / "org").by_kind("owner")[0]
    assert fact.status == "proposed" and fact.source.startswith("pack:")


def test_a_broken_pack_install_never_stops_init(monkeypatch, tmp_path):
    from finops import packs
    from finops.org import store

    def boom():
        raise RuntimeError("index unreadable")

    monkeypatch.setattr(store, "ADAPTERS", [])
    monkeypatch.setattr(packs, "org_adapters", boom)
    assert store.run_adapters(tmp_path / "org") == []
