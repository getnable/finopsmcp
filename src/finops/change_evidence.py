# SPDX-License-Identifier: Apache-2.0
"""Change-management evidence from the guard's decision ledger.

The guard records every verdict it gives on an infrastructure change in a
hash-chained ledger (guard_ledger.py). This module reads that ledger for a
period and answers the questions a change-management control asks (SOC 2
CC8.1 is the usual one): which changes were asked about, who approved each,
when, and under which policy; which were denied; which ran during a change
freeze; and whether the record itself is intact.

It is evidence, not a certification. It shows what nable's guard saw and
decided on the machines where it runs; it cannot show changes made where the
guard was not installed, and an auditor decides what it demonstrates.

What each change record says about approval, honestly:

  approved                an ask the post hook saw run: a person answered the
                          agent harness's prompt. The harness does not say
                          who, so approved_by is empty and approved_via says so
  allowed_out_of_band     a person approved the exact call from their own
                          terminal (`nable guard approve`); approved_by names
                          them as nable recorded it
  declined                an ask that never ran, read as declined only where
                          the harness's post hook is known to work
  approved_later_out_of_band
                          an ask in a harness that cannot ask (Codex, Gemini
                          CLI, Cline), which a person then approved once from
                          their own terminal; answered_by_line is the record
                          of the call that approval let through
  not_run                 an ask in a harness that cannot ask, which nobody
                          approved: the harness turned it into a deny
  unknown                 an ask whose answer the ledger cannot tell
  denied                  the guard stopped it; it did not run
  not_examined            a guard error let the call through unexamined

Nothing here writes anything: the ledger is read, the org model is read, and
the installed packs' policies (applies_to: action) are evaluated against each
change record to list exceptions for a person to review.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .guard_approvals import EXPIRY_MINUTES

NOTE = ("This is change-management evidence drawn from nable's guard ledger. It is not a "
        "SOC 2 report, an attestation or a certification. It covers only what nable's guard "
        "saw where it is installed, and an auditor decides what it demonstrates.")

_CHANGE_DECISIONS = ("ask", "deny", "fail_open")
# How long after an ask a one-time approval can answer it: a pending approval
# lives guard_approvals.EXPIRY_MINUTES from the first ask that made it (a
# minute more for timestamps cut to the second). Its id is short, so the same
# id on a record outside that window is another approval.
_GRANT_WINDOW = timedelta(minutes=EXPIRY_MINUTES + 1)


def _ts(rec: dict[str, Any]) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(rec.get("ts")))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else None


def _approvers(model: Any, rec: dict[str, Any]) -> list[str]:
    """Who the org model's confirmed approval chains say reviews this class
    of change in the scope the guard judged it in. Read now, not then."""
    if model is None:
        return []
    scope = rec.get("scope") if isinstance(rec.get("scope"), dict) else {}
    team = scope.get("team")
    envs = [str(e) for e in scope.get("envs") or []]
    action = str(rec.get("action_type") or "")
    if not action or (not team and not envs):
        return []
    try:
        facts = model.approvals_for(action, team=team, envs=envs, strict=True)
    except Exception:  # noqa: BLE001 - a citation, never the evidence itself
        return []
    out: list[str] = []
    for f in facts:
        least = f.value.get("min", 1)
        out.append(f"{f.subject}: {least} of {', '.join(f.value.get('approvers') or [])}"
                   + (" (change ticket required)" if f.value.get("change_ticket") else ""))
    return out


def _grant_for(rec: dict[str, Any], granted: dict[str, list[tuple[datetime, dict[str, Any]]]]
               ) -> dict[str, Any] | None:
    """The one-time approval that answered this ask: one with its id, on a
    call recorded from the ask until the id expired; None when there is none."""
    aid, ts = rec.get("approval_id"), _ts(rec)
    if not aid or ts is None:
        return None
    for at, entry in granted.get(str(aid), []):
        if ts <= at <= ts + _GRANT_WINDOW:
            return entry
    return None


def _record(rec: dict[str, Any], answer: str | None, ran_at: dict[str, str],
            model: Any, granted: dict[str, list[tuple[datetime, dict[str, Any]]]]
            ) -> dict[str, Any]:
    chain = rec.get("chain") or {}
    decision = rec.get("decision")
    oob = rec.get("approved_out_of_band") if isinstance(rec.get("approved_out_of_band"),
                                                          dict) else None
    later = _grant_for(rec, granted)
    if decision == "fail_open":
        outcome = "not_examined"
    elif decision == "deny":
        outcome = "denied"
    elif oob:
        outcome = "allowed_out_of_band"
    elif later is not None:
        outcome = "approved_later_out_of_band"
    elif rec.get("approval_id"):
        outcome = "not_run"
    else:
        outcome = answer or "unknown"
    approved_by = approved_at = approved_via = None
    if oob or later is not None:
        grant = oob or (later or {}).get("grant") or {}
        approved_by, approved_at = grant.get("by"), grant.get("at")
        approved_via = f"`nable guard approve {grant.get('id')}` in the person's own terminal"
    elif outcome == "approved":
        approved_at = ran_at.get(chain.get("hash") or "")
        approved_via = (f"the {rec.get('harness') or 'agent'} permission prompt (the harness "
                        "does not record who answered)")
    freeze = rec.get("freeze") if isinstance(rec.get("freeze"), dict) else None
    approvers = _approvers(model, rec)
    return {
        "id": (chain.get("hash") or "")[:12],
        "ledger_line": chain.get("line"),
        "chain_ok": bool(chain.get("ok")),
        "ts": rec.get("ts"),
        "harness": rec.get("harness"),
        "session": rec.get("session"),
        "tool": rec.get("tool"),
        "command": rec.get("command"),
        "action_type": rec.get("action_type"),
        "door": rec.get("door"),
        "decision": decision,
        "outcome": outcome,
        "approved": outcome in ("approved", "allowed_out_of_band",
                                "approved_later_out_of_band"),
        "answered_by_line": (later or {}).get("line"),
        "approved_by": approved_by,
        "approved_at": approved_at,
        "approved_via": approved_via,
        "approved_out_of_band": bool(oob),
        "monthly_usd": rec.get("monthly_usd"),
        "owner": rec.get("owner"),
        "scope": rec.get("scope"),
        "under_freeze": bool(freeze),
        "freeze": freeze,
        "policy": {
            "policy_version": rec.get("policy_version"),
            "rule": rec.get("rule"),
            "pack_rules": list(rec.get("pack_rules") or []),
            "org_thresholds": rec.get("org_thresholds"),
            "nable_version": rec.get("nable_version"),
        },
        "reason": rec.get("reason"),
        "approval_chain": approvers,
        "approval_chain_named": bool(approvers),
        "error": rec.get("error") if decision == "fail_open" else None,
    }


def _cell(v: Any, limit: int = 90) -> str:
    text = " ".join(str(v if v is not None else "").split())
    if len(text) > limit:
        text = text[:limit - 3] + "..."
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("`", "'")


def _table(head: list[str], rows: list[list[Any]], empty: str, *,
           wide: int | None = None) -> str:
    """A markdown table; column `wide` (a finding, a reason) is cut at 400
    characters instead of 90."""
    if not rows:
        return f"_{empty}_"
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(_cell(c, 400 if i == wide else 90) for i, c in enumerate(r))
            + " |" for r in rows]
    return "\n".join(out)


def _policy_words(c: dict[str, Any]) -> str:
    bits = []
    if c["freeze"]:
        f = c["freeze"]
        bits.append(f"freeze {f.get('key', '')[:10]} ({f.get('mode')}, "
                    f"{'confirmed' if f.get('sure') else 'not confirmed'})")
    bits += [f"pack rule {r}" for r in c["policy"]["pack_rules"]]
    if c["policy"]["rule"]:
        bits.append(f"gate rule {c['policy']['rule']}")
    if c["door"] == "one_way":
        bits.append("one-way door")
    bits.append(f"policy {c['policy']['policy_version'] or 'unknown'}")
    return "; ".join(bits)


def _shown(c: dict[str, Any]) -> dict[str, str]:
    """Every optional field as text ("-" when absent): a template fills a
    placeholder only from a value, and an empty one would stay ${...}."""
    def text(v: Any) -> str:
        return "-" if v in (None, "") else str(v)
    f = c.get("freeze") or {}
    freeze = (f"{f.get('key', '')} ({f.get('mode')} mode, "
              f"{'confirmed' if f.get('sure') else 'not confirmed'}) until {f.get('end')}"
              if f else "none")
    return {**{k: text(c.get(k)) for k in ("action_type", "command", "harness", "session",
                                           "approved_by", "approved_via", "approved_at",
                                           "monthly_usd", "reason", "error")},
            "freeze": freeze}


def _exceptions(changes: list[dict[str, Any]], policies: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rule in policies:
        if type(rule).__name__ != "PolicyRule" or rule.applies_to != "action":
            continue
        for c in changes:
            hit = rule.evaluate(c)
            if hit is not None:
                out.append({"change": c["id"], "ledger_line": c["ledger_line"], **hit})
    return out


def build(since: datetime | None = None, *, until: datetime | None = None,
          path: Path | None = None, model: Any = None, policies: list[Any] | None = None,
          now: datetime | None = None, with_org: bool = True) -> dict[str, Any]:
    """The evidence for [since, until) (default: the whole ledger, to now).

    `model` is the org model (default: finops.org.load()), `policies` the
    installed packs' policies (default: finops.packs.active("policies")).
    Either failing to load is noted and costs its section, never the rest.
    `with_org=False` leaves the org model out entirely (its approval chains
    and freezes name people): a pack report reads it only when the pack
    declares the org.approvals data scope."""
    from . import guard_ledger
    now = (now or datetime.now(UTC)).astimezone(UTC)
    until = until or now
    notes: list[str] = []
    check = guard_ledger.check(path)
    exported = guard_ledger.export_records(None, path)
    org_note = None
    if not with_org:
        model = None
        org_note = ("the pack does not declare org.approvals, so the org model's approval "
                    "chains and change freezes are not shown")
        notes.append(org_note)
    elif model is None:
        try:
            from . import org
            model = org.load()
        except Exception as e:  # noqa: BLE001 - the org model is a citation here
            org_note = (f"the org model could not be read ({type(e).__name__}), so approval "
                        "chains and freezes are not shown")
            notes.append(org_note)
            model = None
    if policies is None:
        try:
            from . import packs
            policies = packs.active("policies")
        except Exception as e:  # noqa: BLE001 - exceptions are a review aid
            notes.append(f"installed pack policies could not be read ({type(e).__name__})")
            policies = []
    everything = [dict(r, _hash=(r.get("chain") or {}).get("hash")) for r in exported
                  if not r.get("unparseable")]
    answers = guard_ledger.ask_outcomes(everything, now=now)
    ran_at: dict[str, str] = {}
    for r in everything:
        if guard_ledger.is_outcome(r) and r.get("outcome") == guard_ledger.RAN \
                and isinstance(r.get("verdict"), str):
            ran_at.setdefault(r["verdict"], str(r.get("ts")))
    in_period: list[dict[str, Any]] = []
    unparseable = [r for r in exported if r.get("unparseable")]
    for r in everything:
        if guard_ledger.is_outcome(r):
            continue
        ts = _ts(r)
        if ts is None or (since is not None and ts < since) or ts >= until:
            continue
        in_period.append(r)
    # One-time approvals, by id, with the record of the call each let through
    # and when: from the whole ledger, since an ask late in the period can be
    # answered after it ends.
    granted: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
    for r in everything:
        g = r.get("approved_out_of_band")
        at = _ts(r)
        if isinstance(g, dict) and g.get("id") and at is not None \
                and not guard_ledger.is_outcome(r):
            granted.setdefault(str(g["id"]), []).append(
                (at, {"grant": g, "line": (r.get("chain") or {}).get("line")}))

    def intact(line: Any) -> bool:
        """Whether the chain vouches for the record on `line`: it verifies
        from the first record through it, and no check since the anchor
        found history rewritten. An edited record still chains to the one
        before it (only the next shows the edit), so a break at line n
        leaves lines before n - 1 intact and nothing after."""
        if not isinstance(line, int) or check.get("warnings"):
            return False
        if check.get("ok"):
            return line <= int(check.get("records") or 0)
        broken = check.get("broken_at")
        return isinstance(broken, int) and line < broken - 1
    counts: dict[str, int] = {k: 0 for k in (
        "verdicts", "allowed_by_policy", "changes", "asked", "approved", "declined", "unknown",
        "denied", "allowed_out_of_band", "approved_later_out_of_band", "not_run",
        "under_freeze", "not_examined", "pack_rule_hits")}
    changes: list[dict[str, Any]] = []
    for r in in_period:
        counts["verdicts"] += 1
        d = r.get("decision")
        oob = bool(r.get("approved_out_of_band"))
        if r.get("pack_rules"):
            counts["pack_rule_hits"] += 1
        if d not in _CHANGE_DECISIONS and not oob:
            if d in ("allow", "warn"):
                counts["allowed_by_policy"] += 1
            continue
        c = _record(r, answers.get(r["_hash"] or ""), ran_at, model, granted)
        c["chain_ok"] = intact(c["ledger_line"])
        # One-line forms for a template, which reads scalars only.
        c["policy_summary"] = _policy_words(c)
        c["approval_chain_text"] = "; ".join(c["approval_chain"]) or "none named"
        c["show"] = _shown(c)
        changes.append(c)
        counts["changes"] += 1
        if d == "ask":
            counts["asked"] += 1
        counts[c["outcome"]] += 1
        if c["under_freeze"]:
            counts["under_freeze"] += 1
    freezes: list[dict[str, Any]] = []
    chains: list[dict[str, Any]] = []
    if model is not None:
        for f in model.freezes(live_only=False):
            try:
                start = datetime.fromisoformat(str(f.value.get("start"))).astimezone(UTC)
                end = datetime.fromisoformat(str(f.value.get("end"))).astimezone(UTC)
            except ValueError:
                continue
            if (since is not None and end <= since) or start >= until:
                continue
            freezes.append({"key": f.key, "subject": str(f.subject), "start": f.value["start"],
                            "end": f.value["end"], "mode": f.value.get("mode"),
                            "reason": f.value.get("reason"), "status": f.status,
                            "confirmed_by": f.confirmed_by, "confirmed_at": f.confirmed_at})
        for f in model.by_kind("approval"):
            if not f.live:
                continue
            chains.append({"key": f.key, "subject": str(f.subject), "status": f.status,
                           "action_classes": list(f.value.get("action_classes") or []),
                           "approvers": list(f.value.get("approvers") or []),
                           "min": f.value.get("min", 1),
                           "change_ticket": bool(f.value.get("change_ticket")),
                           "source": f.source, "confirmed_by": f.confirmed_by})
    exceptions = _exceptions(changes, policies)
    ledger = {
        "path": check.get("path"), "records": check.get("records"), "head": check.get("head"),
        "chain_ok": bool(check.get("ok")), "broken_at": check.get("broken_at"),
        "problem": check.get("problem"), "warnings": list(check.get("warnings") or []),
        "clean": bool(check.get("clean")), "unparseable_lines": len(unparseable),
        "anchor": check.get("anchor"),
        "records_in_period": len(in_period),
    }
    if not ledger["chain_ok"]:
        exceptions.insert(0, {"change": None, "ledger_line": ledger["broken_at"],
                              "rule": "ledger-chain-broken", "pack": "nable",
                              "action": "block", "severity": "critical",
                              "message": f"The ledger's hash chain breaks at line "
                                         f"{ledger['broken_at']}: {ledger['problem']}. Records "
                                         "after it cannot be relied on as evidence."})
    for w in ledger["warnings"]:
        exceptions.insert(0, {"change": None, "ledger_line": None, "rule": "ledger-anchor",
                              "pack": "nable", "action": "escalate", "severity": "high",
                              "message": f"Since the last clean check, {w}."})
    chain_words = (f"verified: {ledger['records']} records chain from the first to head "
                   f"{ledger['head']}" if ledger["chain_ok"] else
                   f"BROKEN at line {ledger['broken_at']}: {ledger['problem']}")
    tables = {
        "ledger": _table(["Check", "Result"], [
            ["Hash chain", chain_words],
            ["Anchor", "no earlier check to compare with" if not ledger["anchor"] else
             ("matches the last clean check" if not ledger["warnings"]
              else "; ".join(ledger["warnings"]))],
            ["Unparseable lines", ledger["unparseable_lines"]],
            ["Records in the period", ledger["records_in_period"]]], "no ledger", wide=1),
        "changes": _table(
            ["When (UTC)", "Change", "Decision", "Outcome", "Approved by", "Approved at",
             "Under policy", "Ledger line"],
            [[c["ts"], c["command"] or c["action_type"], c["decision"], c["outcome"],
              c["approved_by"] or (c["approved_via"] or "-"), c["approved_at"] or "-",
              c["policy_summary"], c["ledger_line"]] for c in changes],
            "No change was asked about, denied or let through unexamined in this period."),
        "freezes": _table(
            ["Scope", "From", "Until", "Mode", "Status", "Confirmed by", "Reason"],
            [[f["subject"], f["start"], f["end"], f["mode"], f["status"],
              f["confirmed_by"] or "-", f["reason"]] for f in freezes],
            f"Not shown: {org_note}." if org_note else
            "No change freeze overlapped this period.", wide=6),
        "approval_chains": _table(
            ["Scope", "Action classes", "Approvers", "At least", "Change ticket", "Status",
             "Source"],
            [[a["subject"], ", ".join(a["action_classes"]), ", ".join(a["approvers"]),
              a["min"], "yes" if a["change_ticket"] else "no", a["status"], a["source"]]
             for a in chains],
            f"Not shown: {org_note}." if org_note else
            "The org model names no approval chain."),
        "exceptions": _table(
            ["Severity", "Rule", "Change", "Ledger line", "Finding"],
            [[e["severity"], f"{e.get('pack')}:{e['rule']}", e["change"] or "-",
              e["ledger_line"] or "-", e["message"]] for e in exceptions],
            "No exception was found.", wide=4),
    }
    return {
        "kind": "change-management evidence",
        "note": NOTE,
        "generated_at": now.isoformat(timespec="seconds"),
        "period": {"since": since.isoformat(timespec="seconds") if since else None,
                   "until": until.isoformat(timespec="seconds")},
        "ledger": ledger,
        "counts": counts,
        "changes": changes,
        "freezes": freezes,
        "approval_chains": chains,
        "exceptions": exceptions,
        "notes": notes,
        "tables": tables,
    }
