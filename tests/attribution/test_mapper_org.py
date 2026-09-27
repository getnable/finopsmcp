"""The attribution mapper reads the org model's confirmed tag facts on top of
tag_rules.yaml.

What has to stay true:
  - with no org model the mapping is exactly tag_rules.yaml's
  - confirmed tag_key facts add keys (default priority, after the file's own
    rules of that priority), confirmed tag_alias and team facts add aliases
  - proposed facts are never read
  - facts that came from tag_rules.yaml (in memory or imported by `nable org
    init`) are not applied a second time
"""
from __future__ import annotations

import textwrap

import pytest

from finops import org
from finops.attribution import mapper
from finops.org.cli import _who as human

RULES = """
    rules:
      - {tag_key: team, maps_to_field: team, priority: 10}
      - {tag_key: env, maps_to_field: environment, priority: 10}
      - {tag_key: costcenter, maps_to_field: team, priority: 50}
      - {tag_key: team, tag_value_pattern: "infra*", maps_to_field: team,
         maps_to_value: platform, priority: 5}
    team_aliases:
      data: [analytics, ml]
"""
SAMPLES = [
    {"team": "payments"}, {"team": "infra-core"}, {"costcenter": "analytics"},
    {"Team": "ML", "env": "prd"}, {"squad": "pay"}, {"env": "staging"}, {},
]


@pytest.fixture
def rules(tmp_path, monkeypatch):
    for var in ("FINOPS_REQUIRED_TAGS", "FINOPS_PROTECTED_TAGS", "FINOPS_ACCOUNTS_FILE"):
        monkeypatch.delenv(var, raising=False)
    p = tmp_path / "tag_rules.yaml"
    p.write_text(textwrap.dedent(RULES))
    monkeypatch.setenv("FINOPS_TAG_RULES", str(p))
    mapper.reload_rules()
    yield p
    mapper.reload_rules()


def _map_all():
    mapper.reload_rules()
    return [mapper.tags_to_attribution(t) for t in SAMPLES]


def confirmed(kind, subject, value, source="human"):
    org.set_fact(org.make_fact(kind, subject, value, source=source), human("maria"))


def test_without_an_org_model_the_mapping_is_the_files(rules, monkeypatch, tmp_path):
    monkeypatch.setenv("FINOPS_ORG_DIR", str(tmp_path / "no-such-dir"))
    before = _map_all()
    assert before[0] == {"team": "payments", "service": "", "environment": ""}
    assert before[1]["team"] == "platform" and before[2]["team"] == "data"
    assert before[4]["team"] == "unattributed"
    # An empty org dir changes nothing either.
    monkeypatch.setenv("FINOPS_ORG_DIR", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    assert _map_all() == before


def test_confirmed_keys_and_aliases_add_to_the_file(rules):
    confirmed("tag_key", "org:org", {"canonical": "team", "keys": ["squad"]})
    confirmed("tag_alias", "tag_value:pay", {"canonical_key": "team",
                                             "canonical_value": "payments"})
    confirmed("tag_alias", "tag_value:prd", {"canonical_key": "environment",
                                             "canonical_value": "prod"})
    out = _map_all()
    assert out[4]["team"] == "payments"                # squad=pay, through the alias
    assert out[3]["environment"] == "prod"             # env=prd, through the alias
    assert out[3]["team"] == "data"                    # the file's alias still holds
    assert out[0]["team"] == "payments" and out[1]["team"] == "platform"
    # The file's own rules still come first: `team` (priority 10) beats `squad`.
    mapper.reload_rules()
    assert mapper.tags_to_attribution({"team": "search", "squad": "pay"})["team"] == "search"
    assert "squad" in mapper.configured_tag_keys()


def test_a_team_fact_names_its_aliases(rules):
    confirmed("team", "team:payments", {"name": "payments", "aliases": ["pay-svc", "PAY"]})
    mapper.reload_rules()
    assert mapper.tags_to_attribution({"team": "pay-svc"})["team"] == "payments"
    assert mapper.tags_to_attribution({"team": "Pay"})["team"] == "payments"


def test_proposed_facts_are_never_read(rules):
    before = _map_all()
    for kind, subject, value in (
            ("tag_key", "org:org", {"canonical": "team", "keys": ["squad"]}),
            ("tag_alias", "tag_value:payments", {"canonical_key": "team",
                                                 "canonical_value": "billing"}),
            ("team", "team:data", {"name": "data", "aliases": ["payments"]})):
        org.propose(org.make_fact(kind, subject, value, source="inference", confidence=0.9))
    assert _map_all() == before


def test_imported_tag_rules_are_not_applied_twice(rules):
    before = _map_all()
    assert org.import_legacy() > 0
    assert any(f.source == "legacy:tag_rules.yaml" for f in org.load(legacy=False).facts)
    assert _map_all() == before


def test_imported_tag_rules_still_count_once_the_file_is_gone(rules, monkeypatch, tmp_path):
    org.import_legacy()
    monkeypatch.setenv("FINOPS_TAG_RULES", str(tmp_path / "moved-away.yaml"))
    mapper.reload_rules()
    assert mapper.tags_to_attribution({"team": "payments"})["team"] == "payments"
    assert mapper.tags_to_attribution({"costcenter": "ml"})["team"] == "data"


def test_an_org_model_that_fails_leaves_the_file(rules, monkeypatch):
    before = _map_all()
    confirmed("tag_key", "org:org", {"canonical": "team", "keys": ["squad"]})

    def boom(*_a, **_k):
        raise OSError("unreadable")
    monkeypatch.setattr(org, "load", boom)
    assert _map_all() == before
