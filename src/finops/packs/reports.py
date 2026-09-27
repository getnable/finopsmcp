# SPDX-License-Identifier: Apache-2.0
"""Pack report templates, filled from data nable already has.

A report template is text with ${dotted.key} placeholders (content.render:
dict lookups, nothing evaluated). What fills them is the core's, never the
pack's: each top-level name a template uses (`ai` in ${ai.total_usd}) names
one source below, and a source runs only when the installed pack declares the
read_data scope that source reads. A placeholder no source fills stays as
written, so a template never shows a number nobody computed.

    ai   AI and LLM spend by vendor, model, feature tag and customer tag, from
         get_llm_costs and get_ai_cost_attribution (the providers nable is
         connected to); reads focus.cost. It also evaluates the pack's own
         policies against an ai_attribution finding and lists what they flag.

    nable pack report <ns/name> [<report>] [--days N] [--json]
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .errors import PackError


@dataclass(frozen=True)
class Source:
    scope: str
    fill: Callable[..., dict[str, Any]]
    description: str


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
        if rule.pack != pack_id or rule.applies_to != "finding":
            continue
        hit = rule.evaluate(finding)
        if hit:
            lines.append(f"- {hit['severity']} ({hit['action']}, rule {hit['rule']}): "
                         f"{' '.join(hit['message'].split())}")
    return lines


def ai_values(*, days: int, pack_id: str, today: date | None = None) -> dict[str, Any]:
    """The `ai` namespace: every value a string or a number, tables as
    Markdown text, plus `finding` (the ai_attribution finding the pack's
    policies were evaluated against, not a placeholder)."""
    from ..connectors import ai_attribution, llm_costs
    end = today or datetime.now().astimezone().date()   # the local calendar day
    start = end - timedelta(days=days)
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


SOURCES: dict[str, Source] = {
    "ai": Source("focus.cost", ai_values,
                 "AI and LLM spend by vendor, model, feature tag and customer tag"),
}


# ── rendering ─────────────────────────────────────────────────────────────────

def _pick(templates: list[Any], report: str | None, pack_id: str) -> Any:
    if report:
        for t in templates:
            name = t.path.rsplit("/", 1)[-1]
            if report in (t.path, name, name.rsplit(".", 1)[0]):
                return t
        raise PackError(f"{pack_id} has no report {report!r} (it has "
                        f"{', '.join(t.path for t in templates)})")
    if len(templates) > 1:
        raise PackError(f"{pack_id} has {len(templates)} reports; name one of "
                        f"{', '.join(t.path for t in templates)}")
    return templates[0]


def render(pack_id: str, report: str | None = None, *, days: int = 30,
           today: date | None = None) -> dict[str, Any]:
    """Fill an installed pack's report template. Raises PackError."""
    from . import store
    from .content import placeholders
    from .runtime import active, load_problems
    if days < 1:
        raise PackError("--days must be at least 1")
    templates = [t for t in active("reports") if t.pack == pack_id]
    if not templates:
        why = [p for p in load_problems() if p.startswith(f"{pack_id} ")]
        raise PackError(why[0] if why else f"{pack_id} is not installed, or has no reports")
    t = _pick(templates, report, pack_id)
    entry = store.read_index()["packs"].get(pack_id) or {}
    declared = set((entry.get("capabilities") or {}).get("read_data") or ())
    values: dict[str, Any] = {}
    used: list[str] = []
    notes: list[str] = []
    for ns in sorted({p.split(".", 1)[0] for p in placeholders(t.text)}):
        src = SOURCES.get(ns)
        if src is None:
            continue
        if src.scope not in declared:
            notes.append(f"{ns}.* reads {src.scope}, which {pack_id} does not declare in "
                         "read_data, so those placeholders are left as written")
            continue
        values[ns] = src.fill(days=days, pack_id=pack_id, today=today)
        used.append(ns)
    return {"pack": pack_id, "report": t.path, "text": t.render(values), "sources": used,
            "notes": notes, "values": values}
