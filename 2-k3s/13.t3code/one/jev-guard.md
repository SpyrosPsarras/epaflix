# Pi safety layers

The entrypoint installs these layers before T3 starts Pi through `/scripts/pi.sh`.

1. `pi-permission-system` reads `~/.pi/agent/extensions/pi-permission-system/config.json`, written by `pi-setup.py`. It denies destructive host commands, environment dumps including `printenv*`, and access to `/run/jev*`. Hub ASK tools require approval before execution.
2. `cc-safety-net-install.py` builds and hash-checks the reviewed cc-safety-net 2.6.4 Pi extension. It blocks destructive Git and filesystem commands and secret-file access. `ssh-policy.py` protects SSH keys and the vault login while permitting attachment identifiers and public host-key snapshots.
3. Upstream `jev-guard`, pinned to `cda06c49ceaac207904142ec92bfab793bbd18b1`, judges tool calls. `pi.sh` sets `JEV_GUARD_CONFIG=/run/jev-guard/config.json`; `pi-setup.py jev-config` writes it from the mounted key. The config lives in a memory-backed volume. Eligible tool results of at least 200 characters, including `bash` and `read` output, are sent to Jev for instruction screening. pi-redact is registered first, but extension handler order and propagation of redacted results remain unverified. Jev prefixes flagged results with a warning and judges instruction files such as `SKILL.md` and `AGENTS.md` separately. With no key or a Jev error, it warns and allows the call because `JEV_GUARD_FAIL_CLOSED` is not set. A Jev failure during result screening passes the result unscreened. `pi.sh` exempts vaultwarden MCP calls AND results through `JEV_GUARD_SKIP_TOOLS` and `JEV_GUARD_SKIP_SCAN`; neither is sent to Jev. Keep that list in sync with vault tools in `hub_clients.py`. It denies calls that appear to follow instructions from untrusted content; an ask blocks without a UI, though `JEV_GUARD_ASK_SCORE=3` and `JEV_GUARD_ASK_P=1` make asks practically never happen.
4. `pi-redact` masks credentials in tool output before the model sees them.

CI runs the Pi setup and T3 settings Python tests. The image smoke starts Pi in RPC mode, checks registered extension commands and package skills, rejects extension load errors, and exercises the shipped permission pipeline and Jev threshold decisions. It also checks that `--list-models` lists only cliproxy models and migrates OpenCode-era T3 settings with a private rollback backup. It does not test live Jev judgments or model-driven tool execution.

## Not covered

- Secrets inside tool arguments are not checked.
- Jev Auto model routing is dropped.
- Exact environment-value masking is replaced by pi-redact patterns. They miss bare `omp-` keys and mask SSH config output.
- The review-gate skill names the OpenCode tool `jev_jev_noul`; Pi exposes it as `mcp__jev__jev_noul`.
- Unlike the removed OpenCode guard, Jev result screening is not limited to OpenAI and Anthropic providers.
