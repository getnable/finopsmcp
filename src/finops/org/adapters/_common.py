# SPDX-License-Identifier: Apache-2.0
"""What more than one adapter needs: fact building, environment words, team
name normal forms, tag key meanings, and a walk of a repo that finds
infrastructure code."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ..model import Fact, FactError, Subject

# ── facts ─────────────────────────────────────────────────────────────────────


def fact(kind: str, subject: Any, value: dict[str, Any], source: str, confidence: float,
         dollars: float | None = None) -> Fact | None:
    """A proposal, or None when it does not fit the schema (an adapter never
    raises over one odd input)."""
    from ..store import make_fact
    try:
        return make_fact(kind, subject, value, source=source,
                         confidence=round(min(max(confidence, 0.05), 0.95), 2),
                         dollars_monthly=round(dollars, 2) if dollars else None)
    except FactError:
        return None


def dedupe(facts: list[Fact | None]) -> list[Fact]:
    """Drop Nones and repeats of a key, keeping the first."""
    out: list[Fact] = []
    seen: set[str] = set()
    for f in facts:
        if f is not None and f.key not in seen:
            seen.add(f.key)
            out.append(f)
    return out


# ── environments ──────────────────────────────────────────────────────────────

# Whole words (tokens) that say which environment something is. Compound
# spellings are matched before tokens so "non-prod" never reads as "prod".
ENV_COMPOUNDS = {
    "non-prod": "nonprod", "non_prod": "nonprod", "pre-prod": "nonprod",
    "pre_prod": "nonprod", "log-archive": "shared", "log_archive": "shared",
    "shared-services": "shared", "shared_services": "shared",
    "disaster-recovery": "dr", "disaster_recovery": "dr",
}
ENV_TOKENS = {
    "prod": "prod", "production": "prod", "prd": "prod", "live": "prod",
    "nonprod": "nonprod", "preprod": "nonprod", "dev": "nonprod", "development": "nonprod",
    "develop": "nonprod", "staging": "nonprod", "stage": "nonprod", "stg": "nonprod",
    "test": "nonprod", "testing": "nonprod", "qa": "nonprod", "uat": "nonprod",
    "sandbox": "sandbox", "sbx": "sandbox", "playground": "sandbox", "scratch": "sandbox",
    "dr": "dr", "standby": "dr",
    "security": "shared", "logarchive": "shared", "audit": "shared", "sharedservices": "shared",
    "shared": "shared",
}
# Words that mean an environment only in an account or OU name, where
# "security" is the security account; in a directory they are code.
_ACCOUNT_ONLY = {"security", "audit", "logarchive", "sharedservices", "shared", "live"}


def env_words(name: str, *, accounts: bool = True) -> list[tuple[str, str]]:
    """(env, word) for every environment word in `name`, compounds first."""
    low = re.sub(r"\s+", "-", (name or "").strip().lower())
    found: list[tuple[str, str]] = []
    for word, env in ENV_COMPOUNDS.items():
        if re.search(r"(?:^|[^a-z0-9])" + re.escape(word) + r"(?:[^a-z0-9]|$)", low):
            if accounts or env != "shared":
                found.append((env, word))
            low = low.replace(word, " ")
    for tok in re.split(r"[^a-z0-9]+", low):
        env = ENV_TOKENS.get(tok)
        if env and (accounts or tok not in _ACCOUNT_ONLY):
            found.append((env, tok))
    return found


def env_of_name(name: str, *, accounts: bool = True) -> tuple[str, str] | None:
    """(env, word) a name says, or None when it says nothing or disagrees
    with itself ("prod-dev"): unknown stays unknown. DR beats prod ("prod-dr"
    is the standby), sandbox beats nonprod ("dev-sandbox")."""
    hits = env_words(name, accounts=accounts)
    envs = {e for e, _ in hits}
    if not envs:
        return None
    if "prod" in envs and envs & {"nonprod", "sandbox"}:
        return None
    for want in ("dr", "prod", "sandbox", "nonprod", "shared"):
        if want in envs:
            return want, next(w for e, w in hits if e == want)
    return None


# ── team names ────────────────────────────────────────────────────────────────

TEAM_SUFFIXES = ("svc", "service", "team", "squad")


def _parts(value: str) -> list[str]:
    return [p for p in re.split(r"[\s._/:-]+", (value or "").strip().lower()) if p]


def team_norm(value: str) -> str:
    """The normal form of a team value: lowercase, separators dropped, one
    separated -svc/-service/-team/-squad suffix dropped, a plural s dropped
    (five letters or more, not "ss"). Two values with one normal form are
    one team. Deliberately nothing looser: "pay" is not "payments"."""
    parts = _parts(value)
    if len(parts) > 1 and parts[-1] in TEAM_SUFFIXES:
        parts = parts[:-1]
    joined = "".join(parts)
    if len(joined) >= 5 and joined.endswith("s") and not joined.endswith("ss"):
        joined = joined[:-1]
    return joined


def bend(a: str, b: str) -> str | None:
    """How far apart two team values are: "same", "case", "separator"
    (case and separators), "suffix" (a suffix or plural too), or None when
    they are not one team."""
    if a == b:
        return "same"
    if a.strip().lower() == b.strip().lower():
        return "case"
    if "".join(_parts(a)) == "".join(_parts(b)):
        return "separator"
    if team_norm(a) and team_norm(a) == team_norm(b):
        return "suffix"
    return None


# ── tag keys ──────────────────────────────────────────────────────────────────

# Tag key spellings whose meaning is in the name, compared after lowercasing
# and turning - and spaces into _. A key that is not here ("app", "group",
# "tier") is left to its values, or to a person.
KEY_MEANING = {
    "team": "team", "squad": "team", "owning_team": "team", "owner_team": "team",
    "team_name": "team", "teamname": "team", "tribe": "team",
    "owner": "owner", "owned_by": "owner", "ownedby": "owner", "contact": "owner",
    "maintainer": "owner", "technical_owner": "owner", "business_owner": "owner",
    "cost_center": "cost_center", "costcenter": "cost_center", "cost_centre": "cost_center",
    "costcentre": "cost_center", "billing_code": "cost_center", "billingcode": "cost_center",
    "env": "environment", "environment": "environment", "stage": "environment",
    "deployment_environment": "environment",
    "service": "service", "service_name": "service", "servicename": "service",
    "app": "service", "application": "service",
}


def key_meaning(key: str) -> str | None:
    return KEY_MEANING.get(re.sub(r"[\s-]+", "_", (key or "").strip().lower()))


def tag_key_fact(ctx: Any, canonical: str, keys: list[str], source: str, conf: float,
                 dollars: float | None = None) -> Fact | None:
    """A tag_key proposal that only ever adds: None when every key is known
    already for `canonical`, else the known keys plus the new ones, so
    confirming it loses nothing."""
    known: list[str] = []
    for f in ctx.view().candidates("tag_key", Subject("org", "org"), canonical=canonical):
        if f.live:
            for k in f.value.get("keys") or []:
                if k.lower() not in {x.lower() for x in known}:
                    known.append(k)
    new: dict[str, str] = {}
    for k in sorted(keys, key=lambda k: (k.lower(), k)):
        if k.lower() not in {x.lower() for x in known}:
            new.setdefault(k.lower(), k)
    if not new:
        return None
    merged = known + list(new.values())
    return fact("tag_key", Subject("org", "org"), {"canonical": canonical, "keys": merged},
                source, conf, dollars)


# ── the repo walk ─────────────────────────────────────────────────────────────

SKIP_DIRS = {".git", "node_modules", ".terraform", "vendor", ".venv", "venv", "__pycache__",
             ".tox", "dist", "build", "target", "cdk.out", "nable.org", ".idea", ".vscode",
             ".pytest_cache", ".mypy_cache", ".ruff_cache", "site-packages", ".serverless",
             ".aws-sam", ".terragrunt-cache", ".pulumi"}
_YAML = (".yaml", ".yml")
_CFN = re.compile(r"AWSTemplateFormatVersion|^\s*Transform:\s*['\"]?AWS::Serverless|"
                  r"^\s+Type:\s*['\"]?AWS::[A-Za-z0-9]+::", re.MULTILINE)
_K8S_API = re.compile(r"^apiVersion:\s*\S+", re.MULTILINE)
_K8S_KIND = re.compile(r"^kind:\s*[A-Z]\w*", re.MULTILINE)
MAX_FILES = 50_000
_HEAD = 16_384


def _head(p: Path) -> str:
    try:
        if p.stat().st_size > 2_000_000:
            return ""
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(_HEAD)
    except OSError:
        return ""


def iac_kind(p: Path) -> str | None:
    """Which infrastructure-as-code a file is, or None."""
    name = p.name
    low = name.lower()
    if low.endswith((".tf", ".tf.json")):
        return "terraform"
    if name == "Chart.yaml":
        return "helm"
    if name == "cdk.json":
        return "cdk"
    if name in ("Pulumi.yaml", "Pulumi.yml"):
        return "pulumi"
    if low in ("kustomization.yaml", "kustomization.yml"):
        return "kubernetes"
    if low.endswith(_YAML) or low.endswith((".template", ".json")):
        head = _head(p)
        if not head:
            return None
        if _CFN.search(head):
            return "cloudformation"
        if low.endswith(_YAML) and _K8S_API.search(head) and _K8S_KIND.search(head):
            return "kubernetes"
    return None


def scan_iac(root: Path) -> dict[str, dict[str, Any]]:
    """{repo-relative dir: {"kinds": set, "files": [repo-relative file]}} for
    every directory that directly holds infrastructure code. Symlinks are not
    followed; a Helm chart's templates/ belong to the chart, not to plain
    Kubernetes; generated and vendored trees are skipped."""
    out: dict[str, dict[str, Any]] = {}
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not
                             (d.startswith(".") and d != ".github"))
        here = Path(dirpath)
        rel = here.relative_to(root).as_posix()
        rel = "." if rel in ("", ".") else rel
        if "Chart.yaml" in filenames:
            dirnames[:] = [d for d in dirnames if d != "templates"]
        for name in sorted(filenames):
            seen += 1
            if seen > MAX_FILES:
                return out
            kind = iac_kind(here / name)
            if kind is None:
                continue
            slot = out.setdefault(rel, {"kinds": set(), "files": []})
            slot["kinds"].add(kind)
            slot["files"].append(name if rel == "." else f"{rel}/{name}")
    return out


def is_under(path: str, prefix: str) -> bool:
    """Whole-component prefix test on repo paths ("." holds everything)."""
    return prefix == "." or path == prefix or path.startswith(prefix + "/")
