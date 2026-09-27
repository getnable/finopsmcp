# SPDX-License-Identifier: Apache-2.0
"""packs/org-bootstrap: Backstage and GitHub teams adapters, end to end.

The pack validates, and installs into a throwaway data dir signed with a
throwaway stand-in for nable's first-party key (a release is signed with the
real one), which is what lets its code run. An unsigned copy (which cannot
claim first-party) runs only once the org allowlists its exact files by
content digest. Its adapters run through the broker, and `nable org init` writes what they return
as proposals whose source names the pack. The GitHub and Backstage APIs are
a local HTTP server on 127.0.0.1 that the test starts; nothing reaches the
network, and a pack copy that declares no host makes no connection at all.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from finops import org
from finops.org import store as org_store
from finops.packs import broker, sdk, store
from finops.packs import install as inst
from finops.packs.errors import PackError, PolicyRefusal
from finops.setup_wizard import main
from tests import packs_support
from tests.packs_support import copy_pack, sign_pack

packs_env = packs_support.packs_env
first_party_key = packs_support.first_party_key

REPO = Path(__file__).resolve().parent.parent
PACK = REPO / "packs" / "org-bootstrap"
PID = "io.github.getnable/org-bootstrap"
# Made up for this run, and long enough for the broker's redaction.
TOKEN = "ghtest-" + "5f1e0c2a9b7d4e3f"  # pragma: allowlist secret
BACKSTAGE_TOKEN = "bstest-" + "9a8b7c6d5e4f3a2b"  # pragma: allowlist secret

CATALOG = """\
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: checkout
  title: Checkout
spec:
  type: service
  lifecycle: production
  owner: group:default/payments
---
apiVersion: backstage.io/v1alpha1
kind: API
metadata:
  name: checkout-api
spec:
  type: openapi
  owner: payments
"""
GROUPS = """\
apiVersion: backstage.io/v1alpha1
kind: Group
metadata:
  name: payments
spec:
  type: team
  profile:
    displayName: Payments
  parent: group:default/commerce
  members: [user:default/alice, bob]
  children: []
---
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: search
  namespace: retail
spec:
  type: service
  owner: user:default/carol
"""


# ── a mock HTTP API on 127.0.0.1 ──────────────────────────────────────────────

class MockApi:
    """A local HTTP server answering from `routes` ({path: (status, body,
    headers)}), recording every request's path, query and Authorization."""

    def __init__(self):
        self.routes: dict[str, tuple[int, object, dict[str, str]]] = {}
        self.requests: list[dict[str, str]] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                u = urlsplit(self.path)
                api.requests.append({"path": u.path, "query": u.query,
                                     "auth": self.headers.get("Authorization", "")})
                key = u.path + ("?" + u.query if f"{u.path}?{u.query}" in api.routes else "")
                status, body, headers = api.routes.get(key, (404, {"message": "Not Found"},
                                                             {}))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api():
    a = MockApi()
    yield a
    a.close()


def github_routes(api: MockApi, prefix: str = "/api") -> None:
    org_ = f"{prefix}/orgs/acme"
    api.routes.update({
        f"{org_}/teams": (200, [{"slug": "payments", "name": "Payments", "parent": None}],
                          {"Link": f'<{api.base}{org_}/teams?page=2>; rel="next"'}),
        f"{org_}/teams?page=2": (200, [{"slug": "platform", "name": "Platform",
                                        "parent": {"slug": "engineering"}}], {}),
        f"{org_}/teams/payments/members": (200, [{"login": "alice"}, {"login": "bob"}], {}),
        f"{org_}/teams/platform/members": (200, [{"login": "carol"}], {}),
        f"{org_}/teams/payments/repos": (200, [
            {"full_name": "acme/checkout", "permissions": {"admin": True, "push": True}},
            {"full_name": "acme/infra", "permissions": {"push": True}},
            {"full_name": "acme/old", "archived": True, "permissions": {"admin": True}}], {}),
        f"{org_}/teams/platform/repos": (200, [
            {"full_name": "acme/infra", "role_name": "maintain",
             "permissions": {"maintain": True}},
            {"full_name": "acme/checkout", "permissions": {"maintain": True}}], {}),
    })


# ── importing the pack's code for its own unit tests ─────────────────────────

@pytest.fixture
def pack_code(monkeypatch):
    """The pack's modules, imported from packs/org-bootstrap without writing
    bytecode there (install refuses a __pycache__)."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.syspath_prepend(str(PACK))
    from org_bootstrap import backstage, github_teams, web
    yield backstage, github_teams, web
    for mod in [m for m in sys.modules if m.startswith("org_bootstrap")]:
        sys.modules.pop(mod, None)


def _ctx(entry: str, *, network=(), secrets=None, data=None):
    return sdk.Context.for_testing(
        pack_id=PID, kind="adapters", entry_id=entry,
        capabilities={"read_data": ["repo.files"], "network": list(network),
                      "secrets": ["BACKSTAGE_TOKEN", "BACKSTAGE_URL", "GITHUB_API_URL",
                                  "GITHUB_ORG", "GITHUB_TOKEN"]},
        secrets=secrets or {}, data=data or {})


# ── the manifest ──────────────────────────────────────────────────────────────

def test_the_pack_validates_as_nable_pack_validate_does(capsys):
    with pytest.raises(SystemExit) as ei:
        main(["pack", "validate", str(PACK)])
    out = capsys.readouterr().out
    assert ei.value.code == 0, out
    assert out.startswith(f"OK {PID} 1.0.0 (first-party)")
    assert "adapter backstage, adapter github-teams" in out
    r = inst.validate_dir(PACK)
    assert r["ok"] and not r["problems"], r
    # The only warning: this unsigned copy cannot be installed as first-party.
    [warning] = r["warnings"]
    assert warning.startswith(f"install will refuse it: {PID} says it is first-party")
    assert r["capabilities"] == {
        "read_data": ["repo.files"],
        "secrets": ["BACKSTAGE_TOKEN", "BACKSTAGE_URL", "GITHUB_API_URL", "GITHUB_ORG",
                    "GITHUB_TOKEN"],
        "network": ["api.github.com"], "write_org": ["proposals"], "max_autonomy": "L1"}
    assert not any(p.suffix == ".pyc" or p.name == "__pycache__" for p in PACK.rglob("*"))
    # Shipped unsigned: a release is signed with nable's first-party key.
    assert r["signature"]["status"] == "unsigned"


def test_nothing_in_the_pack_uses_an_em_dash_or_an_exclamation_point():
    for p in sorted(PACK.rglob("*")):
        if p.is_file():
            text = p.read_text(encoding="utf-8")
            assert chr(0x2014) not in text, p
            assert chr(33) not in text, p


# ── the adapters on their own ─────────────────────────────────────────────────

def test_backstage_reads_entities_into_owner_and_team_proposals(pack_code):
    backstage, _, _ = pack_code
    repo = {"id": 0, "name": "github.com/acme/shop", "label": "",
            "subject_prefix": "repo_path:github.com/acme/shop//"}
    facts = backstage.file_facts(repo, "services/checkout/catalog-info.yaml", CATALOG)
    got = {(f["fact"], json.dumps(f["subject"], sort_keys=True)): f for f in facts}
    assert got[("owner", '{"id": "checkout", "kind": "service"}')]["value"] == {
        "team": "payments"}
    assert got[("owner", '{"id": "checkout-api", "kind": "service"}')]["confidence"] == 0.8
    path = got[("owner", '"repo_path:github.com/acme/shop//services/checkout"')]
    assert path["value"] == {"team": "payments"} and path["confidence"] == 0.7
    assert path["source"] == "backstage:services/checkout/catalog-info.yaml"
    facts = backstage.file_facts({**repo, "label": "shop/"}, "catalog-info.yaml", GROUPS)
    team = next(f for f in facts if f["fact"] == "team")
    assert team["subject"] == {"kind": "team", "id": "payments"}
    assert team["value"] == {"name": "Payments", "parent": "commerce",
                             "people": ["alice", "bob"]}
    assert team["source"] == "backstage:shop/catalog-info.yaml#0"
    person = next(f for f in facts if f["fact"] == "owner" and "people" in f["value"])
    assert person["subject"] == {"kind": "service", "id": "retail/search"}
    assert person["value"] == {"team": "carol", "people": ["carol"]}
    assert person["confidence"] == pytest.approx(0.48)
    assert backstage.file_facts(repo, "x/catalog-info.yaml", "kind: [unclosed") == []
    assert backstage.file_facts(repo, "x/catalog-info.yaml", "apiVersion: v1\nkind: Pod\n") == []


def test_backstage_asks_nable_for_the_files_by_name_only(pack_code):
    backstage, _, _ = pack_code
    asked = []

    def files(query):
        asked.append(query)
        return {"files": [{"repo": 0, "path": "catalog-info.yaml", "text": CATALOG}],
                "truncated": False}

    ctx = _ctx("backstage", data={"repo.files": files})
    context = {"repos": [{"id": 0, "name": "shop", "label": "",
                          "subject_prefix": "repo_path:"}]}
    facts = backstage.propose(ctx, context)
    assert asked == [{"names": ["catalog-info.yaml", "catalog-info.yml"]}]
    assert "repo_path:." in [f["subject"] for f in facts]
    assert backstage.propose(ctx, {}) == []          # no repos named, nothing read


def test_github_teams_proposes_teams_and_repo_owners(pack_code, api):
    _, github_teams, _ = pack_code
    github_routes(api)
    ctx = _ctx("github-teams", network=[f"127.0.0.1:{api.port}"],
               secrets={"GITHUB_TOKEN": TOKEN, "GITHUB_ORG": "acme",
                        "GITHUB_API_URL": f"{api.base}/api"})
    facts = github_teams.propose(ctx, {})
    teams = {f["subject"]["id"]: f for f in facts if f["fact"] == "team"}
    assert teams["payments"]["value"] == {"name": "Payments", "people": ["@alice", "@bob"]}
    assert teams["platform"]["value"] == {"name": "Platform", "parent": "engineering",
                                          "people": ["@carol"]}
    owners = {f["subject"]: f for f in facts if f["fact"] == "owner"}
    host = "127.0.0.1"                    # an Enterprise Server's own host
    checkout = owners[f"repo_path:{host}/acme/checkout//."]
    assert checkout["value"] == {"team": "payments", "co_owners": ["platform"]}
    assert checkout["confidence"] == 0.6
    assert checkout["source"] == "github-teams:acme/payments:admin"
    infra = owners[f"repo_path:{host}/acme/infra//."]
    assert infra["value"] == {"team": "platform"} and infra["confidence"] == 0.5
    assert not any("old" in s for s in owners)     # archived
    assert TOKEN not in json.dumps(facts)
    assert {r["auth"] for r in api.requests} == {f"Bearer {TOKEN}"}
    assert github_teams.web_host("https://api.github.com") == "github.com"


def test_github_teams_reads_nothing_without_its_secrets_or_a_declared_host(pack_code, api):
    _, github_teams, _ = pack_code
    github_routes(api)
    assert github_teams.propose(_ctx("github-teams", network=[f"127.0.0.1:{api.port}"]),
                                {}) == []
    secrets = {"GITHUB_TOKEN": TOKEN, "GITHUB_ORG": "acme", "GITHUB_API_URL": f"{api.base}/api"}
    for network in ([], ["api.github.com"], ["127.0.0.1:1"]):
        assert github_teams.propose(_ctx("github-teams", network=network, secrets=secrets),
                                    {}) == []
    assert github_teams.propose(_ctx("github-teams", network=["evil.example"],
                                     secrets={**secrets, "GITHUB_API_URL":
                                              "http://evil.example/api"}), {}) == []
    assert api.requests == []


def test_a_redirect_is_refused_so_the_token_never_follows_it(pack_code, api):
    _, _, web = pack_code
    api.routes["/moved"] = (302, {}, {"Location": "http://127.0.0.2:9/steal"})
    with pytest.raises(web.HttpError, match="redirect refused") as ei:
        web.get_json(f"{api.base}/moved", TOKEN)
    assert TOKEN not in str(ei.value)
    api.routes["/fail"] = (401, {"message": "Bad credentials"}, {})
    with pytest.raises(web.HttpError, match=r"^/fail: HTTP 401$"):
        web.get_json(f"{api.base}/fail", TOKEN)
    assert web.next_link({"link": '<https://elsewhere.example/x?page=2>; rel="next"'},
                         "https://api.github.com/orgs/acme/teams") is None


# ── through the broker, as `nable org init` runs them ─────────────────────────

@pytest.fixture
def bootstrap(packs_env, first_party_key, tmp_path, monkeypatch):
    """A repo with catalog files, the pack's secrets in its own vault
    entries, and a helper that installs a copy of the pack (optionally with
    other network hosts) signed with the (test) first-party key, or, with
    signed=False, an unsigned copy that claims only community support and,
    unless allow=False, is allowlisted by its exact files."""
    vault: dict[str, str] = {}
    monkeypatch.setattr(broker, "_vault_get", vault.get)
    monkeypatch.setenv("FINOPS_ORG_DIR", str(tmp_path / "org"))
    # Outside any repository, so org init reads only the one named here.
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "shop"
    (repo / ".git").mkdir(parents=True)
    (repo / "services" / "checkout").mkdir(parents=True)
    (repo / "services" / "checkout" / "catalog-info.yaml").write_text(CATALOG)
    (repo / "catalog-info.yaml").write_text(GROUPS)
    (repo / "node_modules" / "dep").mkdir(parents=True)
    (repo / "node_modules" / "dep" / "catalog-info.yaml").write_text(
        CATALOG.replace("payments", "vendored"))

    def install(network: list[str] | None = None, *, signed: bool = True,
                allow: bool = True) -> str:
        src = copy_pack(PACK, tmp_path / f"src-{len(list(tmp_path.glob('src-*')))}")
        m = src / "nable-pack.toml"
        if network is not None:
            m.write_text(m.read_text().replace('network      = ["api.github.com"]',
                                               f"network      = {json.dumps(network)}"))
        if signed:
            sign_pack(src, first_party_key)
        else:
            m.write_text(m.read_text().replace('support     = "first-party"',
                                               'support     = "community"'))
        inst.install(str(src), yes=True)
        if allow and not signed:
            digest = store.content_digest(store.read_index()["packs"][PID]["files"])
            packs_env.policy(f"packs:\n  allow_unsigned_code: [{PID}@{digest}]\n")
        return PID

    def secret(name: str, value: str) -> None:
        vault[broker.vault_entry_name(PID, name)] = value

    packs_env.install, packs_env.secret, packs_env.repo = install, secret, repo
    return packs_env


def test_signed_with_the_first_party_key_it_runs_with_no_allowlist(bootstrap, tmp_path):
    bootstrap.install()
    pol = tmp_path / "nable.policy.yaml"
    assert not pol.exists() or "allow_unsigned_code" not in pol.read_text()
    assert [a.name for a in broker.org_adapters()] == [f"pack:{PID}/backstage",
                                                       f"pack:{PID}/github-teams"]
    r = broker.propose_facts(PID, "backstage", repos=broker.repo_refs(roots=[bootstrap.repo]))
    assert r.output and r.network["observed"] == []


def test_unsigned_its_first_party_claim_is_refused(bootstrap, tmp_path):
    src = copy_pack(PACK, tmp_path / "unsigned")
    with pytest.raises(PackError, match="first-party"):
        inst.install(str(src), yes=True)
    assert PID not in store.read_index()["packs"]


def test_unsigned_code_runs_only_once_the_org_allowlists_its_digest(bootstrap):
    bootstrap.install(signed=False, allow=False)
    with pytest.raises(PolicyRefusal) as ei:
        broker.propose_facts(PID, "backstage")
    digest = store.content_digest(store.read_index()["packs"][PID]["files"])
    assert f"allow_unsigned_code: [{PID}@{digest}]" in str(ei.value)
    assert broker.org_adapters() == []
    bootstrap.policy(f"packs:\n  allow_unsigned_code: [{PID}@{'0' * 64}]\n")
    assert broker.org_adapters() == []
    bootstrap.policy(f"packs:\n  allow_unsigned_code: [{PID}@{digest}]\n")
    assert [a.name for a in broker.org_adapters()] == [f"pack:{PID}/backstage",
                                                       f"pack:{PID}/github-teams"]


def _pack_runs(monkeypatch, repos):
    monkeypatch.setattr(org_store, "ADAPTERS", [])
    return {r.id: r for r in org_store.run_adapters(repos=repos)}


def test_org_init_proposes_backstage_facts_with_their_source_and_no_network(
        bootstrap, monkeypatch):
    bootstrap.install()
    runs = _pack_runs(monkeypatch, [bootstrap.repo])
    run = runs[f"pack:{PID}/backstage"]
    assert run.error is None, run.error
    assert runs[f"pack:{PID}/github-teams"].facts == []      # no token: GitHub not read
    props = org.load().proposals()
    mine = [f for f in props if f.source.startswith(f"pack:{PID}:backstage:")]
    subjects = {str(f.subject): f for f in mine}
    assert subjects["service:checkout"].value == {"team": "payments"}
    assert subjects["service:checkout-api"].value == {"team": "payments"}
    assert subjects["service:retail/search"].value == {"team": "carol", "people": ["carol"]}
    assert subjects["team:payments"].value["people"] == ["alice", "bob"]
    path = subjects["repo_path:shop//services/checkout"]
    assert path.source == f"pack:{PID}:backstage:services/checkout/catalog-info.yaml"
    assert all(f.status == "proposed" and not f.confirmed and f.confirmed_by is None
               for f in mine)
    assert not any("vendored" in json.dumps(f.value) for f in props)   # node_modules skipped
    # The pack declares a host, so it ran in audit mode, and it connected nowhere.
    r = broker.propose_facts(PID, "backstage", repos=broker.repo_refs(roots=[bootstrap.repo]))
    assert r.network["observed"] == [] and len(r.output) == len(mine)


def test_nable_org_init_runs_the_pack_and_asks_about_what_it_proposed(bootstrap, capsys):
    bootstrap.install()
    with pytest.raises(SystemExit) as ei:
        main(["org", "init", "--repo", str(bootstrap.repo)])
    out = capsys.readouterr().out
    assert ei.value.code == 0, out
    assert f"pack:{PID}/backstage: 6 proposed (6 new)" in out
    assert f"pack:{PID}/github-teams: nothing to propose" in out
    # The week-one questions name the pack and its reader, and change nothing.
    assert "payments owns these 4 subjects" in out
    assert f"From {PID} backstage." in out
    assert "nable org confirm --owner-bulk payments@" in out
    facts = org.load().facts
    assert facts and all(not f.confirmed for f in facts)


def test_nable_pack_run_shows_the_proposals_for_the_repo_it_runs_in(bootstrap, monkeypatch,
                                                                    capsys):
    bootstrap.install()
    monkeypatch.chdir(bootstrap.repo / "services")
    with pytest.raises(SystemExit) as ei:
        main(["pack", "run", PID, "backstage", "--json"])
    body = json.loads(capsys.readouterr().out)
    assert ei.value.code == 0 and body["summary"] == {"facts": 6}
    assert {f["subject"] for f in body["output"]} >= {"service:checkout",
                                                       "repo_path:shop//services/checkout"}
    assert body["network"]["observed"] == []
    assert org.load().facts == []          # shown, not written


def test_org_init_proposes_github_teams_and_the_token_never_leaks(bootstrap, api,
                                                                    monkeypatch, capsys):
    github_routes(api)
    bootstrap.install(["api.github.com", f"127.0.0.1:{api.port}"])
    bootstrap.secret("GITHUB_TOKEN", TOKEN)
    bootstrap.secret("GITHUB_ORG", "acme")
    bootstrap.secret("GITHUB_API_URL", f"{api.base}/api")
    runs = _pack_runs(monkeypatch, [bootstrap.repo])
    run = runs[f"pack:{PID}/github-teams"]
    assert run.error is None, run.error
    assert run.results.count("added") == len(run.facts) == 4
    mine = [f for f in org.load().proposals() if f.source.startswith(f"pack:{PID}:github-teams:")]
    assert {str(f.subject) for f in mine} == {
        "team:payments", "team:platform", "repo_path:127.0.0.1/acme/checkout//.",
        "repo_path:127.0.0.1/acme/infra//."}
    assert all(f.status == "proposed" and not f.confirmed for f in mine)
    assert {r["auth"] for r in api.requests} == {f"Bearer {TOKEN}"}
    # Not in a proposal, the org files, the pack's log or anything printed.
    assert TOKEN not in json.dumps([f.summary() for f in mine])
    for p in (bootstrap.tmp / "org").rglob("*"):
        if p.is_file():
            assert TOKEN not in p.read_text(), p
    log = broker.log_path(broker.prepare(PID, "github-teams")).read_text()
    assert "github-teams" in log and TOKEN not in log
    r = broker.propose_facts(PID, "github-teams")
    assert TOKEN not in json.dumps(r.to_dict())
    assert {o["host"] for o in r.network["observed"]} == {"127.0.0.1"}
    assert all(o["allowed"] for o in r.network["observed"])
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err


def test_a_copy_that_declares_no_network_connects_nowhere(bootstrap, api, monkeypatch):
    github_routes(api)
    bootstrap.install([])
    bootstrap.secret("GITHUB_TOKEN", TOKEN)
    bootstrap.secret("GITHUB_ORG", "acme")
    bootstrap.secret("GITHUB_API_URL", f"{api.base}/api")
    bootstrap.secret("BACKSTAGE_URL", f"{api.base}/backstage")
    r = broker.propose_facts(PID, "github-teams")
    assert r.output == [] and r.network["declared"] == []
    assert r.network["observed"] == [] and api.requests == []
    log = Path(r.log).read_text()
    assert "is not in this pack's declared network (none)" in log and TOKEN not in log
    r = broker.propose_facts(PID, "backstage", repos=broker.repo_refs(roots=[bootstrap.repo]))
    assert r.network["observed"] == [] and api.requests == []
    assert any(str(f.subject) == "service:checkout" for f in r.output)


def test_the_backstage_api_is_read_when_its_host_is_declared(bootstrap, api):
    q = "filter=kind=component&filter=kind=system&filter=kind=api&filter=kind=group&limit=500"
    path = "/backstage/api/catalog/entities/by-query"
    api.routes[f"{path}?{q}"] = (200, {"items": [
        {"apiVersion": "backstage.io/v1alpha1", "kind": "Component",
         "metadata": {"name": "ledger", "namespace": "default"},
         "spec": {"owner": "group:default/finance"}}],
        "pageInfo": {"nextCursor": "c2"}}, {})
    api.routes[f"{path}?{q}&cursor=c2"] = (200, {"items": [
        {"apiVersion": "backstage.io/v1alpha1", "kind": "Group",
         "metadata": {"name": "finance"}, "spec": {"type": "team", "children": []}}],
        "pageInfo": {}}, {})
    bootstrap.install(["api.github.com", f"127.0.0.1:{api.port}"])
    bootstrap.secret("BACKSTAGE_URL", f"{api.base}/backstage/")
    bootstrap.secret("BACKSTAGE_TOKEN", BACKSTAGE_TOKEN)
    r = broker.propose_facts(PID, "backstage")
    subjects = {str(f.subject): f for f in r.output}
    ledger = subjects["service:ledger"]
    assert ledger.value == {"team": "finance"} and ledger.status == "proposed"
    assert ledger.source == f"pack:{PID}:backstage-api:127.0.0.1:component:default/ledger"
    assert "team:finance" in subjects
    assert [r_["query"].endswith("cursor=c2") for r_ in api.requests] == [False, True]
    assert {r_["auth"] for r_ in api.requests} == {f"Bearer {BACKSTAGE_TOKEN}"}
    assert BACKSTAGE_TOKEN not in json.dumps(r.to_dict())
    assert BACKSTAGE_TOKEN not in Path(r.log).read_text()
    assert parse_qs(api.requests[0]["query"])["filter"] == [
        "kind=component", "kind=system", "kind=api", "kind=group"]
