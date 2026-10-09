# Pi safety layers

The entrypoint installs these layers before T3 starts Pi through `/scripts/pi.sh`.

1. `pi-permission-system` reads `~/.pi/agent/extensions/pi-permission-system/config.json`, written by `pi-setup.py`. It denies destructive host commands, environment dumps including `printenv*`, and access to `/run/jev*`. Hub ASK tools require approval before execution.
2. `cc-safety-net-install.py` builds and hash-checks the reviewed cc-safety-net 2.6.4 Pi extension. It blocks destructive Git and filesystem commands and secret-file access. `ssh-policy.py` protects SSH keys and the vault login while permitting attachment identifiers and public host-key snapshots.
3. Upstream `jev-guard`, pinned to `cda06c49ceaac207904142ec92bfab793bbd18b1`, judges tool calls. `pi.sh` sets `JEV_GUARD_CONFIG=/run/jev-guard/config.json`; `pi-setup.py jev-config` writes it from the mounted key. The config lives in a memory-backed volume. It screens eligible tool results of at least 200 characters for text aimed at AI agents, prefixes flagged results with a warning, and judges instruction files such as `SKILL.md` and `AGENTS.md` separately. With no key or a Jev error, it warns and allows the call because `JEV_GUARD_FAIL_CLOSED` is not set. `pi.sh` exempts vaultwarden MCP calls through `JEV_GUARD_SKIP_TOOLS`. It denies calls that appear to follow instructions from untrusted content; an ask blocks without a UI, though `JEV_GUARD_ASK_SCORE=3` and `JEV_GUARD_ASK_P=1` make asks practically never happen.
4. `pi-redact` masks credentials in tool output before the model sees them.

CI runs the Pi setup and T3 settings Python tests. The image smoke starts Pi in RPC mode, checks registered extension commands and package skills, rejects extension load errors, and checks the environment-dump deny policy. It does not test live Jev judgments or model-driven tool execution.
