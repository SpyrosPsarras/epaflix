# Jev guard

Blocks catastrophic shell commands and credential leaks in OpenCode tool calls.
Three layers, cheapest first. Research: `docs/jev-tool-guard-research.md`
(written before the decisions below; this file is the current source).

1. Host rules. The entrypoint seeds `permission.bash` in `opencode.json` with
   denies for `mkfs`, `dd of=/dev/…`, `kubectl delete|drain|cordon`,
   `helm uninstall`, `reboot`, `shutdown`, `poweroff`, `qm stop|shutdown`.
   The node and VM ones enforce the "ask Spyros first" rule; run them yourself.
   It only seeds a missing `permission.bash`, without a `"*"` key, so your edits
   and a top-level default stay. There are no read rules: OpenCode matched
   read patterns against project-relative paths and `**/.ssh/**` missed in a
   test, so reads are left to layers 2 and 3.
2. cc-safety-net, pinned in the entrypoint (`SAFETY_NET`) and put in the
   `plugin` list on every start. It parses commands and blocks destructive git
   and filesystem commands (`rm -rf`, `git reset --hard`, `git push --force`)
   and reads of secret files (SSH keys, `.env`, `~/.aws`, `opencode.json`, CLI
   credential files) in every tool. Bump the pin by hand; Renovate does not
   track it.
3. `files/jev-guard.js`, an OpenCode plugin.
   - Every model, on the pod, nothing sent out:
     - blocks a tool call whose arguments hold a secret environment value
       (names with KEY, TOKEN, SECRET, PASS or AUTH, 16+ characters), the Jev
       key, or a known token shape (`sk-`, `gh[pousr]_`, `github_pat_`,
       `AKIA`, `xox?-`, `glpat-`, `omp-`, age keys, PEM private keys). Env
       values that start with `/` (socket and file paths) are ignored.
       `keepass_*` tools are exempt;
     - blocks a path argument or bash command that names `/run/jev`;
     - blocks bash commands that print the environment: bare `env`, `set`,
       `export`, `declare`, any `printenv`, `/proc/*/environ`, `ps e`, also
       behind `bash|sh|zsh -c`, `command`, `exec`, `nice` or `busybox`;
     - masks the same credentials in every tool output, text and resource
       items, before the model sees it. `keepass_*` output is left alone.
   - OpenAI and Anthropic models only (same rule as `jev-auto.md`; the Jev Auto
     model counts), one Jev call through OpenRouter with the existing
     `/run/jev/openrouter-key`:
     - every bash command except plain routine ones gets a risk score from 0 to 3
       and two probabilities: reads or sends a credential, and downloads and
       runs code. Blocked at risk >= 2.5, credential >= 0.8 or download-and-run
       >= 0.8. Everything below runs. Sending ordinary data out (git push,
       deploys, API calls) is not asked about; wider wording blocked normal work.
       Routine means `ls`, `pwd`, `echo`, `wc`, `rg`, `grep`, `which`, `stat`,
       `du`, `df`, `date`, `uname`, `whoami`, `id`, `tree`,
       `git status|diff|log|show|blame|rev-parse|ls-files|branch --show-current`, `npm test`,
       `npm run test|lint|build`, `node --test`, `pytest`, `go test`,
       `cargo test`, with no chaining, redirection, substitution, `-o`,
       `--output` or `--pre`. Tests and builds run project code; that is
       accepted.
     - output of 200+ characters from tools that return third-party text
       (`webfetch`, `websearch`, `codesearch`, `gmail_*`, `searxng_*`,
       `notion_*`, `t3-code_preview_*`, `kubernetes-epaflix_pods_log`) gets a
       prompt injection probability, after masking. At >= 0.7 a warning is put
       in front of it. Other MCP tools (vault, cluster reads) are not sent.
   - Jev errors and 2 s timeouts fail open and are logged. Layers 1 and 2 and
     the local checks do not depend on Jev.

Blocks reach the model as a tool error telling it not to work around the
block and to ask the user. Blocking is on from the first start.

## Review

Log: `~/.local/state/opencode/jev-guard.jsonl`, mode 0600, rotated once at
20 MB to `jev-guard.jsonl.1`. One line per local block, per bash command from
a model without Jev (`no_jev_model`), per Jev decision and per Jev error:
time, session, call, tool, layer (`local` or `jev`), decision (`block`,
`allow`, `flag`, `error`), reason, Jev answers, model, request id, cost,
latency and the command with credentials masked. Routine commands are not
logged.

```sh
jq -c 'select(.decision=="block")' ~/.local/state/opencode/jev-guard.jsonl
jq -s 'group_by(.decision)|map({(.[0].decision):length})|add' ~/.local/state/opencode/jev-guard.jsonl
jq -s 'map(.costUsd//0)|add' ~/.local/state/opencode/jev-guard.jsonl
```

Thresholds are in `blockAt` in `files/jev-guard.js`. The log keeps Jev's
numbers, so a new threshold can be checked against past calls before it ships.

## Limits

- `bash ./script.sh` is judged by its text, not by what the script does.
- Output masking hides credentials in files the agent reads, so it cannot copy
  a real credential into another file. That is intended.
- A session the plugin has not seen a turn for gets the local checks but no
  Jev check.
- Claude Code and Codex on this pod are not covered.

## Checks

- Offline: `node --test 2-k3s/13.t3code/one/files/jev-guard.test.mjs` (in CI).
- Live, after changing questions or thresholds:
  `XDG_STATE_HOME=$(mktemp -d) node 2-k3s/13.t3code/one/files/jev-guard.live.mjs`.
  It judges 21 labeled commands (never runs them), about $0.0005. On
  2026-09-28 against `jev-1.13` all 21 matched, 250 to 560 ms per call. The
  first question wording blocked `npm install`, `git push`, `kubectl apply`
  and `gh pr create`. `git reset --hard && git clean -fdx` scored 2.12 and is
  left to cc-safety-net.
- An isolated `opencode run` (1.18.33) confirmed the plugin loads, a local
  block reaches the model as a tool error, cc-safety-net blocks
  `git reset --hard`, and bash denies work without a `"*"` key.
