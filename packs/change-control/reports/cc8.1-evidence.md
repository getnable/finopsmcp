# Change-management evidence: SOC 2 CC8.1

Period: ${since} to ${until}. Generated ${generated_at} by nable from the
guard's decision ledger (report ${report} of pack ${pack}).

> ${ledger.guard.note}

## What CC8.1 asks, and what this shows

CC8.1 reads: "The entity authorizes, designs, develops or acquires,
configures, documents, tests, approves, and implements changes to
infrastructure, data, software, and procedures to meet its objectives."

This report speaks to the authorize and approve parts of that criterion for
one population: infrastructure changes that coding agents attempted where
nable's guard runs. For each one it shows what was attempted, what the guard
decided, who approved it and when, and under which policy (the policy
fingerprint, the gate rule, pack rules and any change freeze in force).

It does not show changes made where the guard was not running (a person's own
terminal, a CI pipeline without the guard, a cloud console), and it says
nothing about how changes were designed, tested or documented. Pair it with
the change tickets and pull request reviews that cover those.

## Integrity of the record

${ledger.guard.tables.ledger}

Ledger file: ${ledger.guard.ledger.path}. Head hash: ${ledger.guard.ledger.head}.
Copy the head hash somewhere the agent cannot write (a ticket, a commit, a log
shipper): a later check that finds the same hash at the same record shows
nothing before it was edited, removed or cut from the end.

## Summary

| Verdicts in the period | Count |
|---|---|
| Recorded verdicts | ${ledger.guard.counts.verdicts} |
| Allowed by policy without a question | ${ledger.guard.counts.allowed_by_policy} |
| Asked a person | ${ledger.guard.counts.asked} |
| Approved at the agent's prompt | ${ledger.guard.counts.approved} |
| Declined | ${ledger.guard.counts.declined} |
| Answer unknown | ${ledger.guard.counts.unknown} |
| Asked where the harness cannot ask, then approved from a terminal | ${ledger.guard.counts.approved_later_out_of_band} |
| Asked where the harness cannot ask, and not approved (did not run) | ${ledger.guard.counts.not_run} |
| Calls let through once by a person's approval from their own terminal | ${ledger.guard.counts.allowed_out_of_band} |
| Denied by policy | ${ledger.guard.counts.denied} |
| Judged under a change freeze | ${ledger.guard.counts.under_freeze} |
| Let through unexamined by a guard error | ${ledger.guard.counts.not_examined} |

## Changes: who approved what, when, under which policy

${ledger.guard.tables.changes}

"Approved at the agent's prompt" means the ask ran after a person answered
the agent harness's permission prompt. The harness does not record who
answered, so the approver column names the prompt, not a person: name the
approver in the change ticket. An approval from a person's own terminal
(`nable guard approve`) names who nable recorded.

## Change freezes that overlapped the period

${ledger.guard.tables.freezes}

## Approval chains in the org model (as of ${generated_at})

${ledger.guard.tables.approval_chains}

## Exceptions for review

${ledger.guard.tables.exceptions}

An exception is a question for a reviewer, not a finding about the control.
The same records, with every field, come as JSON from
`nable pack report ${pack} cc8.1-evidence --json`.
