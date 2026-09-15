# Dynamic model limits

Claude limits come from authenticated `https://api.anthropic.com/v1/models`.
OpenRouter limits come from `https://openrouter.ai/api/v1/models`, including
`top_provider.max_completion_tokens` and any smaller top-provider context.
The implementation uses each provider's existing credential and HTTP transport.
It follows Anthropic pagination and matches configured OpenRouter aliases to
upstream IDs before applying limits.

The existing three-hour catalog refresh also triggers these fetches when the
static catalog is unchanged or unavailable. Successful results are cached per
account for two hours. Failed attempts retain the last successful in-memory
result and permit another attempt after five minutes, on the next registration
or periodic refresh. Restarting discards this cache. Missing upstream models
and first-fetch failures use CLIProxyAPI's existing metadata. `--local-model`
disables the periodic updater. Explicit configuration overrides take precedence.

The user chose to trust live catalog values even when documentation conflicts,
including Sonnet 4.5. There are no model-specific limit corrections in this
patch. Retired models absent from live catalogs retain upstream fallback data.

Codex continues to use CLIProxyAPI's separately refreshed subscription model
templates. The prefix fix lets `codex/` IDs find those templates. It does not
query the authenticated Codex catalog per account and does not substitute public
OpenAI API limits. OpenCode reads `max_context_window` before `context_window`.
Codex output values remain template metadata, not independently verified limits.

## Rebuild

Follow `README.md` through the Claude and auth-selection patches. Before its test
and build steps, run:

```sh
git apply "$RECIPE_DIR/model-limits.patch"
cp "$RECIPE_DIR/upstream_model_limits.go" sdk/cliproxy/
cp "$RECIPE_DIR/upstream_model_limits_test.go" sdk/cliproxy/
go test ./sdk/cliproxy/... ./internal/registry ./internal/client/codex/models ./internal/config ./internal/runtime/executor ./internal/runtime/executor/helps
CGO_ENABLED=1 go build -trimpath -o CLIProxyAPI ./cmd/server
```

Publish with a new tag and pin the returned digest in `kustomization.yaml`.
The recipe preserves the existing Claude and auth-selection changes.

Published binary SHA-256:
`1cb90bfdcf34ad42981803a80a3996842cc8a1fee898aa8786eb397746b2e421`.
Image tag `dynamic-limits-20260915`, digest
`sha256:14b34253b9eab832b4acaec612ed8b3397af10d97878b76d78fe8272b1631dec`.

## Verification and deployment

Focused tests cover pagination, response fields, invalid catalog rejection,
alias matching, shared-object preservation, and configuration overrides.
SDK, registry, Codex model, config, and executor package checks passed on rerun.
An existing auth publisher timing test failed on the first run. CGO build passed.
Independent Fable Standards and Spec source reviews passed.

Deployment and live validation are pending PR merge. Argo CD self-heals manual
image changes back to Git. After rollout, inspect `upstream model limits` logs,
`/v1/models?client_version=99.0.0`, and `opencode models cliproxy --verbose`.
Restart existing OpenCode processes to reload the discovery plugin.
Manual OpenRouter output settings from the earlier revision are no longer needed.

## Sources

- https://platform.claude.com/docs/en/api/models/list
- https://platform.claude.com/docs/en/build-with-claude/context-windows
- https://openrouter.ai/api/v1/models
- CLIProxyAPI's `internal/registry/codex_client_models_updater.go`
