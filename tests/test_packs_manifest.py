# SPDX-License-Identifier: Apache-2.0
"""nable-pack.toml validation: every field, the capability vocabulary, the
namespace rules and the API range. Each refusal must name the field and why,
because "invalid pack" sends an author back to guess."""
from __future__ import annotations

import pytest

from finops import packs
from finops.packs import capabilities as caps
from finops.packs.errors import ValidationError
from finops.packs.manifest import check_namespace, parse_manifest
from finops.packs.versions import in_range, parse_range, version_key
from tests.packs_support import manifest_text


def _problems(text: str) -> list[str]:
    with pytest.raises(ValidationError) as ei:
        parse_manifest(text)
    return [str(p) for p in ei.value.problems]


def _one(text: str, field: str) -> str:
    probs = [p for p in _problems(text) if p.startswith(field)]
    assert probs, f"no problem on {field}"
    return probs[0]


def test_a_good_manifest_parses_into_a_typed_manifest():
    m = parse_manifest(manifest_text(capabilities=(
        'read_data = ["org.owners", "focus.cost", "focus.cost"]\n'
        'read_cloud = ["aws:ce:GetCostAndUsage", "k8s:pods:list"]\n'
        'secrets = ["KUBECONFIG"]\nnetwork = ["kubecost.internal:9090"]\n'
        'write_org = ["proposals"]\nact = ["pr", "ticket"]\n'
        'guard = "tighten-only"\nmax_autonomy = "L2"\n'),
        extra='[compat]\nclouds = ["aws"]\nharnesses = ["claude-code"]\n'))
    assert m.id == "io.github.example/demo"
    assert m.tier == "community" and not m.first_party
    # sorted and de-duplicated, so the same asks compare equal whatever the order
    assert m.capabilities["read_data"] == ("focus.cost", "org.owners")
    assert m.capabilities["max_autonomy"] == "L2"
    assert m.provides["policies"] == ("policies/*.yaml",)
    assert m.compat == {"clouds": ("aws",), "harnesses": ("claude-code",)}


def test_the_design_example_manifest_is_valid_for_first_party():
    text = '''
[pack]
name        = "k8s-allocation"
namespace   = "io.github.getnable"
version     = "1.2.0"
description = "Kubernetes allocation that reconciles to the bill"
license     = "Apache-2.0"
nable_api   = ">=1.0,<2.0"
maintainers = ["@owner"]
support     = "first-party"
repository  = "https://github.com/getnable/packs"
source_commit = "9f2c0a1"
status      = "active"

[capabilities]
read_data    = ["focus.cost", "org.owners", "org.environments"]
read_cloud   = ["aws:ce:GetCostAndUsage", "k8s:pods:list", "k8s:nodes:list"]
secrets      = ["KUBECONFIG"]
network      = ["kubecost.internal:9090"]
write_org    = ["proposals"]
act          = ["pr", "ticket"]
guard        = "tighten-only"
max_autonomy = "L2"

[provides]
connectors  = [{id = "kubecost", entry = "nable_k8s.kubecost:main", output = "focus-1.3"}]
adapters    = [{id = "namespace-owners", entry = "nable_k8s.owners:main"}]
policies    = ["policies/*.yaml"]
playbooks   = ["playbooks/helm-requests.yaml"]
reports     = ["reports/showback.md.j2"]
skills      = ["skills/k8s-cost/SKILL.md"]

[compat]
clouds    = ["aws", "gcp", "azure"]
harnesses = ["claude-code", "cursor", "codex", "copilot", "gemini", "cline"]
'''
    m = parse_manifest(text)
    assert [(c.kind, c.id, c.output) for c in m.code] == [
        ("connectors", "kubecost", "focus-1.3"), ("adapters", "namespace-owners", None)]


@pytest.mark.parametrize("field,bad,why", [
    ("name", '"Bad_Name"', "lowercase"),
    ("name", '"x"', "2 to 64"),
    ("version", '"1.2"', "semver"),
    ("version", '"v1.2.0"', "semver"),
    ("description", '""', "non-empty"),
    ("nable_api", '"1.0"', "operator"),
    ("nable_api", '">=2.0"', "does not include"),
    ("maintainers", "[]", "at least one"),
    ("support", '"gold"', "first-party, verified, community, private"),
    ("license", '"<script>"', "SPDX"),
    ("repository", '"http://example.com"', "https"),
    ("source_commit", '"main"', "hex commit"),
    ("status", '"retired"', "active, deprecated, archived"),
])
def test_each_bad_pack_field_is_named_with_its_reason(field, bad, why):
    text = manifest_text()
    import re
    if re.search(rf"^{field} = ", text, flags=re.MULTILINE):
        text = re.sub(rf"^{field} = .*$", f"{field} = {bad}", text, flags=re.MULTILINE)
    else:
        text = text.replace("[capabilities]", f"{field} = {bad}\n\n[capabilities]", 1)
        # (fields not in the default text go at the end of [pack])
        text = text.replace(f'support = "community"\n\n{field}', f'support = "community"\n{field}')
    assert why in _one(text, f"pack.{field}")


def test_required_fields_and_unknown_fields_and_tables():
    probs = _problems('[pack]\nname = "demo"\nnamspace = "io.x"\n[extras]\na = 1\n')
    joined = "\n".join(probs)
    for f in ("pack.namespace: is required", "pack.version: is required",
              "pack.support: is required", "pack.namspace: is not a [pack] field",
              "extras: is not a manifest table"):
        assert f in joined


def test_invalid_toml_is_one_clear_problem():
    probs = _problems("[pack\nname=")
    assert probs and probs[0].startswith("nable-pack.toml:")


@pytest.mark.parametrize("ns,ok", [
    ("io.github.getnable", True),
    ("com.example", True),
    ("io.github.my-org", True),
    ("getnable", False),              # one label
    ("IO.github.x", False),           # uppercase
    ("123.example", False),           # TLD must be letters
    ("io.github.-bad", False),        # leading hyphen
    ("io..github", False),            # empty label
    ("io.github.x/y", False),
])
def test_namespace_must_be_reverse_dns(ns, ok):
    assert (check_namespace(ns) is None) is ok, check_namespace(ns)


def test_first_party_is_reserved_for_nables_namespaces():
    why = _one(manifest_text(support="first-party"), "pack.support")
    assert "reserved" in why and "io.github.getnable" in why
    parse_manifest(manifest_text(support="first-party", namespace="io.github.getnable"))


def test_api_version_constant_and_range():
    assert packs.API_VERSION == "1.0"
    assert in_range("1.0", ">=1.0,<2.0")
    assert not in_range("2.0", ">=1.0,<2.0")
    assert in_range("1.0", "==1") and not in_range("1.0", "!=1.0")
    with pytest.raises(ValueError):
        parse_range(">=1.0,latest")
    # a manifest written for a future API is refused, naming this nable's API
    why = _one(manifest_text().replace(">=1.0,<2.0", ">=1.1"), "pack.nable_api")
    assert "1.0" in why
    assert version_key("1.0.0-rc.1") < version_key("1.0.0") < version_key("1.0.1")


# ── the capability vocabulary ────────────────────────────────────────────────

@pytest.mark.parametrize("line,needle", [
    ('read_data = ["focus.everything"]', "not a data scope"),
    ('read_cloud = ["aws:ec2:TerminateInstances"]', "is not a read"),
    ('read_cloud = ["aws:ec2:*"]', "API action name"),
    ('read_cloud = ["aws:ce"]', "provider:service:Action"),
    ('read_cloud = ["oracle:x:GetY"]', "provider"),
    ('read_cloud = ["aws:secretsmanager:GetSecretValue"]', "credentials or secret"),
    ('read_cloud = ["aws:sts:GetSessionToken"]', "credentials or secret"),
    ('read_cloud = ["aws:ssm:Get*"]', "credentials or secret"),
    ('read_cloud = ["k8s:secrets:list"]', "credentials or secret"),
    ('read_cloud = ["k8s:pods:delete"]', "read verb"),
    ('secrets = ["lowercase"]', "environment-variable"),
    ('secrets = ["FINOPS_LICENSE_KEY"]', "nable's own"),
    ('secrets = ["NABLE_TOKEN"]', "nable's own"),
    ('network = ["https://example.com"]', "no scheme"),
    ('network = ["*.example.com"]', "wildcard"),
    ('network = ["example.com:99999"]', "port"),
    ('network = ["169.254.169.254"]', "metadata"),
    ('network = ["metadata.google.internal:80"]', "metadata"),
    ('write_org = ["confirm"]', "proposals"),
    ('act = ["execute"]', "first-party"),
    ('act = ["merge"]', "not an action kind"),
    ('guard = "loosen"', "tighten-only"),
    ('max_autonomy = "L3"', "ceiling"),
    ('max_autonomy = "L9"', "not one of"),
    ('act = ["pr"]\nmax_autonomy = "L1"', "at least L2"),
    ('netwrok = ["x.com"]', "is not a capability"),
    ('read_data = "focus.cost"', "list of strings"),
])
def test_unknown_or_unsafe_capability_values_are_errors(line, needle):
    probs = _problems(manifest_text(capabilities=line + "\n"))
    assert any(p.startswith("capabilities.") and needle in p for p in probs), probs


def test_first_party_may_execute_and_reach_l3_but_never_l4():
    fp = {"namespace": "io.github.getnable", "support": "first-party"}
    m = parse_manifest(manifest_text(capabilities='act = ["execute"]\nmax_autonomy = "L3"\n', **fp))
    assert m.capabilities["act"] == ("execute",)
    assert "ceiling" in _one(manifest_text(capabilities='max_autonomy = "L4"\n', **fp),
                             "capabilities.max_autonomy")


def test_capability_diff_counts_every_addition():
    old = {"read_data": ("focus.cost",), "network": ("a.example.com",), "max_autonomy": "L1"}
    new = {"read_data": ("focus.cost", "org.owners"), "network": ("b.example.com",),
           "max_autonomy": "L2", "guard": "tighten-only", "secrets": ("TOKEN_X",)}
    d = caps.diff(old, new)
    assert d["added"] == {"read_data": ["org.owners"], "network": ["b.example.com"],
                          "secrets": ["TOKEN_X"], "max_autonomy": ["L2"],
                          "guard": ["tighten-only"]}
    assert d["removed"]["network"] == ["a.example.com"]
    assert caps.diff(new, new) == {"added": {}, "removed": {}}
    # lowering autonomy or dropping a host is not an addition
    assert caps.diff(new, old)["added"] == {"network": ["a.example.com"]}


def test_ceiling_is_strict_and_supports_patterns():
    ask = {"read_data": ("focus.cost",), "read_cloud": ("aws:ce:GetCostAndUsage",),
           "network": ("kube.corp.internal:443",), "max_autonomy": "L2"}
    ok = {"read_data": ["focus.cost"], "read_cloud": ["aws:ce:*"],
          "network": ["*.corp.internal:443"], "max_autonomy": "L2"}
    assert caps.exceeds(ask, ok) == []
    probs = [str(p) for p in caps.exceeds(ask, {"read_data": ["focus.cost"],
                                                 "max_autonomy": "L1"})]
    assert any("read_cloud" in p for p in probs)
    assert any("network" in p for p in probs)
    assert any("max_autonomy" in p for p in probs)


@pytest.mark.parametrize("line,needle", [
    ('policies = ["/etc/*.yaml"]', "relative"),
    ('policies = ["../outside/*.yaml"]', ".."),
    ('scripts = ["x.sh"]', "is not a content type"),
    ('connectors = [{id = "k", entry = "not an entry"}]', "entry point"),
    ('connectors = [{id = "k", entry = "pkg.mod:main"}]', "focus-1.3"),
    ('sinks = [{id = "s", entry = "pkg:main", shell = "sh"}]', "is not a field"),
])
def test_provides_is_strict(line, needle):
    probs = _problems(manifest_text(provides=line + "\n"))
    assert any(p.startswith("provides.") and needle in p for p in probs), probs


def test_integrity_table_is_validated():
    probs = _problems(manifest_text(extra=(
        '[integrity]\nfiles = {"a.yaml" = "nothex", "nable-pack.toml" = "' + "0" * 64
        + '", "../x" = "' + "0" * 64 + '"}\nsigstore = "x"\n')))
    joined = "\n".join(probs)
    assert "integrity.files.a.yaml: must be a lowercase sha256" in joined
    assert "cannot pin its own hash" in joined
    assert "integrity.files.../x" in joined
    assert "integrity.sigstore: is not a field" in joined
