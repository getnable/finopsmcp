import pytest

import finops.blame.culprit as culprit
from finops.blame.culprit import find_cost_culprit
from finops.recommendations.rate_detector import EffectiveRateProfile


@pytest.fixture(autouse=True)
def _stub_rate_detection(monkeypatch):
    """Never hit Cost Explorer in tests. Default to no private pricing (on-demand)."""
    monkeypatch.setattr(
        "finops.recommendations.rate_detector.detect_effective_rates",
        lambda: EffectiveRateProfile(),
    )


def test_happy_path(repo_with_resize):
    res = find_cost_culprit("i-0abc123", str(repo_with_resize), persist=False)
    assert res["resolved"] is True
    assert res["tf_address"] == "aws_instance.web"
    assert res["current_value"] == "m5.4xlarge"
    assert res["previous_value"] == "m5.large"
    assert "bump web to m5.4xlarge" in res["commit"]["summary"]
    assert res["commit"]["author"] == "Dev Example"
    assert res["revert_available"] is True
    assert "m5.large" in res["revert_diff"]
    assert res["pull_request"] is None
    assert res["dry_run"] is True
    # priced from the on-demand delta (m5.4xlarge $560.64 - m5.large $70.08).
    assert res["monthly_cost_added_usd"] == pytest.approx(490.56, abs=0.01)
    assert res["price_basis"] == "on-demand"


def test_pricing_uses_effective_rate(repo_with_resize, monkeypatch):
    # A customer with a 20% EC2 discount: the bump costs them 20% less than list.
    monkeypatch.setattr(
        "finops.recommendations.rate_detector.detect_effective_rates",
        lambda: EffectiveRateProfile(
            overall_discount_pct=0.20, has_private_pricing=True, source="cur_athena",
        ),
    )
    res = find_cost_culprit("i-0abc123", str(repo_with_resize), persist=False)
    assert res["price_basis"] == "effective"
    assert res["monthly_cost_added_usd"] == pytest.approx(490.56 * 0.80, abs=0.01)


def test_resource_not_in_state(repo_with_resize):
    res = find_cost_culprit("i-nope", str(repo_with_resize), persist=False)
    assert res["resolved"] is False and res["stage"] == "resource"


def test_no_commit_uncommitted_line(repo_with_resize, tf_source, state_json):
    (repo_with_resize / "main.tf").write_text(tf_source.format(size="m5.8xlarge"))
    (repo_with_resize / "terraform.tfstate").write_text(state_json("m5.8xlarge"))
    res = find_cost_culprit("i-0abc123", str(repo_with_resize), persist=False)
    assert res["resolved"] is False and res["stage"] == "commit"


def test_tf_block_deleted(repo_with_resize):
    (repo_with_resize / "main.tf").write_text("# block removed\n")
    res = find_cost_culprit("i-0abc123", str(repo_with_resize), persist=False)
    assert res["resolved"] is False and res["stage"] == "block"


def test_git_repo_not_configured(tmp_path, tf_source, state_json):
    (tmp_path / "main.tf").write_text(tf_source.format(size="m5.4xlarge"))
    (tmp_path / "terraform.tfstate").write_text(state_json("m5.4xlarge"))
    res = find_cost_culprit("i-0abc123", str(tmp_path), persist=False)
    assert res["resolved"] is False and res["stage"] == "git"


def test_commit_has_no_pr(repo_with_resize, monkeypatch):
    monkeypatch.setattr(culprit, "resolve_pr_for_commit", lambda *a, **k: None)
    res = find_cost_culprit("i-0abc123", str(repo_with_resize),
                            github_repo="acme/infra", persist=False)
    assert res["resolved"] is True
    assert res["pull_request"] is None
    assert "pr_note" in res


def test_pr_resolved(repo_with_resize, monkeypatch):
    monkeypatch.setattr(
        culprit, "resolve_pr_for_commit",
        lambda *a, **k: {"number": 482,
                         "url": "https://github.com/acme/infra/pull/482",
                         "title": "bump worker pool", "author": "sam",
                         "merged_at": "2026-07-06T00:00:00Z"},
    )
    res = find_cost_culprit("i-0abc123", str(repo_with_resize),
                            github_repo="acme/infra", persist=False)
    assert res["pull_request"]["number"] == 482
    assert res["pull_request"]["author"] == "sam"
