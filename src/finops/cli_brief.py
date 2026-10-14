"""`nable brief` — the overnight run, on demand.

The scheduled job writes a brief every morning. This is the same run, triggered
by hand, plus a way to read the one that already ran.

Delivery is OFF unless `--deliver` is passed, even when NABLE_BRIEF_DELIVER lists
channels. Someone running this in a terminal to look at the output should not
discover they have posted to the team channel a fourth time this morning.
"""
from __future__ import annotations

import json
import os
import sys
import webbrowser
from datetime import date
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOTHING = 0        # an empty brief is good news, not a failure


def add_parser(sub) -> None:
    p = sub.add_parser(
        "brief",
        help="Ranked, reviewed findings, with a drafted change where nable has one",
    )
    p.add_argument("--regions", nargs="+", action="extend", metavar="REGION",
                   help="scan only these regions (space or comma separated)")
    p.add_argument("--latest", action="store_true",
                   help="show the last saved brief instead of running a new scan")
    p.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    p.add_argument("--html", action="store_true",
                   help="write the brief as HTML and open it in a browser")
    p.add_argument("--deliver", action="store_true",
                   help=f"also push to the channels in $NABLE_BRIEF_DELIVER "
                        f"(off by default, even when that is set)")
    p.add_argument("--limit", type=int, default=10, metavar="N",
                   help="how many findings reach the brief (default 10)")
    p.add_argument("--no-save", action="store_true",
                   help="do not write the brief to disk")


def _print_saved(payload: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload, indent=2))
        return EXIT_OK
    print(payload.get("headline", "No brief."))
    print()
    for item in payload.get("items", []):
        amount = item.get("monthly_usd")
        money = f"  ${amount:,.0f}/mo" if amount is not None else \
                f"  {item.get('magnitude') or 'unconfirmed'}"
        print(f"{item.get('title', '')}{money}")
        print(f"    {item.get('in_words', '')}")
        fix = item.get("drafted_fix") or {}
        if fix.get("summary"):
            print(f"    fix: {fix['summary']}")
        for cmd in fix.get("commands") or []:
            print(f"       $ {cmd}")
        print()
    for gap in payload.get("gaps") or []:
        print(f"  not checked: {gap}")
    for line in payload.get("not_shown") or []:
        print(f"  not shown: {line}")
    return EXIT_OK


def run(args) -> int:
    as_json = bool(getattr(args, "json", False))

    from .briefing.run import latest, run_overnight

    if getattr(args, "latest", False):
        saved = latest()
        if saved is None:
            msg = ("No brief saved yet. Run `nable brief` to build one, or let the "
                   "scheduled overnight run produce it.")
            print(json.dumps({"error": "no_brief"}) if as_json else msg,
                  file=sys.stdout if as_json else sys.stderr)
            return EXIT_NOTHING
        return _print_saved(saved, as_json)

    from .cli_scan import _REGION_RE, _split_regions
    regions = _split_regions(getattr(args, "regions", None)) or None
    bad = [r for r in regions or [] if not _REGION_RE.match(r)]
    if bad:
        msg = f"not valid region name(s): {', '.join(bad)}"
        print(json.dumps({"error": "bad_region"}) if as_json else msg,
              file=sys.stdout if as_json else sys.stderr)
        return EXIT_ERROR

    # The audit logs one warning per check that fails; on an account it cannot
    # read that is a screen of the same line, which the brief's own "could
    # not check" section already says once. FINOPS_DEBUG=1 keeps them.
    import logging
    analyzers = logging.getLogger("finops.analyzers")
    was = analyzers.level
    if os.getenv("FINOPS_DEBUG") != "1":
        analyzers.setLevel(logging.ERROR)
    try:
        result = run_overnight(
            deliver_to=None if getattr(args, "deliver", False) else (),
            do_persist=not getattr(args, "no_save", False),
            limit=int(getattr(args, "limit", 10) or 10),
            regions=regions,
            trigger="on_demand",
        )
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return EXIT_ERROR
    except PermissionError:
        # A data directory this user cannot write: the CLI says which one and
        # how to fix it (setup_wizard.main), which "failed (PermissionError)"
        # did not. A local path, never an account id.
        raise
    except Exception as exc:
        # The type, never the message: it can carry account ids and ARNs.
        if as_json:
            print(json.dumps({"error": type(exc).__name__}))
        else:
            print(f"The overnight run failed ({type(exc).__name__}).", file=sys.stderr)
            print("Run with FINOPS_DEBUG=1 for the traceback.", file=sys.stderr)
        if os.getenv("FINOPS_DEBUG") == "1":
            raise
        return EXIT_ERROR
    finally:
        analyzers.setLevel(was)

    brief = result["brief"]
    delivered = result.get("delivered") or {}
    # --deliver is a request to push, so a push that did not land is a failed
    # run: a channel that failed, or no valid channel to push to at all. A cron
    # job sending the morning brief has only the exit code to notice that by.
    asked = bool(getattr(args, "deliver", False))
    undelivered = asked and (not delivered or not all(delivered.values()))
    code = EXIT_ERROR if undelivered else EXIT_OK
    if undelivered:
        from .briefing.run import DELIVER_ENV
        failed = [k for k, v in delivered.items() if not v]
        why = (f"delivery failed: {', '.join(failed)}" if failed else
               f"nothing was delivered: {DELIVER_ENV} names no channel "
               "(slack, teams or email)")
        print(f"nable brief --deliver: {why}", file=sys.stderr)

    if getattr(args, "html", False):
        from .briefing.render import to_html
        out = Path(result["path"]).with_suffix(".html") if result.get("path") else \
            Path.cwd() / f"nable-brief-{date.today().isoformat()}.html"
        out.write_text(to_html(brief))
        print(f"Wrote {out}")
        try:
            webbrowser.open(out.as_uri())
        except Exception:
            pass
        return code

    if as_json:
        summary = dict(result["summary"])
        if asked:
            summary["delivered"] = delivered
        print(json.dumps(summary, indent=2))
    else:
        from .briefing.render import to_markdown
        print(to_markdown(brief))
        if delivered:
            landed = ", ".join(k for k, v in delivered.items() if v) or "nothing"
            print(f"\nDelivered to: {landed}")
        if result.get("path"):
            print(f"Saved: {result['path']}")
    # An account the credentials could not read prints the fix either way. A
    # --deliver that did not land exits 1, the code a cron job sending the
    # brief checks; otherwise the unreadable account exits with `nable scan`'s
    # code for it (3 expired, 6 none).
    unreadable = _unreadable_account(brief)
    return code if code != EXIT_OK else unreadable


# AWS error codes that mean the credentials, not the account, are the problem,
# and the exit code `nable scan` gives each (cli_scan's pinned contract).
_EXPIRED_CODES = frozenset({"TokenRetrievalError", "UnauthorizedSSOTokenError",
                            "SSOTokenLoadError", "ExpiredToken", "ExpiredTokenException",
                            "RequestExpired"})
_NO_CREDS_CODES = frozenset({"NoCredentialsError", "PartialCredentialsError",
                             "InvalidClientTokenId", "UnrecognizedClientException",
                             "SignatureDoesNotMatch", "AuthFailure"})


def _unreadable_account(brief) -> int:
    """EXIT_OK, or, when the AWS audit could not read anything because of
    the credentials, the fix and the exit code `nable scan` gives the same
    failure (3 expired, 6 none or rejected). The brief above it already
    lists every check that failed; this says what to do about it."""
    from .cli_scan import EXIT_EXPIRED, EXIT_NO_CREDS
    codes = set((getattr(brief, "scanned", None) or {}).get("aws_unreadable") or [])
    if not codes or not codes <= (_EXPIRED_CODES | _NO_CREDS_CODES):
        return EXIT_OK
    profile = os.environ.get("AWS_PROFILE") or "default"
    if codes <= _EXPIRED_CODES:
        lines = ["nable brief: your AWS session has expired, so nothing was read.",
                 (f"  fix: `aws sso login --profile {profile}`  (or refresh your temporary "
                  "credentials), then run `nable brief` again")]
        code = EXIT_EXPIRED
    else:
        lines = ["nable brief: no usable AWS credentials, so nothing was read.",
                 "  fix: `aws configure sso` (company SSO) or `aws configure` (access key)",
                 "  then: `nable connect` waits and connects the moment they appear",
                 "  no account at hand? `nable scan --demo` shows nable on sample data"]
        code = EXIT_NO_CREDS
    print("\n".join(lines), file=sys.stderr)
    return code
