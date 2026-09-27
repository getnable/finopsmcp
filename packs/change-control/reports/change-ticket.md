# Change ${item.id}: ${item.show.action_type}

A change record drawn from nable's guard ledger, to paste into your change
tracker. Render one per change with:
`nable pack report ${pack} change-ticket --each ledger.guard.changes --since 7d`

| Field | Value |
|---|---|
| When (UTC) | ${item.ts} |
| Ledger line | ${item.ledger_line} (chain intact at this line: ${item.chain_ok}) |
| Change | `${item.show.command}` |
| Agent harness | ${item.show.harness} |
| Guard decision | ${item.decision} |
| Outcome | ${item.outcome} |
| Approved by | ${item.show.approved_by} |
| Approved via | ${item.show.approved_via} |
| Approved at | ${item.show.approved_at} |
| Under policy | ${item.policy_summary} |
| Change freeze in force | ${item.show.freeze} |
| Approval chain (org model) | ${item.approval_chain_text} |
| Monthly cost figure | ${item.show.monthly_usd} |

Guard's reason: ${item.show.reason}

Justification: (why this change, and why now)

Rollback plan: (how to undo it)

Reviewer sign-off: (name and date)
