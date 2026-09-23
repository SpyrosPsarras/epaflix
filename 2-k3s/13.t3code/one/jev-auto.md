# Jev Auto

Select provider `Jev Auto`, model `auto`, in T3's OpenCode model picker.
Manual selections keep their current behavior. The picker remains on Auto;
the executor is instructed to prefix its reply with its actual model. The
authoritative route is in `$XDG_STATE_HOME/opencode/jev-auto.jsonl`, defaulting
to `~/.local/state/opencode/jev-auto.jsonl`. Reply prefixes are best-effort.

For a standalone initial text request, Jev can choose `gpt-6-luna` at confidence
0.9 or above. Everything else uses `gpt-6-astra`: follow-ups, child sessions,
attachments, synthetic context, long prompts, uncertain classifications and
classifier failures. This first version does not classify conversation history.
The chosen model stays fixed through that message's tool loop. Executor failures
are handled by OpenCode normally; there is no automatic replay on Astra.

Both executors use the existing Codex subscription through CLIProxyAPI. This
preserves the smaller-model option without adding paid OpenRouter executor
requests. It does not establish monetary savings or remaining subscription
quota. Jev classification is billed through the existing OpenRouter key.

The plugin registers Auto only when both executor models are available in the
CLIProxyAPI catalog. The existing shadow classifier skips Auto requests.
Routes log IDs, model, classification, confidence, latency and decision cost,
never prompt text or credential values. Tool permissions are unchanged.

Deployment uses the scripts ConfigMap and pod entrypoint. No direct home-file
installation is required. The existing mounted Jev Secret is reused.

Checks: `node --test 2-k3s/13.t3code/one/files/jev-auto.test.mjs`.
An isolated OpenCode run verified that the first typo task executed on Luna and
a context-dependent follow-up executed on Astra. These are integration checks,
not a task-quality benchmark or a claim of measured savings.
