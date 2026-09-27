# SPDX-License-Identifier: Apache-2.0
"""Terraform: which cloud subjects a repo path deploys, so the path's owner
becomes theirs.

Reads the .tf and .tf.json files in each repo in ctx.repos, and local state
when it is there (terraform.tfstate, terraform.tfstate.d/<workspace>/, and
.terraform/environment for the selected workspace). It never runs terraform
and never reads remote state.

From each directory of Terraform it takes:
  accounts    aws providers: assume_role role_arn (account from the ARN) and
              allowed_account_ids; google `project`; azurerm `subscription_id`
  namespaces  kubernetes_namespace(_v1) metadata.name, helm_release namespace
  resources   from local state: cost-bearing resources, their ids, and the
              accounts their ARNs name
  modules     local module sources, so a module's resources and namespaces
              belong to the directories that call it
  environment default_tags (Environment/Env/Stage), the workspace, or the
              directory name (envs/prod, environments/staging, a "dev" dir)
  tag keys    default_tags keys whose names say team, owner, environment,
              cost center or service

Proposals:
  owner         a cloud subject gets the team that owns its directory, but
                only when an owner fact (CODEOWNERS, or a person) exists for
                that repo path. Confidence: the path owner's minus 0.1, at
                most 0.8; +0.1 when default_tags name the same team (at most
                0.85); a subject deployed from paths with different owners is
                shared: the majority team at 0.4 when it holds two thirds,
                else nothing. With no path owner, default_tags Team alone
                proposes at 0.6.
  environment   the directory where the evidence is (0.8 from default_tags,
                0.7 from a single workspace, 0.65 from an envs/<env> style
                directory, 0.6 from a bare env-named directory; nonprod and
                sandbox 0.05 lower), and the accounts and namespaces it
                deploys (0.05 lower again). Evidence that disagrees, or a
                subject deployed to two environments, proposes nothing.
  tag_key       default_tags keys by meaning, 0.8, adding to (never
                replacing) the keys already known for that meaning
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..model import Fact
from . import _hcl
from ._common import (
    ENV_TOKENS,
    SKIP_DIRS,
    bend,
    dedupe,
    env_of_name,
    fact,
    is_under,
    key_meaning,
    repo_subject,
    tag_key_fact,
)

_ARN_ROLE = re.compile(r"^arn:aws[\w-]*:iam::(\d{12}):role/")
_ARN_ANY = re.compile(r"^arn:aws[\w-]*:[\w-]+:[\w-]*:(\d{12}):")
_ACCOUNT = re.compile(r"^\d{12}$")
_ENV_PARENTS = {"envs", "env", "environments", "environment", "stages", "stage",
                "workspaces", "deployments", "live"}
_MAX_RESOURCES = 300
# Resource types that carry spend of their own, worth an owner.
_COSTLY = {
    "aws_instance", "aws_db_instance", "aws_rds_cluster", "aws_rds_cluster_instance",
    "aws_elasticache_cluster", "aws_elasticache_replication_group", "aws_eks_cluster",
    "aws_eks_node_group", "aws_lambda_function", "aws_s3_bucket", "aws_nat_gateway",
    "aws_lb", "aws_alb", "aws_elb", "aws_ebs_volume", "aws_opensearch_domain",
    "aws_elasticsearch_domain", "aws_msk_cluster", "aws_redshift_cluster",
    "aws_dynamodb_table", "aws_ecs_service", "aws_ecs_cluster", "aws_autoscaling_group",
    "aws_sagemaker_endpoint", "aws_sagemaker_notebook_instance", "aws_kinesis_stream",
    "aws_cloudfront_distribution", "aws_efs_file_system", "aws_emr_cluster",
    "aws_docdb_cluster", "aws_neptune_cluster", "aws_mq_broker",
    "google_compute_instance", "google_container_cluster", "google_container_node_pool",
    "google_sql_database_instance", "google_storage_bucket", "google_bigquery_dataset",
    "azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine",
    "azurerm_kubernetes_cluster", "azurerm_mssql_database", "azurerm_storage_account",
    "azurerm_postgresql_flexible_server",
}


@dataclass
class TfDir:
    repo: Path
    rel: str
    files: list[str] = field(default_factory=list)
    subjects: dict[str, str] = field(default_factory=dict)       # subject -> file
    namespaces: dict[str, str] = field(default_factory=dict)     # name -> file
    modules: dict[str, str] = field(default_factory=dict)        # call name -> repo dir
    default_tags: list[tuple[dict[str, str], str]] = field(default_factory=list)
    workspaces: set[str] = field(default_factory=set)
    resources: list[dict[str, Any]] = field(default_factory=list)


# ── reading ───────────────────────────────────────────────────────────────────

def _tf_dirs(repo: Path) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(repo, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and
                             not d.startswith("."))
        tf = sorted(f for f in filenames if f.endswith((".tf", ".tf.json")))
        if tf:
            rel = Path(dirpath).relative_to(repo).as_posix()
            out["." if rel in ("", ".") else rel] = [Path(dirpath) / f for f in tf]
    return out


def _read(p: Path) -> str:
    try:
        if p.stat().st_size > 5_000_000:
            return ""
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _scope(files: list[tuple[Path, _hcl.Block]], d: Path) -> _hcl.Scope:
    variables: dict[str, str] = {}
    locals_: dict[str, str] = {}
    for _, root in files:
        for b in root.blocks:
            if b.type == "variable" and b.labels and "default" in b.attrs:
                variables[b.labels[0]] = b.attrs["default"]
            elif b.type == "locals":
                locals_.update(b.attrs)
    # terraform.tfvars and *.auto.tfvars override defaults, as terraform does.
    names = ["terraform.tfvars"] + sorted(p.name for p in d.glob("*.auto.tfvars"))
    for name in names:
        p = d / name
        if p.is_file():
            variables.update(_hcl.parse(_read(p)).attrs)
    return _hcl.Scope(variables, locals_)


def _state_resources(state: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in state.get("resources") or []:
        if not isinstance(r, dict) or r.get("mode", "managed") != "managed":
            continue
        rtype = str(r.get("type") or "")
        module = str(r.get("module") or "")
        for inst in r.get("instances") or []:
            attrs = inst.get("attributes") if isinstance(inst, dict) else None
            if not isinstance(attrs, dict):
                continue
            arn = attrs.get("arn") if isinstance(attrs.get("arn"), str) else ""
            rid = attrs.get("id") if isinstance(attrs.get("id"), (str, int)) else ""
            ns = None
            if rtype in ("kubernetes_namespace", "kubernetes_namespace_v1"):
                meta = attrs.get("metadata")
                if isinstance(meta, list) and meta and isinstance(meta[0], dict):
                    ns = meta[0].get("name")
            m = _ARN_ANY.match(arn or "")
            out.append({"type": rtype, "id": str(rid or ""), "arn": arn or "",
                        "account": m.group(1) if m else None, "module": module,
                        "namespace": ns if isinstance(ns, str) else None,
                        "address": f"{module + '.' if module else ''}{rtype}."
                                   f"{r.get('name', '')}"})
    return out


def _read_state(d: Path, td: TfDir, repo: Path) -> None:
    states = [d / "terraform.tfstate"]
    wsdir = d / "terraform.tfstate.d"
    if wsdir.is_dir():
        for ws in sorted(p for p in wsdir.iterdir() if p.is_dir()):
            td.workspaces.add(ws.name)
            states.append(ws / "terraform.tfstate")
    env_file = d / ".terraform" / "environment"
    if env_file.is_file():
        ws = _read(env_file).strip()
        if ws and ws != "default":
            td.workspaces = {ws}          # the selected workspace is the one in use
    for p in states:
        if not p.is_file():
            continue
        try:
            state = json.loads(_read(p) or "{}")
        except ValueError:
            continue
        if not isinstance(state, dict):
            continue
        src = p.relative_to(repo).as_posix()
        for r in _state_resources(state):
            r["file"] = src
            td.resources.append(r)


def read_dir(repo: Path, rel: str, paths: list[Path]) -> TfDir:
    td = TfDir(repo, rel)
    d = repo if rel == "." else repo / rel
    parsed: list[tuple[Path, _hcl.Block]] = []
    for p in paths:
        text = _read(p)
        root = _hcl.parse_json(text) if p.name.endswith(".json") else _hcl.parse(text)
        parsed.append((p, root))
        td.files.append(p.relative_to(repo).as_posix())
    scope = _scope(parsed, d)
    for p, root in parsed:
        src = p.relative_to(repo).as_posix()
        for b in root.blocks:
            if b.type == "provider" and b.labels:
                _provider(b, scope, src, td)
            elif b.type == "resource" and len(b.labels) == 2:
                rtype = b.labels[0]
                if rtype in ("kubernetes_namespace", "kubernetes_namespace_v1"):
                    for meta in b.find("metadata"):
                        name = _hcl.string(meta.attrs.get("name"), scope)
                        if name:
                            td.namespaces.setdefault(name, src)
                elif rtype == "helm_release":
                    name = _hcl.string(b.attrs.get("namespace"), scope)
                    if name:
                        td.namespaces.setdefault(name, src)
            elif b.type == "module" and b.labels:
                source = _hcl.string(b.attrs.get("source"), scope)
                if source and source.startswith(("./", "../")):
                    target = (d / source).resolve()
                    try:
                        mrel = target.relative_to(repo.resolve()).as_posix()
                    except ValueError:
                        continue
                    td.modules[b.labels[0]] = "." if mrel in ("", ".") else mrel
    _read_state(d, td, repo)
    return td


def _provider(b: _hcl.Block, scope: _hcl.Scope, src: str, td: TfDir) -> None:
    kind = b.labels[0]
    if kind == "aws":
        roles = [_hcl.string(r.attrs.get("role_arn"), scope)
                 for r in b.find("assume_role") + b.find("assume_role_with_web_identity")]
        if "assume_role" in b.attrs:
            roles.append(_hcl.mapping(b.attrs["assume_role"], scope).get("role_arn"))
        for arn in roles:
            m = _ARN_ROLE.match(arn or "")
            if m:
                td.subjects.setdefault(f"aws_account:{m.group(1)}", src)
        for acct in _hcl.strings(b.attrs.get("allowed_account_ids"), scope):
            if _ACCOUNT.match(acct):
                td.subjects.setdefault(f"aws_account:{acct}", src)
        for dt in b.find("default_tags"):
            tags = _hcl.mapping(dt.attrs.get("tags"), scope)
            if tags:
                td.default_tags.append((tags, src))
    elif kind in ("google", "google-beta"):
        project = _hcl.string(b.attrs.get("project"), scope)
        if project:
            td.subjects.setdefault(f"gcp_project:{project}", src)
        labels = _hcl.mapping(b.attrs.get("default_labels"), scope)
        if labels:
            td.default_tags.append((labels, src))
    elif kind == "azurerm":
        sub = _hcl.string(b.attrs.get("subscription_id"), scope)
        if sub:
            td.subjects.setdefault(f"azure_subscription:{sub}", src)


def read_repo(repo: Path) -> dict[str, TfDir]:
    return {rel: read_dir(repo, rel, paths) for rel, paths in _tf_dirs(repo).items()}


# ── what a directory says ─────────────────────────────────────────────────────

def _tag_values(td: TfDir, meaning: str) -> set[str]:
    return {v for tags, _ in td.default_tags for k, v in tags.items()
            if key_meaning(k) == meaning and v.strip()}


def dir_env(td: TfDir) -> tuple[str, str, float, str] | None:
    """(env, repo path the evidence is about, confidence, locator) for one
    directory, or None when nothing says or the evidence disagrees."""
    found: list[tuple[str, str, float, str]] = []
    for tags, file in td.default_tags:
        for k, v in tags.items():
            if key_meaning(k) != "environment":
                continue
            got = env_of_name(v, accounts=False)
            if got is None:
                return None               # a tag value we cannot read: say nothing
            found.append((got[0], td.rel, 0.8, file))
    if len(td.workspaces) == 1:
        ws = next(iter(td.workspaces))
        got = env_of_name(ws, accounts=False)
        if got:
            base = "" if td.rel == "." else td.rel + "/"
            found.append((got[0], td.rel, 0.7, f"{base}workspace:{ws}"))
    parts = [] if td.rel == "." else td.rel.split("/")
    for i, part in enumerate(parts):
        low = part.lower()
        got = env_of_name(low, accounts=False)
        if got is None:
            continue
        where = "/".join(parts[:i + 1])
        if i and parts[i - 1].lower() in _ENV_PARENTS:
            found.append((got[0], where, 0.65, where))
        elif low in ENV_TOKENS:
            found.append((got[0], where, 0.6, where))
    if len({e for e, _, _, _ in found}) != 1:
        return None
    env, where, conf, locator = max(found, key=lambda x: x[2])
    if env in ("nonprod", "sandbox"):
        conf -= 0.05
    return env, where, conf, locator


# ── proposing ─────────────────────────────────────────────────────────────────

def _owner_value(f: Fact) -> dict[str, Any]:
    v: dict[str, Any] = {"team": f.value["team"]}
    if f.value.get("people"):
        v["people"] = list(f.value["people"])
    return v


def propose(ctx: Any) -> list[Fact]:
    dirs: list[tuple[str, TfDir]] = []
    for repo in ctx.repos:
        label = ctx.repo_label(repo)
        for _, td in sorted(read_repo(repo).items()):
            dirs.append((label, td))
    if not dirs:
        return []
    by_rel = {(label, td.rel): td for label, td in dirs}
    callers: dict[tuple[str, str], list[TfDir]] = {}
    for label, td in dirs:
        for mrel in td.modules.values():
            callers.setdefault((label, mrel), []).append(td)

    def path_owner(td: TfDir) -> Fact | None:
        # Repo paths are repo-relative in every repo: the guard asks with the
        # path inside whichever repo it runs in.
        return ctx.owner_fact(repo_subject(ctx, td.repo, td.rel))

    claims: dict[str, list[dict[str, Any]]] = {}

    def claim(subject: str, label: str, td: TfDir, locator: str, *,
              home: TfDir | None = None, tagged: bool = True) -> None:
        """`td` deploys `subject`; `home` is the directory that defines it
        (a module), whose owner, when it has one, comes first."""
        where = home if home is not None and path_owner(home) is not None else td
        env = dir_env(td)
        teams = sorted(_tag_values(td, "team")) if tagged else []
        claims.setdefault(subject, []).append({
            "owner": path_owner(where), "tag_team": teams, "env": env,
            "source": f"terraform:{label}{locator}"})

    resources = 0
    for label, td in dirs:
        for subject, src in sorted(td.subjects.items()):
            claim(subject, label, td, src)
        users = callers.get((label, td.rel), [])
        for name, src in sorted(td.namespaces.items()):
            # A namespace defined in a module is deployed by the callers.
            for c in users or [td]:
                claim(f"k8s_namespace:{name}", label, c, src, home=td, tagged=False)
        for r in td.resources:
            home = None
            parts = r["module"].split(".")
            if len(parts) >= 2 and parts[0] == "module":
                mrel = td.modules.get(parts[1])
                home = by_rel.get((label, mrel)) if mrel else None
            if r["account"]:
                claim(f"aws_account:{r['account']}", label, td, r["file"])
            if r["namespace"]:
                claim(f"k8s_namespace:{r['namespace']}", label, td, r["file"], tagged=False)
            if r["type"] in _COSTLY and r["id"] and resources < _MAX_RESOURCES:
                resources += 1
                claim(f"resource:{r['id']}", label, td, r["file"], home=home)

    out: list[Fact | None] = []
    for subject, cs in sorted(claims.items()):
        out.append(_owner(ctx, subject, cs))
        out.append(_env(ctx, subject, cs))
    out.extend(_dir_envs(ctx, dirs))
    out.extend(_tag_keys(ctx, dirs))
    return dedupe(out)


def _owner(ctx: Any, subject: str, cs: list[dict[str, Any]]) -> Fact | None:
    owned = [c for c in cs if c["owner"] is not None]
    usd = ctx.usd(subject)
    if not owned:
        teams = {t for c in cs for t in c["tag_team"]}
        if len(teams) == 1:
            return fact("owner", subject, {"team": next(iter(teams))}, cs[0]["source"],
                        0.6, usd)
        return None
    votes = Counter(c["owner"].value["team"] for c in owned)
    team, n = votes.most_common(1)[0]
    winner = next(c for c in owned if c["owner"].value["team"] == team)
    if len(votes) > 1:
        if n * 3 < len(owned) * 2:
            return None                   # shared, and nobody holds most of it
        return fact("owner", subject, {"team": team}, winner["source"], 0.4, usd)
    conf = min(0.8, winner["owner"].confidence - 0.1)
    tag_teams = {t for c in owned for t in c["tag_team"]}
    if tag_teams and all(bend(t, team) for t in tag_teams):
        conf = min(0.85, conf + 0.1)
    elif tag_teams:
        conf = min(conf, 0.5)             # CODEOWNERS and default_tags disagree
    return fact("owner", subject, _owner_value(winner["owner"]), winner["source"], conf, usd)


def _env(ctx: Any, subject: str, cs: list[dict[str, Any]]) -> Fact | None:
    """One environment for everything that deploys `subject`, or nothing: a
    subject deployed from a prod and an unlabelled directory stays unknown."""
    envs = {c["env"][0] if c["env"] else None for c in cs}
    if len(envs) != 1 or None in envs:
        return None
    c = cs[0]
    env, _, conf, _ = c["env"]
    return fact("environment", subject, {"env": env}, c["source"], conf - 0.05,
                ctx.usd(subject))


def _dir_envs(ctx: Any, dirs: list[tuple[str, TfDir]]) -> list[Fact | None]:
    picked: dict[tuple[Path, str], tuple[str, float, str]] = {}
    for label, td in dirs:
        got = dir_env(td)
        if got is None:
            continue
        env, where, conf, locator = got
        k = (td.repo, where)
        prev = picked.get(k)
        if prev is not None and prev[0] != env:
            picked[k] = ("", 0.0, "")      # two answers for one path: none
        elif prev is None or conf > prev[1]:
            picked[k] = (env, conf, f"terraform:{label}{locator}")
    out: list[Fact | None] = []
    for repo, where in sorted(picked, key=lambda k: (k[1].count("/"), k[1], str(k[0]))):
        env, conf, src = picked[(repo, where)]
        if not env:
            continue
        parents = [p for r, p in picked if r == repo and p != where and is_under(where, p)
                   and picked[(r, p)][0]]
        if parents and picked[(repo, max(parents, key=len))][0] == env:
            continue
        out.append(fact("environment", repo_subject(ctx, repo, where), {"env": env}, src,
                        conf))
    return out


def _tag_keys(ctx: Any, dirs: list[tuple[str, TfDir]]) -> list[Fact | None]:
    keys: dict[str, list[str]] = {}
    src: dict[str, str] = {}
    for label, td in dirs:
        for tags, file in td.default_tags:
            for k in tags:
                meaning = key_meaning(k)
                if meaning and k not in keys.setdefault(meaning, []):
                    keys[meaning].append(k)
                    src.setdefault(meaning, f"terraform:{label}{file}")
    return [tag_key_fact(ctx, meaning, ks, src[meaning], 0.8) for meaning, ks in
            sorted(keys.items())]
