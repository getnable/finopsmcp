#!/usr/bin/env python3
"""Weekly read: the two numbers that actually matter.

Prints PyPI downloads (with mirror traffic and with it stripped) and, when
PostHog credentials are present, the activation count (distinct people who fired
the `provider_connected` event). The download number flatters and predicts
little; the activation number is the one that turns into revenue. Run it weekly.

Usage:
    python3 scripts/weekly-read.py

To include the activation number, also set (the host defaults to us.i.posthog.com):
    POSTHOG_PROJECT_ID=12345 \\
    POSTHOG_PERSONAL_API_KEY=phx_... \\
    python3 scripts/weekly-read.py

Get those in PostHog: Project ID is in Settings -> Project; the personal API key
is Settings -> Personal API keys -> create one with the "query" scope. This is
NOT the phc_ ingestion key, which cannot read data.

Downloads come from pypistats.org (public, no auth). "Mirrors stripped" removes
only the bandersnatch full-index mirror. CI runs, Docker builds, and uvx
cold-starts still count, so treat even that figure as an upper bound on humans.
"""
import json
import os
import subprocess
from datetime import date, timedelta

PKG = os.environ.get("PYPI_PACKAGE", "finops-mcp")
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com").rstrip("/")
POSTHOG_PROJECT_ID = os.environ.get("POSTHOG_PROJECT_ID", "")
POSTHOG_API_KEY = os.environ.get("POSTHOG_PERSONAL_API_KEY", "")
ACTIVATION_EVENT = os.environ.get("ACTIVATION_EVENT", "provider_connected")

# The connect funnel in order, so we can see exactly where people drop before
# they activate. These are the events setup_wizard.py emits.
FUNNEL_STEPS = [
    ("aws_connect_opened", "opened the connect flow"),
    ("ambient_probe_done", "probed for existing AWS creds"),
    ("ambient_confirmed", "accepted a detected profile"),
    ("no_ambient_creds", "no creds found, sent to manual"),
    ("ambient_declined", "declined the detected profile"),
    ("connect_attempted", "attempted a connection"),
    ("verify_failed", "connection verify FAILED"),
    ("provider_connected", "CONNECTED  <- activation"),
]


def _get_json(url, headers=None, data=None):
    # Shell out to curl: it uses the system trust store, which avoids the macOS
    # Python "CERTIFICATE_VERIFY_FAILED" issue with the stdlib HTTPS client.
    cmd = ["curl", "-fsS", "--max-time", "30"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["--data-binary", data.decode() if isinstance(data, bytes) else data]
    cmd.append(url)
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"curl exit {out.returncode}")
    return json.loads(out.stdout)


def downloads():
    recent = _get_json(f"https://pypistats.org/api/packages/{PKG}/recent")["data"]
    # The default overall endpoint returns BOTH with_mirrors and without_mirrors
    # daily rows, so they window identically and compare apples to apples (the
    # without total is always <= the with total over the same dates).
    overall = _get_json(f"https://pypistats.org/api/packages/{PKG}/overall")["data"]
    today = date.today()
    window = {(today - timedelta(days=i)).isoformat() for i in range(1, 31)}
    with_m = sum(r["downloads"] for r in overall
                 if r["category"] == "with_mirrors" and r["date"] in window)
    without_m = sum(r["downloads"] for r in overall
                    if r["category"] == "without_mirrors" and r["date"] in window)
    return recent, with_m, without_m


def activations():
    if not (POSTHOG_PROJECT_ID and POSTHOG_API_KEY):
        return None

    def count(days):
        sql = (
            f"SELECT count(DISTINCT person_id) FROM events "
            f"WHERE event = '{ACTIVATION_EVENT}' "
            f"AND timestamp > now() - INTERVAL {days} DAY"
        )
        body = json.dumps({"query": {"kind": "HogQLQuery", "query": sql}}).encode()
        res = _get_json(
            f"{POSTHOG_HOST}/api/projects/{POSTHOG_PROJECT_ID}/query",
            headers={
                "Authorization": f"Bearer {POSTHOG_API_KEY}",
                "Content-Type": "application/json",
            },
            data=body,
        )
        return res["results"][0][0]

    return count(7), count(30)


def funnel(days=30):
    if not (POSTHOG_PROJECT_ID and POSTHOG_API_KEY):
        return None
    events = ", ".join(f"'{e}'" for e, _ in FUNNEL_STEPS)
    sql = (
        f"SELECT event, count(DISTINCT person_id) FROM events "
        f"WHERE event IN ({events}) AND timestamp > now() - INTERVAL {days} DAY "
        f"GROUP BY event"
    )
    body = json.dumps({"query": {"kind": "HogQLQuery", "query": sql}}).encode()
    res = _get_json(
        f"{POSTHOG_HOST}/api/projects/{POSTHOG_PROJECT_ID}/query",
        headers={
            "Authorization": f"Bearer {POSTHOG_API_KEY}",
            "Content-Type": "application/json",
        },
        data=body,
    )
    return {row[0]: row[1] for row in res.get("results", [])}


def main():
    print(f"\n  Weekly read . {PKG} . {date.today().isoformat()}")
    print("  " + "-" * 52)
    try:
        recent, with_m, without_m = downloads()
        mirror_pct = round(100 * (with_m - without_m) / with_m) if with_m else 0
        print("  PyPI downloads (pypistats default, the bandersnatch mirror already excluded):")
        print(
            f"    day {recent['last_day']:>7,}   "
            f"week {recent['last_week']:>7,}   "
            f"month {recent['last_month']:>8,}"
        )
        print("  Raw total including that mirror (last 30 days, same window):")
        print(
            f"    with mirror {with_m:>8,}   vs without {without_m:>8,}"
            f"   ({mirror_pct}% of all download events is the one mirror)"
        )
        print("    So the headline number already strips the mirror. It still counts CI,")
        print("    Docker, and uvx cold-starts, so treat it as an upper bound on real humans.")
    except Exception as e:  # noqa: BLE001
        print(f"  downloads: could not fetch ({e})")

    print()
    acts = None
    try:
        acts = activations()
    except Exception as e:  # noqa: BLE001
        print(f"  activations: query failed ({e})")
    if acts is not None:
        print(f"  Activations '{ACTIVATION_EVENT}' (the number that matters):")
        print(f"    week {acts[0]:>7,}   month {acts[1]:>8,}")
    else:
        print("  Activations: set POSTHOG_PROJECT_ID + POSTHOG_PERSONAL_API_KEY to show")
        print(f"    distinct '{ACTIVATION_EVENT}'. This is the real read; the rest is noise.")

    try:
        fn = funnel(30)
    except Exception as e:  # noqa: BLE001
        fn = None
        print(f"  funnel: query failed ({e})")
    if fn:
        print()
        print("  Connect funnel (distinct people, last 30 days):")
        top = fn.get("aws_connect_opened", 0) or 1
        for ev, label in FUNNEL_STEPS:
            c = fn.get(ev, 0)
            print(f"    {c:>5,}  {round(100 * c / top):>3}%  {label}")
        conn = fn.get("provider_connected", 0)
        print(f"    => {round(100 * conn / top)}% of people who opened the flow connected")
    print()


if __name__ == "__main__":
    main()
