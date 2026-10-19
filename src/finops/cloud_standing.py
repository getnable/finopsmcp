# SPDX-License-Identifier: Apache-2.0
"""The box's month to date for this machine's person, kept on the laptop for
the guard (D107, D108).

`nable cloud status` asks the box for one person's standing (POST
/api/free-tool/standing, finops.cloud_connect.standing) and keeps the answer
here, beside the guard ledger, as cloud-standing.json (0600):

    {"v": 1, "box": "https://HOST", "host": "HOST", "fetched_at": ISO 8601,
     "month": "YYYY-MM", "person": "<the person asked about>",
     "as_of": ISO 8601 or null, "basis": "list-price equivalent",
     "budgets_from": {...} or null,
     "figures": {"person": {"id", "usd", "days", "last_day", "budget"},
                 "teams": [{"team", "usd", "people", "people_sent",
                            "latest_day", "oldest_last_day", "budget"}],
                 "org": {"usd", "people_sent", "latest_day", "budget"}}}

Every dollar is a decimal string, or null where the box received no records
for it ("no records received", never $0). No key is ever written here.

The guard's AI budget gate reads this file and never the network
(ai_budget.org_budget_status). For each budget that applies on this machine,
the figure it compares is the larger of this machine's UTC month to date and
the box's, never their sum: a sum would count this machine twice once it has
sent its own usage, and twice again after a reinstall gives it a new install
id. The box's figure counts only when all of these hold, at any age (the age
is said with it):

  * the file is there, at most MAX_BYTES, and reads as FORMAT;
  * it is for this UTC month;
  * it is for the person the guard holds this machine's agents to
    (ai_budget.developer);
  * it comes from the box this machine sends its usage to: the box of its
    last `nable cloud connect --send` (cloud-box.json, BOX_FILE_NAME), or
    from any box while this machine has sent to none.

Otherwise the gate uses this machine's figure alone, and `nable guard doctor`
says why (usable()). Only the local budgets.yaml sets a threshold; the box's
budgets are shown, never compared.

Standard library only, and no connection is ever opened here: the guard
imports this module on its hook path.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

FILE_NAME = "cloud-standing.json"
BOX_FILE_NAME = "cloud-box.json"
FORMAT = 1
# The most the guard reads of the file; a larger one is not read at all.
MAX_BYTES = 64 * 1024
BASIS = "list-price equivalent"
NO_RECORDS = "no records received"
STATES = ("under", "close", "over", NO_RECORDS)
# What one answer may list: a person is on a few teams, never hundreds.
TEAMS_MAX = 64
_TEXT_MAX = 320
_USD_RE = re.compile(r"^\d{1,12}(?:\.\d{1,6})?$")
_MONTH_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class StandingError(ValueError):
    """An answer from the box that is not a standing this module keeps."""


def now_epoch() -> float:
    """The time the age of a figure is measured at (tests fix it)."""
    return time.time()


def _dir() -> Path:
    from . import guard_ledger
    return guard_ledger.ledger_path().parent


def path() -> Path:
    """cloud-standing.json, beside the guard ledger."""
    return _dir() / FILE_NAME


def box_path() -> Path:
    """cloud-box.json, beside the guard ledger: the box of this machine's
    last `nable cloud connect --send`."""
    return _dir() / BOX_FILE_NAME


# ── the box's answer, checked ────────────────────────────────────────────────

def _text(v: Any, name: str, *, most: int = _TEXT_MAX) -> str:
    if not isinstance(v, str) or not v or len(v) > most or any(
            ord(c) < 0x20 or ord(c) == 0x7f for c in v):
        raise StandingError(f"{name} is not one line of text of at most {most} characters")
    return v


def _usd(v: Any, name: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not _USD_RE.match(v):
        raise StandingError(f"{name} is not a dollar figure as a decimal string")
    return v


def _count(v: Any, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10 ** 6:
        raise StandingError(f"{name} is not a count")
    return v


def _day(v: Any, name: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not _DAY_RE.match(v):
        raise StandingError(f"{name} is not a UTC day")
    return v


def _iso(v: Any, name: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or len(v) > 64:
        raise StandingError(f"{name} is not a time")
    try:
        datetime.fromisoformat(v)
    except ValueError:
        raise StandingError(f"{name} is not a time") from None
    return v


def _budget(v: Any, name: str) -> dict[str, Any] | None:
    if v is None:
        return None
    if not isinstance(v, dict):
        raise StandingError(f"{name} is not a budget")
    state = v.get("state")
    if state not in STATES:
        raise StandingError(f"{name}.state is not one of {', '.join(STATES)}")
    return {"monthly_usd": _usd(v.get("monthly_usd"), f"{name}.monthly_usd"),
            "per_person_usd": _usd(v.get("per_person_usd"), f"{name}.per_person_usd"),
            "state": state}


def _obj(v: Any, name: str) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise StandingError(f"{name} is not an object")
    return v


def normalize(answer: Any, *, box: str, person: str, fetched_at: datetime) -> dict[str, Any]:
    """The file's document from the box's answer, keeping only the fields
    this module checks, or StandingError. The answer must be about `person`."""
    a = _obj(answer, "the answer")
    month = a.get("month")
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        raise StandingError("month is not YYYY-MM")
    p = _obj(a.get("person"), "person")
    pid = _text(p.get("id"), "person.id").strip().lower()
    if pid != person:
        raise StandingError("the answer is about another person than the one asked about")
    teams = a.get("teams")
    if not isinstance(teams, list) or len(teams) > TEAMS_MAX:
        raise StandingError(f"teams is not a list of at most {TEAMS_MAX}")
    o = _obj(a.get("org"), "org")
    from_ = a.get("budgets_from")
    if from_ is not None:
        f = _obj(from_, "budgets_from")
        from_ = {"version": _count(f.get("version"), "budgets_from.version"),
                 "uploaded_at": _iso(f.get("uploaded_at"), "budgets_from.uploaded_at")}
    host = box.split("://", 1)[-1]
    return {
        "v": FORMAT, "box": box, "host": host,
        "fetched_at": fetched_at.astimezone(UTC).isoformat(timespec="seconds"),
        "month": month, "person": person,
        "as_of": _iso(a.get("as_of"), "as_of"),
        "basis": BASIS, "budgets_from": from_,
        "figures": {
            "person": {"id": pid, "usd": _usd(p.get("usd"), "person.usd"),
                       "days": _count(p.get("days", 0), "person.days"),
                       "last_day": _day(p.get("last_day"), "person.last_day"),
                       "budget": _budget(p.get("budget"), "person.budget")},
            "teams": [{"team": _text(t.get("team"), f"teams[{i}].team", most=128),
                       "usd": _usd(t.get("usd"), f"teams[{i}].usd"),
                       "people": _count(t.get("people", 0), f"teams[{i}].people"),
                       "people_sent": _count(t.get("people_sent", 0),
                                             f"teams[{i}].people_sent"),
                       "latest_day": _day(t.get("latest_day"), f"teams[{i}].latest_day"),
                       "oldest_last_day": _day(t.get("oldest_last_day"),
                                               f"teams[{i}].oldest_last_day"),
                       "budget": _budget(t.get("budget"), f"teams[{i}].budget")}
                      for i, t in enumerate(_obj(x, "a team") for x in teams)],
            "org": {"usd": _usd(o.get("usd"), "org.usd"),
                    "people_sent": _count(o.get("people_sent", 0), "org.people_sent"),
                    "latest_day": _day(o.get("latest_day"), "org.latest_day"),
                    "budget": _budget(o.get("budget"), "org.budget")},
        },
    }


# ── kept ─────────────────────────────────────────────────────────────────────

def _write(target: Path, doc: dict[str, Any]) -> Path:
    data = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise StandingError(f"the standing is {len(data):,} bytes, over the {MAX_BYTES:,} the "
                            "guard reads")
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


def write(doc: dict[str, Any]) -> Path:
    """Keep `doc` (normalize's) as cloud-standing.json, 0600, all or nothing."""
    return _write(path(), doc)


def bind(box: str, *, at: datetime | None = None) -> Path:
    """Record `box` as the box this machine sends its usage to."""
    when = (at or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    return _write(box_path(), {"v": FORMAT, "box": box, "at": when})


def _read_json(p: Path) -> tuple[Any, str | None]:
    """(document, None), or (None, why it was not read). A file that is not
    there is (None, None)."""
    try:
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"it could not be opened ({exc.strerror or type(exc).__name__})"
    with os.fdopen(fd, "rb") as fh:
        raw = fh.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        return None, f"it is over the {MAX_BYTES // 1024} KiB the guard reads"
    try:
        return json.loads(raw.decode("utf-8")), None
    except (UnicodeDecodeError, ValueError):
        return None, "it is not JSON"


def bound_box() -> tuple[str | None, str | None]:
    """(the box this machine last sent to, None), (None, None) when it has
    sent to none, or (None, why the record was not read)."""
    doc, why = _read_json(box_path())
    if why:
        return None, f"{BOX_FILE_NAME} was not read: {why}"
    if doc is None:
        return None, None
    box = doc.get("box") if isinstance(doc, dict) and doc.get("v") == FORMAT else None
    if not isinstance(box, str) or not box.startswith("https://"):
        return None, f"{BOX_FILE_NAME} was not read: it is not this format"
    return box, None


def read() -> tuple[dict[str, Any] | None, str | None]:
    """(the kept standing, None), (None, None) when there is none, or (None,
    why it was not read): over MAX_BYTES, not JSON, or not this format."""
    doc, why = _read_json(path())
    if why or doc is None:
        return None, why
    try:
        if not isinstance(doc, dict) or doc.get("v") != FORMAT:
            raise StandingError("not this format")
        box = doc.get("box")
        if not isinstance(box, str) or not box.startswith("https://"):
            raise StandingError("it names no https box")
        fetched = datetime.fromisoformat(str(doc.get("fetched_at")))
        checked = normalize({**doc, "person": doc.get("figures", {}).get("person"),
                             "teams": doc.get("figures", {}).get("teams"),
                             "org": doc.get("figures", {}).get("org")},
                            box=box, person=str(doc.get("person")), fetched_at=fetched)
    except (StandingError, AttributeError, TypeError, ValueError) as exc:
        return None, f"it is not a standing this version reads ({exc})"
    return checked, None


def usable(doc: dict[str, Any] | None, why: str | None, *, person: str | None,
           month: str) -> str | None:
    """None when the box's figures in `doc` count for `person` in `month`,
    else why not, in words for `nable guard doctor` and the reason."""
    if why:
        return f"{FILE_NAME} was not read: {why}"
    if doc is None:
        return (f"there is no {FILE_NAME}: `nable cloud status --box https://HOST` reads the "
                "box's standing")
    if doc["month"] != month:
        return (f"{FILE_NAME} is for {doc['month']}, not this UTC month ({month}); run "
                f"`nable cloud status --box {doc['box']}` again")
    if not person:
        return (f"{FILE_NAME} is for {doc['person']}, and nobody is named on this machine "
                "(FINOPS_GUARD_PERSON, or git's user.email)")
    if doc["person"] != person:
        return (f"{FILE_NAME} is for {doc['person']}, and this machine's agents spend for "
                f"{person}; run `nable cloud status --box {doc['box']} --as {person}`")
    bound, bound_why = bound_box()
    if bound_why:
        return bound_why
    if bound is not None and bound != doc["box"]:
        return (f"{FILE_NAME} is from {doc['host']}, and this machine last sent its usage to "
                f"{bound.split('://', 1)[-1]}; run `nable cloud status --box {bound}`")
    return None


def for_gate(person: str | None, month: str) -> tuple[dict[str, Any] | None, str | None]:
    """(the standing the gate may use, None), or (None, why it may not)."""
    doc, why = read()
    reason = usable(doc, why, person=person, month=month)
    return (None, reason) if reason else (doc, None)


def _dollars(v: Any) -> Decimal | None:
    if not isinstance(v, str):
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


def figure(doc: dict[str, Any], scope: str, subject: str) -> Decimal | None:
    """The box's figure for one budget the guard holds this machine to: the
    person's month to date (scope person or per_person), the team's (scope
    team, subject team:ID, matched without regard to case), or the org's.
    None when the box has none for it (no records received, or a team it
    does not list)."""
    f = doc["figures"]
    if scope in ("person", "per_person"):
        return _dollars(f["person"]["usd"])
    if scope == "team":
        want = subject.split(":", 1)[-1].strip().lower()
        for t in f["teams"]:
            if t["team"].strip().lower() == want:
                return _dollars(t["usd"])
        return None
    if scope == "org":
        return _dollars(f["org"]["usd"])
    return None


def age_hours(doc: dict[str, Any], *, now: float | None = None) -> float | None:
    """How old the box's figures are: from its as_of, else from when they
    were fetched."""
    at = doc.get("as_of") or doc.get("fetched_at")
    try:
        t = datetime.fromisoformat(str(at)).timestamp()
    except ValueError:
        return None
    return max((now_epoch() if now is None else now) - t, 0) / 3600
