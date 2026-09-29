# Jev Auto

Select provider `Jev Auto`, model `auto`, in T3's OpenCode model picker.
Manual selections keep their current behavior. The picker remains on Auto;
the executor is instructed to prefix its reply with its actual model. The
authoritative route is in `$XDG_STATE_HOME/opencode/jev-auto.jsonl`, defaulting
to `~/.local/state/opencode/jev-auto.jsonl`. Reply prefixes are best-effort.

For a standalone initial text request, Jev can choose `gpt-6-luna` at confidence
0.9 or above. Everything else uses `gpt-6-astra`: follow-ups,
attachments, synthetic context, long prompts, uncertain classifications and
classifier failures. This first version does not classify conversation history.
The chosen model stays fixed through that message's tool loop. Executor failures
are handled by OpenCode normally; there is no automatic replay on Astra.

Both executors use the existing Codex subscription through CLIProxyAPI. This
preserves the smaller-model option without adding paid OpenRouter executor
requests. It does not establish monetary savings or remaining subscription
quota. Jev classification is billed through the existing OpenRouter key.

The plugin registers Auto only when both executor models are in the
CLIProxyAPI catalog on a `codex/` or `claude/` route.

## Subagents

Pick any OpenAI or Anthropic model for the thread (for example Opus 5.5). When
the agent starts a subagent (the `task` tool), the subagent's first message
gets one Jev call with three questions: how capable a model the task needs
(small, medium, large), how much reasoning effort (low, medium, high), and
whether it is mostly prose rather than code. Code maps the answers:

| Tier | Code | Prose |
| --- | --- | --- |
| Small | `gpt-6-luna` | `gpt-6-luna` |
| Medium | `gpt-6-astra` | `claude-sonnet-5` |
| Large | `gpt-6-sol` | `claude-opus-5-5` |

Effort goes to the provider as `reasoning_effort` (Codex routes) or
`output_config.effort` (Claude routes), only while the subagent runs on the
routed model. Fable is not in the pool. The route is fixed for the subagent
session while the OpenCode server runs, including when it is resumed; after a
restart a resumed subagent is routed again. Only the tier confidence is gated;
the effort and prose answers are used as they come. If the tier confidence is
below 0.5, the answer is malformed, the model is missing from the catalog, or
Jev errors or takes longer than 3 s, the subagent keeps the parent's model and
effort (Astra under Auto). The session lookup before it has its own 3 s limit,
so the worst case adds about 6 s. A new subagent under a non-OpenAI/Anthropic
parent gets no routing; one routed earlier keeps its route. Normal tools use no model and are not routed.

A line `route: <model>:<effort>` (or `<model>_<effort>`, the form `jev_decide`
candidate ids allow) on its own line in the subagent's task pins
the route with no Jev call and is logged as `status: "pinned"`. The review gate
in AGENTS.md uses it to run the reviewer on the model its own Jev call picked.
Pins accept the five tier models (Luna, Astra, Sol, Sonnet 5, Opus 5.5) and any `claude-haiku-*` model in the catalog
(Haiku is enabled in CLIProxy by `17.remote-pi/cliproxy/files/reconcile-config.psql`).
Haiku gets no effort parameter; Anthropic does not support effort on it. A pin
to any other model, a model missing from the catalog, or an effort other than
low, medium or high is ignored and Jev routes as usual.

Jev answers three questions rather than choosing from model names because a
single Choice over the 15 model and effort pairs had confidence 0.22 to 0.42 on
four of six sample tasks in live runs; the split questions scored 0.81 to 1.0.

On 2026-09-28 eight sample subagent tasks all routed with tier confidence 0.81
or above, for example file searches to Luna low, a security review to Opus high
and a migration design to Sol high. An isolated `opencode run` with an Opus 5.5
parent ran an `explore` subagent on `codex/gpt-6-luna` with
`reasoning_effort: low`.

## Skills

For every message you send on an OpenAI or Anthropic model (not subagents), one
Jev call reads the message and the skill list from the agent's system prompt
and asks whether a skill is needed and which one. If Jev picks a skill with
confidence 0.8, or 0.5 when the need scores 0.4 or more, one line goes into the
system prompt for that message: `Jev skill pick: the "<name>" skill fits this
request. Load it with the skill tool before you start, unless it clearly does
not apply.` The agent still decides. Calls without the skill list (titles,
summaries) do not pick, and neither does a message whose session lookup failed
(it may be a subagent). A pick that arrives after a newer message is dropped.
About $0.0001 and 250 to 810 ms per message; Jev errors inject nothing.

On 2026-09-28 ten sample requests against the 34 installed skills all got the
expected skill or none on a second run (a first run a minute earlier had 5
transient Jev errors, which injected nothing), including a credential request (keepassxc-secrets) and
two small-talk messages (none). An isolated run put the `grilling` line into the
system prompt for "Grill me on my plan".

## OpenAI and Anthropic only

The same plugin limits every use of Jev in OpenCode to OpenAI and Anthropic
models: a direct `openai` or `anthropic` provider, or a CLIProxyAPI catalog
route starting `codex/` or `claude/`. OpenRouter routes (`openrouter/`, such as
GLM, DeepSeek, MiniMax) and anything else get no Jev.

- `jev-checks.md` (screen, gate and verify, find and rerank) is added to the
  system prompt of allowed models only. It is not a global `instructions`
  entry; the entrypoint removes it from `opencode.json`.
- Every `jev_*` MCP tool call is refused unless the session's latest turn ran
  on an allowed model (after Auto's rewrite). Unknown sessions and models not
  in the catalog fail closed. The model gets the refusal as a tool error and
  continues without Jev.
- Claude Code and Codex never get the jev MCP (hub_clients.py OPENCODE_ONLY).

Records have `mode` `auto`, `subagent` or `skill` and log IDs, model,
answers, confidence, latency and decision cost, never prompt text or credential
values. Tool permissions are unchanged. The subagent task and your message are
sent to Jev as they are; jev-guard (when deployed) blocks tool calls, including
`task`, whose arguments hold a credential.

Deployment uses the scripts ConfigMap and pod entrypoint. No direct home-file
installation is required. The existing mounted Jev Secret is reused.

Checks: `node --test 2-k3s/13.t3code/one/files/jev-auto.test.mjs`.
An isolated OpenCode run verified that the first typo task executed on Luna and
a context-dependent follow-up executed on Astra. These are integration checks,
not a task-quality benchmark or a claim of measured savings.
