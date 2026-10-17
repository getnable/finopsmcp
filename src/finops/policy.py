"""Policy-bounded action gate: the advisory half (B1) of the cost guardrail.

An agent describes a remediation action it is considering; this module checks it
against a human-authored policy and returns allow / block / escalate. ADVICE ONLY:
nable never executes the action, a human applies it. This is the seed of the
request-path guardrail. The auto-execute half (B2) is a separate, explicit decision
and is intentionally NOT implemented here, propose-only stays fully intact.
"""
from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any

GATE_ALLOW = "allow"        # reversible, allowlisted, in budget: a human can apply it
GATE_BLOCK = "block"        # not in the human's allowlist: do not propose applying it
GATE_ESCALATE = "escalate"  # one-way door or over budget: a human must review first

# Remediation action types nable can propose, classified by Bezos door. Two-way =
# reversible (a PR you can revert, an instance you can restart). One-way =
# irreversible or a financial commitment.
TWO_WAY_DOORS = {
    "rightsizing", "tag_fix", "gp2_to_gp3", "graviton_migration",
    "spot_migration", "stop_idle", "schedule_nonprod", "ticket",
    # Reversible infrastructure mutations. These MUST be known here: the shell
    # guard classifies `terraform apply` as infra_apply and allows it by default,
    # so the MCP gate has to agree or the two halves contradict each other (the
    # gate used to BLOCK terraform_apply as "unknown" while the guard waved the
    # same command through). Budget/threshold escalation still applies.
    "infra_apply", "terraform_apply", "helm_upgrade", "kubectl_apply",
}
ONE_WAY_DOORS = {
    "idle_cleanup", "delete_resource", "terminate_instance",
    "release_ip", "purchase_commitment", "snapshot_delete",
}

# How a one-way action reads in a sentence, for the escalation reason.
_ONE_WAY_WHAT = {
    "idle_cleanup": "Cleaning up an idle resource",
    "delete_resource": "Deleting a resource",
    "terminate_instance": "Terminating an instance",
    "release_ip": "Releasing an IP address",
    "purchase_commitment": "Buying a commitment",
    "snapshot_delete": "Deleting a snapshot",
}

DEFAULT_POLICY: dict[str, Any] = {
    "allowed_action_types": sorted(TWO_WAY_DOORS),  # reversible actions that are in-policy
    "max_auto_monthly_usd": 500.0,                  # a cost increase above this escalates
    "escalate_one_way_doors": True,                 # irreversible / financial always need a human
    # Velocity cap: the monthly run-rate the shell guard lets through without a
    # prompt, summed over a rolling window. Each action can sit under the
    # per-action threshold while ten of them in an hour do not; this is the
    # line for the ten. None means four times max_auto_monthly_usd ($2,000 at
    # the default), so it takes at least five priced launches the guard let
    # through silently to reach it, and raising the per-action threshold moves
    # it too. 0 turns it off.
    "velocity_cap_monthly_usd": None,
    "velocity_window_minutes": 60.0,
    # Loop detection: the same creation (same verb and key arguments) let
    # through this many times within the window looks like an agent retrying
    # rather than deciding, so the next one asks. Below 2 turns it off.
    "loop_repeat_count": 3,
    "loop_window_minutes": 10.0,
    # What a change that would take a cloud budget over its limit gets: "ask"
    # (escalate, a human confirms) or "deny" (block, a hard stop). Set it in
    # nable.policy.yaml as `on_budget_breach: deny`, or with
    # FINOPS_POLICY_ON_BUDGET_BREACH. The guard's FINOPS_GUARD_STOP_ON_BUDGET
    # overrides it for one session or CI run, both ways.
    "on_budget_breach": "ask",
}

BUDGET_BREACH_ACTIONS = ("ask", "deny")
POLICY_FILE_NAME = "nable.policy.yaml"


VELOCITY_CAP_MULTIPLE = 4.0


def velocity_cap(pol: dict[str, Any]) -> float:
    """The effective velocity cap in $/mo per window; 0 when it is off."""
    cap = pol.get("velocity_cap_monthly_usd")
    if cap is None:
        cap = VELOCITY_CAP_MULTIPLE * float(pol.get("max_auto_monthly_usd", 500.0))
    return max(float(cap), 0.0)


def door_of(action_type: str) -> str:
    if action_type in ONE_WAY_DOORS:
        return "one_way"
    if action_type in TWO_WAY_DOORS:
        return "two_way"
    return "unknown"


def is_one_way(action_type: str) -> bool:
    return action_type in ONE_WAY_DOORS


def policy_file_path() -> Path:
    """FINOPS_POLICY_FILE, else nable.policy.yaml in nable's data directory.

    Never the working directory: agents and MCP clients run inside whatever
    project is open, so a repo could ship a policy that loosens itself."""
    raw = os.getenv("FINOPS_POLICY_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    from .guard_ledger import _data_dir  # storage.db's rule, without SQLAlchemy
    return _data_dir() / POLICY_FILE_NAME


# (path, mtime_ns, size) -> the keys load_policy takes from the file. The guard
# asks for the policy several times per verdict; the file is parsed once.
_FILE_CACHE: dict[str, Any] = {}


# The policy file's numeric settings: (key, whether 0 is allowed, whole
# number). The same names as DEFAULT_POLICY and the same rules as their
# FINOPS_POLICY_* env overrides (_NUMERIC_ENV), which win over the file.
_FILE_NUMBERS = (("max_auto_monthly_usd", True, False),
                 ("velocity_cap_monthly_usd", True, False),
                 ("velocity_window_minutes", False, False),
                 ("loop_repeat_count", True, True),
                 ("loop_window_minutes", False, False))
# Every top-level key the file is read for, here and elsewhere: `packs` below
# (pack_policy) and `remediation` (remediation/gate.py).
FILE_KEYS = ("on_budget_breach", *(k for k, _z, _w in _FILE_NUMBERS), "packs", "remediation")


def _file_number(val: Any, zero_ok: bool, whole: bool) -> float | int | None:
    """A usable number from the file, or None: finite, 0 or more (above 0
    for a window), and whole where it counts something. True and False are
    not numbers here, though Python would take them for 1 and 0."""
    if isinstance(val, bool):
        return None
    if isinstance(val, str):
        try:
            val = float(val.strip())
        except ValueError:
            return None
    if not isinstance(val, (int, float)) or not math.isfinite(val):
        return None
    if not (val >= 0 if zero_ok else val > 0):
        return None
    if whole:
        return int(val) if float(val).is_integer() else None
    return float(val)


def _default_text(key: str) -> str:
    if key == "velocity_cap_monthly_usd":
        return f"{VELOCITY_CAP_MULTIPLE:g} x max_auto_monthly_usd"
    return repr(DEFAULT_POLICY[key])


def _read_policy_file() -> tuple[dict[str, Any], list[str]]:
    """(the keys the policy file sets, validated; what is wrong with it).
    on_budget_breach and the numeric settings in _FILE_NUMBERS. No keys when
    there is no file, it cannot be parsed, or a value is not one this policy
    knows: a broken file never loosens the gate and never breaks it. The
    problems say so, for `nable guard check`, `nable doctor` and guard doctor."""
    try:
        path = policy_file_path()
        st = path.stat()
    except (OSError, ValueError):
        return {}, []
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _FILE_CACHE.get("key") == key:
        return dict(_FILE_CACHE["keys"]), list(_FILE_CACHE["problems"])
    keys: dict[str, Any] = {}
    problems: list[str] = []
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except Exception as e:  # noqa: BLE001 - an unreadable file is no file
        doc = None
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        problems.append(f"{path} could not be read ({type(e).__name__}: "
                        f"{str(e).splitlines()[0] if str(e) else 'no detail'}{where}), so "
                        "the defaults apply")
    if isinstance(doc, dict):
        val = doc.get("on_budget_breach")
        if isinstance(val, str) and val.strip().lower() in BUDGET_BREACH_ACTIONS:
            keys["on_budget_breach"] = val.strip().lower()
        elif val is not None:
            problems.append(f"{path} sets on_budget_breach: {val!r}, which is not "
                            f"{' or '.join(BUDGET_BREACH_ACTIONS)}, so the default "
                            f"({DEFAULT_POLICY['on_budget_breach']}) applies")
        for name, zero_ok, whole in _FILE_NUMBERS:
            if doc.get(name) is None:
                continue
            x = _file_number(doc[name], zero_ok, whole)
            if x is not None:
                keys[name] = x
                continue
            need = ("a whole number of 0 or more" if whole else
                    "a finite number of 0 or more" if zero_ok else "a finite number above 0")
            problems.append(f"{path} sets {name}: {doc[name]!r}, which is not {need}, "
                            f"so the default ({_default_text(name)}) applies")
        for name in doc:
            if name not in FILE_KEYS:
                problems.append(f"{path} sets {name!r}, which is not a setting this file "
                                f"takes ({', '.join(FILE_KEYS)}), so it changes nothing")
    elif doc is not None:
        problems.append(f"{path} is a YAML {type(doc).__name__}, not a mapping of "
                        "settings, so the defaults apply")
    packs = _parse_packs_section(doc, path, readable=not problems or isinstance(doc, dict))
    problems.extend(packs["problems"])
    _FILE_CACHE.update(key=key, keys=keys, problems=problems, packs=packs)
    return dict(keys), list(problems)


# ── packs: the org's rules for extension packs ───────────────────────────────
# nable.policy.yaml may carry a `packs:` section (finops.packs reads it through
# pack_policy()). Nothing else in this module reads it, so the keys above behave
# exactly as before. It is read from the same place, never the working
# directory: a repo that could ship its own pack policy could allow itself.
#
#   packs:
#     allowed_sources: ["git+https://github.com/getnable/*"]   # globs over sources
#     blocked_sources: ["io.github.someone/*"]                  # sources or pack ids
#     require_signed: true
#     allowed_capabilities:                                     # a ceiling
#       read_data: [focus.cost, org.owners]
#       network: []
#       max_autonomy: L1
#     registry: https://packs.example.com/index.json
#     trusted_keys:                         # org signing keys (`nable pack keygen`)
#       - name: acme-platform
#         key: <base64 Ed25519 public key>
#     allow_unsigned_code: [io.github.acme/internal-connector@<content digest>]
#
# Unlike the keys above, a packs section that cannot be used does not fall back
# to "no restrictions": it fails closed. `invalid` is set and every install is
# refused until it is fixed, because the admin who wrote a broken allowlist
# meant to restrict something.

PACK_POLICY_KEYS = ("allowed_sources", "blocked_sources", "require_signed",
                    "allowed_capabilities", "registry", "trusted_keys",
                    "allow_unsigned_code")


def _pack_policy_default() -> dict[str, Any]:
    return {"allowed_sources": None, "blocked_sources": [], "require_signed": False,
            "allowed_capabilities": None, "registry": None, "trusted_keys": [],
            "allow_unsigned_code": [], "invalid": False, "problems": [], "path": None}


def _ed25519_public_key(text: Any) -> bytes | None:
    """The 32 raw bytes of a base64 (standard or URL-safe) Ed25519 public
    key, or None. Stdlib only: the signature itself is checked in
    finops.packs.signing, which loads cryptography when it needs it."""
    import base64
    import binascii
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    try:
        data = base64.b64decode(raw.replace("-", "+").replace("_", "/")
                                + "=" * (-len(raw) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None
    return data if len(data) == 32 else None


def _parse_trusted_keys(val: Any, path: Path, refused: str,
                        probs: list[str]) -> list[dict[str, str]]:
    if not isinstance(val, list):
        probs.append(f"{path} sets packs.trusted_keys to {val!r}, which is not a list of "
                     f"{{name, key}} entries, {refused}")
        return []
    keys: list[dict[str, str]] = []
    for i, item in enumerate(val):
        where = f"{path} sets packs.trusted_keys[{i}]"
        if not isinstance(item, dict) or set(item) - {"name", "key"}:
            probs.append(f"{where} to {item!r}, which is not a mapping with name and key, "
                         f"{refused}")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            probs.append(f"{where}.name to {name!r}, which is not a non-empty name, {refused}")
            continue
        if _ed25519_public_key(item.get("key")) is None:
            probs.append(f"{where}.key ({name}), which is not a base64 Ed25519 public key "
                         f"(32 bytes), {refused}")
            continue
        keys.append({"name": name.strip(), "key": str(item["key"]).strip()})
    return keys


def _str_list(val: Any) -> list[str] | None:
    if isinstance(val, list) and all(isinstance(x, str) and x.strip() for x in val):
        return [x.strip() for x in val]
    return None


def _parse_packs_section(doc: Any, path: Path, *, readable: bool) -> dict[str, Any]:
    out = _pack_policy_default()
    out["path"] = str(path)
    refused = "so pack installs are refused until it is fixed"
    if not readable or (doc is not None and not isinstance(doc, dict)):
        # The file exists and nobody can read it, and what it failed to say
        # may have been a packs section.
        out["invalid"] = True
        return out
    if not isinstance(doc, dict) or doc.get("packs") is None:
        return out
    sec = doc["packs"]
    probs: list[str] = out["problems"]
    if not isinstance(sec, dict):
        probs.append(f"{path} sets packs: to a YAML {type(sec).__name__}, not a mapping, {refused}")
        out["invalid"] = True
        return out
    for key in sec:
        if key not in PACK_POLICY_KEYS:
            probs.append(f"{path} sets packs.{key}, which is not one of "
                         f"{', '.join(PACK_POLICY_KEYS)}, {refused}")
    for key in ("allowed_sources", "blocked_sources"):
        if key in sec:
            vals = [] if sec[key] == [] else _str_list(sec[key])
            if vals is None:
                probs.append(f"{path} sets packs.{key} to {sec[key]!r}, which is not a list "
                             f"of source patterns, {refused}")
            else:
                out[key] = vals
    if "require_signed" in sec:
        if isinstance(sec["require_signed"], bool):
            out["require_signed"] = sec["require_signed"]
        else:
            probs.append(f"{path} sets packs.require_signed to {sec['require_signed']!r}, "
                         f"which is not true or false, {refused}")
    if "allowed_capabilities" in sec:
        from .packs.capabilities import LIST_KEYS, SCALAR_KEYS  # light, stdlib only
        ceil = sec["allowed_capabilities"]
        if not isinstance(ceil, dict):
            probs.append(f"{path} sets packs.allowed_capabilities to {ceil!r}, which is not "
                         f"a mapping of capability to allowed values, {refused}")
        else:
            clean: dict[str, Any] = {}
            for k, v in ceil.items():
                if k in LIST_KEYS and (v == [] or _str_list(v) is not None):
                    clean[k] = _str_list(v) or []
                elif k in SCALAR_KEYS and isinstance(v, str):
                    clean[k] = v.strip()
                else:
                    probs.append(f"{path} sets packs.allowed_capabilities.{k} to {v!r}, which "
                                 f"is not a capability with a list (or, for guard and "
                                 f"max_autonomy, a value), {refused}")
            out["allowed_capabilities"] = clean
    if "registry" in sec:
        reg = sec["registry"]
        if isinstance(reg, str) and reg.strip():
            out["registry"] = reg.strip()
        else:
            probs.append(f"{path} sets packs.registry to {reg!r}, which is not a URL or a "
                         f"path, {refused}")
    if "trusted_keys" in sec and sec["trusted_keys"] is not None:
        out["trusted_keys"] = _parse_trusted_keys(sec["trusted_keys"], path, refused, probs)
    if "allow_unsigned_code" in sec:
        from .packs.registry import parse_ref  # light, stdlib only
        val = sec["allow_unsigned_code"]
        ids = [] if val == [] else _str_list(val)

        def _entry_ok(x: str) -> bool:
            # "ns/name@<content digest>" pins the exact files; a bare id is
            # honoured only with packs.allowed_sources (finops.packs.broker).
            pid, sep, digest = x.partition("@")
            return bool(parse_ref(pid)) and "@" not in pid and (
                not sep or bool(re.fullmatch(r"[0-9a-f]{64}", digest)))
        bad = [x for x in ids or [] if not _entry_ok(x)]
        if ids is None or bad:
            probs.append(f"{path} sets packs.allow_unsigned_code to {val!r}, which is not a "
                         "list of pack ids pinned to a content digest such as "
                         f"io.github.acme/connector@<64-hex digest>, {refused}")
        else:
            out["allow_unsigned_code"] = ids
    out["invalid"] = bool(probs)
    return out


def pack_policy() -> dict[str, Any]:
    """The packs: section of the policy file, validated. Keys:
    allowed_sources (list, or None for no allowlist), blocked_sources (list),
    require_signed (bool), allowed_capabilities (dict, or None for no
    ceiling), registry (str or None), trusted_keys (list of {name, key}),
    allow_unsigned_code (list of pack ids), invalid (bool: refuse every install),
    problems, path. No policy file means no restrictions. Never raises."""
    try:
        policy_file_path().stat()
    except (OSError, ValueError):
        return _pack_policy_default()
    try:
        _read_policy_file()
        packs = _FILE_CACHE.get("packs")
    except Exception:  # noqa: BLE001 - fail closed, never break the caller
        packs = None
    if not isinstance(packs, dict):
        out = _pack_policy_default()
        out["invalid"] = True
        return out
    out = dict(packs)
    out["problems"] = list(packs["problems"])
    return out


def _policy_file_keys() -> dict[str, Any]:
    return _read_policy_file()[0]


# Numeric env overrides: (env var, policy key, whether 0 is allowed). A value
# that is not a finite number of 0 or more (a window: more than 0) keeps the
# default: "nan", "inf" or a negative figure would switch a gate off.
_NUMERIC_ENV = (("FINOPS_POLICY_MAX_AUTO_USD", "max_auto_monthly_usd", True),
                ("FINOPS_POLICY_VELOCITY_CAP_USD", "velocity_cap_monthly_usd", True),
                ("FINOPS_POLICY_VELOCITY_WINDOW_MIN", "velocity_window_minutes", False),
                ("FINOPS_POLICY_LOOP_WINDOW_MIN", "loop_window_minutes", False))


def _env_number(env: str, zero_ok: bool) -> tuple[float | None, str | None]:
    """(the value, None), (None, why it was refused), or (None, None) when unset."""
    raw = os.getenv(env, "").strip()
    if not raw:
        return None, None
    try:
        x = float(raw)
    except ValueError:
        x = math.nan
    if math.isfinite(x) and (x >= 0 if zero_ok else x > 0):
        return x, None
    need = "a finite number of 0 or more" if zero_ok else "a finite number above 0"
    return None, f"{env}={raw} is not {need}, so the default applies"


def _env_count(env: str) -> tuple[int | None, str | None]:
    raw = os.getenv(env, "").strip()
    if not raw:
        return None, None
    try:
        n = int(raw)
    except ValueError:
        n = -1
    if n >= 0:
        return n, None
    return None, f"{env}={raw} is not a whole number of 0 or more, so the default applies"


def policy_problems() -> list[str]:
    """Settings load_policy() ignored, each with why: a policy file it could
    not parse or a value it does not know, and env overrides that are not a
    usable number. For `nable guard doctor`; never raises."""
    try:
        problems = _read_policy_file()[1]
        for env, _, zero_ok in _NUMERIC_ENV:
            why = _env_number(env, zero_ok)[1]
            if why:
                problems.append(why)
        why = _env_count("FINOPS_POLICY_LOOP_COUNT")[1]
        if why:
            problems.append(why)
        ob = os.getenv("FINOPS_POLICY_ON_BUDGET_BREACH", "").strip()
        if ob and ob.lower() not in BUDGET_BREACH_ACTIONS:
            problems.append(f"FINOPS_POLICY_ON_BUDGET_BREACH={ob} is not "
                            f"{' or '.join(BUDGET_BREACH_ACTIONS)}, so it is ignored")
        return problems
    except Exception:  # noqa: BLE001 - a diagnostic must not break doctor
        return []


# The settings an explicit check reports as in effect, and the env var that
# overrides each one.
_IN_EFFECT = (("max_auto_monthly_usd", "FINOPS_POLICY_MAX_AUTO_USD"),
              ("velocity_cap_monthly_usd", "FINOPS_POLICY_VELOCITY_CAP_USD"),
              ("velocity_window_minutes", "FINOPS_POLICY_VELOCITY_WINDOW_MIN"),
              ("loop_repeat_count", "FINOPS_POLICY_LOOP_COUNT"),
              ("loop_window_minutes", "FINOPS_POLICY_LOOP_WINDOW_MIN"),
              ("on_budget_breach", "FINOPS_POLICY_ON_BUDGET_BREACH"))


def _env_applies(env: str) -> bool:
    if env == "FINOPS_POLICY_LOOP_COUNT":
        return _env_count(env)[0] is not None
    if env == "FINOPS_POLICY_ON_BUDGET_BREACH":
        return os.getenv(env, "").strip().lower() in BUDGET_BREACH_ACTIONS
    zero_ok = next(z for e, _k, z in _NUMERIC_ENV if e == env)
    return _env_number(env, zero_ok)[0] is not None


def policy_report() -> dict[str, Any]:
    """What an explicit check says about the policy it judged with:
    {"path", "exists", "broken", "problems", "fix", "in_effect"}.

    in_effect maps each setting load_policy() returned to its value and where
    it came from: "default", "file" or the env var. broken is True when the
    policy file, or a FINOPS_POLICY_* override, could not be used as written;
    the defaults in_effect names are what applied instead. The guard hook
    never prints this: on every tool call it keeps its fail-safe of judging
    with the defaults. Never raises."""
    try:
        path: Path | None = policy_file_path()
        exists = path.is_file()
    except (OSError, ValueError):
        path, exists = None, False
    problems = policy_problems()
    try:
        pol = load_policy()
        from_file = _policy_file_keys()
    except Exception:  # noqa: BLE001 - a diagnostic must not break its caller
        pol, from_file = dict(DEFAULT_POLICY), {}
    in_effect: dict[str, dict[str, Any]] = {}
    for key, env in _IN_EFFECT:
        source = env if _env_applies(env) else "file" if key in from_file else "default"
        value = pol.get(key)
        if key == "velocity_cap_monthly_usd" and value is None:
            value, source = velocity_cap(pol), f"{source}: 4 x max_auto_monthly_usd"
        in_effect[key] = {"value": value, "source": source}
    fix = None
    if problems:
        where = str(path) if path else POLICY_FILE_NAME
        fix = (f"correct {where} (YAML `key: value` lines, taking {', '.join(FILE_KEYS)}) "
               "or the FINOPS_POLICY_* variable named, or move the file away to use the "
               "defaults on purpose; `nable guard doctor` checks it again")
    return {"path": str(path) if path else None, "exists": exists, "broken": bool(problems),
            "problems": problems, "fix": fix, "in_effect": in_effect}


def in_effect_text(report: dict[str, Any]) -> str:
    """policy_report()'s in_effect as one line of `key value (source)`."""
    return ", ".join(f"{k} {v['value']:g} ({v['source']})" if isinstance(v["value"], float)
                     else f"{k} {v['value']} ({v['source']})"
                     for k, v in report["in_effect"].items())


def load_policy() -> dict[str, Any]:
    """The default policy with optional env overrides, so a human can author the
    policy without a config system:
      FINOPS_POLICY_MAX_AUTO_USD       a dollar threshold (float)
      FINOPS_POLICY_ALLOWED_ACTIONS    comma-separated action types
      FINOPS_POLICY_VELOCITY_CAP_USD   monthly run-rate allowed per window (float, 0 = off)
      FINOPS_POLICY_VELOCITY_WINDOW_MIN  the window, in minutes (float, default 60)
      FINOPS_POLICY_LOOP_COUNT         identical creations that make a loop (int, 0 = off)
      FINOPS_POLICY_LOOP_WINDOW_MIN    ...within this many minutes (float, default 10)
      FINOPS_POLICY_ON_BUDGET_BREACH   ask | deny, for a change over a cloud budget

    The same settings, by their policy key, may also be set in the policy
    file (policy_file_path(), nable.policy.yaml in nable's data directory):

        on_budget_breach: deny        # a change over budget is blocked, not asked
        max_auto_monthly_usd: 200     # and velocity_cap_monthly_usd,
                                      # velocity_window_minutes, loop_repeat_count,
                                      # loop_window_minutes

    The env var wins over the file. A value that cannot be used (not a
    finite number of 0 or more, a window of 0, an unknown on_budget_breach, a
    file that does not parse) keeps the default; policy_problems() lists it.
    """
    pol: dict[str, Any] = dict(DEFAULT_POLICY)
    pol["allowed_action_types"] = list(DEFAULT_POLICY["allowed_action_types"])
    pol.update(_policy_file_keys())

    ob = os.getenv("FINOPS_POLICY_ON_BUDGET_BREACH", "").strip().lower()
    if ob in BUDGET_BREACH_ACTIONS:
        pol["on_budget_breach"] = ob

    for env, key, zero_ok in _NUMERIC_ENV:
        x, _ = _env_number(env, zero_ok)
        if x is not None:
            pol[key] = x

    lc, _ = _env_count("FINOPS_POLICY_LOOP_COUNT")
    if lc is not None:
        pol["loop_repeat_count"] = lc

    al = os.getenv("FINOPS_POLICY_ALLOWED_ACTIONS", "").strip()
    if al:
        pol["allowed_action_types"] = [a.strip() for a in al.split(",") if a.strip()]
    return pol


def _apply_learning(out: dict[str, Any], action_type: str, signal: dict[str, Any] | None) -> dict[str, Any]:
    """Fold this customer's decision history into the gate. Safety invariant: learning
    is a one-way ratchet toward caution. It may tighten an ALLOW to ESCALATE when the
    customer habitually declines this kind of action, or annotate an ALLOW they
    habitually approve. It NEVER loosens a BLOCK or ESCALATE into ALLOW, so learning
    can never become an excuse to act more aggressively than the static policy allows.

    `signal` is a per-source signal (learning.signal.signal_for); its `verdict` is
    "suppress" / "boost" only once there is enough history (WARM), otherwise "neutral",
    so a sparse ledger is a silent no-op.
    """
    if not isinstance(signal, dict):
        return out
    verdict = signal.get("verdict")
    if verdict not in ("suppress", "boost"):
        return out

    out["learned"] = {
        "verdict": verdict,
        "act_rate": signal.get("act_rate"),
        "accuracy": signal.get("accuracy"),
        "coverage": signal.get("coverage"),
        "why": signal.get("why"),
    }
    if out["gate"] == GATE_ALLOW and verdict == "suppress":
        out["gate"] = GATE_ESCALATE
        out["reason"] = (
            f"Policy would allow this, but your history shows you usually decline "
            f"'{action_type}' actions like this, so a human should confirm it's wanted."
        )
        out["learned"]["adjustment"] = "allow_to_escalate"
    elif out["gate"] == GATE_ALLOW and verdict == "boost":
        out["reason"] += " Your history shows you usually approve these."
        out["learned"]["adjustment"] = "confidence_added"
    return out


def evaluate_action_gate(
    action_type: str,
    monthly_delta_usd: float = 0.0,
    cost_verdict: str | None = None,
    *,
    policy: dict[str, Any] | None = None,
    signal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Advisory gate for a proposed remediation action.

    action_type: e.g. "rightsizing" (reversible) or "idle_cleanup" (one-way).
    monthly_delta_usd: the action's cost impact (negative = a saving).
    cost_verdict: the preflight verdict ("ok"/"warn"/"over_budget"/"no_budget"), if known.
    signal: optional per-source learning signal; folded in caution-only (see _apply_learning).

    Returns {gate, reason, rule, action_type, door, monthly_delta_usd, [learned]}.
    `rule` names what decided: "one_way", "allowlist", "over_budget", "threshold"
    or "allowed". nable never executes; this advises a human. Pure, never raises
    on normal input.
    """
    pol = policy or load_policy()
    delta = float(monthly_delta_usd or 0.0)
    door = door_of(action_type)
    out: dict[str, Any] = {
        "action_type": action_type,
        "door": door,
        "monthly_delta_usd": round(delta, 2),
    }

    # ---- Static policy (the floor of caution) ----
    if cost_verdict == "over_budget" and pol.get("on_budget_breach") == "deny":
        # The human chose a hard stop for over-budget changes. It comes first:
        # a stop is stricter than the confirmation a one-way door gets.
        out["gate"] = GATE_BLOCK
        out["rule"] = "over_budget"
        out["reason"] = ("This change would push you over budget, and your policy sets "
                         "on_budget_breach: deny, so it is blocked.")
    elif door == "one_way" and pol.get("escalate_one_way_doors", True):
        # One-way doors always escalate (irreversible or a financial commitment).
        # The reason is for a human: what the action does, in words, not the
        # policy's own vocabulary (action_type and door carry that).
        out["gate"] = GATE_ESCALATE
        out["rule"] = "one_way"
        what = _ONE_WAY_WHAT.get(action_type, "This action")
        out["reason"] = (f"{what} cannot be "
                         f"{'cancelled' if action_type == 'purchase_commitment' else 'undone'},"
                         " so a human must confirm it before it is applied.")
    elif action_type not in set(pol.get("allowed_action_types", [])):
        # Not in the human's allowlist -> block.
        out["gate"] = GATE_BLOCK
        out["rule"] = "allowlist"
        out["reason"] = (f"'{action_type}' is not in your allowlist of permitted actions; "
                         "nable will not propose applying it.")
    elif cost_verdict == "over_budget":
        # Over budget (per the cost preflight) -> escalate.
        out["gate"] = GATE_ESCALATE
        out["rule"] = "over_budget"
        out["reason"] = ("This change would push you over budget; a human should review it "
                         "before it is applied.")
    elif delta > float(pol.get("max_auto_monthly_usd", 500.0)):
        # Cost increase above the auto threshold -> escalate (savings are always fine).
        cap = float(pol.get("max_auto_monthly_usd", 500.0))
        out["gate"] = GATE_ESCALATE
        out["rule"] = "threshold"
        out["reason"] = (f"The +${delta:,.0f}/mo impact is over your ${cap:,.0f} auto threshold; "
                         "a human should review it.")
    else:
        # Reversible, allowlisted, within budget and threshold.
        out["gate"] = GATE_ALLOW
        out["rule"] = "allowed"
        out["reason"] = (f"'{action_type}' is reversible, in your allowlist, and within budget; "
                         "a human can apply it within your policy.")

    # ---- Learning layer (caution-only ratchet) ----
    return _apply_learning(out, action_type, signal)
