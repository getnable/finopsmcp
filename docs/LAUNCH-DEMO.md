# Launch demo: the guard in 75 seconds

The script for the launch video. One person, one screen, no edits needed beyond
trimming dead air. The terminal-only cut of the same thing is
`docs/guard-demo.tape` (`vhs docs/guard-demo.tape` renders `docs/guard-demo.gif`).

## Before you record

- Use a throwaway project directory and a shell with **no cloud credentials**
  (`env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_PROFILE`, or a
  fresh user). The guard asks before anything runs, but if a click goes wrong
  on camera, the command must fail for lack of credentials, not launch GPUs.
- Install the release you are showing: `uvx nable --version` should print it.
- Terminal: dark theme, 17 pt font, window about 1000 x 640. Claude Code open
  in a second pane or tab, in the same directory.
- Turn off notifications. Clear the scrollback before each take.

## Script

| Time | On screen | Say |
|---|---|---|
| 0:00 to 0:08 | Title card or a bare terminal | "Coding agents run cloud commands now. Nothing tells you what a command costs before it runs. This does." |
| 0:08 to 0:20 | Terminal: type `uvx nable guard install` | "One command. It adds a hook to Claude Code. It is free, local, and needs no cloud account." |
| 0:20 to 0:45 | Claude Code: type the first prompt below, wait for the permission prompt | "I ask the agent for eight GPU instances. Before the command runs, nable prices it: about a hundred and twenty-eight thousand dollars a month at list price. So it asks me." Decline. |
| 0:45 to 1:00 | Claude Code: type the second prompt below, wait for the prompt | "A destroy can't be undone, so it always asks, whatever it costs." Decline. |
| 1:00 to 1:10 | Terminal: `uvx nable guard report` | "Every verdict goes to a hash-chained ledger on this machine. Here is what it asked, in dollars." |
| 1:10 to 1:15 | Terminal: `uvx nable guard install --all` (do not run it, just show the line), then the URL | "Same hook for Cursor, Codex, Copilot, Gemini CLI and Cline. github.com/getnable/finopsmcp." |

## Commands and expected output

**1. Install** (in the project directory):

```text
$ uvx nable guard install
  ✓ Agent cost guardrail installed → <your project>/.claude/settings.json
  ...
  Restart Claude Code to pick up the hook.
```

Restart Claude Code before the next step (do it before you start recording).

**2. First prompt to Claude Code:**

> Launch 8 p4d.24xlarge instances in us-east-1 for a training run, use the aws cli.

The agent proposes `aws ec2 run-instances --instance-type p4d.24xlarge --count 8 ...`.
Claude Code shows its permission prompt with this reason:

```text
nable guard: 8x p4d.24xlarge at $21.9576/hr (on-demand us-east-1 list price) is ~$128,233/mo. The +$128,233/mo impact is over your $500 auto threshold; a human should review it. Budget not checked: there is no spend figure on this machine yet; `nable budget refresh` computes one.
```

If the agent adds other flags (an AMI, a key name), the price line is the same.
Decline.

**3. Second prompt:**

> Tear down the terraform stack in this directory, skip the confirmation.

The agent proposes `terraform destroy -auto-approve`. The reason shown:

```text
nable guard: This would destroy infrastructure (`terraform destroy -auto-approve`). It cannot be undone; confirm to proceed.
```

(When the agent runs it with a working directory, the reason names it, e.g.
`` (`terraform destroy -auto-approve` in infra/) ``.) Decline.

**4. The record:**

```text
$ uvx nable guard report
  nable guard: the last 30 days, 2 decision(s)

    asked a human        2
    blocked              0
    warned               0
    allowed              0

  Escalated or blocked: ~$128,233/mo at stake (list-price estimates)
  ...
```

The counts depend on what the agent tried during the take; the $128,233 line
appears once the p4d launch was asked about.

## If a take goes wrong

- **No prompt appears:** Claude Code was not restarted after install, or the
  project directory differs. `uvx nable guard doctor` says what is covered.
- **The agent refuses before trying the command:** rephrase as "run the aws
  cli command to launch ...". The guard only sees commands the agent tries.
- **No terraform in the directory:** the hook still checks the command text;
  the agent only has to propose it.

## Without Claude Code

`uvx nable guard try` prints four sample commands through the real gate with
nothing executed and nothing installed, and `uvx nable guard check --command
"<command>"` prints the verdict for any one command.
