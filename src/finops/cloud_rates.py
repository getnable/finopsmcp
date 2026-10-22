# SPDX-License-Identifier: Apache-2.0
"""The workspace's own effective rates, kept on the laptop for the guard (D145 to D148).

The guard prices an EC2 instance, an RDS instance and an ElastiCache node at
list price. A paid workspace's box derives, from its own AWS bill, what that
workspace actually pays against the public on-demand price of the same usage
(the box's workspace_costs.rates), and sends it in the status answer the
laptop already reads: a `rates` block (contract 1, below). No endpoint and no
request is added. `nable cloud status` and `nable cloud connect --send` keep
it here, as cloud-rates.json (0600) beside the guard ledger, only when it
comes from the box and the workspace this machine last sent to
(cloud_standing.bound()); otherwise it is shown and not kept, with why. A
block that is not contract 1 exactly is refused whole.

Contract 1, as the box writes it:

    {"v": 1, "read": true, "workspace_ref": "ws_<32 hex>", "as_of": "YYYY-MM-DD",
     "window": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, "source": "<words>",
     "columns": ["<CUR column>", ...],
     "rows": [{"product_code", "region", "usage_type", "unit", "on_demand_factor",
               "blended_factor", "covered_share", "days", "line_items"}],
     "more": N, "not_served": {"<reason>": N}}

or, with nothing to serve, {"v": 1, "read": false, "reason", "workspace_ref"}.
Factors are decimal strings of 4 places within 0.3000 to 1.0000, or null; at
most ROWS_MAX rows and BLOCK_MAX_BYTES.

What the guard does with it (D146, D148): an ask shows the list price times
the on-demand factor of the same product, region and usage type, as "at your
effective rate", with the factor's window and as_of, and says the ask is
judged at list price. Nothing is judged at the effective figure: verdicts,
thresholds, the velocity cap and budgets read the list figure exactly as
before (rule 13). Where a price book prices the SKU, the book's figure stands
and no effective rate is applied. A rate that is missing, older than
STALE_DAYS past its as_of, another workspace's or another box's, or not held
for that usage type, says "at list price" and why.

The file is read on the guard's priced path only, at most MAX_BYTES, without
following a symlink, and never the network: standard library only, and no
connection is opened here.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from . import cloud_standing

FILE_NAME = "cloud-rates.json"
FORMAT = 1
BLOCK_MAX_BYTES = 65_536
# The block, and the few fields this module adds around it.
MAX_BYTES = BLOCK_MAX_BYTES + 4_096
ROWS_MAX = 2_000
STALE_DAYS = 45
COLUMNS_MAX = 64
FACTOR_MIN, FACTOR_MAX = Decimal("0.3000"), Decimal("1.0000")
PRODUCTS = ("AmazonEC2", "AmazonRDS", "AmazonElastiCache")
TOP_KEYS = frozenset({"v", "read", "workspace_ref", "as_of", "window", "source", "columns",
                      "rows", "more", "not_served"})
ROW_KEYS = frozenset({"product_code", "region", "usage_type", "unit", "on_demand_factor",
                      "blended_factor", "covered_share", "days", "line_items"})
_FACTOR_RE = re.compile(r"^[01]\.\d{4}$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REGION_RE = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-\d$")
_USAGE_RE = re.compile(r"^(?:BoxUsage|InstanceUsage|Multi-AZUsage|NodeUsage):"
                       r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}$")
_WORD_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_REASON_RE = re.compile(r"^[a-z0-9_]{1,40}$")
# How the guard's note starts: tests strip it to compare the rest.
EFFECTIVE_LEAD = "at your effective rate"
LIST_LEAD = "Priced at list price"
BOOK_LEAD = "Your price book prices"
JUDGED_AT_LIST = "The guard judges this ask at list price, as before."


class RatesError(ValueError):
    """A block that is not contract 1: refused whole."""


def path() -> Path:
    """cloud-rates.json, beside the guard ledger."""
    from . import guard_ledger
    return guard_ledger.ledger_path().parent / FILE_NAME


def _text(v: Any, name: str, most: int) -> str:
    if not isinstance(v, str) or not v or len(v) > most or any(
            ord(c) < 0x20 or ord(c) == 0x7f for c in v):
        raise RatesError(f"{name} is not one line of text of at most {most} characters")
    return v


def _day(v: Any, name: str) -> str:
    if not isinstance(v, str) or not _DAY_RE.match(v):
        raise RatesError(f"{name} is not a day")
    try:
        date.fromisoformat(v)
    except ValueError:
        raise RatesError(f"{name} is not a day") from None
    return v


def _count(v: Any, name: str, most: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= most:
        raise RatesError(f"{name} is not a count")
    return v


def _factor(v: Any, name: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not _FACTOR_RE.match(v) \
            or not FACTOR_MIN <= Decimal(v) <= FACTOR_MAX:
        raise RatesError(f"{name} is not a factor of 4 places within 0.3000 to 1.0000")
    return v


def check(block: Any, *, workspace_ref: str | None) -> dict[str, Any]:
    """`block` when it is contract 1 with read true and names `workspace_ref`
    (status's own), else RatesError: one field out of shape refuses it whole."""
    if not isinstance(block, dict):
        raise RatesError("the rates block is not an object")
    size = len(json.dumps(block, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if size > BLOCK_MAX_BYTES:
        raise RatesError(f"the rates block is {size:,} bytes, over {BLOCK_MAX_BYTES:,}")
    if block.get("v") != FORMAT or block.get("read") is not True:
        raise RatesError("the rates block is not version 1 with rates read")
    if set(block) != TOP_KEYS:
        raise RatesError("the rates block's keys are not contract 1's")
    ref = cloud_standing.workspace_ref(block.get("workspace_ref"))
    if ref is None or ref != workspace_ref:
        raise RatesError("the rates block names another workspace than the status it came in")
    _day(block["as_of"], "as_of")
    w = block["window"]
    if not isinstance(w, dict) or set(w) != {"start", "end"}:
        raise RatesError("window is not {start, end}")
    if _day(w["start"], "window.start") > _day(w["end"], "window.end"):
        raise RatesError("window starts after it ends")
    _text(block["source"], "source", 400)
    cols = block["columns"]
    if not isinstance(cols, list) or len(cols) > COLUMNS_MAX:
        raise RatesError(f"columns is not a list of at most {COLUMNS_MAX}")
    for c in cols:
        if not isinstance(c, str) or not _WORD_RE.match(c):
            raise RatesError("a column is not a column name")
    rows = block["rows"]
    if not isinstance(rows, list) or len(rows) > ROWS_MAX:
        raise RatesError(f"rows is not a list of at most {ROWS_MAX:,}")
    seen = set()
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or set(r) != ROW_KEYS:
            raise RatesError(f"rows[{i}] is not a rate row")
        if r["product_code"] not in PRODUCTS:
            raise RatesError(f"rows[{i}].product_code is not one the guard prices")
        if not isinstance(r["region"], str) or not _REGION_RE.match(r["region"]):
            raise RatesError(f"rows[{i}].region is not a region")
        if not isinstance(r["usage_type"], str) or not _USAGE_RE.match(r["usage_type"]):
            raise RatesError(f"rows[{i}].usage_type is not one the guard prices")
        _text(r["unit"], f"rows[{i}].unit", 32)
        od = _factor(r["on_demand_factor"], f"rows[{i}].on_demand_factor")
        bl = _factor(r["blended_factor"], f"rows[{i}].blended_factor")
        if od is None and bl is None:
            raise RatesError(f"rows[{i}] serves no factor")
        share = r["covered_share"]
        if not isinstance(share, str) or not _FACTOR_RE.match(share) \
                or not Decimal(0) <= Decimal(share) <= Decimal(1):
            raise RatesError(f"rows[{i}].covered_share is not a share of 4 places")
        _count(r["days"], f"rows[{i}].days", 62)
        _count(r["line_items"], f"rows[{i}].line_items", 10 ** 12)
        key = (r["product_code"], r["region"], r["usage_type"])
        if key in seen:
            raise RatesError(f"rows[{i}] repeats a key")
        seen.add(key)
    _count(block["more"], "more", 10 ** 9)
    ns = block["not_served"]
    if not isinstance(ns, dict) or len(ns) > 32:
        raise RatesError("not_served is not an object of counts")
    for k, n in ns.items():
        if not isinstance(k, str) or not _REASON_RE.match(k):
            raise RatesError("not_served has a key that is not a reason code")
        _count(n, f"not_served.{k}", 10 ** 9)
    return block


# ── kept ─────────────────────────────────────────────────────────────────────

def _write(doc: dict[str, Any]) -> Path:
    data = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise RatesError(f"the rates are {len(data):,} bytes, over the {MAX_BYTES:,} the guard "
                         "reads")
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return target


def _bound_to(box: str, workspace: str | None) -> str | None:
    """None when `box` and `workspace` are the ones this machine last sent
    to, else why not."""
    sent, why = cloud_standing.bound()
    if why:
        return why
    host = box.split("://", 1)[-1]
    if sent is None:
        return ("this machine has sent nothing to a box yet, so nothing says these rates "
                f"are its workspace's; `nable cloud connect --box {box} --send` binds one")
    if sent["box"] != box:
        return (f"this machine last sent to {sent['box'].split('://', 1)[-1]}, not {host}, "
                "so the guard keeps that workspace's rates, not these")
    if sent["workspace"] is None or sent["workspace"] != workspace:
        return (f"these rates are another workspace's on {host} than the one this machine "
                f"last sent to; send again with `nable cloud connect --box {box} --send`")
    return None


def keep(block: Any, *, box: str, workspace_ref: str | None,
         fetched_at: datetime) -> dict[str, Any]:
    """Keep a status answer's rates block for the guard, when it may: {"kept",
    "why", "file", "rows"}. Never raises for the block: a block that is not
    contract 1 is refused whole and said."""
    if block is None:
        return {"kept": False, "why": "the box's status carries no rates block (an older box)",
                "file": None, "rows": 0}
    if isinstance(block, dict) and block.get("read") is False:
        if block.get("workspace_ref") != workspace_ref or workspace_ref is None:
            return {"kept": False, "why": "the rates block was refused whole: it names another "
                                          "workspace than the status it came in",
                    "file": None, "rows": 0}
        reason = block.get("reason")
        why = (reason if isinstance(reason, str) and reason
               else "the box serves no rates for this workspace")
        why = "".join(c for c in why[:500] if c.isprintable())
        # The workspace this machine sends to serves no rates now: the
        # guard prices at list price, and a file kept before is removed.
        if _bound_to(box, workspace_ref) is None:
            with contextlib.suppress(OSError):
                path().unlink()
        return {"kept": False, "why": why, "file": None, "rows": 0}
    try:
        check(block, workspace_ref=workspace_ref)
    except RatesError as exc:
        return {"kept": False, "why": f"the rates block was refused whole: {exc}", "file": None,
                "rows": 0}
    not_bound = _bound_to(box, workspace_ref)
    if not_bound:
        return {"kept": False, "why": not_bound, "file": None, "rows": len(block["rows"])}
    doc = {"v": FORMAT, "box": box, "host": box.split("://", 1)[-1],
           "fetched_at": fetched_at.astimezone(UTC).isoformat(timespec="seconds"),
           "block": block}
    try:
        target = _write(doc)
    except (RatesError, OSError) as exc:
        return {"kept": False, "why": f"not kept ({type(exc).__name__}): {exc}", "file": None,
                "rows": len(block["rows"])}
    return {"kept": True, "why": None, "file": str(target), "rows": len(block["rows"])}


def read() -> tuple[dict[str, Any] | None, str | None]:
    """(the kept rates, None), (None, None) when none are kept, or (None, why
    they were not read)."""
    p = path()
    try:
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"{FILE_NAME} could not be opened ({exc.strerror or type(exc).__name__})"
    with os.fdopen(fd, "rb") as fh:
        raw = fh.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        return None, f"{FILE_NAME} is over the {MAX_BYTES // 1024} KiB the guard reads"
    try:
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict) or doc.get("v") != FORMAT:
            raise RatesError("not this format")
        box = doc.get("box")
        if not isinstance(box, str) or not box.startswith("https://"):
            raise RatesError("it names no https box")
        block = doc.get("block")
        check(block, workspace_ref=block.get("workspace_ref") if isinstance(block, dict)
              else None)
    except (UnicodeDecodeError, ValueError, AttributeError) as exc:
        return None, f"{FILE_NAME} is not rates this version reads ({exc})"
    return doc, None


def for_guard(*, today: date | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """(the rates the guard may use, None), or (None, why it prices at list):
    none kept, not read, another box's or workspace's than this machine last
    sent to, or more than STALE_DAYS past their as_of."""
    doc, why = read()
    if why:
        return None, why
    if doc is None:
        return None, "no effective rate from your workspace is kept on this machine"
    block = doc["block"]
    not_bound = _bound_to(doc["box"], block["workspace_ref"])
    if not_bound:
        return None, not_bound
    now = today or datetime.fromtimestamp(cloud_standing.now_epoch(), UTC).date()
    as_of = date.fromisoformat(block["as_of"])
    if now > as_of + timedelta(days=STALE_DAYS):
        return None, (f"your workspace's rates are as of {as_of}, more than {STALE_DAYS} days "
                      "ago; `nable cloud status` reads them again")
    return doc, None


def lookup(doc: dict[str, Any], product_code: str, region: str | None,
           usage_type: str) -> dict[str, Any] | None:
    """The rate row for one key, or None."""
    if not region:
        return None
    for r in doc["block"]["rows"]:
        if (r["product_code"], r["region"], r["usage_type"]) == (product_code, region,
                                                                  usage_type):
            return r
    return None


# ── what the guard shows and records ─────────────────────────────────────────

def _usd(x: Decimal) -> str:
    return f"${x:,.0f}"


def _sku_words(s: dict[str, Any]) -> str:
    where = f" in {s['region']}" if s.get("region") else ""
    return f"{s['quantity']:g}x {s['usage_type']}{where}"


def _public(s: dict[str, Any]) -> dict[str, Any]:
    """A SKU as contract 2 records it (`priced`): the product code, region,
    usage type, quantity, unit and the hours a month it was priced over, and
    the hourly rate one unit was priced at (usd_hour), so the box reconciles
    the usage type like for like when the line's monthly_usd also holds what
    the bill names under another one (an instance's EBS volumes)."""
    return {"product_code": s["product_code"], "region": s.get("region"),
            "usage_type": s["usage_type"], "quantity": s["quantity"], "unit": s["unit"],
            "hours": s["hours"], "usd_hour": s["usd_hour"]}


def effective(skus: list[dict[str, Any]], *, judged_monthly: float | None = None
              ) -> dict[str, Any] | None:
    """What an estimate's priced SKUs come to at the workspace's effective
    rate, for the guard: {"priced", "price_basis", "effective" or None,
    "note"}. None when nothing priced is one the rates could cover.

    `judged_monthly` is the estimate's own monthly figure, the one the guard
    judged: the effective figure is it with each matched SKU's list monthly
    replaced by that times its on-demand factor, so what the bill names
    under another usage type (EBS volumes, storage) stays at list. A SKU a
    price book prices keeps the book's figure. Reads cloud-rates.json, never
    the network."""
    skus = [s for s in skus if isinstance(s, dict) and s.get("product_code") in PRODUCTS]
    if not skus:
        return None
    priced: Any = _public(skus[0]) if len(skus) == 1 else [_public(s) for s in skus[:20]]
    books = [s for s in skus if s.get("price_book")]
    open_skus = [s for s in skus if not s.get("price_book")]
    book_note = ""
    if books:
        names = ", ".join(dict.fromkeys(s["usage_type"].split(":", 1)[-1] for s in books))
        book_note = (f"{BOOK_LEAD} {names}, so its figure stands and no effective rate is "
                     "applied to it.")
    if not open_skus:
        return {"priced": priced, "price_basis": "list", "effective": None, "note": book_note}
    doc, why = for_guard()
    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    unmatched: list[tuple[dict[str, Any], str]] = []
    for s in open_skus:
        if doc is None:
            unmatched.append((s, why or "no rate is kept"))
            continue
        if not s.get("region"):
            unmatched.append((s, ("the command names no region (--region, or an inline "
                                  "AWS_REGION=), so its list price is us-east-1's while it "
                                  "may launch elsewhere, and no rate of your workspace's is "
                                  "matched")))
            continue
        row = lookup(doc, s["product_code"], s["region"], s["usage_type"])
        if row is None or row["on_demand_factor"] is None:
            unmatched.append((s, (f"your workspace's rates hold no on-demand factor for "
                                  f"{s['usage_type']} in {s['region']}")))
            continue
        matched.append((s, row))
    if not matched:
        whys = "; ".join(dict.fromkeys(w for _s, w in unmatched))
        note = f"{LIST_LEAD}: {whys}."
        return {"priced": priced, "price_basis": "list", "effective": None,
                "note": " ".join(x for x in (note, book_note) if x)}
    block = doc["block"]
    listed = Decimal(str(judged_monthly)) if judged_monthly is not None else sum(
        (Decimal(str(s["monthly_usd"])) for s in skus), Decimal(0))
    total = listed
    parts = []
    for s, row in matched:
        f = Decimal(row["on_demand_factor"])
        sku_list = Decimal(str(s["monthly_usd"]))
        total -= sku_list - sku_list * f
        share = Decimal(row["covered_share"]) * 100
        parts.append(f"on-demand factor {row['on_demand_factor']} for {_sku_words(s)}, "
                     f"{share:.0f}% of whose usage ran on commitments")
    total = max(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), Decimal(0))
    single = len(skus) == 1 and len(matched) == 1
    first = matched[0][1]
    eff = {"monthly_usd": float(total),
           "factor": first["on_demand_factor"] if single else None,
           "blended_factor": first["blended_factor"] if single else None,
           "as_of": block["as_of"], "window": [block["window"]["start"], block["window"]["end"]],
           "workspace_ref": block["workspace_ref"]}
    if not single and isinstance(priced, list):
        rows = {(s["product_code"], s["usage_type"], s.get("region")): r for s, r in matched}
        for p in priced:
            r = rows.get((p["product_code"], p["usage_type"], p.get("region")))
            p["factor"] = r["on_demand_factor"] if r else None
            p["blended_factor"] = r["blended_factor"] if r else None
    rest = ""
    if unmatched:
        rest = " The rest is at list price: " + "; ".join(
            f"{_sku_words(s)}, {w}" for s, w in unmatched) + "."
    note = (f"This is ~{_usd(total)}/mo {EFFECTIVE_LEAD}, from your workspace's own bill "
            f"over {block['window']['start']} to {block['window']['end']} (as of "
            f"{block['as_of']}): " + "; ".join(parts) + "." + rest + f" {JUDGED_AT_LIST}")
    return {"priced": priced, "price_basis": "effective", "effective": eff,
            "note": " ".join(x for x in (note, book_note) if x)}


def _profile_region(environ: Any) -> str | None:
    """The region the AWS CLI's config file gives the active profile, when
    it names one (AWS_CONFIG_FILE, else ~/.aws/config; AWS_PROFILE, else
    default). At most MAX_BYTES of it are read, and only from a regular file:
    a FIFO or a device in its place is not waited on."""
    import configparser
    import stat
    where = environ.get("AWS_CONFIG_FILE", "").strip() or str(Path.home() / ".aws" / "config")
    try:
        fd = os.open(os.path.expanduser(where), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > MAX_BYTES:
        return None
    profile = (environ.get("AWS_PROFILE", "").strip()
               or environ.get("AWS_DEFAULT_PROFILE", "").strip() or "default")
    cp = configparser.RawConfigParser(strict=False)
    try:
        cp.read_string(raw.decode("utf-8", "replace"))
    except configparser.Error:
        return None
    section = "default" if profile == "default" else f"profile {profile}"
    if not cp.has_section(section) and profile == "default":
        section = "profile default"
    v = cp.get(section, "region", fallback="") if cp.has_section(section) else ""
    v = v.strip()
    return v if _REGION_RE.match(v) else None


def env_region(environ: Any = None) -> str | None:
    """The region the AWS CLI would use when a command names none:
    AWS_REGION, then AWS_DEFAULT_REGION, then the active profile's region in
    the AWS CLI's config file (_profile_region)."""
    env = os.environ if environ is None else environ
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        v = env.get(name, "").strip()
        if v and _REGION_RE.match(v):
            return v
    return _profile_region(env)


def text(got: dict[str, Any], block: Any) -> str:
    """What `nable cloud status` and `nable cloud connect --send` say of the
    rates block: kept for the guard, or shown and not kept, and why."""
    if isinstance(block, dict) and block.get("read") is True and isinstance(
            block.get("rows"), list) and isinstance(block.get("window"), dict):
        w = block["window"]
        head = (f"Effective rates: {len(block['rows']):,} usage type(s) from the workspace's "
                f"own bill over {w.get('start')} to {w.get('end')}, as of {block.get('as_of')}"
                + (f", {block['more']:,} more not sent" if block.get("more") else ""))
    else:
        head = "Effective rates: none served"
    if got.get("kept"):
        return (f"{head}. Kept in {got['file']} for the guard, which shows an ask's price at "
                "your effective rate beside list price and judges it at list price.")
    return f"{head}. Not kept for the guard, which prices at list price: {got.get('why')}."
