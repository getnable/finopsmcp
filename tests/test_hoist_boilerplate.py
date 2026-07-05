"""hoist_finding_boilerplate dedupes per-category why/remediation across a
report's findings. 20 idle disks used to carry the same two sentences 20 times
(~140 tokens each); after hoisting the guidance lives once in playbooks and each
finding keeps only its unique data.
"""
from finops.token_budget import hoist_finding_boilerplate


def _f(cat, why="shared why", rem=("step 1", "step 2"), rid="r"):
    return {"category": cat, "resource_id": rid,
            "finding": {"title": f"t-{rid}", "why": why, "remediation": list(rem)}}


def test_identical_boilerplate_is_hoisted_once():
    report = {"findings": [_f("idle_disk", rid=f"d{i}") for i in range(5)]}
    hoist_finding_boilerplate(report)
    assert report["playbooks"]["idle_disk"]["why"] == "shared why"
    assert report["playbooks"]["idle_disk"]["remediation"] == ["step 1", "step 2"]
    for f in report["findings"]:
        assert "why" not in f["finding"] and "remediation" not in f["finding"]
        assert f["finding"]["title"].startswith("t-")  # unique data stays inline


def test_bespoke_text_stays_inline():
    report = {"findings": [_f("idle_vm", why="vm A is special"),
                           _f("idle_vm", why="vm B is different")]}
    hoist_finding_boilerplate(report)
    # why differs across the category -> not hoisted; remediation identical -> hoisted
    assert "why" not in report.get("playbooks", {}).get("idle_vm", {})
    assert report["playbooks"]["idle_vm"]["remediation"] == ["step 1", "step 2"]
    assert all("why" in f["finding"] for f in report["findings"])


def test_no_envelopes_is_a_noop():
    report = {"findings": [{"category": "x", "resource_id": "r"}]}
    hoist_finding_boilerplate(report)
    assert "playbooks" not in report


def test_token_saving_is_real():
    import json
    findings = [_f("idle_disk", why="w" * 200, rem=["r" * 200], rid=f"d{i}") for i in range(20)]
    before = len(json.dumps({"findings": [dict(f, finding=dict(f["finding"])) for f in findings]}))
    report = {"findings": findings}
    hoist_finding_boilerplate(report)
    after = len(json.dumps(report))
    assert after < before * 0.5  # over half the payload was repeated boilerplate
