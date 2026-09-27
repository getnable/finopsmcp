# SPDX-License-Identifier: Apache-2.0
"""The capability vocabulary: everything a pack may ask for, as a closed set.

A pack declares what it needs in `[capabilities]`. The core shows that list at
install, diffs it on every update, and for code-bearing packs enforces it in
the broker (broker.py: secrets, read_data, network, write_org, act and
max_autonomy; read_cloud is shown and approved but not brokered yet, since
the broker hands a pack no cloud credentials). pricing is required by the
manifest check: a pack that ships price books must declare it. A value outside
this module's vocabulary is a validation error, not a warning: an unknown
capability is one nobody can review, so it is one nobody can approve.

    read_data     nable data scopes (READ_DATA_SCOPES)
    read_cloud    provider:service:Action, read verbs only, no credential or
                  secret services (aws:ce:GetCostAndUsage, k8s:pods:list)
    secrets       credentials (tokens, keys, passwords): environment-variable
                  names the core passes in from the pack's own vault
                  namespace (`nable pack secret set`). Their values are
                  redacted from everything the pack returns and logs, and a
                  proposal or a row that carries one is refused. nable's own
                  (FINOPS_*, NABLE_*) are never grantable, and cloud
                  credential names (AWS_*, GOOGLE_*, AZURE_CLIENT_SECRET, ...)
                  only to a first-party pack (is_cloud_credential)
    settings      non-secret configuration (an org name, an API URL), passed
                  in the same way from the same vault namespace (`nable pack
                  setting set`) but not treated as credentials: a pack may
                  put a setting's value in what it proposes. A setting name
                  that looks like a credential (*_TOKEN, *_KEY, *PASSWORD*,
                  *SECRET*, ...) is refused, and so is a name declared as both
    network       host[:port] egress allowlist; empty means none
    write_org     "proposals" only: a pack may propose org facts, never confirm
    act           "pr" and "ticket"; "execute" is first-party only
    guard         "tighten-only": its guard rules can turn allow into ask and
                  ask into deny, never the reverse
    pricing       "override": its price books replace list prices in the
                  estimates nable shows. They inform displayed estimates only:
                  the guard and the cost preflight judge at the higher of the
                  list price and the book rate, so a price book can never make
                  a change look cheaper to anything that allows or asks
    max_autonomy  L0..L2 for everyone, L3 for first-party; L4 for no pack

Absent keys mean nothing: no data, no cloud, no secrets, no network.
"""
from __future__ import annotations

import fnmatch
import ipaddress
import re
from typing import Any

from .errors import Problem

READ_DATA_SCOPES: dict[str, str] = {
    "focus.cost": "cost rows in FOCUS shape (billed and effective cost by service, account, tag)",
    "focus.usage": "usage quantities in FOCUS shape",
    "org.owners": "who owns which accounts, repositories and services",
    "org.environments": "which accounts and clusters are prod, staging or dev",
    "org.teams": "teams and their members",
    "budgets": "budgets and month-to-date spend against them",
    "recommendations": "open savings recommendations",
    "ledger.guard": "the guard's decision ledger (redacted commands and verdicts)",
    "repo.files": ("files it names (such as catalog-info.yaml) in the repositories `nable org "
                   "init` reads, read on this machine by nable and handed to it as text"),
}

WRITE_ORG: dict[str, str] = {
    "proposals": "may propose org facts (owners, environments); a human confirms them",
}

ACT: dict[str, str] = {
    "pr": "may propose a pull request for a human to review",
    "ticket": "may propose a ticket in your tracker",
    "execute": "may apply a change itself (first-party only, through the separate executor)",
}
FIRST_PARTY_ONLY_ACT = frozenset({"execute"})

GUARD: dict[str, str] = {
    "tighten-only": "guard rules may turn allow into ask and ask into deny, never the reverse",
}

PRICING: dict[str, str] = {
    "override": ("its price books replace list prices in the estimates nable shows; the "
                 "guard and budget checks still judge at the higher of list and book rate"),
}

AUTONOMY_LEVELS: tuple[str, ...] = ("L0", "L1", "L2", "L3", "L4")
MAX_AUTONOMY_COMMUNITY = "L2"
MAX_AUTONOMY_FIRST_PARTY = "L3"
# Opening a PR or a ticket is L2 ("Propose"); a pack that acts must say so.
MIN_AUTONOMY_TO_ACT = "L2"

CLOUD_PROVIDERS: tuple[str, ...] = ("aws", "gcp", "azure", "k8s")
# A read_cloud action must start with one of these (case-insensitive).
READ_VERBS: tuple[str, ...] = ("Get", "List", "Describe", "Lookup", "Search", "BatchGet",
                               "Query")
K8S_VERBS: tuple[str, ...] = ("get", "list", "watch")
# Reads that hand out credentials or secret material. (provider, service,
# action prefix); an empty prefix denies the whole service.
_DENIED_CLOUD: tuple[tuple[str, str, str], ...] = (
    ("aws", "sts", ""),
    ("aws", "secretsmanager", ""),
    ("aws", "kms", ""),
    ("aws", "ssm", "GetParameter"),
    ("aws", "ecr", "GetAuthorizationToken"),
    ("aws", "codeartifact", "GetAuthorizationToken"),
    ("aws", "iam", "GetAccountAuthorizationDetails"),
    ("gcp", "secretmanager", ""),
    ("gcp", "iamcredentials", ""),
    ("azure", "keyvault", ""),
    ("k8s", "secrets", ""),
)

RESERVED_SECRET_PREFIXES: tuple[str, ...] = ("FINOPS_", "NABLE_")
# Secret names that are cloud credentials, or point a cloud SDK or CLI at
# them. A pack's secrets come only from its own vault entries (never from the
# environment or nable's provider keys), but a pack asking for one of these
# is asking the person approving it to paste in their cloud keys, and code
# that runs out of process with a cloud credential is exactly what read_cloud
# (not brokered yet) exists to replace. So only a first-party pack may
# declare them. The families:
#   AWS_*         every AWS SDK setting (keys, session token, profile, the
#                 container and web-identity credential URLs and files)
#   GOOGLE_*, CLOUDSDK_*   Google client libraries and gcloud (
#                 GOOGLE_APPLICATION_CREDENTIALS, access-token files)
#   AZURE_*, ARM_*         Azure SDK EnvironmentCredential and Terraform's
#                 azurerm provider (client secrets, certificates, passwords)
#   KUBECONFIG    a kubeconfig holds cluster credentials
#   *_SECRET_ACCESS_KEY, *_SESSION_TOKEN   the same credentials under
#                 another vendor's prefix (S3-compatible stores, for example)
CLOUD_CREDENTIAL_PREFIXES: tuple[str, ...] = ("AWS_", "GOOGLE_", "CLOUDSDK_", "AZURE_", "ARM_")
CLOUD_CREDENTIAL_NAMES: frozenset[str] = frozenset({"KUBECONFIG"})
CLOUD_CREDENTIAL_SUFFIXES: tuple[str, ...] = ("_SECRET_ACCESS_KEY", "_SESSION_TOKEN")
# Cloud instance metadata endpoints: reaching one hands a process the
# machine's own cloud credentials, whatever the pack declared.
_METADATA_HOSTS = frozenset({
    "169.254.169.254", "169.254.170.2", "100.100.100.200", "metadata.google.internal",
    "metadata", "instance-data", "instance-data.ec2.internal",
})

LIST_KEYS: tuple[str, ...] = ("read_data", "read_cloud", "secrets", "settings", "network",
                              "write_org", "act", "pricing")
SCALAR_KEYS: tuple[str, ...] = ("guard", "max_autonomy")
KEYS: tuple[str, ...] = LIST_KEYS + SCALAR_KEYS

_SERVICE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")
_ACTION = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,127}\*?$")
_SECRET = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
# A setting's value is not redacted, so a name that reads as a credential is
# refused as a setting: declare it under secrets. Plain words, on purpose.
_CREDENTIAL_WORDS = re.compile(r"(?:^|_)(?:TOKEN|KEY|PAT|PASS)(?:$|_)|SECRET|PASSWORD|PASSWD|"
                               r"CREDENTIAL|PRIVATE|APIKEY")
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _level(value: str) -> int:
    return AUTONOMY_LEVELS.index(value)


def check_read_cloud(value: str) -> str | None:
    """Why a read_cloud pattern is refused, or None when it is fine."""
    parts = value.split(":")
    if len(parts) != 3:
        return "must be provider:service:Action, e.g. aws:ce:GetCostAndUsage"
    provider, service, action = parts
    if provider not in CLOUD_PROVIDERS:
        return f"provider {provider!r} is not one of {', '.join(CLOUD_PROVIDERS)}"
    if not _SERVICE.match(service):
        return f"service {service!r} must be lowercase letters, digits, dots or hyphens"
    if provider == "k8s":
        if action not in K8S_VERBS:
            return (f"for k8s the last part is a read verb ({', '.join(K8S_VERBS)}), "
                    f"not {action!r}")
    else:
        if not _ACTION.match(action):
            return (f"action {action!r} must be an API action name, optionally ending in "
                    "one * (e.g. Describe*)")
        stem = action.rstrip("*").lower()
        if not any(stem.startswith(v.lower()) for v in READ_VERBS):
            return (f"action {action!r} is not a read: it must start with one of "
                    f"{', '.join(READ_VERBS)}")
    stem, wild = action.rstrip("*").lower(), action.endswith("*")
    for p, s, prefix in _DENIED_CLOUD:
        if p == provider and s == service:
            pre = prefix.lower()
            if not pre or stem.startswith(pre) or (wild and pre.startswith(stem)):
                return (f"{provider}:{service} reads credentials or secret material, "
                        "which no pack may do")
    return None


def check_network(value: str) -> str | None:
    """Why a network entry is refused, or None. host[:port], no scheme, no path,
    no wildcards, never a cloud metadata endpoint."""
    if any(c in value for c in "/@*?[] \t"):
        return "must be host or host:port with no scheme, path, user or wildcard"
    host, sep, port = value.rpartition(":") if ":" in value else (value, "", "")
    if sep and (not port.isdigit() or not 1 <= int(port) <= 65535):
        return f"port {port!r} must be a number from 1 to 65535"
    host = host.lower()
    if host in _METADATA_HOSTS or host.startswith("169.254."):
        return f"{host} is a cloud metadata endpoint, which hands out machine credentials"
    try:
        ipaddress.IPv4Address(host)
        return None
    except ValueError:
        pass
    labels = host.split(".")
    if len(host) > 253 or not all(_LABEL.match(label) for label in labels):
        return f"{host!r} is not a hostname or an IPv4 address"
    return None


def is_cloud_credential(value: str) -> bool:
    return (value.startswith(CLOUD_CREDENTIAL_PREFIXES) or value in CLOUD_CREDENTIAL_NAMES
            or value.endswith(CLOUD_CREDENTIAL_SUFFIXES))


def check_secret(value: str, *, first_party: bool = False) -> str | None:
    if not isinstance(value, str) or not _SECRET.match(value):
        return "must be an environment-variable name such as EXAMPLE_API_TOKEN"
    if value.startswith(RESERVED_SECRET_PREFIXES):
        return f"{value} is one of nable's own settings, which no pack may read"
    if is_cloud_credential(value) and not first_party:
        return (f"{value} is a cloud credential (or points a cloud SDK at one), which only a "
                "first-party pack may declare; a connector that needs cloud data waits for "
                "read_cloud")
    return None


def check_setting(value: str, *, first_party: bool = False) -> str | None:
    """Why a settings name is refused, or None. The same names as a secret,
    minus any that reads as a credential and every cloud credential name."""
    why = check_secret(value, first_party=first_party)
    if why:
        return why
    if is_cloud_credential(value):
        return f"{value} is a cloud credential (or points a cloud SDK at one), never a setting"
    if _CREDENTIAL_WORDS.search(value):
        return (f"{value} reads as a credential, and a setting's value is not redacted: "
                "declare it under secrets")
    return None


def validate(raw: Any, *, first_party: bool) -> tuple[dict[str, Any], list[Problem]]:
    """Normalize `[capabilities]` and list what is wrong with it.

    Returns ({key: tuple of values or a string}, problems). Lists are sorted
    and de-duplicated so two manifests that ask for the same things compare
    equal, whatever order they were written in."""
    problems: list[Problem] = []
    out: dict[str, Any] = {}
    if raw is None:
        return out, problems
    if not isinstance(raw, dict):
        return out, [Problem("capabilities", "must be a table")]
    for key in raw:
        if key not in KEYS:
            problems.append(Problem(f"capabilities.{key}",
                                    f"is not a capability; known: {', '.join(KEYS)}"))
    for key in LIST_KEYS:
        if key not in raw:
            continue
        vals = raw[key]
        if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
            problems.append(Problem(f"capabilities.{key}", "must be a list of strings"))
            continue
        for v in vals:
            why = _check_value(key, v, first_party=first_party)
            if why:
                problems.append(Problem(f"capabilities.{key}", f"{v!r}: {why}"))
        out[key] = tuple(sorted(set(vals)))
    if "guard" in raw:
        g = raw["guard"]
        if g not in GUARD:
            problems.append(Problem("capabilities.guard",
                                    f"{g!r}: the only value is \"tighten-only\"; a pack "
                                    "can never loosen or turn off the guard"))
        else:
            out["guard"] = g
    if "max_autonomy" in raw:
        a = raw["max_autonomy"]
        ceiling = MAX_AUTONOMY_FIRST_PARTY if first_party else MAX_AUTONOMY_COMMUNITY
        if a not in AUTONOMY_LEVELS:
            problems.append(Problem("capabilities.max_autonomy",
                                    f"{a!r} is not one of {', '.join(AUTONOMY_LEVELS)}"))
        elif _level(a) > _level(ceiling):
            who = "a first-party pack" if first_party else "a pack that is not first-party"
            problems.append(Problem("capabilities.max_autonomy",
                                    f"{a} is above {ceiling}, the ceiling for {who}"))
        else:
            out["max_autonomy"] = a
    both = set(out.get("secrets", ())) & set(out.get("settings", ()))
    if both:
        problems.append(Problem("capabilities.settings",
                                f"{', '.join(sorted(both))} is declared both as a secret and as "
                                "a setting; a credential is declared under secrets only"))
    if out.get("act") and _level(out.get("max_autonomy", "L0")) < _level(MIN_AUTONOMY_TO_ACT):
        problems.append(Problem("capabilities.max_autonomy",
                                f"act lists {', '.join(out['act'])}, which is proposing "
                                f"(L2), so max_autonomy must be at least {MIN_AUTONOMY_TO_ACT}"))
    return out, problems


def _check_value(key: str, v: str, *, first_party: bool) -> str | None:
    if key == "read_data":
        if v not in READ_DATA_SCOPES:
            return f"not a data scope; known: {', '.join(READ_DATA_SCOPES)}"
        return None
    if key == "read_cloud":
        return check_read_cloud(v)
    if key == "secrets":
        return check_secret(v, first_party=first_party)
    if key == "settings":
        return check_setting(v, first_party=first_party)
    if key == "network":
        return check_network(v)
    if key == "write_org":
        return None if v in WRITE_ORG else "the only value is \"proposals\""
    if key == "pricing":
        return None if v in PRICING else 'the only value is "override"'
    if key == "act":
        if v not in ACT:
            return f"not an action kind; known: {', '.join(ACT)}"
        if v in FIRST_PARTY_ONLY_ACT and not first_party:
            return "is reserved for first-party packs"
        return None
    return "unknown"


def describe(key: str, value: str) -> str:
    """One line a person approving the pack can read."""
    table = {"read_data": READ_DATA_SCOPES, "write_org": WRITE_ORG, "act": ACT,
             "guard": GUARD, "pricing": PRICING}.get(key, {})
    if value in table:
        return f"{value}: {table[value]}"
    if key == "network":
        return f"{value}: may connect to this host"
    if key == "secrets":
        return (f"{value}: receives this credential from its own vault entry (`nable pack "
                f"secret set <pack> {value}`), never from your environment or nable's own keys; "
                "its value is redacted from everything the pack returns")
    if key == "settings":
        return (f"{value}: receives this setting, not a credential, from its own vault entry "
                f"(`nable pack setting set <pack> {value} VALUE`); it may appear in what the "
                "pack proposes")
    if key == "read_cloud":
        return f"{value}: may call this read-only cloud API"
    if key == "max_autonomy":
        return f"{value}: the most autonomy any action it proposes can have"
    return value


def as_sets(caps: dict[str, Any]) -> dict[str, set[str]]:
    return {k: set(caps.get(k, ())) for k in LIST_KEYS}


def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """{"added": {key: [values]}, "removed": {key: [values]}}.

    Raising max_autonomy counts as added (the old level as removed), and so
    does gaining guard. An update with anything under "added" waits for a
    person to approve it again, whatever the auto-update setting."""
    added: dict[str, list[str]] = {}
    removed: dict[str, list[str]] = {}
    o, n = as_sets(old), as_sets(new)
    for k in LIST_KEYS:
        if n[k] - o[k]:
            added[k] = sorted(n[k] - o[k])
        if o[k] - n[k]:
            removed[k] = sorted(o[k] - n[k])
    oa, na = old.get("max_autonomy", "L0"), new.get("max_autonomy", "L0")
    if _level(na) > _level(oa):
        added["max_autonomy"] = [na]
        removed["max_autonomy"] = [oa]
    elif _level(na) < _level(oa):
        removed["max_autonomy"] = [oa]
    if new.get("guard") and not old.get("guard"):
        added["guard"] = [new["guard"]]
    elif old.get("guard") and not new.get("guard"):
        removed["guard"] = [old["guard"]]
    return {"added": added, "removed": removed}


def exceeds(caps: dict[str, Any], ceiling: dict[str, Any]) -> list[Problem]:
    """What `caps` asks for beyond an org's `packs.allowed_capabilities`.

    The ceiling is strict: a key the ceiling does not list allows nothing.
    read_cloud and network entries in the ceiling may be glob patterns
    ("aws:ce:*", "*.corp.internal:443"); a pack value is covered when it
    matches one. A ceiling with no `settings` reads its `secrets` for them: a
    name an org allows as a credential it allows as a setting. max_autonomy
    is a level; guard is a value."""
    problems: list[Problem] = []
    for k in LIST_KEYS:
        src = "secrets" if k == "settings" and "settings" not in ceiling else k
        allowed = [str(x) for x in (ceiling.get(src) or [])]
        for v in caps.get(k, ()):
            if not any(fnmatch.fnmatchcase(v, pat) for pat in allowed):
                problems.append(Problem(f"capabilities.{k}",
                                        f"{v!r} is outside packs.allowed_capabilities.{k}"))
    if "max_autonomy" in caps:
        cap = ceiling.get("max_autonomy")
        if cap not in AUTONOMY_LEVELS or _level(caps["max_autonomy"]) > _level(cap):
            problems.append(Problem("capabilities.max_autonomy",
                                    f"{caps['max_autonomy']} is above "
                                    f"packs.allowed_capabilities.max_autonomy ({cap or 'unset'})"))
    if "guard" in caps and ceiling.get("guard") != caps["guard"]:
        problems.append(Problem("capabilities.guard",
                                f"{caps['guard']!r} is outside packs.allowed_capabilities.guard"))
    return problems
