# SPDX-License-Identifier: Apache-2.0
"""Data-pack content: the six schemas, the small rule language, and the
promise that nothing in a data pack can execute."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from finops.packs.content import (
    load_content,
    load_file,
    parse_guard_rules,
    parse_playbooks,
    parse_policies,
    render,
    tighten,
)
from finops.packs.errors import Problem


def _load(kind: str, tmp_path: Path, name: str, text: str) -> tuple[list, list[str]]:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    problems: list[Problem] = []
    items = load_file(kind, p, name, problems)
    return items, [str(x) for x in problems]


def _policy(match: dict, effect: dict | None = None) -> list:
    doc = {"rules": [{"id": "r1", "description": "d", "applies_to": "finding", "match": match,
                      "effect": effect or {"action": "flag", "message": "m"}}]}
    problems: list[Problem] = []
    rules = parse_policies(doc, "p.yaml", problems)
    assert not problems, problems
    return rules


# ── the rule language ────────────────────────────────────────────────────────

@pytest.mark.parametrize("cond,obj,want", [
    ({"field": "a", "op": "eq", "value": "x"}, {"a": "x"}, True),
    ({"field": "a", "op": "ne", "value": "x"}, {"a": "y"}, True),
    ({"field": "a", "op": "in", "value": ["x", "y"]}, {"a": "y"}, True),
    ({"field": "a", "op": "not_in", "value": ["x"]}, {"a": "x"}, False),
    ({"field": "a.b", "op": "regex", "value": "^prod-"}, {"a": {"b": "prod-api"}}, True),
    ({"field": "a", "op": "gt", "value": 3}, {"a": 4}, True),
    ({"field": "a", "op": "gte", "value": 3}, {"a": 3}, True),
    ({"field": "a", "op": "lt", "value": 3}, {"a": 3}, False),
    ({"field": "a", "op": "lte", "value": 3.5}, {"a": 3}, True),
    ({"field": "a", "op": "gt", "value": 3}, {"a": "4"}, False),     # no coercion
    ({"field": "a", "op": "gt", "value": 0}, {"a": True}, False),    # a bool is not a number
    ({"field": "a", "op": "gt", "value": 3}, {}, False),             # missing is false
    ({"field": "a", "op": "exists"}, {"a": None}, True),
    ({"field": "a", "op": "exists", "value": False}, {}, True),
    ({"field": "a", "op": "gt", "value_from": "b"}, {"a": 5, "b": 4}, True),
    ({"field": "a", "op": "gt", "value_from": "b"}, {"a": 5}, False),
])
def test_conditions(cond, obj, want):
    (rule,) = _policy({"all": [cond]})
    assert rule.matches(obj) is want


def test_all_and_any_combine_and_the_message_renders_from_the_finding():
    (rule,) = _policy(
        {"all": [{"field": "spend.growth_pct", "op": "gt", "value": 10}],
         "any": [{"field": "credits.runway_months", "op": "lt", "value": 6},
                 {"field": "spend.projected", "op": "gt", "value_from": "credits.balance"}]},
        {"action": "escalate", "severity": "high",
         "message": "grew ${spend.growth_pct}%, $$${credits.balance} left"})
    hit = rule.evaluate({"spend": {"growth_pct": 20, "projected": 10}, "credits":
                         {"runway_months": 3, "balance": 500}})
    assert hit == {"rule": "r1", "pack": "", "action": "escalate", "severity": "high",
                   "message": "grew 20%, $500 left"}
    assert rule.evaluate({"spend": {"growth_pct": 20}, "credits": {"runway_months": 9}}) is None
    assert rule.evaluate("not a dict") is None


@pytest.mark.parametrize("rule,needle", [
    ({"match": {"all": [{"field": "a", "op": "like", "value": 1}]}}, "is not one of"),
    ({"match": {"all": [{"field": "a", "op": "gt", "value": "big"}]}}, "finite number"),
    ({"match": {"all": [{"field": "a", "op": "in", "value": "x"}]}}, "needs a list"),
    ({"match": {"all": [{"field": "a", "op": "regex", "value": "("}]}}, "regular expression"),
    ({"match": {"all": [{"field": "a", "op": "eq", "value": 1, "value_from": "b"}]}},
     "exactly one"),
    ({"match": {"all": [{"field": "a()", "op": "eq", "value": 1}]}}, "dotted path"),
    ({"match": {"all": [{"field": "a", "op": "eq", "value": 1, "lambda": "x"}]}},
     "not a condition key"),
    ({"match": {}}, "all and/or any"),
    ({"effect": {"action": "allow", "message": "m"}}, "never loosen"),
    ({"effect": {"action": "flag", "message": "${"}}, "malformed"),
    ({"effect": {"action": "flag", "severity": "urgent", "message": "m"}}, "severity"),
    ({"applies_to": "account"}, "applies_to"),
    ({"script": "rm -rf"}, "is not a field"),
])
def test_bad_policy_rules_are_named(rule, needle):
    base = {"id": "r1", "description": "d", "applies_to": "finding",
            "match": {"all": [{"field": "a", "op": "eq", "value": 1}]},
            "effect": {"action": "flag", "message": "m"}}
    base.update(rule)
    problems: list[Problem] = []
    assert parse_policies({"rules": [base]}, "p.yaml", problems) == []
    assert any(needle in str(p) for p in problems), problems


def test_duplicate_ids_and_top_level_keys():
    problems: list[Problem] = []
    r = {"id": "a", "description": "d", "match": {"all": [{"field": "a", "op": "exists"}]},
         "effect": {"action": "flag", "message": "m"}}
    parse_policies({"rules": [r, {**r}], "imports": ["x"]}, "p.yaml", problems)
    text = "\n".join(map(str, problems))
    assert "used twice" in text and "imports" in text


# ── nothing executes ─────────────────────────────────────────────────────────

def test_a_python_yaml_tag_is_a_parse_error_not_a_call(tmp_path):
    marker = tmp_path / "pwned"
    text = ("rules:\n  - !!python/object/apply:os.system ['touch " + str(marker) + "']\n")
    items, probs = _load("policies", tmp_path, "evil.yaml", text)
    assert items == [] and probs and "not valid YAML" in probs[0]
    assert not marker.exists()
    items, probs = _load("price_books", tmp_path, "evil2.yaml",
                         "rates: !!python/name:os.system\n")
    assert items == [] and probs


def test_templates_are_inert_text(tmp_path):
    evil = ("{{ ''.__class__.__mro__[1].__subclasses__() }} "
            "{% for x in range(3) %}loop{% endfor %} ${a.__class__} ${a} {0.__class__} $$")
    (tpl,), probs = _load("reports", tmp_path, "r.md.j2", evil)
    assert not probs
    out = tpl.render({"a": "ok"})
    assert out.startswith("{{ ''.__class__.__mro__[1].__subclasses__() }}")
    assert "{% for x in range(3) %}loop{% endfor %}" in out
    assert "${a.__class__}" in out          # attribute access is not a dict key: literal
    assert " ok " in out and "{0.__class__}" in out and out.endswith("$")


def test_render_reads_dict_keys_only():
    class Obj:
        hidden = "leak"
    assert render("${o.hidden}", {"o": Obj()}) == "${o.hidden}"
    assert render("${a.b}", {"a": {"b": 2}}) == "2"
    assert render("${a}", {"a": {"nested": 1}}) == "${a}"    # non-scalar stays literal
    assert render("${missing} $$", {}) == "${missing} $"


def test_non_text_and_oversized_files_are_refused(tmp_path):
    _, probs = _load("reports", tmp_path, "bin.md", "a\x00b")
    assert "binary" in probs[0]
    p = tmp_path / "latin.md"
    p.write_bytes(b"caf\xe9")
    problems: list[Problem] = []
    assert load_file("reports", p, "latin.md", problems) == []
    assert "UTF-8" in str(problems[0])


# ── guard rules ──────────────────────────────────────────────────────────────

def _guard(rules: list) -> tuple[list, list[str]]:
    problems: list[Problem] = []
    out = parse_guard_rules({"rules": rules}, "g.yaml", problems)
    return out, [str(p) for p in problems]


def test_guard_rules_match_commands_and_mcp_calls():
    rules, probs = _guard([
        {"id": "gpu", "pattern": r"run-instances.*--instance-type[= ]p4d", "verdict": "ask",
         "reason": "GPU", "price_hint": {"monthly_usd": 24000}},
        {"id": "del", "target": "mcp", "pattern": r"^delete_budget ", "verdict": "deny",
         "reason": "no"},
    ])
    assert not probs
    gpu, dele = rules
    assert gpu.matches_command("aws ec2 run-instances --instance-type p4d.24xlarge")
    assert not gpu.matches_command("aws ec2 describe-instances")
    assert not gpu.matches_tool("run-instances --instance-type p4d")
    assert dele.matches_tool("delete_budget", {"budget_id": 3})
    assert not dele.matches_command("delete_budget x")
    assert gpu.price_hint == {"monthly_usd": 24000.0, "note": None}


@pytest.mark.parametrize("rule,needle", [
    ({"verdict": "allow"}, "may only tighten"),
    ({"verdict": "warn"}, "may only tighten"),
    ({"pattern": ".*"}, "every call"),
    ({"pattern": "(unclosed"}, "regular expression"),
    ({"pattern": "a" * 600}, "longer than"),
    ({"target": "http"}, "target"),
    ({"price_hint": {"monthly_usd": -1}}, "0 or more"),
    ({"price_hint": 5}, "mapping"),
    ({"exec": "x"}, "is not a field"),
])
def test_bad_guard_rules_are_named(rule, needle):
    base = {"id": "r", "pattern": "terraform apply", "verdict": "ask", "reason": "r"}
    base.update(rule)
    out, probs = _guard([base])
    assert out == [] and any(needle in p for p in probs), probs


def test_tighten_never_loosens():
    rules, _ = _guard([{"id": "a", "pattern": "apply", "verdict": "ask", "reason": "r"},
                       {"id": "d", "pattern": "destroy", "verdict": "deny", "reason": "r"}])
    assert tighten("allow", rules, command="terraform apply")["verdict"] == "ask"
    assert tighten("deny", rules, command="terraform apply")["verdict"] == "deny"
    assert tighten("ask", rules, command="terraform destroy")["verdict"] == "deny"
    assert tighten("allow", rules, command="ls")["verdict"] == "allow"
    # a verdict the ordering does not know passes through untouched
    assert tighten("fail_open", rules, command="terraform destroy")["verdict"] == "fail_open"
    hit = tighten("warn", rules, command="terraform apply")
    assert hit["verdict"] == "ask" and hit["rules"][0]["id"] == "a"


# ── playbooks ────────────────────────────────────────────────────────────────

_PB = {"id": "gp3", "finding_type": "gp2_volume", "iac": "terraform",
       "description": "gp2 to gp3", "placeholders": {"address": "resource address"},
       "diff_template": '-  type = "gp2"\n+  type = "gp3"  # ${address}, costs $$0 to change\n',
       "verify": {"check": "volume type is gp3", "within_days": 3},
       "rollback": "Set type back to gp2."}


def test_playbook_renders_declared_placeholders_only():
    problems: list[Problem] = []
    (pb,) = parse_playbooks({"playbooks": [_PB]}, "pb.yaml", problems)
    assert not problems
    out = pb.render({"address": "aws_ebs_volume.data"})
    assert "# aws_ebs_volume.data, costs $0" in out
    with pytest.raises(ValueError):
        pb.render({})
    with pytest.raises(TypeError):
        pb.render({"address": {"__class__": 1}})


@pytest.mark.parametrize("change,needle", [
    ({"diff_template": "${other}"}, "does not declare"),
    ({"placeholders": {"address": "x", "unused": "y"}}, "never uses"),
    ({"diff_template": '"${var.name}"'}, "does not declare"),   # HCL needs $$
    ({"iac": "ansible"}, "iac"),
    ({"verify": {"check": "x", "within_days": 400}}, "1 to 90"),
    ({"rollback": ""}, "non-empty"),
])
def test_bad_playbooks_are_named(change, needle):
    problems: list[Problem] = []
    assert parse_playbooks({"playbooks": [{**_PB, **change}]}, "pb.yaml", problems) == []
    assert any(needle in str(p) for p in problems), problems


# ── price books ──────────────────────────────────────────────────────────────

def test_price_book_yaml_and_csv(tmp_path):
    (r,), probs = _load("price_books", tmp_path, "p.yaml", yaml.safe_dump({"rates": [{
        "provider": "AWS", "sku": "p4d.24xlarge", "unit": "hour", "rate": 21.5,
        "currency": "USD", "effective_from": "2026-01-01", "effective_to": "2026-12-31"}]}))
    assert not probs
    assert (r.provider, r.rate, r.effective_to) == ("aws", 21.5, date(2026, 12, 31))
    assert r.in_effect(date(2026, 6, 1)) and not r.in_effect(date(2027, 1, 1))
    items, probs = _load("price_books", tmp_path, "p.csv",
                         "provider,sku,unit,rate,currency,effective_from\n"
                         "gcp,a2-highgpu-1g,hour,2.9,USD,2026-02-01\n")
    assert not probs and items[0].sku == "a2-highgpu-1g"


@pytest.mark.parametrize("row,needle", [
    ({"rate": -1}, "0 or more"),
    ({"rate": float("nan")}, "0 or more"),
    ({"unit": "fortnight"}, "unit"),
    ({"currency": "usd"}, "three-letter"),
    ({"effective_from": "soon"}, "date"),
    ({"effective_to": "2025-01-01"}, "before effective_from"),
    ({"discount": 0.1}, "is not a field"),
])
def test_bad_price_rows_are_named(tmp_path, row, needle):
    base = {"provider": "aws", "sku": "m5.large", "unit": "hour", "rate": 0.09,
            "currency": "USD", "effective_from": "2026-01-01"}
    base.update(row)
    items, probs = _load("price_books", tmp_path, "p.yaml", yaml.safe_dump({"rates": [base]}))
    assert items == [] and any(needle in p for p in probs), probs


# ── skills ───────────────────────────────────────────────────────────────────

def test_skill_frontmatter(tmp_path):
    (s,), probs = _load("skills", tmp_path, "s/SKILL.md",
                        "---\nname: my-skill\ndescription: When to use it.\n---\n\nBody.\n")
    assert not probs and (s.name, s.description, s.body) == ("my-skill", "When to use it.",
                                                               "Body.")
    _, probs = _load("skills", tmp_path, "t/SKILL.md",
                     "---\nname: x\ndescription: d\nallowed-tools: Bash\n---\nBody\n")
    assert any("cannot pre-approve tools" in p for p in probs)
    _, probs = _load("skills", tmp_path, "u/SKILL.md", "no frontmatter")
    assert any("frontmatter" in p for p in probs)
    _, probs = _load("skills", tmp_path, "v/README.md", "---\nname: x\ndescription: d\n---\nB\n")
    assert any("SKILL.md" in p for p in probs)
    _, probs = _load("skills", tmp_path, "w/SKILL.md", "---\nname: x\ndescription: d\n---\n")
    assert any("no instructions" in p for p in probs)


def test_load_content_expands_globs_and_flags_empty_ones(tmp_path):
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "b.yaml").write_text(
        "rules:\n  - {id: b, description: d, match: {all: [{field: a, op: exists}]}, "
        "effect: {action: flag, message: m}}\n")
    (tmp_path / "policies" / "a.yaml").write_text(
        "rules:\n  - {id: a, description: d, match: {all: [{field: a, op: exists}]}, "
        "effect: {action: flag, message: m}}\n")
    c = load_content(tmp_path, {"policies": ("policies/*.yaml",), "reports": ("r/*.md",)})
    assert [r.id for r in c.items["policies"]] == ["a", "b"]    # sorted, not directory order
    assert [str(p) for p in c.problems] == ["provides.reports: 'r/*.md' matches no file"]
