"""`nable brief` on real scan findings: drafted changes, honest headings.

Dogfooded on a mock account, every item read "No change drafted." under a help
line promising "with each change drafted"; the header said "Overnight run" for
a run started by hand; each item was titled with a bare resource id; and the
list of findings that were checked but ranked lower sat under "What nable could
not check".
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

from finops.briefing.brief import build_brief
from finops.briefing.render import to_html, to_markdown

NOW = datetime(2026, 9, 25, 9, tzinfo=UTC)
TODAY = date(2026, 9, 25)


def _no_probe(*a, **k):
    return None


def _scan_finding(waste_type, rid, usd, resource_type, detail, **kw):
    f = {"resource_id": rid, "resource_type": resource_type, "waste_type": waste_type,
         "estimated_monthly_savings": usd, "detail": detail, "severity": "medium",
         "region": "us-east-1", "account_id": "1"}
    f.update(kw)
    return f


_VOL = _scan_finding("unattached_ebs_volume", "vol-6a3", 71.0, "EBS Volume",
                     "500 GB gp3 volume is unattached (state=available). Name: untagged.")
_EIP = _scan_finding("unassociated_elastic_ip", "eipalloc-1", 3.65, "Elastic IP",
                     "Elastic IP 1.2.3.4 is not associated with any instance.")
_GP2 = _scan_finding("gp2_should_migrate_to_gp3", "vol-gp2", 2.0, "EBS Volume",
                     "100 GB gp2 volume.")


def _brief(findings, **kw):
    return build_brief(findings, today=TODAY, use_llm=False, now=NOW, prober=_no_probe, **kw)


def test_an_unattached_volume_from_the_scan_gets_snapshot_then_delete():
    fix = _brief([_VOL]).items[0].drafted_fix
    assert fix["summary"] == "Snapshot vol-6a3, then delete it."
    assert fix["commands"][0].startswith("aws ec2 create-snapshot --volume-id vol-6a3 --region us-east-1")
    assert fix["commands"][-1] == "aws ec2 delete-volume --volume-id vol-6a3 --region us-east-1"


def test_an_unused_elastic_ip_gets_its_release_command():
    fix = _brief([_EIP]).items[0].drafted_fix
    assert fix["summary"] == "Release eipalloc-1."
    assert fix["commands"] == [
        "aws ec2 release-address --allocation-id eipalloc-1 --region us-east-1"]


def test_a_gp2_volume_gets_the_in_place_change():
    fix = _brief([_GP2]).items[0].drafted_fix
    assert fix["commands"] == [
        "aws ec2 modify-volume --volume-id vol-gp2 --volume-type gp3 --region us-east-1"]
    assert fix["reversible"] is True


def test_titles_say_what_where_and_the_detail_is_in_the_words():
    item = _brief([_VOL]).items[0]
    assert item.title == "Unattached EBS volume vol-6a3 (us-east-1)"
    assert "500 GB gp3 volume is unattached" in item.in_words()


def test_an_on_demand_run_is_not_called_overnight():
    md = to_markdown(_brief([_VOL], trigger="on_demand"))
    assert "On-demand run" in md and "Overnight run" not in md
    assert "Overnight run" in to_markdown(_brief([_VOL]))
    html = to_html(_brief([_VOL], trigger="on_demand"))
    assert "while you were asleep" not in html


def test_findings_ranked_out_are_not_listed_as_unchecked():
    many = [dict(_VOL, resource_id=f"vol-{i}") for i in range(4)]
    md = to_markdown(_brief(many, limit=2))
    assert "What nable could not check" not in md
    assert "# Checked, not shown here" in md
    assert "2 further finding(s)" in md


def test_the_help_does_not_promise_a_draft_for_every_item():
    import argparse

    from finops import cli_brief
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    cli_brief.add_parser(sub)
    helps = {a.dest: a.help for a in sub._choices_actions}
    assert "each change drafted" not in helps["brief"]


def test_brief_takes_regions_and_runs_on_demand(monkeypatch, capsys):
    from finops import cli_brief
    seen = {}

    def fake_run(**kw):
        seen.update(kw)
        b = _brief([_VOL], trigger=kw.get("trigger", "scheduled"))
        return {"brief": b, "path": None, "delivered": {}, "summary": b.to_dict()}

    monkeypatch.setattr("finops.briefing.run.run_overnight", fake_run)
    args = SimpleNamespace(json=False, latest=False, html=False, deliver=False,
                           limit=10, no_save=True, regions=["us-east-1,eu-west-1"])
    assert cli_brief.run(args) == 0
    assert seen["regions"] == ["us-east-1", "eu-west-1"]
    assert seen["trigger"] == "on_demand"
    assert "On-demand run" in capsys.readouterr().out
