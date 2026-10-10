# Pi safety layers

The entrypoint installs these layers before T3 starts Pi through `/scripts/pi.sh`.

1. `pi-permission-system` reads `~/.pi/agent/extensions/pi-permission-system/config.json`, written by `pi-setup.py`. It denies destructive host commands and environment dumps including `printenv*`. It allows every MCP tool. T3's MCP extension skips approval in full-access mode and asks for non-read-only tools in other runtime modes.
2. `cc-safety-net-install.py` builds and hash-checks the reviewed cc-safety-net 2.6.4 Pi extension. It blocks destructive Git and filesystem commands and secret-file access. `ssh-policy.py` protects SSH keys and the vault login while permitting attachment identifiers and public host-key snapshots.
3. `pi-redact` masks credentials in tool output before the model sees them.

CI runs the Pi setup and T3 settings Python tests. The image smoke starts Pi in RPC mode, checks registered extension commands and package skills, rejects extension load errors, and exercises the shipped permission pipeline. It also checks that `--list-models` lists only cliproxy models and migrates OpenCode-era T3 settings with a private rollback backup. It does not test model-driven tool execution.

## Not covered

- No model judges tool-call risk. Only commands and paths covered by pi-permission-system and cc-safety-net rules, including deny paths configured by ssh-policy.py, are blocked.
- Secrets inside tool arguments are not checked.
- Tool output, fetched content and instruction files are not screened for prompt injection, and no layer blocks tool calls that follow injected instructions.
- Exact environment-value masking is replaced by pi-redact patterns. They miss bare `omp-` keys and mask SSH config output.
