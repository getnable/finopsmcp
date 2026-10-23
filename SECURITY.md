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
`authority.yaml` goes only to a box whose status lists
`org_file:authority.yaml` in `accepts`; to any other box the send is refused
before a byte leaves, and `--without-org-file NAME` (repeatable) leaves a
named file out, shown in the dry run and covered by the confirm code, since
what leaves is exactly what was shown.
`nable cloud status` also POSTs exactly `{"person": P}` to
`/api/free-tool/standing` when the box's status lists `standing` in `reads`:
P is `--as WHO`, else `FINOPS_GUARD_PERSON`, else git's `user.email`, the
person the guard holds this machine's agents to, and it is sent only when it
is an email or `github:LOGIN` of at most 320 characters; every answer says
who was asked. Nothing else is in that request. The answer is kept in
`cloud-standing.json` beside the ledger (0600), without the key, only when
it is about the guard's own person and from the box and the workspace this
machine last sent its usage to (the box's opaque workspace reference, kept in
`cloud-box.json` at each send): on a pooled box one URL serves every
workspace, so a standing read with another workspace's key is never used.
The guard reads that file and never the network, and its ledger lines never
carry the box's figure, host or times, since the ledger can be sent to
another workspace.
The status answer may also carry the workspace's effective rates (a `rates`
block: per EC2, RDS and ElastiCache usage type and region, what the
workspace's own bill paid against the public on-demand price, as factors).
`nable cloud status` and a send keep it in `cloud-rates.json` beside the
ledger (0600), without the key, only when it names the workspace its status
names and comes from the box and the workspace this machine last sent to; a
block that is not exactly the shape this version reads is refused whole and
said. The guard reads that file on a priced call only, never the network,
and uses it only for the price it shows: every verdict, threshold, velocity
cap and budget is judged at list price as before, and a price book's figure
stands where it prices the SKU. A ledger line records what was priced, on
which basis it was shown and, at an effective rate, that figure with its
factor, window, as_of and the workspace reference it came from. When a person
approved a priced create and it ran, the post hook's outcome line records the
resource ids the tool printed (EC2 instance ids, RDS and ElastiCache ARNs,
terraform's `[id=...]` in those forms; at most 20), raw, so the workspace can
find them on its bill; they stay in this machine's ledger and leave only in
the lines a send carries, and the dry run counts the lines that name them.
Nothing on the priced path reads the AWS CLI's config file: a command that
names no region is shown at list price. The
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
unless you send the org model. The month to date the guard compares is read
from the coding agents' own logs (Claude Code's transcripts under its projects
directory, Codex's rollouts), which are not among the guard's own files: an
agent that rewrites or removes them outside the guard's view lowers this
machine's figure, and the guard does not ask before it does. A response two
transcripts hold counts once, the larger copy, and usage already sent to a box
stays there. `nable guard doctor` says so. Asking before an agent writes those
transcripts (D118) is not built in this release: the guard's purchase path
changed in it, and the cost of asking on every transcript write is measured
first.

**Purchase authority in the org model.** `authority.yaml` says, per purchase
kind a paid workspace's reservation agent buys (`rds_ri`, `elasticache_ri`,
`database_savings_plan`), its rung (`off`, `recommend` or `approve_each`), the
most a plan may commit to it, who may approve it (`email:` entries) and when
the entry expires (within 366 days). A person writes it (`nable org set
authority`, at a terminal or with `--as`); no adapter, pack, MCP tool or
agent proposal may. It is one of the guard's own files in every org dir
(a repo's `nable.org/`, the data dir's model, `FINOPS_ORG_DIR`): a write,
move, truncation or deletion by an agent asks, by tool and by shell, and the
reason names it. A workspace applies the stricter of the file and its own
opt-in, so the file can only narrow, and an entry nable cannot read holds its
kind (or every kind) at recommend rather than being skipped. Nothing on this
machine acts on it.
A git command that can rewrite the org model's files without naming them
asks: a merge, pull, rebase, cherry-pick, revert, am, apply or hard reset,
read-tree -u, checkout-index of all files, a bisect that checks a commit
out, sparse-checkout, submodule update, merge-index, mergetool or
filter-branch whenever the repo holds org files, and a branch switch (`git
checkout BRANCH` or `COMMIT`, `git switch [--detach] REF`) when the org
directory's tree differs between HEAD and the target, naming the files, or,
forced, when the files on disk differ from the target's. A switch through
another git directory, index or work tree (`--git-dir`, `GIT_DIR`,
`GIT_WORK_TREE`, `GIT_INDEX_FILE`, `-c core.worktree`) asks wherever that
work tree holds org files. An alias is judged as what it runs; a shell
alias, one the guard cannot read and a command git does not ship ask
wherever the repo holds org files. The guard reads the repo's own objects,
refs and config for that in process, loose and packed, under its meter and
without waiting on any file: it runs no git and opens no connection. When
it cannot tell (a ref it cannot resolve, a missing object, a repo that
replaces objects, the meter spent) it asks whenever the repo holds org
files. What git itself runs for you (a hook, a filter or a merge driver in
its config) is not seen, and `nable guard doctor` says so.

**Reads that never wait.** Every file the guard hook and its post hook read
is opened without waiting (`O_NONBLOCK`) and read only when it is a regular
file, at a size cap: the org model's files and its git metadata, packs and
their index, the ledger and its anchor, `cloud-rates.json` and the box
standing, `budgets.yaml` and the budget summary, `nable.policy.yaml`,
approvals, the AI budget tally and the coding agents' transcripts, the
committed price tables, adapter configs and settings. A FIFO, device or
socket an agent puts in place of one is reported as not read, never as zero
and never as a hook that runs out of time. `tests/test_guard_reads_ratchet.py`
holds every module the guard can import (its static import closure, imports
inside functions included) to that, and every write in it to an allow-list
with its reason. That includes the AI budget gate's Codex session metadata,
token totals and rollout files and its Cursor usage cache (`harness_usage`),
the background refresh's state file (`background_refresh`) and the vault's
machine id and key file (`security.vault`): no read on the hook path is left
to convert.

**Authority for classes that move no money.** `authority.yaml` can hold
tagging, tickets and off-hours schedules at a rung a person wrote; `bounded`
lets a call through without an ask only inside a bound the file names, only
from a confirmed entry in a trusted org model, and never for a purchase kind.
A paid workspace's standing (`authority-standing.json`) is kept only from the
box and workspace this machine is bound to and only when it names that
workspace, a block of at most 4 KiB written 0600 and atomically, and read
through `read_regular`; it can only narrow a rung, and a standing that was not read
holds `bounded` at approve_each. It is one of the guard's own files, so an
agent's write of it asks. A class call's ledger line carries what the call
acted on (`authority`), each string redacted like the command and the whole
at most 4,096 characters, and never the approvers. A class call is read
wherever the shell runs it (a loop, a conditional, a subshell, `eval`, an
MCP server's shell form), and a command too long or too odd to split is
matched by linear scans, so padding never runs the hook past its timeout. A
bound is never read from what the guard cannot see: a ticket whose
repository gh would pick from more than one remote, from `GH_REPO`, or after
a `cd` or a remote change in the command is outside a bound that names
repositories, and an off-hours stop whose AWS account (from `--profile`,
`AWS_PROFILE`, `AWS_DEFAULT_PROFILE` or the default profile) cannot be
pinned to a confirmed environment is outside its bound. A scheduled
Auto Scaling action that sets a capacity above zero is a capacity change,
never in a class.

**Data directory.** `FINOPS_DATA_DIR` is read by one helper: a leading `~`
expands, and a relative path is refused, so every part of nable (the vault,
the ledger, the guard's own files) uses the default directory instead, and
the guard protects the directory nable writes. A command says so once on
stderr; the guard hook never fails over it; `nable guard doctor` names the
value and the directory used.

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
