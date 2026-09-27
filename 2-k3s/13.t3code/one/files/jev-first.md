# Ask Jev what to do first

The `jev` MCP server is the published @jkudish/jev-mcp (TypeSafe's Jev
decision model). It returns typed choices with probabilities, not prose.

At the start of every new user task, before any other tool call, call
`jev_jev_decide` once:

- `decision`: "What should be done first?"
- `evidence`: the user's request and any context you already have.
- `priorities`: what the user asked you to optimize or avoid.
- `candidates`: 2 to 6 concrete first steps you would consider, each with an
  `id` and a one-line `description`.

Follow `recommendation.selected` when `escaped` is false and `confidence` is at
least 0.6, unless it conflicts with the user's instructions, safety rules or
facts you verified. If Jev escaped (`ask_user`, `investigate`, `none`), follow
that escape instead of re-asking. Otherwise use your own judgement and say why.
State the choice and confidence in one line, then continue.

Call it once per task, not per step. A Jev choice never authorizes
deployments, deletions, credential access or sending messages. If the tool is
unavailable or errors, say so and continue without it.
