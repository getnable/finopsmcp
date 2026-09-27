# SPDX-License-Identifier: Apache-2.0
"""The org model in the guard: who owns what a command touches, which team's
budgets apply to it, and which thresholds a human set for that team or
environment (finops.org).

guard.py imports this only on the paths that need it: a priced change, and
a verdict that asks or denies. An ordinary command never loads it. Everything
here may raise; guard._OrgLens catches it, judges the call again as if there
were no org model, and records the fail-open, so an unreadable or odd org
model can cost a citation or a team scope, never a verdict.

What each fact may change, following "guesses may restrict, never enable":

  owner        cited in an ask or a deny ("owned by payments
               (#payments-oncall)"); a proposed owner is cited as "likely".
               A citation changes no decision, so a guess may be shown.
  team scope   the team whose budgets a priced change is checked against,
               when FINOPS_GUARD_TEAM is unset: the confirmed owner of the
               working directory's repo_path. Only confirmed, since it picks
               which budget and which threshold apply.
  threshold    a confirmed threshold fact for that team or for the
               environment the command touches (confirmed environment facts
               only) replaces max_auto_monthly_usd and the velocity cap, up or
               down: a human set it. A proposed threshold is never read.

"Confirmed" here is strict (OrgModel queries with strict=True): a fact from
a repo's nable.org/ that no person has trusted (`nable org trust --here`)
is somebody else's word. It is cited as "likely", never picks the team, and
its thresholds may only lower a figure, below the trusted model's or, with
none, the policy's own. A bare repo_path fact outside a repo names no repo
and never picks the team either. Repo paths are asked about with the repo
named ("repo_path:github.com/acme/x//infra"), so a path in one repo is never
an answer about another.
"""
from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path
from typing import Any

# An AWS ARN names the account it lives in.
_ARN_ACCOUNT_RE = re.compile(r"\barn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:(\d{12}):")
_PROFILE_RE = re.compile(r"(?<!\S)--profile(?:=|\s+)([^\s;&|'\"]+)")
# -n / --namespace on a kubectl, helm or oc command, in the same shell segment.
_K8S_RE = re.compile(r"\b(?:kubectl|helm|oc)\b[^;&|]*")
_NAMESPACE_RE = re.compile(r"(?<!\S)(?:-n|--namespace)(?:=|\s+)([a-z0-9][a-z0-9.-]{0,62})(?!\S)")
_DIR_TOOLS_RE = re.compile(r"\b(?:terraform|tofu|terragrunt|pulumi|cdk|sam)\s")
_CHDIR_RE = re.compile(r"-chdir=(\S+)")


def load_model(cwd: str | None):
    """The org model the working directory sees: FINOPS_ORG_DIR, else the
    nable.org/ of the repo holding `cwd` (the agent's directory, which may
    not be the hook's) over the data dir's, else the data dir's. Read
    through a cache (_cached) so a hook call does not parse YAML when
    nothing changed."""
    return _cached(cwd)


# The parsed model, beside the decision ledger, keyed on what it was read
# from: the org files (and their directory), the legacy files and the two
# FINOPS_*_TAGS variables. Importing PyYAML and parsing a few hundred facts
# costs a hook call ~25 ms; reading this back costs ~5. Derived data only:
# a stale or broken cache is a miss, and a hit is exactly what a fresh read
# of the same files returns.
_CACHE_NAME = "org-model-cache.json"
_CACHE_VERSION = 3


def _stamp(p: Path) -> list[int] | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size, st.st_ino]


def _cache_key(layers: list[Any]) -> str:
    import json

    from .org.legacy import accounts_path, tag_rules_path
    from .org.model import KNOWN_FILES
    from .org.store import trust_path
    parts: list[Any] = [_CACHE_VERSION]
    for layer in layers:
        d = layer.dir
        # Trust and the repo a bare path belongs to are part of what was read.
        parts += [str(d), _stamp(d), layer.source, layer.trusted, layer.anchor, layer.layer]
        parts += [[n, _stamp(d / n)] for n in KNOWN_FILES]
    for p in (tag_rules_path(), accounts_path(), trust_path()):
        parts += [str(p), _stamp(p)]
    parts += [os.environ.get(v, "") for v in ("FINOPS_REQUIRED_TAGS", "FINOPS_PROTECTED_TAGS")]
    return json.dumps(parts)


def _cache_path() -> Path:
    from . import guard_ledger
    return guard_ledger.ledger_path().with_name(_CACHE_NAME)


def _meta(f: Any) -> dict[str, Any]:
    return {"src": f.src_dir, "trusted": f.trusted, "anchor": f.anchor, "layer": f.layer}


def _cached(cwd: str | None):
    """org.load() as seen from `cwd`, from the cache when its key still matches."""
    import json

    from . import org
    from .org.model import Fact, OrgModel
    layers = org.layers(None, cwd=cwd)
    key = _cache_key(layers)
    path = _cache_path()
    import gc
    # Thousands of small objects and no cycles among them: the collector
    # would walk them all several times over for nothing (half the time).
    collecting = gc.isenabled()
    gc.disable()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and doc.get("key") == key:
            facts = []
            for o, meta, row in doc["facts"]:
                f = Fact.from_cache(row, origin=o)
                f.src_dir, f.trusted = meta["src"], bool(meta["trusted"])
                f.anchor, f.layer = meta["anchor"], int(meta["layer"])
                facts.append(f)
            return OrgModel(facts, dir=layers[0].dir, dir_source=layers[0].source,
                            warnings=list(doc.get("warnings") or []), layers=layers)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    finally:
        if collecting:
            gc.enable()
    m = org.load(None, cwd=cwd)
    if not m.facts:
        return m       # nothing was parsed, so there is nothing to save
    tmp = None
    try:
        import tempfile
        body = json.dumps({"key": key, "warnings": m.warnings,
                           "facts": [[f.origin, _meta(f), f.to_cache()] for f in m.facts]})
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{_CACHE_NAME}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        # No cache is a slower next call, nothing more.
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    return m


def _profile_accounts(profile: str, model) -> list[str]:
    """Account ids an AWS CLI profile is known to map to: an account fact
    whose value names the profile, or an accounts.yaml entry (accounts.py)."""
    out = [f.subject.id for f in model.by_kind("account")
           if f.live and f.subject.kind == "aws_account"
           and str(f.value.get("profile") or "") == profile]
    from .org.legacy import accounts_path
    p = accounts_path()
    if p.is_file():
        import yaml
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for e in (data.get("accounts") or []) if isinstance(data, dict) else []:
            if isinstance(e, dict) and str(e.get("profile") or "") == profile \
                    and e.get("account_id"):
                out.append(str(e["account_id"]).strip())
    return out


def _work_dir(command: str, cwd: str | None) -> Path:
    """Where the command acts: after a `cd` and a -chdir for an IaC tool that
    works on its directory, else the working directory."""
    base = Path(cwd or os.getcwd())
    from .guard import _cd_target, _segmented
    seg = _segmented(command)
    tool = _DIR_TOOLS_RE.search(seg)
    if tool is None:
        return base
    base, _ = _cd_target(seg, tool.start(), str(base))
    chdir = _CHDIR_RE.search(seg[tool.start():])
    if chdir:
        base = base / Path(chdir.group(1)).expanduser()
    return base


def subjects(command: str, cwd: str | None, model) -> list[str]:
    """What the command touches, most specific first, as "kind:id": the
    Kubernetes namespaces it names, the AWS accounts in its ARNs, behind its
    --profile or in FINOPS_GUARD_ACCOUNT, then the repo path it runs in
    (with its repo named)."""
    from .org import repo_subject
    flat = " ".join(command.split())
    out: dict[str, None] = {}
    for seg in _K8S_RE.findall(flat):
        for ns in _NAMESPACE_RE.findall(seg):
            out[f"k8s_namespace:{ns}"] = None
    for acct in _ARN_ACCOUNT_RE.findall(flat):
        out[f"aws_account:{acct}"] = None
    for profile in _PROFILE_RE.findall(flat):
        for acct in _profile_accounts(profile, model):
            out[f"aws_account:{acct}"] = None
    env_acct = os.getenv("FINOPS_GUARD_ACCOUNT", "").strip()
    if re.fullmatch(r"\d{12}", env_acct):
        out[f"aws_account:{env_acct}"] = None
    try:
        where = _work_dir(command, cwd)
    except (OSError, ValueError):
        where = Path(cwd or os.getcwd())
    rel = repo_subject(where) if where.exists() else None
    if rel is None and cwd:
        rel = repo_subject(cwd)
    if rel is not None:
        out[rel] = None
    return list(out)


def owner(model, subs: list[str]):
    """The owner to cite: the first confirmed answer in subject order, else
    the first proposed one. None when nothing says."""
    first = None
    for s in subs:
        r = model.owner_of(s, strict=True)
        if r is None or not r.team:
            continue
        if r.confirmed:
            return r
        first = first or r
    return first


def owner_words(r) -> str:
    """"Owned by payments (#payments-oncall)." or, for a proposal, "Likely
    owned by payments (#payments-oncall), not confirmed."."""
    chan = f" ({r.channel})" if r.channel else ""
    if r.confirmed:
        return f"Owned by {r.team}{chan}."
    return f"Likely owned by {r.team}{chan}, not confirmed."


def owner_field(r) -> dict[str, Any]:
    """The compact owner a verdict and the ledger carry."""
    out: dict[str, Any] = {"team": r.team, "confirmed": bool(r.confirmed)}
    if r.channel:
        out["channel"] = r.channel
    return out


def team_scope(model, cwd: str | None) -> tuple[str | None, str | None]:
    """(team, where it came from) for the working directory: the confirmed
    owner of its repo_path, or (None, None). A proposal never picks a team:
    the team decides which budget and threshold apply."""
    from .org import repo_subject
    subject = repo_subject(cwd or os.getcwd())
    if subject is None:
        return None, None
    r = model.owner_of(subject, strict=True)
    if r is None or not r.confirmed or not r.team:
        return None, None
    return r.team, f"org model, {r.matched or subject}"


def confirmed_envs(model, subs: list[str]) -> list[str]:
    """The environments the command's subjects are confirmed to be in."""
    out: dict[str, None] = {}
    for s in subs:
        env, confirmed = model.environment_of(s, strict=True)
        if confirmed and env != "unknown":
            out[env] = None
    return list(out)


def _ceiling() -> dict[str, float]:
    """The policy's own figures: what a threshold from an untrusted org dir
    must come in under to apply at all."""
    from .policy import load_policy, velocity_cap
    pol = load_policy()
    return {"max_auto_monthly_usd": float(pol.get("max_auto_monthly_usd", 500.0)),
            "velocity_cap_usd": velocity_cap(pol)}


def thresholds(model, team: str | None, envs: list[str]) -> dict[str, Any]:
    """Confirmed per-scope thresholds for this team and these environments
    (org.threshold_for, strict), {} when none applies. With more than one
    environment, each figure is the lowest any of them sets: the command
    touches all of them. `files` names the file each figure came from."""
    ceiling = _ceiling() if any(not layer.trusted for layer in model.layers) else None
    if not envs:
        return model.threshold_for(team, None, strict=True, ceiling=ceiling)
    out: dict[str, Any] = {}
    scope: dict[str, str] = {}
    files: dict[str, str] = {}
    for env in envs:
        t = model.threshold_for(team, env, strict=True, ceiling=ceiling)
        for name in ("max_auto_monthly_usd", "velocity_cap_usd"):
            if name in t and (name not in out or t[name] < out[name]):
                out[name] = t[name]
                scope[name] = t["scope"][name]
                if (t.get("files") or {}).get(name):
                    files[name] = t["files"][name]
                else:
                    files.pop(name, None)
    if out:
        out["scope"] = scope
        if files:
            out["files"] = files
    return out


def whose(t: dict[str, Any], name: str) -> str:
    """"for team payments (in /repo/nable.org/policy.yaml)": the scope whose
    confirmed threshold set `name`, and the file it is in; "" for none. What
    guard._OrgLens.whose says, so a person can find and fix the figure."""
    scope = (t.get("scope") or {}).get(name)
    if not scope:
        return ""
    kind, _, ident = str(scope).partition(":")
    words = "for the org" if kind == "org" else f"for {kind} {ident}"
    where = (t.get("files") or {}).get(name)
    return f"{words} (in {where})" if where else words
