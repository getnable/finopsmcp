# SPDX-License-Identifier: Apache-2.0
"""The first-party change-control pack (packs/change-control), end to end.

What has to stay true:
  - it validates as shipped, declares no network, no secrets and no act,
    and installs (signed) into a throwaway home
  - its guard rules only tighten: deploys ask and teardowns are denied only
    while a change freeze covers them; a freeze nobody confirmed only asks;
    admin merges, force pushes to protected branches and branch protection
    changes are denied always
  - its freeze-window templates render into org facts that load as
    proposals, and a proposal only ever makes the guard ask
  - its adapter proposes approval chains from CODEOWNERS and GitHub exports
    through the broker, with no network, and never confirms one
  - its report turns the ledger into CC8.1 evidence (who approved what,
    when, under which policy; the chain verified), as markdown and JSON,
    says it is evidence and not a certification, and says so when the
    ledger was tampered with
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import finops.guard as g
import finops.guard_ledger as gl
import finops.guard_packs as gpk
from finops import ai_budget, guard_approvals, guard_org, org
from finops.org.cli import _who as human
from finops.packs import broker, reports
from finops.setup_wizard import main
from tests import packs_support
from tests.packs_support import copy_pack, sign_pack

packs_env = packs_support.packs_env
first_party_key = packs_support.first_party_key

PACK = Path(__file__).resolve().parent.parent / "packs" / "change-control"
PID = "io.github.getnable/change-control"
DEPLOY = "kubectl apply -f deploy.yaml"
DESTROY = "terraform destroy -auto-approve"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD", "FINOPS_GUARD_TEAM",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_ACCOUNT", "FINOPS_POLICY_FILE",
                "FINOPS_PROFILE", "FINOPS_DATA_DIR", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_ON_BUDGET_BREACH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "no-accounts.yaml"))
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})
    db = sys.modules.get("finops.storage.db")
    if db is not None:
        monkeypatch.setattr(db, "_DATA_DIR", None)


def _cli(capsys, *argv: str) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as ei:
        main(list(argv))
    out = capsys.readouterr()
    return ei.value.code, out.out, out.err


@pytest.fixture
def installed(packs_env, first_party_key, tmp_path, capsys):
    """The pack, signed with the (test) first-party key and installed through
    the CLI into this test's packs root, under a throwaway HOME."""
    src = copy_pack(PACK, tmp_path / "change-control")
    sign_pack(src, first_party_key)
    code, out, err = _cli(capsys, "pack", "install", str(src), "--yes", "--json")
    assert code == 0, err
    body = json.loads(out)
    assert body["status"] == "installed" and body["pack"]["id"] == PID
    gpk.invalidate()
    return src


def _window(hours_ago=1, hours_left=2):
    now = datetime.now(UTC)
    return ((now - timedelta(hours=hours_ago)).isoformat(),
            (now + timedelta(hours=hours_left)).isoformat())


def _freeze(*, confirmed=True, mode="ask"):
    start, end = _window()
    f = org.make_fact("freeze", "org:org", {"start": start, "end": end,
                                            "reason": "Quarter close", "mode": mode},
                      source="human")
    return org.set_fact(f, human("maria")) if confirmed else org.propose(f)


def _verdict(command, **kw):
    v = g.gate_command(command, record=kw.pop("record", False), **kw)
    return v["decision"] if v else None


# ── the manifest ──────────────────────────────────────────────────────────────

def test_it_validates_as_shipped_with_minimal_capabilities(packs_env, capsys):
    code, out, _ = _cli(capsys, "pack", "validate", str(PACK), "--json")
    body = json.loads(out)
    assert code == 0 and body["ok"], body["problems"]
    assert body["id"] == PID and body["tier"] == "first-party"
    assert body["provides"] == {"policies": 7, "guard_rules": 9, "reports": 3, "skills": 1}
    assert body["code"] == [{"kind": "adapters", "id": "approval-chains",
                             "entry": "nable_change_control.approvals:propose"}]
    caps = body["capabilities"]
    assert caps == {"read_data": ["ledger.guard"], "write_org": ["proposals"],
                    "guard": "tighten-only", "max_autonomy": "L1"}
    # nothing that reaches out: no network, no secrets, no tickets or PRs
    assert not {"network", "secrets", "act", "read_cloud", "pricing"} & set(caps)
    # shipped unsigned: a release is signed with nable's first-party key
    assert body["signature"]["status"] == "unsigned"


def test_its_text_has_no_em_dashes_or_exclamation_points():
    for p in PACK.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            text = p.read_text(encoding="utf-8")
            assert "\u2014" not in text, p
            if p.suffix != ".py":
                assert "!" not in text.replace("!=", ""), p


# ── the guard ─────────────────────────────────────────────────────────────────

def test_outside_a_freeze_deploys_are_untouched(installed):
    assert _verdict(DEPLOY) is None
    assert _verdict("helm upgrade web ./chart") is None
    assert _verdict("gh pr merge 12 --squash") is None
    assert _verdict("git push origin feature/x") is None
    # the guard's own ask on a one-way door stands, no looser
    assert _verdict(DESTROY) == "ask"


def test_admin_merges_force_pushes_and_protection_changes_are_always_denied(installed):
    for command in ("gh pr merge 12 --admin --squash",
                    "git push --force origin main",
                    "git push origin main --force-with-lease",
                    "git push origin +main",
                    "gh api -X DELETE repos/acme/infra/branches/main/protection",
                    "gh api repos/acme/infra/rulesets/7 --method PUT --input r.json"):
        v = g.gate_command(command, record=False)
        assert v is not None and v["decision"] == "deny", command
        assert f"of pack {PID}" in v["reason"]
    assert _verdict("git push --force origin feature/x") is None
    assert _verdict("gh api repos/acme/infra/branches/main/protection") is None


def test_a_force_push_is_denied_by_the_branch_it_lands_on_not_a_word_in_it(installed):
    # A rebased feature branch whose name holds "main" is force-pushed every
    # day; denying that would teach people to turn the pack off.
    for command in ("git push --force origin feature/main-menu",
                    "git push -f origin fix-main-page",
                    "git push origin main-fix --force",
                    "git push -f origin my-production-notes",
                    "git push -f origin main:feature/x"):
        assert _verdict(command) is None, command
    for command in ("git push -uf origin main",
                    "git push -fu origin master",
                    "git -C infra push -f origin main",
                    "git push -f origin HEAD:main",
                    "git push -f origin refs/heads/production",
                    "git push origin +HEAD:main",
                    "git push -f main"):
        assert _verdict(command) == "deny", command


def test_during_a_freeze_only_a_push_to_a_release_branch_asks(installed):
    _freeze(confirmed=True)
    assert _verdict("git push origin main") == "ask"
    assert _verdict("git push origin release/1.2") == "ask"
    assert _verdict("git -C infra push origin HEAD:main") == "ask"
    assert _verdict("git push origin fix-main-page") is None
    assert _verdict("git push origin feature/release-notes") is None


def test_a_confirmed_freeze_makes_deploys_ask_and_teardowns_deny(installed):
    _freeze(confirmed=True)
    v = g.gate_command(DEPLOY, record=False)
    assert v["decision"] == "ask"
    assert "A Kubernetes change during a change freeze" in v["reason"]
    assert "A change freeze is in force for the whole org" in v["reason"]
    assert _verdict("helm upgrade web ./chart") == "ask"
    assert _verdict("terraform apply -auto-approve") == "ask"
    assert _verdict("gh pr merge 12 --squash") == "ask"
    v = g.gate_command(DESTROY, record=False)
    assert v["decision"] == "deny" and "waits until the change freeze ends" in v["reason"]
    assert _verdict("helm uninstall web") == "deny"
    assert _verdict("ls -la") is None


def test_the_freeze_templates_load_as_proposals_that_only_ask(installed, monkeypatch, capsys):
    code, text, err = _cli(capsys, "pack", "report", PID, "freeze-windows",
                           "--set", "year=2026", "--set", "next_year=2027",
                           "--set", "offset=+00:00")
    assert code == 0, err
    assert "${" not in text
    org_dir = Path(os.environ["FINOPS_ORG_DIR"])
    (org_dir / "freezes.yaml").write_text(text, encoding="utf-8")
    model = org.load()
    freezes = model.by_kind("freeze")
    assert len(freezes) == 5 and not model.warnings
    assert all(f.status == "proposed" for f in freezes)
    assert all(f.source.startswith(f"pack:{PID}:freeze-windows:") for f in freezes)
    # On Christmas Eve the year-end window is in force: a proposal, so even the
    # deny rule only asks.
    monkeypatch.setattr(guard_org, "_now", lambda: datetime(2026, 12, 24, 12, tzinfo=UTC))
    v = g.gate_command(DEPLOY, record=False)
    assert v["decision"] == "ask" and "proposed, not confirmed" in v["reason"]
    assert _verdict(DESTROY) == "ask"
    monkeypatch.setattr(guard_org, "_now", lambda: datetime(2026, 8, 1, 12, tzinfo=UTC))
    assert _verdict(DEPLOY) is None


# ── the adapter, through the broker ───────────────────────────────────────────

def test_the_adapter_proposes_approval_chains_with_no_network(installed, tmp_path, capsys,
                                                               monkeypatch):
    repo = tmp_path / "infra-repo"
    (repo / ".github").mkdir(parents=True)
    (repo / ".github" / "CODEOWNERS").write_text(
        (PACK / "samples" / "CODEOWNERS").read_text())
    monkeypatch.chdir(tmp_path)
    code, out, err = _cli(capsys, "pack", "run", PID, "approval-chains",
                          "--context", "repo=infra-repo",
                          "--context", f"branch_protection={PACK / 'samples'}/branch-protection.json",
                          "--context", f"environments={PACK / 'samples'}/environments.json",
                          "--json")
    assert code == 0, err
    body = json.loads(out)
    facts = {f["subject"]: f for f in body["output"]}
    assert set(facts) == {"team:platform", "team:payments", "team:search", "environment:prod"}
    pay = facts["team:payments"]
    assert pay["fact"] == "approval" and pay["status"] == "proposed"
    assert pay["value"]["approvers"] == ["team:payments", "team:platform", "github:maria"]
    assert pay["value"]["min"] == 2                      # branch protection's review count
    assert facts["team:search"]["value"]["min"] == 1     # never more than it names
    assert facts["environment:prod"]["value"]["approvers"] == ["team:release-managers",
                                                               "github:maria"]
    assert all(f["source"].startswith(f"pack:{PID}:") for f in body["output"])
    assert body["network"]["declared"] == [] and not [
        o for o in body["network"]["observed"] if o.get("allowed")]
    # nothing was written to the org model: shown, a person decides
    assert org.load().by_kind("approval") == []


def _approvals_module(monkeypatch):
    """The adapter's module, imported as a pack's own tests would (without
    the broker), leaving no bytecode in the pack: a pack ships source only."""
    import importlib.util
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location(
        "_change_control_approvals", PACK / "nable_change_control" / "approvals.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_adapter_never_confirms_even_when_asked_to(tmp_path, monkeypatch):
    approvals = _approvals_module(monkeypatch)
    from finops.packs.sdk import Context
    (tmp_path / "CODEOWNERS").write_text("/infra/ @acme/platform\n/docs/ @dana\n")
    ctx = Context.for_testing(pack_id=PID, kind="adapters")
    facts = approvals.propose(ctx, {"cwd": str(tmp_path)})
    assert [f["subject"] for f in facts] == [{"kind": "team", "id": "platform"}]
    assert all("status" not in f and "confirmed_by" not in f for f in facts)
    facts[0]["status"] = "confirmed"
    problems: list[str] = []
    fact = broker._fact(PID, facts[0], problems, 0)
    assert fact.status == "proposed" and problems


# ── the evidence report ───────────────────────────────────────────────────────

def _ask_and_run(command: str, **kw) -> dict:
    """An ask the guard records, then the post hook's `ran` for it."""
    v = g.gate_command(command, record=True, session_id="s1", **kw)
    assert v["decision"] == "ask", v
    asks = [r for r in gl.read(outcomes=True) if r.get("decision") == "ask"]
    gl.append({"kind": gl.OUTCOME, "outcome": gl.RAN, "harness": "claude-code",
               "event": "PostToolUse", "session": "s1", "tool": "Bash",
               "verdict": asks[-1]["_hash"], "verdict_ts": asks[-1]["ts"],
               "linked_by": "command"})
    return v


def _evidence_ledger(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _ask_and_run(DESTROY)                                       # approved at the prompt
    assert _verdict("gh pr merge 12 --admin", record=True) == "deny"
    # Codex cannot ask: its deny carries an approval a person gives once.
    v = g.gate_command("terraform destroy -target=aws_instance.old", harness="codex",
                       cwd=str(work), record=True, session_id="c1")
    aid = v["reason"].split("nable guard approve ", 1)[1].split("`", 1)[0]
    guard_approvals.approve(aid, human("dana@acme.example"))
    assert g.gate_command("terraform destroy -target=aws_instance.old", harness="codex",
                          cwd=str(work), record=True, session_id="c1") is None
    _freeze(confirmed=True)
    _ask_and_run(DEPLOY)                                        # approved during the freeze


def test_the_report_is_cc81_evidence_in_markdown(installed, tmp_path, capsys):
    _evidence_ledger(tmp_path)
    code, md, err = _cli(capsys, "pack", "report", PID, "cc8.1-evidence")
    assert code == 0, err
    assert md.startswith("# Change-management evidence: SOC 2 CC8.1")
    assert "not a SOC 2 report, an attestation or a certification" in md
    assert "${" not in md
    assert "| Hash chain | verified:" in md
    assert "| Asked a person | 3 |" in md and "| Denied by policy | 1 |" in md
    assert "| Approved at the agent's prompt | 2 |" in md
    assert "then approved from a terminal | 1 |" in md
    assert "| Calls let through once by a person's approval from their own terminal | 1 |" in md
    assert "| Answer unknown | 0 |" in md
    assert "dana@acme.example" in md                   # who approved out of band
    assert "claude-code permission prompt" in md       # an approval the harness cannot name
    assert "freeze" in md and "Quarter close" in md
    assert f"{PID}:approved-during-freeze" in md
    assert f"{PID}:approver-not-named" in md
    assert "\u2014" not in md


def test_the_report_exports_json_with_every_record(installed, tmp_path, capsys):
    _evidence_ledger(tmp_path)
    out_file = tmp_path / "evidence.json"
    code, _, err = _cli(capsys, "pack", "report", PID, "cc8.1-evidence", "--json",
                        "--out", str(out_file))
    assert code == 0, err
    body = json.loads(out_file.read_text())
    ev = body["values"]["ledger"]["guard"]
    assert ev["ledger"]["chain_ok"] and ev["ledger"]["clean"]
    assert "not a SOC 2 report" in ev["note"]
    by_outcome = {c["outcome"]: c for c in ev["changes"]}
    assert set(by_outcome) == {"approved", "denied", "allowed_out_of_band",
                               "approved_later_out_of_band"}
    oob = by_outcome["allowed_out_of_band"]
    assert oob["approved_by"] == "dana@acme.example" and oob["approved_at"]
    # The Codex ask that approval answered points at the call it let through.
    asked = by_outcome["approved_later_out_of_band"]
    assert asked["approved_by"] == "dana@acme.example"
    assert asked["answered_by_line"] == oob["ledger_line"]
    frozen = [c for c in ev["changes"] if c["under_freeze"]]
    assert frozen and frozen[0]["approved"] and frozen[0]["freeze"]["reason"] == "Quarter close"
    assert frozen[0]["policy"]["pack_rules"] == [f"{PID}:kubernetes-change-during-freeze"]
    assert all(c["policy"]["policy_version"] for c in ev["changes"])
    assert all(c["chain_ok"] for c in ev["changes"])
    rules = {e["rule"] for e in ev["exceptions"]}
    assert {"approved-during-freeze", "approver-not-named", "approved-out-of-band"} <= rules


def test_a_ticket_per_change(installed, tmp_path, capsys):
    _evidence_ledger(tmp_path)
    code, out, err = _cli(capsys, "pack", "report", PID, "change-ticket",
                          "--each", "ledger.guard.changes", "--json")
    assert code == 0, err
    tickets = json.loads(out)["text"]
    assert len(tickets) == 5
    assert all(t.startswith("# Change ") and "${" not in t for t in tickets)
    assert any("| Approved by | dana@acme.example |" in t for t in tickets)


def test_a_tampered_ledger_is_reported_not_hidden(installed, tmp_path, capsys):
    _evidence_ledger(tmp_path)
    p = gl.ledger_path()
    lines = p.read_text().splitlines()
    i = next(n for n, line in enumerate(lines) if '"decision":"deny"' in line)
    lines[i] = lines[i].replace('"decision":"deny"', '"decision":"allow"')
    p.write_text("\n".join(lines) + "\n")
    code, md, _ = _cli(capsys, "pack", "report", PID, "cc8.1-evidence")
    assert code == 0
    # The edited record still parses; the one after it no longer chains to it.
    assert f"BROKEN at line {i + 2}" in md
    assert "nable:ledger-chain-broken" in md and "cannot be relied on as evidence" in md


def test_the_report_reads_the_ledger_only_because_the_pack_declares_it(installed,
                                                                         packs_env):
    idx = json.loads((packs_env.root / "index.json").read_text())
    assert idx["packs"][PID]["capabilities"]["read_data"] == ["ledger.guard"]
    assert reports.scopes_in((PACK / "reports" / "cc8.1-evidence.md").read_text()) == \
        ["ledger.guard"]
    # The freeze template reads no data at all.
    assert reports.scopes_in((PACK / "reports" / "freeze-windows.yaml").read_text()) == []


def test_the_adapter_reads_github_exports_conservatively(tmp_path, monkeypatch):
    approvals = _approvals_module(monkeypatch)
    from finops.packs.sdk import Context
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "CODEOWNERS").write_text(
        "# comment\n/infra/ @acme/platform @acme/sre\n/web/ @dana\n"
        "/bad/ not-an-owner\n!/negated/ @acme/x\n/esc\\#aped/ @acme/escaped # trailing\n")
    (tmp_path / "bp.json").write_text(json.dumps({"required_pull_request_reviews": {
        "required_approving_review_count": 5, "require_code_owner_reviews": False}}))
    (tmp_path / "envs.json").write_text(json.dumps({"environments": [
        {"name": "Staging", "protection_rules": [
            {"type": "required_reviewers",
             "reviewers": [{"type": "User", "reviewer": {"login": "sam"}}]}]},
        {"name": "customer-demo", "protection_rules": [
            {"type": "required_reviewers",
             "reviewers": [{"type": "User", "reviewer": {"login": "sam"}}]}]},
        {"name": "production", "protection_rules": [{"type": "wait_timer"}]}]}))
    ctx = Context.for_testing(pack_id=PID, kind="adapters")
    facts = approvals.propose(ctx, {"cwd": str(tmp_path), "branch_protection": "bp.json",
                                    "environments": "envs.json"})
    by = {f"{f['subject']['kind']}:{f['subject']['id']}": f for f in facts}
    # people-only, invalid and negated lines propose nothing; an unknown
    # environment name and one with no required reviewers are left out
    assert set(by) == {"team:platform", "team:escaped", "environment:nonprod"}
    plat = by["team:platform"]
    assert plat["value"]["approvers"] == ["team:platform", "team:sre"]
    assert plat["value"]["min"] == 2                    # 5 asked for, 2 named
    assert plat["confidence"] == 0.4                    # code owners not required
    assert "code owner reviews not required" in plat["source"]
    assert by["environment:nonprod"]["value"]["approvers"] == ["github:sam"]
    # nothing to read is nothing proposed, not an error
    assert approvals.propose(ctx, {"cwd": str(tmp_path / "nowhere")}) == []
