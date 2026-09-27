# SPDX-License-Identifier: Apache-2.0
"""The first-party commitments-bounds pack (packs/commitments-bounds), end to
end.

What has to stay true:
  - it validates as shipped, carries no code, declares no network, secrets,
    act or pricing, and installs (signed) into a throwaway home
  - with it installed, commitment advice is cut to its bounds: a Compute
    Savings Plan past the coverage target is cut to it, a purchase whose term
    runs into a migration blackout is dropped, and advice is never enlarged
  - another pack can make its bounds stricter, never looser
  - every commitment purchase an agent tries asks, never less, and the
    reason names the bound it would breach
  - nothing in it buys anything
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import finops.guard as g
import finops.guard_packs as gpk
from finops import ai_budget
from finops.packs import content, runtime
from finops.packs import install as inst
from finops.packs.errors import Problem
from finops.recommendations import commitment_bounds as cb
from finops.recommendations import commitments as c
from finops.recommendations import database_savings_plans as dsp
from finops.setup_wizard import main
from tests import packs_support
from tests.packs_support import copy_pack, sign_pack

packs_env = packs_support.packs_env
first_party_key = packs_support.first_party_key

PACK = Path(__file__).resolve().parent.parent / "packs" / "commitments-bounds"
PID = "io.github.getnable/commitments-bounds"
SP_BUY = ("aws savingsplans create-savings-plan --savings-plan-offering-id "
          "0f1e2d3c-aaaa-bbbb-cccc-111122223333 --commitment 5")
GCP_3Y = "gcloud compute commitments create c1 --plan 36-month --resources vcpu=8,memory=32GB"
GCP_1Y = "gcloud compute commitments create c1 --plan 12-month --resources vcpu=8,memory=32GB"
AZ_3Y_UPFRONT = ("az reservations reservation-order purchase --reservation-order-id r1 "
                 "--sku Standard_D2s_v3 --term P3Y --billing-plan Upfront --quantity 2")


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
    src = copy_pack(PACK, tmp_path / "commitments-bounds")
    sign_pack(src, first_party_key)
    code, out, err = _cli(capsys, "pack", "install", str(src), "--yes", "--json")
    assert code == 0, err
    assert json.loads(out)["pack"]["id"] == PID
    gpk.invalidate()
    return src


def _variant(tmp_path, name: str, bounds: str) -> str:
    """An org's own bounds pack beside the first-party one."""
    src = copy_pack(PACK, tmp_path / name)
    toml = (src / "nable-pack.toml").read_text()
    toml = (toml.replace('namespace   = "io.github.getnable"', 'namespace   = "com.acme"')
                .replace('support     = "first-party"', 'support     = "community"')
                .replace('name        = "commitments-bounds"', f'name        = "{name}"'))
    (src / "nable-pack.toml").write_text(toml)
    (src / "policies" / "bounds.yaml").write_text(bounds)
    shutil.rmtree(src / "guard")
    (src / "nable-pack.toml").write_text(toml.replace('guard_rules = ["guard/*.yaml"]\n', ""))
    inst.install(str(src), yes=True)
    runtime.invalidate()
    return f"com.acme/{name}"


def _bounds_file() -> dict:
    problems: list[Problem] = []
    doc = content.safe_load((PACK / "policies" / "bounds.yaml").read_text())
    [b] = [i for i in content.parse_policies(doc, "bounds.yaml", problems)
           if type(i).__name__ == "CommitmentBounds"]
    assert not problems
    return b


# ── the manifest ──────────────────────────────────────────────────────────────

def test_it_validates_as_shipped_with_minimal_capabilities(packs_env, capsys):
    code, out, _ = _cli(capsys, "pack", "validate", str(PACK), "--json")
    body = json.loads(out)
    assert code == 0 and body["ok"], body["problems"]
    assert body["id"] == PID and body["tier"] == "first-party"
    assert body["provides"] == {"policies": 2, "guard_rules": 3, "skills": 1}
    assert body["code"] == []                       # nothing in it runs
    assert body["capabilities"] == {"read_data": ["recommendations"],
                                    "guard": "tighten-only", "max_autonomy": "L1"}
    assert body["signature"]["status"] == "unsigned"


def test_its_text_has_no_em_dashes_or_exclamation_points():
    for p in PACK.rglob("*"):
        if p.is_file():
            text = p.read_text(encoding="utf-8")
            assert "—" not in text and "!" not in text, p


def test_the_guard_reasons_cite_the_bounds_the_policy_sets():
    b = _bounds_file()
    rules = (PACK / "guard" / "commitments.yaml").read_text()
    assert f"at most\n      {b.max_term_months} months" in rules or \
        f"at most {b.max_term_months} months" in " ".join(rules.split())
    flat = " ".join(rules.split())
    assert f"coverage at most {b.coverage_target_pct:g}% of eligible spend" in flat
    assert b.payment_options == ("no-upfront",) and "(no-upfront only)" in flat
    bounds_text = " ".join((PACK / "policies" / "bounds.yaml").read_text().split())
    assert f"coverage at most {b.coverage_target_pct:g}%" in bounds_text


# ── the guard ─────────────────────────────────────────────────────────────────

def test_every_purchase_asks_and_names_the_bound(packs_env, first_party_key, tmp_path,
                                                 capsys):
    before_sp = g.gate_command(SP_BUY, record=False)
    assert before_sp["decision"] == "ask" and "max_term_months" not in before_sp["reason"]
    upfront = SP_BUY + " --upfront-payment-amount 1000"
    before_upfront = g.gate_command(upfront, record=False)
    # Google Cloud and Azure purchases are not priced or classified by the
    # guard itself: the pack turns its silence into an ask.
    assert g.gate_command(GCP_3Y, record=False) is None
    assert g.gate_command(AZ_3Y_UPFRONT, record=False) is None
    src = copy_pack(PACK, tmp_path / "cb")
    sign_pack(src, first_party_key)
    assert _cli(capsys, "pack", "install", str(src), "--yes")[0] == 0
    gpk.invalidate()

    v = g.gate_command(GCP_3Y, record=False)
    assert v["decision"] == "ask"
    assert "longer than the bound max_term_months (at most 12 months)" in v["reason"]
    assert f"rule commitment-term-over-bound of pack {PID}" in v["reason"]

    v = g.gate_command(GCP_1Y, record=False)
    assert v["decision"] == "ask" and "max_term_months" in v["reason"]
    assert "commitment-term-over-bound" not in v["reason"]

    v = g.gate_command(AZ_3Y_UPFRONT, record=False)
    assert v["decision"] == "ask"
    assert "max_term_months" in v["reason"] and "payment_options" in v["reason"]

    v = g.gate_command(upfront, record=False)
    assert v["decision"] == "ask"
    # the guard's own ask stands, with the bound beside it
    assert v["reason"].startswith(before_upfront["reason"].rstrip())
    assert "outside the bound payment_options (no-upfront only)" in v["reason"]

    v = g.gate_command(SP_BUY, record=False)
    assert v["decision"] == "ask" and "coverage_target_pct" in v["reason"]
    # reads are untouched, and nothing here ever denies
    assert g.gate_command("aws ec2 describe-reserved-instances-offerings", record=False) is None
    assert g.gate_command("gcloud compute commitments list", record=False) is None


# ── the advice ────────────────────────────────────────────────────────────────

class _CE:
    """Cost Explorer in its documented response shapes: 40% Savings Plans
    coverage and about 6,000 USD a month of uncovered on-demand compute."""

    def get_savings_plans_utilization(self, **kw):
        return {"Total": {"Utilization": {"UtilizationPercentage": "95",
                                          "UnusedCommitment": "0"}}}

    def get_reservation_utilization(self, **kw):
        return {"Total": {"UtilizationPercentage": "90", "RICostForUnusedHours": "0"}}

    def get_savings_plans_coverage(self, **kw):
        return {"SavingsPlansCoverages": [{"Coverage": {
            "SpendCoveredBySavingsPlans": "4000", "OnDemandCost": "6000",
            "TotalCost": "10000", "CoveragePercentage": "40"}}]}

    def get_reservation_coverage(self, **kw):
        return {"Total": {"CoverageHours": {"CoverageHoursPercentage": "50"}}}

    def get_cost_and_usage(self, **kw):
        return {"ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": a}}}
                                  for a in ("6000", "6200", "6100")]}


def _analysis(monkeypatch):
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: _CE())
    a = c.analyze_commitments()
    assert a is not None
    return a, c.commitment_summary(a)


def _sp(recs):
    return [r for r in recs if r["type"] == "savings_plan"]


def test_advice_is_cut_to_the_coverage_target(packs_env, first_party_key, tmp_path,
                                              monkeypatch, capsys):
    _, before = _analysis(monkeypatch)
    [raw] = _sp(before["recommendations"])
    assert "commitment_bounds" not in before and "bounds" not in raw
    src = copy_pack(PACK, tmp_path / "cb")
    sign_pack(src, first_party_key)
    assert _cli(capsys, "pack", "install", str(src), "--yes")[0] == 0
    runtime.invalidate()

    _, after = _analysis(monkeypatch)
    [cut] = _sp(after["recommendations"])
    # 40% covered, 6,000 a month uncovered: 80% coverage leaves room for
    # 4,000 of the 6,000, two thirds of the purchase.
    assert cut["bounds"]["cut"] and cut["bounds"]["factor"] == pytest.approx(2 / 3, abs=1e-3)
    assert cut["commitment_per_month"] == pytest.approx(raw["commitment_per_month"] * 2 / 3,
                                                        abs=0.01)
    assert cut["monthly_savings"] < raw["monthly_savings"]
    assert f"bound {PID}:default-bounds" in cut["description"]
    assert after["commitment_bounds"]["coverage_target_pct"] == 80
    assert after["cut_by_bounds"][0]["dropped"] is False
    # the warnings are not purchases and pass through as they were
    assert [r for r in after["recommendations"] if r["type"] != "savings_plan"] == \
        [r for r in before["recommendations"] if r["type"] != "savings_plan"]


def test_a_migration_blackout_drops_the_purchase(installed, tmp_path, monkeypatch):
    now = datetime.now(UTC)
    start = (now + timedelta(days=90)).replace(microsecond=0).isoformat()
    end = (now + timedelta(days=180)).replace(microsecond=0).isoformat()
    pid = _variant(tmp_path, "acme-bounds", f"""\
version: 1
commitment_bounds:
  - id: eu-move
    description: No new compute commitments while eu-west-1 moves to Graviton.
    blackouts:
      - id: eu-graviton-move
        start: "{start}"
        end: "{end}"
        reason: Moving eu-west-1 compute to Graviton
        providers: [aws]
        regions: [eu-west-1]
""")
    _, summary = _analysis(monkeypatch)
    assert _sp(summary["recommendations"]) == []
    [gone] = summary["cut_by_bounds"]
    assert gone["dropped"] and "eu-graviton-move" in gone["why"]
    assert f"{pid}:eu-move" in gone["by"]


def test_another_pack_cannot_loosen_the_bounds(installed, tmp_path, monkeypatch):
    _variant(tmp_path, "loose-bounds", """\
version: 1
commitment_bounds:
  - id: loose
    description: Cover everything, for three years, all up front.
    coverage_target_pct: 100
    max_term_months: 36
    payment_options: [no-upfront, partial-upfront, all-upfront]
""")
    b = cb.in_force()
    assert (b.coverage_target_pct, b.max_term_months, b.payment_options) == \
        (80, 12, ("no-upfront",))
    _, summary = _analysis(monkeypatch)
    [cut] = _sp(summary["recommendations"])
    assert cut["bounds"]["factor"] == pytest.approx(2 / 3, abs=1e-3)


def test_the_database_plan_is_bounded_too(installed, monkeypatch):
    monkeypatch.setattr(dsp, "boto3", type("B", (), {"client": staticmethod(
        lambda *_a, **_k: object())}))
    monkeypatch.setattr(dsp, "_get_rds_spend", lambda *_a: 5000.0)
    monkeypatch.setattr(dsp, "_get_database_sp_coverage", lambda *_a: 70.0)
    r = dsp.recommend_database_savings_plans()
    # 70% covered, 1,500 uncovered: 80% leaves room for 500 of it, a third.
    assert r["bounds"]["cut"] and r["bounds"]["factor"] == pytest.approx(1 / 3, abs=1e-3)
    assert r["estimated_monthly_savings"] == pytest.approx(1500 * 0.30 / 3, abs=0.01)
    monkeypatch.setattr(dsp, "_get_database_sp_coverage", lambda *_a: 85.0)
    r = dsp.recommend_database_savings_plans()
    assert r["bounds"]["dropped"] and r["finding"] is None
    assert "at or over the 80% target" in r["bounds"]["why"]


def test_a_tampered_pack_withholds_purchase_advice(installed, packs_env, monkeypatch):
    bounds = (packs_env.root / "io.github.getnable" / "commitments-bounds" / "1.0.0"
              / "policies" / "bounds.yaml")
    bounds.write_text(bounds.read_text().replace("coverage_target_pct: 80",
                                                 "coverage_target_pct: 100"))
    runtime.invalidate()
    _, summary = _analysis(monkeypatch)
    assert _sp(summary["recommendations"]) == []
    assert "could not be loaded" in summary["cut_by_bounds"][0]["why"]


def test_nothing_in_the_pack_buys_anything():
    text = " ".join(p.read_text() for p in PACK.rglob("*") if p.is_file())
    assert "never buys" in text
    manifest = (PACK / "nable-pack.toml").read_text()
    assert "act " not in manifest and "execute" not in manifest


def test_an_agents_purchase_attempt_is_an_exception_in_change_evidence(installed):
    from finops import change_evidence
    assert g.gate_command(SP_BUY, record=True)["decision"] == "ask"
    ev = change_evidence.build(model=None)
    [hit] = [e for e in ev["exceptions"] if e["rule"] == "agent-tried-to-buy-a-commitment"]
    assert hit["pack"] == PID and hit["severity"] == "high"
    assert "nable never buys one" in hit["message"] and "savingsplans" in hit["message"]
