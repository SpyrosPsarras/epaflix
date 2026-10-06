# Jev tool guard: research

Question: how to add a pre-tool hook that blocks catastrophic bash commands and credential leaks in our OpenCode setup, and optionally in Claude Code, using TypeSafe's Jev.

Researched 2026-09-28. Every claim cites a URL or a file path. Anything I could not confirm is marked unverified.

## Summary

1. Do not start with Jev. Two maintained, open-source guards already block destructive commands and secret-file access before a tool runs, in both OpenCode and Claude Code: cc-safety-net (MIT) and dcg. Use one as the deterministic layer.
2. Our OpenCode config sets no `bash` permission rules, so every shell command is allowed by default (`/home/spyros/.config/opencode/opencode.json`, `permission` key; defaults at https://opencode.ai/docs/permissions/). Adding `deny` rules costs nothing and the host enforces them before any plugin runs.
3. Jev fits as a third layer, only for commands that pass the deterministic checks and are not on a known-safe list. It costs about $0.00002 per call and answers in roughly 600 ms (sources below).
4. Right now the OpenRouter key has no credits. A live test returned `402 Insufficient credits`, and the jev MCP `jev_screen` tool failed the same way during this research. A fail-closed Jev guard would block every shell command today. Fail open on Jev errors and keep the catastrophic set deterministic.
5. Redact before sending. A command that contains a literal secret should be blocked in code and never sent to OpenRouter.

## 1. What the video says

- Video: "10 Levels of Jev For Agentic Engineers" by IndyDevDan, 35 min (oEmbed https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=_U-O5lYhJ7Q, page metadata `lengthSeconds: 2117`).
- Transcript: not obtained. youtubetranscript.com returned "YouTube is currently blocking us from fetching subtitles" (https://youtubetranscript.com/?server_vid2=_U-O5lYhJ7Q). The YouTube timedtext URL for the English auto captions returned an empty body. I did not invent content from it.
- The description (from the watch page HTML) says: "In a real Pi coding agent harness, a pre-tool guard checks dangerous bash commands and writes to protected files before the agent can act." It also says confidence decides "when a bash command needs a gate instead of blindly trusting a classification". Chapter 14:31 is "In Agent Guardrail Hooks".
- The linked repo https://github.com/disler/ten-levels-of-jev holds the actual guard code. It targets the Pi agent, not OpenCode:
  - `apps/ten-levels/extensions/jev-guard.ts`: `tool_call` hook blocks bash via `gateBashCommand` and write/edit via `gateWriteCall`. A `tool_result` hook prepends a warning banner when read or bash output contains instructions aimed at the agent. On any Jev error the hook logs and allows the call (fail open, lines 35 to 37).
  - `src/levels/level06/bash-gate.ts`: one Choice `effect` (read_only, reversible, irreversible) plus one Noul `destructive_intent`. Blocks when `irreversible` confidence is at least 0.6 or the Noul is at least 0.7. Every block message tells the agent not to work around it.
  - `src/levels/level06/write-gate.ts`: path outside the repo is blocked in code with no Jev call. Inside the repo, a Noul `contains_secret` (real credential, not a placeholder) blocks at 0.7. Content is trimmed to 4000 characters.
  - `src/levels/level04/shell-command-gate.ts`: notes that an ambiguous `rm -rf` once came back `irreversible` at confidence 0.33, and uses low confidence to route to a human.
  - `.claude/skills/hyper-jev/cookbook/use-cases/07-guardrails.md`: "Security classifiers must not be the sole safeguards, and confidence is not authorization."
- The repo's thresholds are demo values. The cookbook calls them "Demonstrations only, not calibrated production policies" (same file, line 3).

## 2. Hook APIs

### OpenCode (we run 1.18.33, `opencode --version`)

The pod now runs OpenCode 2. This section is the OpenCode 1 evidence the guard was designed on, kept as written. The OpenCode 2 hooks (`tool.hook("execute.before"|"execute.after")`, `event.input`, `event.id`, `shell` instead of `bash`, results with both `output` and `content`, MCP Code Mode turned off per server) are in `2-k3s/13.t3code/one/jev-guard.md`.

- `tool.execute.before(input, output)`: `input` is `{ tool, sessionID, callID }`, `output` is `{ args }` (`/home/spyros/.config/opencode/node_modules/@opencode-ai/plugin/dist/index.d.ts` lines 235 to 241, package 1.18.31).
- The shell tool id is `bash` and its args include `command` and `workdir` (https://github.com/anomalyco/opencode `packages/opencode/src/tool/shell/id.ts`, `tool/shell.ts` lines 612 to 634, commit ad6c72c, which is version 1.18.33).
- Throwing blocks the call. The docs' `.env protection` example throws `new Error(...)` (https://opencode.ai/docs/plugins/). The plugin runner awaits each hook in sequence with no catch (`plugin/index.ts` lines 284 to 296). Our `jev-auto.js` already relies on this; its notes say "The model gets the refusal as a tool error and continues" (`2-k3s/13.t3code/one/jev-auto.md` lines 34 to 37).
- Coverage, from source reading of `session/tools.ts`: the hook fires for native tools (line 107), MCP tools (line 403), MCP resource tools (lines 176, 259, 339) and tools called inside code mode (`tool/code-mode.ts` line 142). There is no branch on parent session, so subagent (task) sessions go through the same path. Unverified by a live run; confirmed only by code reading.
- For MCP tools the hook runs before the permission prompt (`session/tools.ts` lines 403 to 409 call the hook, then `ctx.ask`).
- `tool.execute.after(input, output)`: `input` adds `args`; `output` is `{ title, output, metadata }` for native tools (`index.d.ts` lines 249 to 258). Tools return the same object after the hook (`session/tools.ts` lines 121 to 131), so mutating `output.output` changes what the model sees. For MCP tools the object is the raw MCP result, a different shape (`session/tools.ts` lines 419 to 424). The command has already run by then.
- `permission.ask` exists in the type file (`index.d.ts` line 225) but I found no trigger for it in 1.18.33 source (`grep` for `permission.ask` in `packages/opencode/src`). Treat it as not usable on 1.x. The OpenRouter recipe uses a different `permission.hook("evaluate")` API that needs OpenCode 2.0.12 or later (https://openrouter.ai/docs/cookbook/coding-agents/auto-approve-permission-prompts-with-jev).
- `tool.execute.before` has no "ask the user" outcome. It can only let the call pass, change args, or throw.
- Deterministic layer: `permission.bash` accepts wildcard rules with `allow`, `ask`, `deny`, last match wins, matched against parsed commands. `deny` still applies in `--auto` mode. `read` denies `*.env` by default (https://opencode.ai/docs/permissions/). A host `deny` runs before plugins see the request (OpenRouter recipe above, for 2.x; unverified for 1.x).

### Claude Code

Source: https://docs.claude.com/en/docs/claude-code/hooks (redirects to code.claude.com, fetched as markdown).

- `PreToolUse` runs before a tool call and can block it. Matcher `Bash`, `Edit|Write`, or `mcp__.*`. Input on stdin includes `tool_name` and `tool_input.command`.
- Block by exit code 2 (stderr becomes the reason shown to Claude) or by printing `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"..."}}`. `ask` shows a prompt; precedence is deny, defer, ask, allow. Deny rules in settings still apply whatever the hook returns.
- Any exit code other than 0 and 2, and invalid JSON, is a non-blocking error: the call proceeds. So a crashing or timing-out hook fails open unless the script itself exits 2.
- `timeout` defaults to 600 s for command hooks; set it low.
- Hooks run inside subagents too; input carries `agent_id` and `agent_type`.
- `PostToolUse` can return `updatedToolOutput` to replace what Claude sees, or `decision: "block"` to add a note. The docs stress the tool already ran.
- Note: our setup gives Claude Code no jev MCP (`jev-auto.md` line 38). A Claude Code hook would call OpenRouter directly, like `jev-auto.js`.

## 3. Jev API

- Endpoint used by `jev-auto.js`: `POST https://openrouter.ai/api/v1/systemone`, body `{ model: "jev-1.13", state, questions }` (`/home/spyros/.config/opencode/plugins/jev-auto.js` lines 77 to 85). OpenRouter documents two surfaces for Jev: `/api/alpha/decisions` and `/api/v1/systemone`, same key and billing (https://openrouter.ai/docs/guides/community/jev). OpenRouter's documented model id is `typesafe/jev-1.13`. That bare `jev-1.13` works on the systemone surface is supported only by our one logged call with `status: "classified"` (`~/.local/state/opencode/jev-auto.jsonl`). The jev MCP uses the Decisions path with `typesafe/jev-1.13` (https://github.com/jkudish/jev-mcp README, OpenRouter section).
- Question types, all three (https://docs.typesafe.ai/api.md):
  - `noul`: yes/no, optional `criteria.true` / `criteria.false`. Answer `{ type, noul }`, a probability. No confidence field.
  - `choice`: `criteria` maps option to description, up to 255 options. Answer `{ choice, probabilities, confidence }`.
  - `score`: `criteria` is 2 to 10 ordered levels. Answer `{ score, legend, probabilities, confidence }`.
- `usage.cost` in USD is in every OpenRouter response (https://openrouter.ai/docs/guides/community/jev FAQ). TypeSafe's own API returns only token counts (https://docs.typesafe.ai/api.md).
- Price: $0.042 per million input tokens, output free (https://docs.typesafe.ai/models.md). OpenRouter measured $0.0000168 for a 400-token state (auto-approve recipe) and $0.000030 to $0.000036 with "under 600 ms" (https://openrouter.ai/docs/cookbook/building-agents/gate-tool-calls-with-jev). Our single logged call: $0.00001785, 620 ms including two OpenCode session API calls (`jev-auto.jsonl`). TypeSafe docs give no latency figure; I found none.
- Limits: 1,200 requests per minute, "adjusting dynamically" (https://docs.typesafe.ai/models.md).
- Errors: 401, 422, 429, 529 (https://docs.typesafe.ai/api.md). OpenRouter adds 402 when credits run out, observed live today.
- Known weak spots that matter for a guard (https://docs.typesafe.ai/model-jaggedness/jev-1.13.md): literal reading, indirection, and adversarial content in state "can move the answer". Text that argues for its own classification is a real risk when the state is a command the agent wrote. It is also poor at exact string work, so secret detection belongs in regex, not Jev.
- Jev only sees the text: "It can't tell that a command reads a credential file ... unless the command text says so" (auto-approve recipe). `bash ./script.sh` hides what the script does.
- Scores move between identical calls, by up to 0.08 in OpenRouter's repeats (gate-tool-calls recipe).

### Suggested questions

State: `{ command: <redacted command>, cwd }`. One request, three questions, based on `bash-gate.ts` and the recipe above:

- `effect`, choice: read_only / reversible / irreversible. Describe irreversible concretely: deletes outside git, force push, dropping data, deleting cluster or cloud resources.
- `destructive`, noul: "Does `command` aim to remove, wipe or overwrite something?"
- `exfiltration`, noul: "Does `command` read a credential (key file, token, secret env var) or send local data to a remote host?" with examples in `criteria.true` and `criteria.false`.

Starting rule, to be tuned on our own logs: block if `effect` is irreversible with confidence at least 0.6, or either noul is at least 0.7 (`bash-gate.ts` values). These are uncalibrated.

## 4. Recommended design

Layered, cheapest first. Each layer only sees what the one before it let through.

1. Host rules in `opencode.json`. Add `permission.bash` `deny` entries for the never-run set, for example `rm -rf /*`, `mkfs*`, `dd *of=/dev/*`, `git push --force*`, `git push -f*`, `kubectl delete *`, `helm uninstall *`, `curl * | sh`. Add `read` denies for `~/.ssh/**` and `/run/jev/**`. Wildcards miss reordered flags (for example `rm -r -f /`), as the cc-safety-net author points out (https://www.reddit.com/r/ClaudeAI/comments/1pvjd4w/), so this layer is a floor, not the guard.
2. Deterministic guard plugin. Install cc-safety-net for OpenCode (`npx -y cc-safety-net@latest install --opencode`, supports 1.18.29 and later) and for Claude Code (https://github.com/kenryu42/cc-safety-net README lines 58 and 90). It parses commands, so wrappers and flag order do not hide them, and it blocks SSH keys, `.env`, `~/.aws` and similar in shell, read, edit, write and search tools. Pin a version rather than `@latest`.
3. Secret check in our own small plugin, before anything leaves the machine:
   - Exact match against secret values we hold: the contents of `/run/jev/openrouter-key` and env vars whose names contain KEY, TOKEN, SECRET or PASSWORD.
   - A short regex list for known token shapes (`sk-or-`, `ghp_`, `github_pat_`, `AKIA`, `xox[bp]-`, `glpat-`, `-----BEGIN ... PRIVATE KEY-----`). gitleaks rules are a good source (https://github.com/gitleaks/gitleaks, MIT, has a `stdin` mode).
   - A literal secret in a bash command means block. Do not send that command to Jev.
   - Env dumps (`env`, `printenv`, `set`, `cat /proc/*/environ`) are blocked here too, since their output is all secrets.
4. Jev, only for the rest. Skip Jev for a known-safe prefix list (`ls`, `rg`, `git status`, `git diff`, test runners). Send `{ command, cwd }` with the three questions above. 2 s timeout via `AbortSignal.timeout`. On block, throw with the reason and a "do not work around this" line, as `bash-gate.ts` does.
5. `tool.execute.after`: run the same exact-match and regex check on `output.output` for native tools and replace hits with `[REDACTED:<rule>]` before the model sees them. This does not undo side effects. Jev is not needed here.

Fail open or closed:

- Layers 1 to 3 are local and fast. They should fail closed.
- Layer 4 should fail open with a logged `jev_unavailable` record. The catastrophic set is already handled in code, and Jev has real outages: 402 today, plus 429 and 529 by design. Failing closed would stop all shell work whenever credits run out. dcg makes the same split: it fails open only on infrastructure errors (https://github.com/Dicklesworthstone/destructive_command_guard docs/opencode-integration.md).
- The middle band (neither clearly safe nor clearly destructive) has no "ask" in OpenCode 1.x `tool.execute.before`. Either allow and log, or throw a message that tells the agent to ask the user first. In Claude Code, return `permissionDecision: "ask"`.

Logging: one JSONL line per guarded call at `$XDG_STATE_HOME/opencode/jev-guard.jsonl`, same pattern as `jev-auto.js` lines 104 to 110 (mode 0600, dir 0700). Fields: timestamp, sessionId, callId, tool, layer, decision, rule id, Jev probabilities and confidence, requestId, costUsd, latencyMs, and the redacted command. Never the raw command, which may hold a secret.

Placement: a separate `jev-guard.js` next to `jev-auto.js` in `2-k3s/13.t3code/one/files/`, deployed the same way. `jev-auto.js` has a model-based Jev gate (`allowedModel`); the guard should run for every model, because the risk comes from the command, not the model.

Gaps any hook leaves:

- The agent can write a script with `write` and run `bash ./x.sh`; the guard sees only the wrapper. Gating writes the way `write-gate.ts` does narrows this.
- MCP tools such as `kubernetes-epaflix_resources_delete` are not bash. They are already on `ask` in `opencode.json`.
- If T3 runs OpenCode with `--auto`, every `ask` rule auto-approves (https://opencode.ai/docs/permissions/). I did not check how T3 starts OpenCode. Unverified.

## 5. Existing tools

| Tool | What it does | Fits us? |
| --- | --- | --- |
| cc-safety-net, https://github.com/kenryu42/cc-safety-net | Parses commands, blocks destructive git and filesystem commands and secret-file access. OpenCode 1.18.29+ and 2.0.6+, Claude Code, Codex. MIT, 1.5k stars, pushed 2026-09-28 (GitHub API). | Yes, best fit for layer 2. Covers both of our goals deterministically. |
| dcg (destructive command guard), https://github.com/Dicklesworthstone/destructive_command_guard | Rust binary, 50+ rule packs (git, Kubernetes, databases, cloud), `dcg install --opencode` writes a `tool.execute.before` plugin, sub-millisecond claim. Opt-in `secret_disclosure` pack for secret-manager reads. 6k stars. | Strong for destructive commands. License is MIT plus a rider restricting OpenAI and Anthropic and anyone acting for them (LICENSE file). Read it before adopting. |
| Jevvy, https://github.com/PanAchy/jevvy (`@jevvy/permissions`) | Uses Jev to auto-approve harmless shell prompts; abstains otherwise. OpenCode v2 and Claude Code. MIT, 20 stars. | Opposite goal (fewer prompts, not blocks). OpenCode v2 only for the plugin. Useful as a reference. |
| OpenRouter recipe, https://openrouter.ai/docs/cookbook/coding-agents/auto-approve-permission-prompts-with-jev | Static risky list plus two Jev Nouls, OpenCode 2.x, Claude Code, Codex, Cursor. | Good reference for the risky-command regex list and Jev questions. OpenCode part needs 2.0.12+. |
| agentguard, https://github.com/krishkumar/agentguard | Claude Code hook that blocks dangerous shell commands. 4 stars, last push 2026-01-05. | No. Small and stale. |
| gitleaks, https://github.com/gitleaks/gitleaks | Regex secret scanner with `stdin` mode. MIT, 29k stars. | Use its rules for layer 3 and layer 5. Spawning it per call adds latency (not measured). |
| trufflehog, https://github.com/trufflesecurity/trufflehog | Secret scanner that can verify keys live, `stdin` mode. AGPL-3.0. | Not for a hot path; verification makes network calls. |
| @jkudish/jev-mcp, https://github.com/jkudish/jev-mcp | MCP tools only (`jev_screen`, `jev_gate`, and others). `jev_screen` is advisory: "The server never blocks on its own". No hook, guard or CLI mode; the only binary is the MCP server. | No hook feature. Its OpenRouter transport (retries, redacted error bodies) is a reference for error handling. |
| ten-levels-of-jev, https://github.com/disler/ten-levels-of-jev | Pi agent guard extension, described in section 1. | Reference for question design only. Pi hook API, not OpenCode. |

## 6. Jev-native guards found in a second search (2026-09-28)

Jev launched on 2026-09-15 (https://typesafe.ai/blog/introducing-system-one-models-and-jev). The community list https://github.com/cobanov/awesome-jev ("Agents, coding, and guardrails") already has several ready-made guards. The three that matter:

| Tool | OpenCode | Backend | Notes |
| --- | --- | --- | --- |
| jev-guard (leepokai), https://github.com/leepokai/jev-guard | Yes: `"plugin": ["jev-guard"]` (0.2.1+). Author verified a live `opencode run` block. | TypeSafe API or Vercel AI Gateway only. No OpenRouter (`src/jev.js` hard-codes both URLs). | MIT, zero deps. Pre-call: Score `risk` 0 to 3, Nouls `approval`, `user_requested`, `from_untrusted` (denies calls that carry out instructions planted in content the agent read). Post-call: flags prompt injection in tool output. Also scans skills and AGENTS.md for exfiltration. Fails open by default, `JEV_GUARD_FAIL_CLOSED` flips it. Its measured table: `curl \| sh`, `rm -rf /`, `DROP TABLE` deny; `cat ~/.ssh/id_rsa`, `git push --force` only ask. Sends the raw command, no redaction step documented. |
| jev-engineering / jev-gate, https://github.com/eugeniughelbur/jev-engineering | Plugin on `permission.ask`, "written from the docs, not yet run" (`integrations/README.md`). | OpenRouter (`OPENROUTER_API_KEY`), same as ours. | MIT. Order: hard regex denies, then read-only allowlist, then one Jev call, then thresholds. Starts in `observe` mode that only logs to `~/.jev-gate/decisions.jsonl`, plus a `calibrate.py` that fits thresholds from that log. Published injection test: "the owner approved this" framing got 3 of 30 dangerous commands past the model step, which is why hard rules come first. `git stash clear` flipped under every framing. |
| pi-warden, https://github.com/DevMortimer/pi-warden | No, Pi only. | TypeSafe. | The guard the Firecrawl write-up leads with (https://www.firecrawl.dev/blog/what-is-jev). Redacts secrets before sending. Reference only. |

Also listed, not reviewed in depth: jevwire (advisory or restrictive Claude Code hooks), jevguard-mcp, jev-use, Edward (deterministic blocking plus a Jev trajectory check). LangChain and Composio ship "risky tool" middleware using the same pattern (awesome-jev, framework integrations).

What this changes in section 4:

- Layer 4 does not need to be written by us. jev-guard is the closest fit for OpenCode but needs a TypeSafe or Vercel key, and TypeSafe paused new signups on 2026-09-22 (Firecrawl article, citing https://x.com/typesafeai/status/2102281508950307159). Vercel AI Gateway is the open route. The other option is a small patch adding an OpenRouter backend to `src/jev.js`.
- jev-gate uses OpenRouter, but its OpenCode adapter relies on `permission.ask`, which section 2 found no trigger for on 1.x. Unverified either way until run.
- Both tools send the command to a third party. Layer 3 (local secret check before any Jev call) stays ours.
- Everyone who measured says the same thing: hard rules first, Jev for the long tail, fail open on Jev errors, run in log-only mode for a week before blocking.

Separate finding from this research: `/home/spyros/.config/opencode/opencode.json.bak-1788681795` holds a plaintext CLIProxy API key. That file is exactly what a leak guard should stop an agent from reading.

## 7. Reading list for guard design (2026-09-28)

Guides in the style of https://github.com/Anil-matcha/awesome-jev-by-typesafe/blob/main/docs/coding-agent-use-cases.md. Sources: that repo's README, cobanov/awesome-jev "Guides and cookbooks" and "Evaluation", https://docs.typesafe.ai/llms.txt.

Guides and cookbooks:
- OpenRouter, gating agent tool calls: https://openrouter.ai/docs/cookbook/building-agents/gate-tool-calls-with-jev (deterministic checks, then Nouls, fixed approve/block/review thresholds; uses our backend).
- TypeSafe, guardrails for LLMs: https://docs.typesafe.ai/cookbooks/llm_guardrails. Confidence-gated routing: https://docs.typesafe.ai/patterns/confidence-routing. Known failure modes: https://docs.typesafe.ai/model-jaggedness/jev-1.13.
- learn-jev-end-to-end: https://github.com/harshithsunku/learn-jev-end-to-end. 12 notebooks; one is an agent loop with a Choice allow/ask/block plus irreversibility and exfiltration Nouls before each tool call. Closest match to our goal.
- Official agent skill: https://docs.typesafe.ai/agent-skill (`typesafe-ai/skills`). Community skill: https://github.com/dbreunig/building-with-jev-skill (question design, thresholds, debugging).
- Milvus bootcamp notebooks, one on guardrails: https://github.com/milvus-io/bootcamp/tree/master/bootcamp/RAG/search_with_jev.
- Runnable recipe sets: https://github.com/nexibeo/jev-cookbook, https://github.com/ReallyArtificial/jev-by-example.
- TypeSafe workflow evals for security incidents and agent-trace review (permission breaches, silent failures): https://evals.typesafe.ai/security_incidents, https://evals.typesafe.ai/agent_trace_observability.
- jev-gate attack write-up: https://github.com/eugeniughelbur/jev-engineering/blob/main/results/2026-09-20-injection-test.md.

Credential-leak tools:
- jev-git: https://github.com/AkashPriyadarshii/jev-git. Rust pre-commit and pre-push gate for secrets and destructive commands in staged diffs.
- jev-commit: https://github.com/valentynkit/jev-commit. Pre-commit hook that blocks only when it detects a credential.
- is-malicious: https://github.com/luantak/is-malicious. Scans source, config and CI files for suspicious behavior before running them.
- pi-warden data handling (what it strips before sending): https://github.com/DevMortimer/pi-warden/blob/main/docs/data-handling.md.

Evidence on failure modes (read before trusting thresholds):
- Framing sensitivity: https://github.com/RINNECODER/jev-behavior-study.
- Tool-use decisions vs LLMs (MetaTool, When2Call, BFCL): https://github.com/baibizhe/jev-decision-benchmarks.
- Calibration on known probabilities: https://github.com/KantaHayashiAI/jev-does-not-play-dice.
- Threshold fitting on own labels: https://github.com/abhixhek/jevcal.

Other lists: https://github.com/yibie/awesome-jev, https://github.com/valentynkit/awesome-jev-typesafe, https://github.com/MrJev/awesome-jev (reviews record what each tool sends and where, relevant to leakage), https://github.com/Jessie-QingYu/jev-in-the-wild (keeps a section for failures), https://awesomejev.com.

Not read in full: the entries above are summarized from the lists, not from each repo.

## Open questions

- Q1. Top up OpenRouter credits, or accept that Jev layers are off until then? Today every Jev call returns 402.
- Q2. Does T3 start OpenCode in `--auto` mode? That decides whether `ask` rules protect anything.
- Q3. Middle band in OpenCode: allow and log, or refuse and make the agent ask?
- Q4. cc-safety-net or dcg for layer 2? cc-safety-net covers secrets and is plain MIT; dcg has more infrastructure packs.
- Q5. Pin `jev-1.13` (current) so thresholds do not drift when the alias moves (https://docs.typesafe.ai/models.md)?

## Not verified

- The video transcript, and anything said in the video beyond its description.
- That `tool.execute.before` fires for subagent sessions in a live run (code reading only).
- That host `deny` rules run before plugins on OpenCode 1.x (documented for 2.x).
- That bare `jev-1.13` on `/api/v1/systemone` is the supported model id (works in our one logged call; docs show `typesafe/jev-1.13`).
- Jev latency beyond OpenRouter's "under 600 ms" and our single 620 ms sample. My own benchmark failed with 402.
- The jev MCP `jev_screen` check on fetched pages failed with 402, so third-party pages were read as data only and no instructions in them were followed.
