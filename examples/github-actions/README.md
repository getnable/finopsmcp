# GitHub Actions examples

Copy-paste templates. None of them runs in this repo: copy one into your own
repo at `.github/workflows/` and set what its header says it needs.

## On the CLI (finops-mcp 0.13.0)

These install `finops-mcp==0.13.0` and run `nable` in a shell step, so what
they do is what `nable` does on your machine. Each one's exit codes are in its
header and in [docs/HEADLESS.md](../../docs/HEADLESS.md).

- `budget-gate.yml`: fail a PR or a weekly run when a budget in `budget.yml` is
  breached, with `nable budget ci-gate --fail-on-breach --json`. Needs no cloud
  credentials; it reads nable's cost history, so it needs a runner whose data
  directory has that history, and it fails with exit 2 (cannot check) rather
  than pass without it.
- `scheduled-scan.yml`: run `nable scan --json` every morning against an AWS
  account, through a read-only role assumed with GitHub OIDC, and keep the JSON
  as an artifact. Without the role it fails with the scan's exit 6 and says so.
- `guard-policy-check.yml`: on a PR, check that the repo's `nable.policy.yaml`
  can be used as written, and that the commands in
  `.nable/guarded-commands.txt` get the verdicts you expect. Needs no cloud
  credentials.
- `commitments-status.yml`: every Monday, list the reservations and Savings
  Plans an AWS account holds with `nable commitments status --json
  --fail-within 30` (run through `uvx --from finops-mcp==0.13.0`), keep the
  JSON as an artifact for 90 days, and fail when one ends within 30 days
  (exit 1) or a commitment read failed (exit 2, cannot check). It assumes a
  read-only role through GitHub OIDC (`NABLE_AWS_ROLE_ARN`), so no key is
  stored; the role needs `nable iam-template`'s OptionalCommitments statement.
  Free Describe calls only.

Each was checked by parsing it as YAML and running its shell steps locally
against the CLI with no cloud credentials (the `uses:` steps aside): the budget
gate with no `budget.yml` (exit 2), with no cost history (exit 2), under its
limit (0) and breached (1); the scan with no credentials (fails, exit 6, the
error document kept); the policy check with no files (passes, with notices), a
broken policy (fails), a denied command (fails), commands that only ask
(passes with a warning, fails with `FAIL_ON_ASK: "1"`); the commitments check
with no credentials (fails, exit 2, the error document kept).

## On the composite actions (pinned to 0.8.216)

- `budget-check.yml` runs `.github/actions/budget-check` at the 0.8.216 commit,
  which installs the newest finops-mcp from PyPI and posts a PR comment. It
  counts a budget with no cost data as on track, so on a fresh runner it passes
  having checked nothing; `budget-gate.yml` is the gate that fails closed.
- `cost-check.yml` runs `.github/actions/cost-check` at the same commit and
  comments the estimated monthly cost of the infrastructure changes in a PR.

Both need `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` as repo secrets, and
optionally `FINOPS_LICENSE_KEY`.
