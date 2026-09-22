# Jev recommendation trial

The primary OpenCode home gets `jev-shadow.js` from the pod entrypoint.
It observes incoming user messages through `chat.message`, records a task class,
and never changes the selected model, user message, tools or permissions.
Legacy `t3env-*` provider homes are not enrolled.

## Enable and disable

The SOPS-encrypted `jev-secret.enc.yaml` is rendered by KSOPS and mounted
read-only at `/run/jev/openrouter-key`. `JEV_OPENROUTER_KEY_FILE` selects that
path. The plugin reads it for each message. An absent or empty key disables
network requests and log writes. To disable through GitOps, remove the plugin
installation and its installed copy through the deployment entrypoint.
Outside Kubernetes the fallback is `~/.config/opencode/jev-openrouter-key`.
Never commit a plaintext credential.

The plugin itself loads when OpenCode starts. Restart the affected OpenCode
sessions after installation. Deploy the ConfigMap/entrypoint through the normal
T3 deployment workflow; avoid restarting the server during active turns.

Only direct user text is sent to OpenRouter, capped at 12,000 characters.
Attachments and oversized messages are skipped. Synthetic text, including
expanded file contents, is excluded. No conversation history or tool output is
sent. Short follow-ups should produce `needs_context`, not a cheap-model route.
Each eligible user message makes one request with a 2.5-second network timeout,
no retries. The hook is awaited and adds this latency before generation.
Failures leave normal OpenCode processing intact.

## Inspect the trial

Records are in `~/.local/state/opencode/jev-shadow.jsonl`, or under
`$XDG_STATE_HOME/opencode` when set. They contain project and directory, session
and message IDs, selected model, task class, confidence, elapsed time, request
ID, token usage and actual decision cost. Prompt text and credentials are not
logged. `[jev-shadow]` entries in OpenCode stderr identify outcomes. This does
not add a T3 UI panel or include Jev spend in T3's model usage totals.

After roughly 50 tasks, join session/message IDs to OpenCode history and label
whether each recommendation was appropriate. Count errors, skipped inputs,
latency and total Jev cost. Separate OpenRouter cash spend from Codex/Claude
subscription quota. Confidence alone is not evidence of routing accuracy.
The trial incurs decision costs but does not yet save model costs.

Task classes are `routine`, `complex` and `needs_context`; model mappings require
measured task outcomes and are deliberately not enabled in this trial.

## Verification

Run `node --test 2-k3s/13.t3code/one/files/jev-shadow.test.mjs`.
OpenCode v1.18.31's `createUserMessage` invokes `chat.message` after resolving
parts and before persisting the message and entering the model loop. The plugin
only reads that payload. Source:
https://github.com/anomalyco/opencode/blob/v1.18.31/packages/opencode/src/session/prompt.ts
