# SPDX-License-Identifier: Apache-2.0
"""Render an installed pack's report template over nable's own data.

A report template is text with ${dotted.key} placeholders (content.render:
dict lookups only, nothing evaluated). What fills them is the core's, never
the pack's: a placeholder under a source's name reads that source
(`${ledger.guard.counts.approved}`, `${ai.total_usd}`). The core builds the
source's values itself, in process, and only when the pack declares the
`[capabilities].read_data` scope the source reads: a template that reads a
scope its pack did not declare is refused, so what a report can show is
what was approved at install. A placeholder nothing fills stays as written,
so a template never shows a number nobody computed.

    SOURCES       placeholder name -> Source(the read_data scope it needs,
                  the core function that builds its values)
      ledger.guard   change-management evidence from the guard ledger
                     (finops.change_evidence); reads ledger.guard
      ai             AI and LLM spend by vendor, model, feature tag and
                     customer tag, from get_llm_costs and
                     get_ai_cost_attribution; reads focus.cost. It also
                     evaluates the pack's own policies against an
                     ai_attribution finding and lists what they flag.
    render()      one report, or one per item of a list (`each`), as text,
                  with the values it was rendered from (the JSON export)

`sets` fills plain placeholders a template leaves to the person
(`--set year=2027`); they can never stand in for a source. The window is
`since`/`until`, or `days` (the last N days, the same as --since Nd).

    nable pack report <ns/name> [<report>] [--since WHEN | --days N]
        [--until WHEN] [--set K=V] [--each PATH] [--json] [--out FILE]
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from .errors import PackError, PolicyRefusal

_SET_KEY = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
# The values render() fills itself: the period read, when, which report, and
# the record --each is on. An evidence report that says another period than
# the one it read would be forged, so --set never stands in for them.
_CORE_KEYS = frozenset({"generated_at", "since", "until", "pack", "report", "item"})
MAX_SET_VALUE = 200
DEFAULT_AI_DAYS = 30


@dataclass(frozen=True)
class Source:
    """What fills one placeholder name: the read_data scope it needs, and
    fill(*, pack_id, since, until, days, today) -> its values."""
    scope: str
    fill: Callable[..., dict[str, Any]]
    description: str


# ── ledger.guard: change-management evidence ──────────────────────────────────

def _ledger_guard(*, since: datetime | None = None, until: datetime | None = None,
                  **_: Any) -> dict[str, Any]:
    from .. import change_evidence
    return change_evidence.build(since, until=until)


# ── ai: AI spend by vendor, model, feature and customer ───────────────────────

VENDOR_NAMES = {"openai": "OpenAI", "anthropic": "Anthropic", "bedrock": "AWS Bedrock",
                "vertex": "Google Vertex AI", "openrouter": "OpenRouter", "litellm": "LiteLLM",
                "azure_openai": "Azure OpenAI"}
# The request tag (LiteLLM) or trace tag (Langfuse) keys that name a feature
# or a customer: "feature:search", "customer=acme".
TAG_KEYS = {"feature": ("feature",), "customer": ("customer", "tenant")}
MAX_ROWS = 15


def _usd(v: float) -> str:
    return f"{v:,.2f}"


def _pct(part: float, whole: float) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole > 0 else "n/a"


def _table(head: str, rows: list[tuple[str, float]], total: float, *, empty: str) -> str:
    if not rows:
        return empty
    shown = rows[:MAX_ROWS]
    lines = [f"| {head} | USD | Share |", "|---|---:|---:|"]
    lines += [f"| {name} | {_usd(v)} | {_pct(v, total)} |" for name, v in shown]
    if len(rows) > MAX_ROWS:
        rest = sum(v for _, v in rows[MAX_ROWS:])
        lines.append(f"| {len(rows) - MAX_ROWS} more | {_usd(rest)} | {_pct(rest, total)} |")
    return "\n".join(lines)


def _tag_split(groups: list[dict[str, Any]]) -> tuple[dict[str, dict[str, float]], list[str]]:
    """{"feature": {name: usd}, "customer": {...}} from tag groups, and the
    providers they came from. LiteLLM and Langfuse can both log one call, so
    a tag value's spend is the largest any one provider reports, never the
    sum across them."""
    out: dict[str, dict[str, float]] = {k: {} for k in TAG_KEYS}
    providers: set[str] = set()
    for g in groups:
        tag = str(g.get("group") or g.get("id") or "").strip()
        key, sep, value = tag.partition(":")
        if not sep:
            key, sep, value = tag.partition("=")
        if not sep or not value.strip():
            continue
        for dim, keys in TAG_KEYS.items():
            if key.strip().lower() in keys:
                try:
                    usd = float(g.get("cost_usd") or 0.0)
                except (TypeError, ValueError):
                    continue
                name = value.strip()
                out[dim][name] = max(out[dim].get(name, 0.0), usd)
                providers.add(str(g.get("provider") or "?"))
    return out, sorted(providers)


def _policy_lines(pack_id: str, finding: dict[str, Any]) -> list[str]:
    from .runtime import active
    lines = []
    for rule in active("policies"):
        if rule.pack != pack_id or getattr(rule, "applies_to", None) != "finding":
            continue
        hit = rule.evaluate(finding)
        if hit:
            lines.append(f"- {hit['severity']} ({hit['action']}, rule {hit['rule']}): "
                         f"{' '.join(hit['message'].split())}")
    return lines


def _ai_window(since: datetime | None, until: datetime | None, days: int | None,
               today: date | None) -> tuple[date, date, int]:
    """(start, end, days) in local calendar days: `days` up to `today` (or
    `until`, or now); else from `since`'s day; else the last 30 days."""
    end = today or (until.astimezone().date() if until else datetime.now().astimezone().date())
    if days is None:
        days = max(1, (end - since.astimezone().date()).days) if since else DEFAULT_AI_DAYS
    return end - timedelta(days=days), end, days


def ai_values(*, pack_id: str, since: datetime | None = None, until: datetime | None = None,
              days: int | None = None, today: date | None = None, **_: Any) -> dict[str, Any]:
    """The `ai` namespace: every value a string or a number, tables as
    Markdown text, plus `finding` (the ai_attribution finding the pack's
    policies were evaluated against, not a placeholder)."""
    from ..connectors import ai_attribution, llm_costs
    start, end, days = _ai_window(since, until, days, today)
    gaps: list[str] = []
    try:
        llm = llm_costs.get_all_llm_costs(start_date=start, end_date=end, days=days)
    except Exception as e:  # noqa: BLE001 - a report says what it could not read
        llm = {"error": f"{type(e).__name__}: {e}"}
    try:
        tags = ai_attribution.get_ai_cost_attribution("tag", days=days, end_date=end)
    except Exception as e:  # noqa: BLE001
        tags = {"error": f"{type(e).__name__}: {e}"}
    total = float(llm.get("total_usd") or 0.0)
    vendors = sorted(((VENDOR_NAMES.get(k, k), float(v or 0.0))
                      for k, v in (llm.get("by_provider") or {}).items() if v),
                     key=lambda kv: (-kv[1], kv[0]))
    models = sorted(((str(k), float(v or 0.0)) for k, v in (llm.get("by_model") or {}).items()
                     if v), key=lambda kv: (-kv[1], kv[0]))
    split, tag_providers = _tag_split(tags.get("groups") or [])
    tagged = {dim: sum(vals.values()) for dim, vals in split.items()}
    for key in ("error", "note"):
        if llm.get(key):
            gaps.append(f"Spend: {llm[key]}")
    if llm.get("unpriced_models"):
        gaps.append("Priced at a fallback rate: " + ", ".join(map(str, llm["unpriced_models"])))
    if not tag_providers:
        gaps.append("No feature or customer tags were read: they come from LiteLLM request "
                    "tags or Langfuse trace tags (feature:<name>, customer:<id>), so every "
                    "dollar above counts as untagged.")
    if tags.get("failed_providers"):
        gaps.append("Tags not read from: " + ", ".join(sorted(tags["failed_providers"])))
    gaps.append("Bedrock and Vertex spend has no request tags; it is split by model only.")

    def tag_table(dim: str) -> str:
        rows = sorted(split[dim].items(), key=lambda kv: (-kv[1], kv[0]))
        untagged = max(0.0, total - tagged[dim])
        if untagged > 0:
            rows.append((f"(no {dim} tag)", untagged))
        return _table(dim.capitalize(), rows, total,
                      empty=f"No {dim} tags and no AI spend were read for this period.")

    def share(dim: str) -> float | None:
        return round(min(100.0, 100.0 * tagged[dim] / total), 1) if total > 0 else None

    finding = {"type": "ai_attribution", "period_days": days, "total_usd": round(total, 2),
               "feature_tagged_pct": share("feature"), "customer_tagged_pct": share("customer"),
               "untagged_feature_usd": round(max(0.0, total - tagged["feature"]), 2),
               "untagged_customer_usd": round(max(0.0, total - tagged["customer"]), 2),
               "tag_sources": tag_providers}
    flags = _policy_lines(pack_id, finding)

    def shown(v: float | None) -> str:
        return "unknown" if v is None else f"{v:g}%"

    return {
        "period": f"{start} to {end}", "start": str(start), "end": str(end), "days": days,
        "total_usd": _usd(total), "vendor_count": len(vendors), "model_count": len(models),
        "by_vendor": _table("Vendor", vendors, total,
                            empty="No AI spend was read for this period."),
        "by_model": _table("Model", models, total, empty="No AI spend was read for this period."),
        "by_feature": tag_table("feature"), "by_customer": tag_table("customer"),
        "feature_tagged": shown(finding["feature_tagged_pct"]),
        "customer_tagged": shown(finding["customer_tagged_pct"]),
        "policy_findings": "\n".join(flags) or "No attribution policy flags this period.",
        "gaps": "\n".join(f"- {g}" for g in gaps),
        "finding": finding,
    }


# Placeholder name -> the source that fills it and the read_data scope it reads.
SOURCES: dict[str, Source] = {
    "ledger.guard": Source("ledger.guard", _ledger_guard,
                           "change-management evidence from the guard ledger"),
    "ai": Source("focus.cost", ai_values,
                 "AI and LLM spend by vendor, model, feature tag and customer tag"),
}


# ── rendering ─────────────────────────────────────────────────────────────────

def sources_in(text: str) -> list[str]:
    """The sources (SOURCES keys) a template's placeholders read."""
    from .content import placeholders
    out: list[str] = []
    for ph in placeholders(text):
        for name in SOURCES:
            if (ph == name or ph.startswith(name + ".")) and name not in out:
                out.append(name)
    return out


def scopes_in(text: str) -> list[str]:
    """The read_data scopes a template's placeholders read."""
    return list(dict.fromkeys(SOURCES[n].scope for n in sources_in(text)))


def find(pack_id: str, name: str | None = None) -> Any:
    """The installed, loaded pack's report template called `name`: its path
    in the pack (reports/cc8.1-evidence.md), its file name or its stem. With
    no name, the pack's only report."""
    from . import runtime
    if pack_id not in runtime.loaded_packs():
        why = [p for p in runtime.load_problems() if p.startswith(f"{pack_id} ")]
        raise PackError(f"{pack_id} is not installed and loaded"
                        + (f": {why[0]}" if why else ""))
    mine = [r for r in runtime.active("reports") if r.pack == pack_id]
    if not mine:
        raise PackError(f"{pack_id} has no reports")
    if name is None:
        if len(mine) == 1:
            return mine[0]
        raise PackError(f"{pack_id} has {len(mine)} reports; name one of "
                        f"{', '.join(sorted(r.path for r in mine))}")
    for r in mine:
        p = PurePosixPath(r.path)
        stem = p.name.split(".", 1)[0]
        if name in (r.path, p.name, stem, p.stem):
            return r
    known = ", ".join(sorted(PurePosixPath(r.path).name for r in mine))
    raise PackError(f"{pack_id} has no report {name!r} (its reports: {known})")


def _declared(pack_id: str) -> tuple[str, ...]:
    from . import store
    e = store.read_index()["packs"].get(pack_id) or {}
    return tuple((e.get("capabilities") or {}).get("read_data") or ())


def _set_values(sets: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    roots = {s.split(".", 1)[0] for s in SOURCES}
    for k, v in (sets or {}).items():
        if not _SET_KEY.match(k):
            raise PackError(f"--set {k!r}: a name is lowercase letters, digits and _")
        if k in roots:
            raise PackError(f"--set {k}: {k} is a data scope, which only nable fills")
        if k in _CORE_KEYS:
            raise PackError(f"--set {k}: {k} is what the report read or when, which nable fills")
        v = str(v)
        if len(v) > MAX_SET_VALUE or any(ord(c) < 32 for c in v):
            raise PackError(f"--set {k}: the value must be one line of at most "
                            f"{MAX_SET_VALUE} characters")
        out[k] = v
    return out


def _nest(values: dict[str, Any], scope: str, data: dict[str, Any]) -> None:
    cur = values
    parts = scope.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = data


def _lookup(values: dict[str, Any], path: str) -> Any:
    cur: Any = values
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def render(pack_id: str, name: str | None = None, *, since: datetime | None = None,
           until: datetime | None = None, days: int | None = None,
           sets: dict[str, str] | None = None, each: str | None = None,
           now: datetime | None = None, today: date | None = None) -> dict[str, Any]:
    """{"pack", "report", "scopes" (read_data scopes read), "sources" (the
    SOURCES that filled it), "text" (str, or a list with `each`), "values"}.
    `days` is the last N days (--since Nd; ai counts them in local calendar
    days up to `today`). Raises PolicyRefusal for a scope the pack did not
    declare, PackError for anything else."""
    if days is not None:
        if days < 1:
            raise PackError("--days must be at least 1")
        if since is not None:
            raise PackError("--days N is the same as --since Nd: give one of them")
    tmpl = find(pack_id, name)
    used = sources_in(tmpl.text + (f" ${{{each}}}" if each else ""))
    declared = _declared(pack_id)
    for n in used:
        if SOURCES[n].scope not in declared:
            raise PolicyRefusal(f"{pack_id} report {tmpl.path} reads {SOURCES[n].scope}, "
                                "which the pack does not declare in [capabilities].read_data")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if days is not None:
        since = now - timedelta(days=days)
    values: dict[str, Any] = {
        "generated_at": now.isoformat(timespec="seconds"),
        "since": since.isoformat(timespec="seconds") if since else "the first record",
        "until": (until or now).isoformat(timespec="seconds"),
        "pack": pack_id, "report": tmpl.path,
        **_set_values(sets),
    }
    for n in used:
        _nest(values, n, SOURCES[n].fill(pack_id=pack_id, since=since, until=until,
                                         days=days, today=today))
    if each is None:
        text: Any = tmpl.render(values)
    else:
        items = _lookup(values, each)
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise PackError(f"--each {each}: that is not a list of records in the values "
                            "this report reads")
        text = [tmpl.render({**values, "item": item}) for item in items]
    # The pack's own credentials never reach a report, whatever put them in
    # the data it read: they are redacted from the text and the values.
    from .broker import _scrub
    creds = _credentials(pack_id)
    return {"pack": pack_id, "report": tmpl.path,
            "scopes": list(dict.fromkeys(SOURCES[n].scope for n in used)),
            "sources": used, "text": _scrub(text, creds), "values": _scrub(values, creds)}


def _credentials(pack_id: str) -> dict[str, str]:
    """{name: value} of the credentials (secrets) the installed pack
    declares and the org has set."""
    from . import broker, store
    e = store.read_index()["packs"].get(pack_id) or {}
    out: dict[str, str] = {}
    for name in (e.get("capabilities") or {}).get("secrets") or ():
        v = broker.secret_value(pack_id, str(name))
        if v:
            out[str(name)] = v
    return out

