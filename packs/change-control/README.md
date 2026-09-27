# Change control (SOC 2)

`io.github.getnable/change-control` turns nable's guard into a change-control
seatbelt for coding agents, and its decision ledger into change-management
evidence mapped to SOC 2 CC8.1.

The evidence is evidence, not a certification. It shows what nable's guard
saw and decided where it is installed. It cannot show changes made where the
guard was not running, and an auditor decides what it demonstrates.

## What it does

**Guard rules during a change freeze** (`guard/change-window.yaml`). While a
freeze in the org model covers what a command touches, Kubernetes changes,
Helm upgrades, infrastructure applies and merges to release branches ask, and
teardowns (`terraform destroy`, `helm uninstall`, deleting a namespace or a
stack) are denied. Outside a freeze these rules do nothing. A freeze nobody
confirmed (a proposal, or one from a repo nobody trusted) only ever asks,
even for the deny rule. The guard's reason names the freeze, why, and when it
ends.

**Guard rules, always** (`guard/branch-protection.yaml`). An agent never
merges with `--admin`, force-pushes a protected branch, or changes branch
protection or rulesets. It may propose those changes; a person makes them.

Every rule only tightens: allow to ask, ask to deny, never the other way.

**Freeze-window templates** (`reports/freeze-windows.yaml`). Year-end,
quarter-close and a peak-season production freeze, as org facts with status
`proposed`:

```
nable pack report io.github.getnable/change-control freeze-windows \
  --set year=2026 --set next_year=2027 --set offset=-05:00
```

Move the dates to your own calendar, add the entries you want to
`nable.org/freezes.yaml`, and confirm them with `nable org review` and
`nable org confirm KEY`. Until a person confirms one, it only makes the guard
ask.

**Approval chains from GitHub** (adapter `approval-chains`). Reads
CODEOWNERS and, if you give them, the branch protection and deployment
environment settings exported with your own `gh`:

```
gh api repos/OWNER/REPO/branches/main/protection > bp.json
gh api repos/OWNER/REPO/environments > envs.json
nable pack run io.github.getnable/change-control approval-chains \
  --context repo=. --context branch_protection=bp.json --context environments=envs.json
```

Each CODEOWNERS rule that names a team proposes an `approval` fact for that
team (its owners approve its changes, `min` from the required review count).
A deployment environment with required reviewers proposes one for the nable
environment it maps to (production to prod, staging to nonprod). Everything
it returns is a proposal: nable writes it with status `proposed` and a source
that starts with the pack id, and a person confirms it. `nable org init`
runs it too, in the directory it is started from.

**CC8.1 evidence** (`reports/cc8.1-evidence.md`). From the guard ledger:
whether the hash chain verifies (and against the last anchor), then every
change the guard asked about, denied or let through unexamined, with who
approved it, when, how, and under which policy (the policy fingerprint, the
gate rule, pack rules and any change freeze), the freezes and approval chains
in the org model, and exceptions for review from `policies/change-records.yaml`.

```
nable pack report io.github.getnable/change-control cc8.1-evidence --since 90d
nable pack report io.github.getnable/change-control cc8.1-evidence --since 90d --json --out evidence.json
```

An ask approved at the agent's permission prompt is shown as exactly that:
the harness does not record who answered, so the report says so and flags
it for the change ticket. An approval given with `nable guard approve` names
who nable recorded.

**Change tickets** (`reports/change-ticket.md`). One markdown ticket per
change, to paste into your tracker:

```
nable pack report io.github.getnable/change-control change-ticket \
  --each ledger.guard.changes --since 7d
```

The pack opens no ticket itself: it declares no `act`, so it cannot.

## What it may read

| Capability | Why |
|---|---|
| `read_data = ["ledger.guard", "org.approvals"]` | the evidence and ticket reports read the guard's decision ledger, and cite the org model's approval chains and change freezes (the logins and emails they name, and who confirmed them) |
| `write_org = ["proposals"]` | the adapter proposes approval facts; a person confirms them |
| `guard = "tighten-only"` | its guard rules only ever tighten a verdict |
| `max_autonomy = "L1"` | it recommends; it proposes no action |

The adapter reads the CODEOWNERS file and the JSON exports you point it at,
and nothing else it is given. It reads them itself, from the directory it is
pointed at (`repo`, or where nable ran), not through the `repo.files` data
scope, so it declares no `repo.files` and no `repo_files`. The broker runs it
out of process in a scrubbed environment; like any code pack on a laptop it
can read what your user can, which is what its signature vouches for.

## Secrets

None. The pack declares no secrets and holds none. GitHub settings come from
exports you make with your own `gh`, so no token reaches the pack.

## Network

None. The pack declares no network hosts; on Linux the broker runs its
adapter in an empty network namespace where the kernel allows it. Reading
branch protection live from the GitHub API would need a declared host and a
token; this pack reads exports instead.

## Signing

A first-party claim is honoured only with a signature from nable's
first-party key. The copy in this repository is unsigned; `nable pack
validate packs/change-control` says so, and install refuses it until a
release is signed.
