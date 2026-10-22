
# Isolated runtime facts

## Secrets

Read secrets only with the stock client:

```sh
vault run --name '<exact item name>' [--password-env VAR] [--username-env VAR] [--field NAME=VAR] [--attachment NAME --attachment-env VAR] -- <command>
```

Target variables must match `VAULT_[A-Z][A-Z0-9_]{0,63}`, for example
`--password-env VAULT_API_TOKEN` or `--field token=VAULT_JIRA_TOKEN`.
Values reach only that command. Attachment variables hold temporary file paths.
The child receives no stdin. The client discards its stdout and stderr and prints only its
exit code. To retrieve a non-secret result, create a private temp directory with
`mktemp -d`, have the command write the result to a file there, then read that
file afterwards. Never write the secret value itself.
`--name` must match exactly one item in this pod's collection, case-sensitive,
without a folder prefix. Legacy mixed-pod paths like `/Folder/name` do not apply.
Use `vault list [filter]` to discover scoped item names, types and available value-source names without values.
When `vault list sops` shows `sops-age-k3s-cluster` in this pod, use non-interactive SOPS commands with:
`vault run --name sops-age-k3s-cluster --password-env VAULT_SOPS_AGE_KEY -- sh -c 'SOPS_AGE_KEY="$VAULT_SOPS_AGE_KEY" sops ...'`
Because `vault run` gives the child no stdin and drops its output, interactive `sops edit` and plain `sops -d` to stdout do not work. Use `sops set`, `sops -d --output <private temp file>`, or `sops -e/-d -i`; keep decrypted files private and delete them when finished.
A refusal without detail usually means an invalid target variable or an item name that does not
match exactly one item. Writes use `vault create/update/trash/attach`; read
[the write contract](https://github.com/SpyrosPsarras/epaflix/blob/main/2-k3s/13.t3code/env/vault-writes.md)
before writing. Agent `bw` is intentionally unavailable and not logged in;
there are no Vaultwarden MCP tools. Use only this environment's vault.

## Python packages

There is no system pip. For Python packages, create a virtual environment with `python3 -m venv <dir>` and use `<dir>/bin/pip` and `<dir>/bin/python`, or install a tool into `~/.local/bin`. Do not use sudo or system package managers.

## Model routing

A thread resumed after a pod restart may lack T3 delegation tools such as `delegate_task`, `task_status`, and `orchestrator_capabilities`; if so, write a handoff file and ask the owner to continue in a new thread instead of working without subagents.

T3 `delegate_task` works with `providerInstanceId: pi` and model IDs
`cliproxy/<route>/<model>`. Call `orchestrator_capabilities` before the first
delegation and pass a model ID exactly as that list prints it. Do not guess
public Anthropic or OpenAI names from memory; the catalogue is served by
CLIProxy and its names and set change often. The Codex instance is disabled
here because its discovered bare model IDs fail CLIProxy routing.
