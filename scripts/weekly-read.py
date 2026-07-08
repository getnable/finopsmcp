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
from pathlib import Path


def _load_env_local() -> None:
    """Read KEY=VALUE lines from the repo's .env.local into the environment so
    you can drop POSTHOG_PROJECT_ID / POSTHOG_PERSONAL_API_KEY there once instead
    of exporting them on every run. Real env vars already set win over the file.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env.local"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and val and key not in os.environ:
            os.environ[key] = val


_load_env_local()

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


def _hogql(sql):
    body = json.dumps({"query": {"kind": "HogQLQuery", "query": sql}}).encode()
    res = _get_json(
        f"{POSTHOG_HOST}/api/projects/{POSTHOG_PROJECT_ID}/query",
        headers={"Authorization": f"Bearer {POSTHOG_API_KEY}", "Content-Type": "application/json"},
        data=body,
    )
    return res.get("results", [])


def timeline():
    """When the 545 actually arrived. Recovers each machine's first-seen day
    (earliest of any event) and buckets it, so you can lay the install curve over
    your own memory of what you shipped when. Channel isn't tagged, but a spike on
    the day you posted to HN is HN. Timestamps are day-granular by design, so this
    is daily resolution, which is all campaign correlation needs.
    """
    if not (POSTHOG_PROJECT_ID and POSTHOG_API_KEY):
        return None
    run = ("'heartbeat', 'tool_called', 'provider_connected'")
    # Weekly new-install cohorts (first-seen bucketed by ISO week).
    weekly = _hogql(
        "SELECT toStartOfWeek(first_day) AS wk, count() AS n FROM ("
        f"  SELECT person_id, min(toDate(timestamp)) AS first_day FROM events "
        f"  WHERE event IN ({run}) GROUP BY person_id"
        ") GROUP BY wk ORDER BY wk"
    )
    # The biggest single arrival days, the ones worth matching to a campaign.
    spikes = _hogql(
        "SELECT first_day, count() AS n FROM ("
        f"  SELECT person_id, min(toDate(timestamp)) AS first_day FROM events "
        f"  WHERE event IN ({run}) GROUP BY person_id"
        ") GROUP BY first_day ORDER BY n DESC, first_day DESC LIMIT 8"
    )
    # Every day a machine first connected a provider (only a handful, list them).
    connects = _hogql(
        "SELECT first_day, count() AS n FROM ("
        "  SELECT person_id, min(toDate(timestamp)) AS first_day FROM events "
        "  WHERE event = 'provider_connected' GROUP BY person_id"
        ") GROUP BY first_day ORDER BY first_day"
    )
    return {"weekly": weekly, "spikes": spikes, "connects": connects}


def reach(days=30):
    """The denominator the download count can't give you: distinct machines that
    actually RAN nable (fired a heartbeat) and that actually USED a tool, vs the
    ones that connected a provider. The gap between ran-it and connected is the
    real activation wall. Every server start fires a 'heartbeat'; 'tool_called'
    means they invoked at least one tool; 'provider_connected' is activation.
    CI and opted-out installs never send these, so this is humans, not mirrors.
    """
    if not (POSTHOG_PROJECT_ID and POSTHOG_API_KEY):
        return None
    events = ("heartbeat", "tool_called", ACTIVATION_EVENT)
    in_list = ", ".join(f"'{e}'" for e in events)

    def counts(window_days):
        sql = (
            f"SELECT event, count(DISTINCT person_id) FROM events "
            f"WHERE event IN ({in_list}) "
            f"AND timestamp > now() - INTERVAL {window_days} DAY GROUP BY event"
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

    # All-time uses a wide window; PostHog free retention caps it anyway.
    return {"month": counts(days), "all": counts(3650)}


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
        tl = timeline()
    except Exception as e:  # noqa: BLE001
        tl = None
        print(f"  timeline: query failed ({e})")
    if tl:
        print()
        print("  When they arrived (new machines by first-seen day; correlate to what you shipped):")
        if tl["weekly"]:
            peak = max(r[1] for r in tl["weekly"]) or 1
            for wk, n in tl["weekly"]:
                bar = "#" * round(24 * n / peak)
                wk_s = str(wk)[:10]
                print(f"    week of {wk_s}  {n:>4}  {bar}")
        if tl["spikes"]:
            print("  Biggest arrival days (match these to a post/send):")
            for day, n in tl["spikes"]:
                print(f"    {str(day)[:10]}  {n:>4} new")
        if tl["connects"]:
            print("  Days a machine first connected a provider:")
            for day, n in tl["connects"]:
                print(f"    {str(day)[:10]}  {n}")

    try:
        rc = reach(30)
    except Exception as e:  # noqa: BLE001
        rc = None
        print(f"  reach: query failed ({e})")
    if rc:
        print()
        print("  Reach vs activation (distinct machines, humans only, CI stripped):")
        for scope, label in (("month", "last 30 days"), ("all", "all time")):
            r = rc[scope]
            ran = r.get("heartbeat", 0)
            used = r.get("tool_called", 0)
            conn = r.get(ACTIVATION_EVENT, 0)
            pct = f"{round(100 * conn / ran)}%" if ran else "n/a"
            print(f"    {label:>13}:  ran {ran:>5,}   used a tool {used:>5,}   "
                  f"connected {conn:>5,}   ({pct} of runners activated)")
        print("    'ran' is the honest denominator the download count can't give you.")
        print("    ran -> connected is the activation wall; that gap is the work.")

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
