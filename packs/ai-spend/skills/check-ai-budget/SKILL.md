---
name: check-ai-budget
description: Use before starting a long or expensive task (a large refactor, a change across many files, a long agent loop, a big test or eval run, a batch of LLM calls), and when asked how much AI budget is left.
---

# Check the AI budget before a long task

Your own tokens cost money, and a long task can spend in an hour what a
normal day spends. nable tracks this agent's spend against the budget the
person set, on this machine.

Before a long task:

1. Estimate the tokens the task will take. A rough guide: reading a large
   file is 10 to 50 thousand tokens, a change across 20 files with tests is
   500 thousand to 2 million, a long autonomous loop can pass 5 million.
2. Call the `check_ai_budget` tool with `estimated_next_tokens` set to that
   estimate (or run `nable ai-budget --json` where there is no MCP server).
3. Read the verdict and relay it in one sentence, with the headroom it
   reports:
   - `ok`: go ahead.
   - `warn`: tell the person what is left and what the task will likely
     use, and offer a smaller first step. Continue only if they say so.
   - `over`: stop before starting and ask. Do not start a reduced version
     on your own.
4. Midway through a task that is running long, check again.

Rules:

- The verdict is advice and the person decides. Never change the budget
  yourself (`set_ai_budget` is theirs to call), and never split a task into
  smaller sessions to stay under a cap.
- Dollar figures are list-price estimates from token counts, not the bill.
  Say so if you quote one.
- When you write code that calls an LLM API, tag each call with
  `feature:<name>` and, where there is one, `customer:<id>` (a LiteLLM
  request tag or a Langfuse trace tag), so the spend can be attributed.
