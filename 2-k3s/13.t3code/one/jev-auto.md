# Jev Auto

Select provider `Jev Auto`, model `auto`, in T3's OpenCode model picker.
Manual selections keep their current behavior. The picker remains on Auto:
the session's model stays `jev-auto/auto`, and the plugin's OpenCode 2
`http.request` hook puts the turn's real model (for example
`claude/claude-opus-5-5`) into each request body. Auto is an OpenAI-compatible
(Chat Completions) model, so every routed family, Claude included, is sent to
CLIProxyAPI's `/chat/completions`. The executor is instructed to prefix its reply
with its actual model. The
authoritative route is in `$XDG_STATE_HOME/opencode/jev-auto.jsonl`, defaulting
to `~/.local/state/opencode/jev-auto.jsonl`. Reply prefixes are best-effort.

For a standalone initial text request, Jev can choose the newest Luna at
confidence 0.9 or above. Ordinary work uses the newest Sol. Nontrivial deep
dives and substantive decisions or user discussion use the newest Opus at
confidence 0.5 or above. The Sol default also covers follow-ups,
attachments, synthetic context, long prompts, uncertain classifications and
classifier failures. This first version does not classify conversation history.
The chosen provider protocol stays fixed through that message's tool loop;
Claude models can change within that protocol as described below. Executor failures
are handled by OpenCode normally; there is no automatic replay on another model.

Luna and Sol use the existing Codex subscription through CLIProxyAPI; Opus
uses the existing Claude route. This
preserves the smaller-model option without adding paid OpenRouter executor
requests. It does not establish monetary savings or remaining subscription
quota. Jev classification is billed through the existing OpenRouter key.

The plugin registers Auto (an OpenCode 2 provider transform over the catalog
that `cliproxy-models.js` adds) only when both executor models are in the
CLIProxyAPI catalog on a `codex/` or `claude/` route. OpenCode 2 has no plugin
hook for the provider allowlist, so `jev-auto` is listed in `enabled_providers`
by the entrypoint and in the StatefulSet's `OPENCODE_CONFIG_CONTENT`.

## Subagents

Pick any OpenAI or Anthropic model for the thread (for example Opus 5.5). When
the agent starts a subagent (the OpenCode 2 `subagent` tool, `task` in
OpenCode 1), the subagent's first message
gets one Jev call with three questions: how capable a model the task needs
(small, medium, large), how much reasoning effort (low, medium, high), and
whether it mainly requires a substantive decision or discussion with the
user. Routine triage, validation, code review and reporting findings do not
count as dialogue. The answers map to a model family:

| Tier | Work | Decisions and user discussion |
| --- | --- | --- |
| Small | Luna | Opus |
| Medium | Sol | Opus |
| Large, nontrivial deep dive | Opus | Opus |

High reasoning effort alone does not make a task a deep dive. An ordinary
review can use Sol high. Astra remains available through manual selection
and explicit pins but has no automatic tier. Fable stays excluded.

## Newest version wins

A family always means its highest version in the live CLIProxy catalog, among
models on a `codex/` or `claude/` route. OpenCode reads that catalog at start
(`files/cliproxy-models.js`), so a new release is used after the next t3code
restart with no change here or in AGENTS.md. Versions come from the model ID
and compare as numbers: `gpt-6-luna` is Luna 6, `gpt-5.6-sol` Sol 5.6,
`claude-opus-5-5` Opus 5.5, `claude-haiku-4-5-20251001` Haiku 4.5 (the date
is ignored). IDs that do not follow these forms, such as
`claude-opus-4-6-1m`, are ignored. The same rule picks Jev Auto's models
(the newest Luna for trivial tasks, Sol for ordinary work and Opus for deep
dives and dialogue). On 2026-09-29
it resolved Sonnet to `claude-sonnet-5-5`, which had just appeared in the
catalog. `cliproxy-models.js` keeps a model for 14 days after CLIProxy stops
listing it, so a newest version that is withdrawn keeps being chosen, and its
calls fail, until it ages out or its catalog entry is removed.

The route is applied with `session.switchModel` on the child session, the
effort as the model variant (`low`, `medium`, `high`; the catalog plugin gives
Codex models `reasoningEffort` variants and Claude models other than Haiku
`effort` variants). T3 shows that model as the child's model; the parent's
selector does not change. T3-owned delegated tasks are separate top-level
threads in OpenCode, not native children: they keep the model T3 sends, and
their `route:` pin applies only if the task runs a native subagent. Fable is not in the pool. The route is fixed for the subagent
session while the OpenCode server runs, including when it is resumed; after a
restart a resumed subagent is routed again. Only the tier confidence is gated;
the effort and dialogue answers are used as they come. If the tier confidence is
below 0.5, the answer is malformed, the model is missing from the catalog, or
Jev errors or takes longer than 3 s, the subagent keeps the parent's model and
effort (Sol under Auto). The session lookup is an in-process OpenCode call. A new subagent under a non-OpenAI/Anthropic
parent gets no routing; one routed earlier keeps its route. Normal tools use no model and are not routed.

A line `route: <family>:<effort>` (or `<family>_<effort>`, the form
`jev_decide` candidate ids allow) on its own line in the subagent's task pins
the route with no Jev call and is logged as `status: "pinned"`. If OpenCode
refuses the switch, the record says `status: "switch_failed"` with the child's
real model in `actualModel`. Each later prompt restores the route's model and
effort if either moved, logging another `switch_failed` record on a refusal; until
it succeeds the child's steps are routed as usual. The review gate
in AGENTS.md uses it to run the reviewer on the model its own Jev call picked.
The injected `jev-checks.md` tells the agent to pass the same Sol/Opus
preferences to that call, so an explicit reviewer selection follows the
policy too.
Families are `luna`, `astra`, `sol`, `haiku`, `sonnet` and `opus`; a full ID
such as `claude-opus-5` also works. Either way the newest version of that
family runs. Haiku is enabled in CLIProxy by
`17.remote-pi/cliproxy/files/reconcile-config.psql` and gets no effort
parameter; Anthropic does not support effort on it. A pin to any other family
(Fable), a family with no model in the catalog, or an effort other than low,
medium or high is ignored and Jev routes as usual.

Jev answers three questions rather than choosing from model names because a
single Choice over the 15 model and effort pairs had confidence 0.22 to 0.42 on
four of six sample tasks in live runs; the split questions scored 0.81 to 1.0.

On 2026-09-28 eight sample subagent tasks all routed with tier confidence 0.81
or above, for example file searches to Luna low, a security review to Opus high
and a migration design to Sol high. An isolated `opencode run` with an Opus 5.5
parent ran an `explore` subagent on `codex/gpt-6-luna` with
`reasoning_effort: low`.

## Every step

After the model is chosen, each step of the tool loop is re-routed: every
agent-loop request (OpenCode 2 request kind `primary`; title, compaction and
generate calls are left alone) that the `cliproxy` or `jev-auto` provider
sends with tools is rewritten in the plugin's `http.request` hook before it
leaves OpenCode. There is no local proxy. Jev reads the task (the first user message) and the latest action with its
result, both cut to a few thousand characters and without thinking, and scores
the next step's tier (small, medium, large), effort and whether it needs a
substantive decision or user discussion.

- Claude requests: the step runs on the newest Haiku, Sonnet or Opus for the
  tier, at Jev's effort: `output_config.effort` on the Anthropic Messages wire
  (Claude models picked by hand), `reasoning_effort` on Chat Completions (Auto's
  Claude turns), which CLIProxyAPI translates. The requested model's family
  decides, not the URL. Any Claude model can go to any
  of the three. Decisions and user discussion select Opus even at a lower
  tier. Haiku gets no thinking or effort and at most 64K output tokens,
  and is skipped (Sonnet instead) when the request is over 300K characters:
  code, JSON and Greek run at 2 to 3 characters a token, so that is 100K to
  150K tokens of its 200K window.
- Codex requests: only `reasoning_effort` changes; the model stays, because a
  vendor switch mid-task breaks the history. Auto turns routed to Luna or Sol
  stay on Codex for the whole turn.
- Unchanged: requests without tools, OpenRouter models, pinned subagents
  (`route:`) whose request goes to the pinned model, and any step where the tier confidence is below 0.5 (Claude), Jev
  errors or takes longer than 3 s. Effort is not gated on confidence: with a
  0.5 gate, 6 of the first 8 live Codex steps were skipped, their effort
  confidences 0.22 to 0.46 with scores between low and medium. A wrong effort
  changes less than a wrong model.

A request that is not rewritten is left as the same object. A rewritten one is
rebuilt with the same URL (query string included), method, headers (without a
stale `content-length`) and abort signal, so streaming, upstream errors, retries
and aborts stay OpenCode's own. `jev-steps.test.mjs` covers both wires, the
untouched cases and the abort; the runtime smoke's OpenCode 2 parity step
(`env/tools/opencode-parity.mjs`) runs two Auto turns and a Claude tool loop
through the real binary against a mock CLIProxy.

Each step costs one Jev call ($0.00002 to $0.00007 in the live runs) and 250 to 550 ms, and a model
switch drops the prompt cache. Records have `mode: "step"`: session, requested
and actual model, effort, Jev's scores and confidences, latency, cost. No
prompt text. On 2026-09-29 isolated `opencode run`s fixed a failing test: an
Opus 5.5 thread given the task in Greek took 4 steps, two on Sonnet 5.5 and two
on Haiku 4.5; an Astra thread took 13, each with Jev's effort; a pinned
`route: sonnet_low` subagent ran untouched.

## Skills

For every message you send on an OpenAI or Anthropic model (not subagents), one
Jev call reads the message and the installed skills (OpenCode 2 `skill.list`,
their ids and descriptions) and asks whether a skill is needed and which one. If Jev picks a skill with
confidence 0.8, or 0.5 when the need scores 0.4 or more, one line goes into the
system prompt for that message: `Jev skill pick: the "<id>" skill fits this
request. Load it with the skill tool (id "<id>") before you start, unless it
clearly does not apply.` The agent still decides. Titles and summaries do not
pick, and neither does a message whose session lookup failed (it may be a
subagent). A pick that arrives after a newer message is dropped.
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
- Every `jev_*` MCP tool call is refused unless the session's latest step ran
  on an allowed model (Auto counts; its targets are all allowed). Unknown sessions and models not
  in the catalog fail closed. The model gets the refusal as a tool error and
  continues without Jev.
- Claude Code and Codex never get the jev MCP (hub_clients.py OPENCODE_ONLY).

Records have `mode` `auto`, `subagent` or `skill` and log IDs, model,
answers, confidence, latency and decision cost, never prompt text or credential
values. Tool permissions are unchanged. The subagent task and your message are
sent to Jev as they are; jev-guard (when deployed) blocks tool calls, including
`subagent`, whose arguments hold a credential.

Deployment uses the scripts ConfigMap and pod entrypoint. No direct home-file
installation is required. The existing mounted Jev Secret is reused.

Checks: `node --test 2-k3s/13.t3code/one/files/jev-auto.test.mjs` and
`jev-steps.test.mjs`; the OpenCode 2 parity step of `env/tools/smoke.sh`
(`node env/tools/opencode-parity.mjs <opencode binary> <config dir> [home]`,
also runnable on a workstation against a private HOME).
An isolated OpenCode run verified that the first typo task executed on Luna and
a context-dependent follow-up executed on Astra. These are integration checks,
not a task-quality benchmark or a claim of measured savings.

The routing-policy tests cover Sol defaults, Opus deep dives and dialogue,
confidence fallback, explicit pins and newest-version resolution. Earlier
live-run examples above describe the policy at the time of those runs.
