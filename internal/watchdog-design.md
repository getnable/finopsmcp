# Watchdog design

The watchdog is an always-on agent that watches spend versus utilization and
prepares one-click fixes. It never executes them. Cloud access is read-only.

## The one non-negotiable rule

PROPOSE-ONLY, ALWAYS. The watchdog detects waste and prepares the exact
remediation. It never mutates cloud state, opens no shell to a provider, runs
no apply. The only actor that can change anything is a human tapping approve,
and even then the apply runs through the same reviewed path a person would run
by hand. There is no auto-execute path in this design and none may be added
without an explicit, signed-off, per-action opt-in (see "Future opt-in
auto-execute" below).

## The flow

detect -> prepare -> push one-click -> human approves -> verify

1. **Detect.** A scheduled job runs the correlator continuously. The correlator
   fuses two read-only signals per resource: spend (what it costs) and
   utilization (how little it is used). It ranks resources that are
   underutilized, not merely anomalous, each carrying the dollar waste it
   represents.

2. **Prepare.** For every finding the watchdog builds a prepared remediation:
   the exact fix, ready to push. Nothing runs. A rightsizing finding carries the
   Terraform PR that `open_rightsizing_pr` would open. An idle finding carries
   the precise cleanup command as data. A commitment finding carries the
   purchase recommendation. The remediation is a description plus a callable
   handle, never an invocation.

3. **Push one-click.** The watchdog pushes a single approval card to the owner
   over the existing notification path (Slack / Teams / n8n, the same
   `src/finops/notifications` used by the anomaly and digest jobs). The card
   shows the resource, the waste, the fix, and one approve button.

4. **Human approves.** The owner taps approve. This is the only place a human is
   in the loop and the only place anything can change. Approval triggers the
   already-reviewed prepare path (for rightsizing, that means opening the PR;
   the human still merges and applies it). No approval, no change.

5. **Verify.** After the change lands, the savings verifier loop
   (`src/finops/recommendations/verifiers.py`, run by `job_auto_verify`)
   re-reads live cloud state and records the realized saving. Present means not
   yet landed and it retries. Gone or resized means the saving is booked. The
   verifier reads only. It never remediates.

## What it reuses

The watchdog is mostly wiring over parts that already exist and are already
read-only.

- **Scheduler jobs** (`src/finops/scheduler/jobs.py`). The watchdog job follows
  the exact pattern of `job_ai_monitor` / `job_credit_check`: an async check
  function, a sync `job_*` wrapper via `_run`, file-based dedup so the same
  finding alerts once, and registration in `start_scheduler` behind a cron env
  var. It reuses the single-owner scheduler lock so only one host fires.

- **Correlator inputs.** The correlator imports `analyze_rightsizing`
  (`src/finops/recommendations/rightsizing.py`) for running-but-underutilized
  resources with CPU/memory utilization plus dollar waste, and
  `scan_idle_resources` (`src/finops/cleanup/idle.py`) for zero-utilization
  idle resources with dollar waste. Both already own their boto3 / CloudWatch /
  Compute Optimizer logic. The correlator does not duplicate any of it.

- **Anomaly detection** (`src/finops/anomaly/`). The watchdog is the
  utilization-driven complement to anomaly detection. Anomaly answers "did
  spend spike?"; the correlator answers "is this resource underused right now,
  spike or not?". A future pass can cross-reference the two so a spend spike on
  an already-underutilized resource ranks higher.

- **Budget enforcer** (`src/finops/budget/`). The two-tier warn/block alerting
  is the escalation signal. A finding on an account that is over budget is worth
  pushing sooner and ranking higher.

- **PR-prep** (`open_rightsizing_pr`, `open_terraform_tag_pr`). These already
  PREPARE PRs (patch files, open a PR, mark acted_on). They do not apply
  Terraform; the human still merges and runs apply. The watchdog reuses them as
  the prepare step for rightsizing and tag findings.

- **Verifier loop** (`src/finops/recommendations/verifiers.py`). Already
  registered for rightsizing (`verify_ec2_change`) and idle cleanup
  (`verify_idle_cleanup`), already run daily by `job_auto_verify`. The watchdog
  reuses it as the verify step with no change.

## Future opt-in auto-execute (not built)

If a customer ever wants the watchdog to apply a fix without a per-finding tap,
it would be gated like this and only like this:

- **Off by default, always.** The switch ships off. Nothing auto-applies until
  the customer explicitly flips it.
- **Per action, not global.** The customer enables auto-execute for a specific
  action class (for example "release unattached EBS under $5/mo") and nothing
  else. There is no "auto-execute everything" toggle.
- **Blast-radius capped.** Each enabled action carries a hard cap: max monthly
  dollars per apply, max resources per run, protected-tag exclusion (reuse the
  existing `FINOPS_PROTECTED_TAGS` guard in `idle.py`), and a dry-run diff the
  customer signed off on.
- **Still verified.** Every auto-applied change runs through the same verifier
  loop, so a bad apply is caught by measurement.
- **Reversible and logged.** Every auto-apply is recorded and, where the
  provider supports it, reversible.

Until such a switch is explicitly signed off, the watchdog stays propose-only
and read-only. This document describes that switch so the boundary is clear. It
does not implement it, and the correlator and jobs shipped here contain no
apply path.
