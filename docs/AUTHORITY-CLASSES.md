# Authority for classes that move no money

An agent tags resources, opens tickets and stops what nobody uses at night.
None of it buys anything, so a team can let an agent do more of it without
asking, one rung at a time, within a bound a person writes and can audit.
This is the authority ladder for those classes. It sits in the same
`authority.yaml` as purchase authority and never changes a purchase kind's
answer.

Nothing here buys, approves or executes anything. The guard only asks,
denies or lets a call through that the agent was going to make anyway.

## The three classes

| Class | Calls in it | Removals (in the class, never within a bound) |
|---|---|---|
| `tag` | `aws ec2 create-tags`, `aws resourcegroupstaggingapi tag-resources`, `aws rds add-tags-to-resource`, `aws elasticache add-tags-to-resource`, any `aws SERVICE tag-resource`, `aws s3api put-bucket-tagging` (it replaces the whole tag set, so it is never within a bound), `kubectl label` | `aws ec2 delete-tags`, `untag-resources`, `remove-tags-from-resource`, `untag-resource`, `kubectl label NAME KEY-` |
| `ticket` | `gh issue create`, `glab issue create`, `jira issue create`, and an MCP tool whose name says create with issue or ticket (the GitHub server's create_issue, Atlassian's createJiraIssue, and issue_write with method `create`) | none: a reversal of a ticket is recorded by hand |
| `offhours_schedule` | `aws ec2 stop-instances` and `start-instances`; `aws rds stop-db-instance`, `start-db-instance`, `stop-db-cluster`, `start-db-cluster`; `aws autoscaling put-scheduled-update-group-action` and its batch form; `aws scheduler create-schedule` and `update-schedule` whose target is an EC2 or RDS stop or start | `aws autoscaling delete-scheduled-action` and its batch form, `aws scheduler delete-schedule`, and a schedule updated to `--state DISABLED` |

Tagging in IAM, Organizations, SSO and KMS stays out of the `tag` class: a
tag there can grant access, so the guard judges it as it always has.

A command that any of the guard's money-moving, one-way or priced rules
matches is never in these classes, whatever else it holds:
`aws ec2 create-tags ... && aws ec2 terminate-instances ...` is judged as a
termination. The class's own removals are the one exception (the guard's
generic delete rule matches `delete-tags` too); they are never within a
bound, and the guard's own ask for them stands.

## The rungs

| Rung | What the guard does with a call in the class |
|---|---|
| not named | Nothing changes: the call is judged exactly as before there was a file. |
| `off` | Denied, with a reason that says how to change it. |
| `recommend` | Denied: an agent proposes it, and a person runs it. |
| `approve_each` | Asks every time, naming the approvers. The default for a class an entry names. |
| `bounded` | No ask within the bound; an ask outside it. It never removes an ask the guard raises without the file. |

`bounded` is for these classes only. An entry that gives a purchase kind
(`rds_ri`, `elasticache_ri`, `database_savings_plan`) `bounded` is refused,
naming `value.rung`, and holds that kind at recommend.

## The bounds

| Class | Bound fields |
|---|---|
| `tag` | `tag_keys` (1 to 20 keys, required at bounded; AWS tag keys are case-sensitive and so is the match), `max_resources_per_call` (1 to 100, default 20) |
| `ticket` | `max_per_day` (1 to 50, required at bounded; counted on this machine, by UTC day, from its own ledger), `repos` (optional, `OWNER/NAME`; a ticket whose repository cannot be read is outside a bound that names repos) |
| `offhours_schedule` | `environments` (a non-empty subset of `nonprod`, `sandbox` and `dr`, required at bounded; `prod`, `shared` and `unknown` are refused), `max_resources_per_call` (1 to 50, default 10) |

For an off-hours call at bounded, every environment the org model resolves
for the call must be in the bound, and no production context may be seen.
A call whose environment cannot be resolved asks. A call whose resources
cannot be counted (a selector, a file, a substitution, `xargs`) asks.

Any other field in an entry is refused, never kept, and the refusal names
it: a misspelt bound must never be ignored.

## Who sets it

- `approvers` (as `email:ADDRESS`) and `expires` are required at
  `approve_each` and at `bounded`.
- Per class the strictest live entry answers: the lowest rung, the tag
  keys, repos and environments every entry names, the smallest count.
- Only a confirmed entry in a trusted org model holds `bounded`. A proposal,
  or an entry in a cloned repo nobody trusted (`nable org trust --here`),
  holds at most `approve_each`: it can only narrow.
- An expired entry, an entry dated after today, a refused entry and a file
  that cannot be read hold the class at `approve_each`, never `bounded`.
  A refused entry whose class cannot be read, or a file that cannot be
  read, also holds every purchase kind at recommend, as before.
- No MCP tool writes, promotes, reverses or lifts any of it.

```
nable org set authority --class tag --rung bounded --tag-key team --tag-key env --max-resources 20 --approver email:ana@acme.com --expires 2027-06-30
nable org set authority --class ticket --rung bounded --max-per-day 5 --repo acme/payments --approver email:ana@acme.com --expires 2027-06-30
nable org set authority --class offhours_schedule --rung bounded --environment nonprod --approver email:ana@acme.com --expires 2027-06-30
nable org authority
```

`nable org set authority` for one of these classes is `approve_each` when
`--rung` is left out. `nable org authority` and `nable org status` show each
class, its rung and bound, and each refused entry.

## On a machine connected to a workspace

On a machine that sends to a paid workspace (`nable cloud connect --send`),
the guard applies the stricter of the repo's `authority.yaml` and the
workspace's standing, which `nable cloud status` keeps beside the guard
ledger as `authority-standing.json`, only from the box and workspace this
machine last sent to. The workspace can only narrow:

- a class the workspace demoted after a reversal is at `approve_each` at most,
  until a signed-in admin the class's entry names lifts it, after a fresh
  sign-in;
- a rung in force on the workspace lower than the file's applies; one higher
  never does;
- a standing that could not be read holds every class the file has at
  `bounded` at `approve_each`.

`authority-standing.json` is one of the guard's own files: an agent's write
or delete of it asks.

## Reversals

```
nable guard reverse
nable guard reverse 3f9a1c0b2d4e --why "wrong team tag"
```

With no hash, `nable guard reverse` lists the calls in a class that ran on
this machine. With a hash (the ledger line's sha256, or a unique prefix of
12 or more), it appends a `reversed` outcome naming that line. Anyone may
record one, an agent included, since it only narrows. It refuses a hash not
in the ledger, a line in no class, and a call that never ran. Once the
ledger is sent, the workspace demotes the class at once and its count
toward `bounded` starts again. A workspace also infers a reversal from the
ledgers it receives: for `tag`, a removal of the same key from the same
resource within 30 days; for `offhours_schedule`, a deletion or a disable
of the same schedule within 30 days. Tickets are reversed by hand only.

The ledger's verify-log still verifies the chain with a reversed line in it,
and an ask's outcome (approved, declined, unknown) reads exactly as before.

## What the ledger line records

A verdict line in one of these classes carries `authority_class`, `rung` and
`within_bound`, and `authority`: what the call acted on (its op and tool,
the resources and tag keys, the repository, the schedules and group, the
environments), the class's rung in the file, its bound and why the call was
outside it. That is what a workspace reads to tie a later removal to the
call. Each string in it is redacted like the command, and the whole is at
most 4,096 characters: past that only its single values stay, and
`trimmed` says so. The approvers stay in `authority.yaml`. A call the guard
would have let through in silence is recorded as an allow line, so the
post hook can link its outcome, even when no file names its class (rung
`none`): it is judged exactly as before, and `nable guard reverse` can
still name it.

## What the post hook records

A verdict line in one of these classes gets a `ran` outcome whether it was
an allow or an ask, so a workspace can count executions and the time from
an ask to its run. Any other allow still gets none.

## Free and paid

The classes, the rungs, the bounds, the guard's asks and `nable guard
reverse` are free, in this package. The evidence (executions, reversals,
approval latency), the recommendation to move a class to `bounded`, a
demotion that reaches every connected machine and the workspace's
confirmation of a widening are part of a paid workspace. With no
`authority.yaml`, nothing changes for anyone, so nothing that was free moves
behind a paywall.

## Older readers

Release 0.13.0 of this package, and a workspace older than these classes,
read an entry for `tag`, `ticket` or `offhours_schedule` as a class they do
not know: they refuse it and hold every purchase kind at recommend. That is
the safe side: nothing an older reader does with the file is wider than
what it says. Upgrade every machine that reads the file before adding an
entry for one of these classes, or keep purchase authority in a layer those
machines read on its own.

A machine that read the file while it ran an older release kept a parse of
it that refused these entries. This release does not reuse that parse: it
reads the file again, so an unchanged file is not held to the older answer.
