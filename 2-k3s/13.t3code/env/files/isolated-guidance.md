
# Isolated runtime facts

## Secrets

Read secrets only with the stock client:

```sh
vault run --name '<exact item name>' [--password-env VAR] [--field NAME=VAR] [--attachment NAME --attachment-env VAR] -- <command>
```

Target variables must match `VAULT_[A-Z][A-Z0-9_]{0,63}`, for example
`--password-env VAULT_API_TOKEN` or `--field token=VAULT_JIRA_TOKEN`.
Values reach only that command. Attachment variables hold temporary file paths.
The client discards the child command's stdout and stderr and prints only its
exit code. To retrieve a non-secret result, create a private temp directory with
`mktemp -d`, have the command write the result to a file there, then read that
file afterwards. Never write the secret value itself.
`--name` must match exactly one item in this pod's collection, case-sensitive,
without a folder prefix. Legacy mixed-pod paths like `/Folder/name` do not apply.
There is no list command; ask the owner for exact item names. A refusal without
detail usually means an invalid target variable or an item name that does not
match exactly one item. Writes use `vault create/update/trash/attach`; read
[the write contract](https://github.com/SpyrosPsarras/epaflix/blob/main/2-k3s/13.t3code/env/vault-writes.md)
before writing. Agent `bw` is intentionally unavailable and not logged in;
there are no Vaultwarden MCP tools. Use only this environment's vault.

## Model routing

T3 `delegate_task` works with `providerInstanceId: pi` and model IDs
`cliproxy/<route>/<model>`, such as `cliproxy/codex/gpt-6.1-sol` or
`cliproxy/claude/claude-opus-5-5`; the Codex instance is disabled here because
its discovered bare model IDs fail CLIProxy routing.
