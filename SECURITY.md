# Security Policy

nable is local-first by design: it runs on your machine, your cloud
credentials stay in your OS keyring, cost data caches in a local SQLite
database, and there is no nable backend that receives either. The security
architecture is documented at https://getnable.com/security.

Besides the provider APIs you connect (AWS, GCP, Azure, OpenAI and so on),
these are the outbound paths, named directly.

**Usage telemetry is off by default (opt-in).** Nothing is sent unless you
answer yes to the one-time question the CLI asks after a command, or set
`NABLE_TELEMETRY=1`. `NABLE_NO_TELEMETRY=1`, `DO_NOT_TRACK=1` and
`FINOPS_AIRGAP=1` keep it off even if something else opts in, and CI runners
never send. `nable doctor` shows the current state. Until telemetry is on,
the only thing written for it is your answer to that question
(`~/.config/finops/.telemetry`, `yes` or `no`), so it is asked once. The random
install id file (`~/.config/finops/.install_id`) is created only once you opt
in. An opted-in
event goes to PostHog (`us.i.posthog.com`, IP dropped server-side) and carries
only:

- a random install id (a UUID, not derived from you or the machine) and the date;
- the event name, for example `tool_called` with the tool's name, or a setup
  step with the provider's name (`aws`, `openai`) and an error class when a
  connect failed;
- for `nable scan`, whether it ran on demo data, how many providers it covered,
  how long it took and, when it failed, the error class, exception type and
  the source line it failed at;
- in the periodic heartbeat, your plan (free, trial, pro), how many providers
  are connected, how many distinct tools were used and a hash of their names;
- runtime facts: nable version, Python major.minor, OS family, install method
  (uvx, pipx, venv, docker, system), environment kind, whether a terminal is
  attached, whether it runs in a container, and the age of the install id.

It never carries cost figures, account IDs, resource names, file paths,
hostnames, usernames, credentials or your query text.

**Version check.** To tell you when your build is out of date, nable makes one
GET to `https://pypi.org/pypi/finops-mcp/json` when `nable scan`, the first-run
welcome or the MCP server starts (at most once per process, 2 second cap). It
sends nothing about you beyond what any HTTPS request carries. Turn it off with
`FINOPS_NO_UPDATE_CHECK=1` or `NABLE_NO_UPDATE_CHECK=1`; `NABLE_NO_TELEMETRY=1`,
`DO_NOT_TRACK=1` and `FINOPS_AIRGAP=1` also disable it.

**Sign-in and the setup email, both only when you ask.** `nable login` POSTs
your email to `https://getnable.com/api/account/send-code`, then the email and
the code you type to `https://getnable.com/api/account/verify-code`, which
returns your license key. After `nable setup`, an optional prompt offers the
quickstart guide by email; only if you type an address is it POSTed to
`https://getnable.com/api/subscribe`. That prompt is skipped under
`FINOPS_AIRGAP=1`.

**LLM calls you turn on.** The optional recommendation critic
(`NABLE_CRITIC_LLM=1` together with `ANTHROPIC_API_KEY`) sends a
recommendation's figures to Anthropic's API, using your key, never to a nable
server. The hosted package's AI assistant (the Slack bot and the dashboard Ask
tab) does the same with your question and its results; the open-source package
ships neither.

## Reporting a vulnerability

Email **chandan@nable.sh** with the details. Please include steps to
reproduce and the version (`finops --version`).

- You will get an acknowledgment within 48 hours.
- We aim to ship a fix for confirmed vulnerabilities within 14 days, faster
  for anything credential- or license-related.
- Please do not open a public issue for security reports until a fix is
  released. We will credit you in the changelog unless you prefer otherwise.

## Supported versions

Only the latest release on PyPI receives security fixes. `finops upgrade`
updates in place; pinned installs should track the latest patch release.

## Track record

Disclosed and fixed issues are documented in CHANGELOG.md, including the
retirement of v1 license keys after their signing secret appeared in public
git history (0.8.59 rotated the keypair, 0.8.61 retired v1 verification).

## Scope notes for researchers

- The MCP server runs with the invoking user's privileges by design; local
  privilege boundaries are out of scope.
- The interesting surfaces are: credential storage (`src/finops/vault*`),
  license verification (`src/finops/license.py`), the Slack bot's approval
  flow (`slack_bot/`), the account/licensing edge functions (`web/api/`),
  and the cloud-credential scoping templates (`finops setup aws
  --iam-template`).
