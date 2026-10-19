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
  attached, whether it runs in a container, and the age of the install id;
- two usage counts, only under a consent that named them: a yes to the
  consent prompt, which names them (the consent prompt names them from
  0.13.0), or `NABLE_TELEMETRY=1`. A yes given to the prompt before it named
  them covers the rest of this list and not these, and that person is asked
  once more, by the same rules. `commitments_status_run`, once per `nable commitments status` that read or
  tried to: its exit class (0, 1 or 2), how many commitments were read, how
  many of those end within 90 days, how many services were not read, and
  whether `--json` was on. `guard_commitment_asks`: how many guard ledger
  records asked before buying a commitment (decision ask, action type
  purchase_commitment) since the last count sent, and how many days that
  count covers. It is counted from the local ledger at most once a day, from
  a CLI or MCP start, and never from inside the guard hook, which loads no
  telemetry; without consent the ledger is not read for it. Each event is a
  closed list of integers, yes/no flags and fixed values, so no account id,
  ARN, dollar figure, command, path or resource name can travel in one.

It never carries cost figures, account IDs, resource names, file paths,
hostnames, usernames, credentials or your query text.

**Cloud connect, only when you send it.** `nable cloud connect` carries the
org model files in your org dir and the guard ledger's lines, exactly as
stored, to a paid workspace you name with `--box https://HOST`. Without
`--send` it is a dry run that opens no connection: it prints the destination
host, the files, the record counts, the size, the sha256 and a confirm code,
and saves the exact bytes (0600) for you to read. `--send --confirm CODE`
sends those bytes and nothing else to that box: the code is the sha256 of the
box, a newline and the bytes, so it refuses when they changed since you
looked or when the dry run was for another box. A send is a person's
decision: without a terminal it refuses unless `--as WHO` names who decided,
and the guard asks before an agent runs `--send`, `--out` or `nable cloud
status`. `--out` never writes one of the guard's own files, nable's data
directory, or anything but a regular file of yours. It POSTs to
`https://HOST/api/free-tool/connect` and reads `/api/free-tool/status`, over
HTTPS only, follows no redirect, and is refused under `FINOPS_AIRGAP=1`.
`--delete-org-file NAME` adds one `org_file_delete` line per file, carrying
the file name alone, shown in the dry run and covered by its confirm code.
`nable cloud status` also POSTs exactly `{"person": P}` to
`/api/free-tool/standing` when the box's status lists `standing` in `reads`:
P is `--as WHO`, else `FINOPS_GUARD_PERSON`, else git's `user.email`, the
person the guard holds this machine's agents to. Nothing else is in that
request. The answer is kept in `cloud-standing.json` beside the ledger
(0600), without the key; the guard reads that file and never the network. The
workspace key is read from `NABLE_WORKSPACE_KEY`, a `--key-file` with mode
0600, or a hidden prompt; it is never an argument, and it is never stored,
printed or logged. No host is built in, and nable adds nothing about the
machine (its name, your user name) of its own. The ledger's records are the
agent's commands and the paths the guard judged, as recorded, with
credentials redacted and nothing else changed, so they can name absolute
paths, hosts and user names (`ssh deploy@db1.internal`, `/home/you/...`);
the dry run counts the records that do and points at the saved file.

It also carries this machine's coding-agent usage, as `agent_usage` records:
one per install id, UTC day, developer, agent, model and project, for the
closed days of the last 28 (fewer when Claude Code's `cleanupPeriodDays` keeps
transcripts for less, so no day is sent short), with the tokens by direction, the
list-price-equivalent dollars for tokens of a model `llm_prices` has a cited
row for (`price_source` names the row), and the count of tokens it does not
price. The install id is 128 random bits made once and kept beside the ledger;
the developer is the sender (`--as WHO`, else at a terminal git's
`user.email`, then `$USER`); the project is the repo's identity (its remote's
slug) only when your org model names that repo in a repo_path fact, else
`unmapped`. No prompt, response, file content, path, working directory, host
name or machine id is in a record, and a model logged as a path is sent as
`unknown`. Without a terminal and without `--as` there is no developer and no
record; `--no-agent-usage` leaves them out. They are in the dry run's bytes and
covered by its confirm code like everything else.

**AI budgets in the org model.** `budgets.yaml` is an org model file like the
others: the guard asks before an agent writes, moves or deletes it, in a
repo's `nable.org/`, in nable's data directory and at `FINOPS_ORG_DIR`. The
person a budget is matched by is `FINOPS_GUARD_PERSON`, else git's
`user.email`, which the guard reads by running `git config --get user.email`
once and keeps in `guard-person.json` beside the ledger (another of its own
files) until git's config files change. The guard asks before an agent runs
`git config` to change `user.email`, or to write `include.path` or an
`includeIf.<condition>.path` (either can bring in another file's `[user]`),
and before an agent writes, moves or deletes a file git reads it from: the
repo's `.git/config` (and a worktree's), `~/.gitconfig`, the XDG
`git/config`, `GIT_CONFIG_GLOBAL` and the system file are among its own files.
As a backstop, when git's email for a repo changes since the guard last read
it, the next call asks once, naming both, and the earlier email's budgets still
apply for the rest of that UTC month. Nothing about a budget leaves the machine
unless you send the org model.

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
