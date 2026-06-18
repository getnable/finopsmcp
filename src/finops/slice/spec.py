"""
SliceSpec: the one query DSL the agent compiles natural language into.

A SliceSpec groups, filters, and excludes over any FOCUS dimension. The dimension
set is a CLOSED registry so the agent cannot invent columns, and parse_spec()
validates every agent-supplied field before it reaches the engine. The result of a
run carries a CardSpec, the portable contract every surface (web, Slack, Claude)
renders and can pin.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# ── Closed registries (the agent's allowlist) ────────────────────────────────
# Categorical FOCUS dimensions a slice may group or filter by. "date" (time) and
# "Tags[<key>]" (an arbitrary tag) are handled specially in the engine.
FOCUS_DIMENSIONS: set[str] = {
    "ServiceName", "ServiceCategory", "ProviderName",
    "RegionId", "RegionName", "SubAccountId", "SubAccountName",
    "ResourceId", "ResourceName", "ResourceType",
    "ChargeCategory", "ChargeDescription",
    "CommitmentDiscountId", "CommitmentDiscountType",
}
# The time pseudo-dimension and tag pseudo-dimensions are also groupable/filterable.
TIME_DIMENSION = "date"

METRICS: set[str] = {"BilledCost", "EffectiveCost", "ListCost"}
GRANULARITIES: set[str] = {"TOTAL", "DAILY", "MONTHLY"}
FILTER_OPS: set[str] = {"eq", "in", "neq", "not_in", "contains", "regex"}
CARD_TEMPLATES: set[str] = {"line", "bar", "stacked_bar", "table", "kpi", "heatmap"}

DEFAULT_METRIC = "EffectiveCost"
MAX_LIMIT = 500


class SliceSpecError(ValueError):
    """Raised when an agent-supplied slice is malformed or uses unknown dimensions."""


def is_tag_dim(dim: str) -> bool:
    return dim.startswith("Tags[") and dim.endswith("]") and len(dim) > 6


def is_valid_dimension(dim: str) -> bool:
    return dim == TIME_DIMENSION or dim in FOCUS_DIMENSIONS or is_tag_dim(dim)


@dataclass
class FilterClause:
    dimension: str
    op: str               # one of FILTER_OPS
    values: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SliceSpec:
    """One cost query. The pure engine runs this over a list of FocusRecords."""
    dimensions: list[str] = field(default_factory=list)   # group-by, e.g. ["RegionId"] or ["date","ServiceName"]
    filters: list[FilterClause] = field(default_factory=list)
    exclusions: list[FilterClause] = field(default_factory=list)
    metric: str = DEFAULT_METRIC
    granularity: str = "TOTAL"      # only meaningful when "date" is in dimensions
    order_by: str = "metric"        # "metric" (desc) or a dimension name (asc)
    limit: int = 50

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class SliceResult:
    rows: list[dict]                # [{<dim>: value, ..., "metric": float}]
    total: float
    metric: str
    dimensions: list[str]
    record_count: int = 0
    currency: str = "USD"
    truncated: bool = False         # True if more groups existed than the limit

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CardSpec:
    """How to render a slice result, and the slice that regenerates it (for pinning)."""
    title: str
    template: str                   # one of CARD_TEMPLATES
    metric: str
    dimensions: list[str]
    slice: dict                     # the SliceSpec.to_dict() that produced this, so it can be re-run
    refresh_secs: int = 43200       # 12h, matches the cost cache TTL

    def to_dict(self) -> dict:
        return asdict(self)


# ── Validation: agent dict -> SliceSpec ──────────────────────────────────────

def _parse_clause(raw: dict, kind: str) -> FilterClause:
    if not isinstance(raw, dict):
        raise SliceSpecError(f"each {kind} must be an object with dimension/op/values")
    dim = raw.get("dimension")
    op = (raw.get("op") or "in").lower()
    values = raw.get("values")
    if not isinstance(dim, str) or not is_valid_dimension(dim):
        raise SliceSpecError(f"unknown {kind} dimension {dim!r}; allowed: {sorted(FOCUS_DIMENSIONS)} or 'date' or 'Tags[key]'")
    if op not in FILTER_OPS:
        raise SliceSpecError(f"unknown {kind} op {op!r}; allowed: {sorted(FILTER_OPS)}")
    if isinstance(values, (str, int, float)):
        values = [values]
    if not isinstance(values, list) or not values:
        raise SliceSpecError(f"{kind} on {dim!r} needs a non-empty 'values' list")
    return FilterClause(dimension=dim, op=op, values=[str(v) for v in values])


def parse_spec(raw: dict) -> SliceSpec:
    """Validate an agent-supplied slice dict into a SliceSpec, or raise SliceSpecError.

    This is the agent's guardrail: unknown dimensions, bad ops, bad metrics, and
    out-of-range limits are rejected with a message the agent can self-correct on.
    """
    if not isinstance(raw, dict):
        raise SliceSpecError("slice must be an object")

    dims = raw.get("dimensions") or []
    if isinstance(dims, str):
        dims = [dims]
    if not isinstance(dims, list):
        raise SliceSpecError("dimensions must be a list of dimension names")
    for d in dims:
        if not isinstance(d, str) or not is_valid_dimension(d):
            raise SliceSpecError(f"unknown dimension {d!r}; allowed: {sorted(FOCUS_DIMENSIONS)} or 'date' or 'Tags[key]'")
    if len(dims) > 3:
        raise SliceSpecError("at most 3 dimensions per slice (keeps cards legible)")

    metric = raw.get("metric") or DEFAULT_METRIC
    if metric not in METRICS:
        raise SliceSpecError(f"unknown metric {metric!r}; allowed: {sorted(METRICS)}")

    gran = (raw.get("granularity") or "TOTAL").upper()
    if gran not in GRANULARITIES:
        raise SliceSpecError(f"unknown granularity {gran!r}; allowed: {sorted(GRANULARITIES)}")

    order_by = raw.get("order_by") or "metric"
    if order_by != "metric" and order_by not in dims:
        raise SliceSpecError(f"order_by must be 'metric' or one of the dimensions {dims}")

    try:
        limit = int(raw.get("limit") or 50)
    except (TypeError, ValueError):
        raise SliceSpecError("limit must be an integer")
    limit = max(1, min(limit, MAX_LIMIT))

    filters = [_parse_clause(c, "filter") for c in (raw.get("filters") or [])]
    exclusions = [_parse_clause(c, "exclusion") for c in (raw.get("exclusions") or [])]

    return SliceSpec(
        dimensions=dims, filters=filters, exclusions=exclusions,
        metric=metric, granularity=gran, order_by=order_by, limit=limit,
    )
