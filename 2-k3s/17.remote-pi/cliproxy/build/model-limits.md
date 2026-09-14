# Model limit correction, 2026-09-14

Apply `model-limits.patch` after the existing Claude and auth-selection patches
in `README.md`, before building. Keep CGO enabled and the same base image.
The resulting image is pinned in `../../kustomization.yaml`.

The published binary SHA-256 is
`b460eca2ec9ed224ae2630d0e60659c94cd4721b32cbe20216c6be341feaf9a3`.

## Findings

The authenticated Codex catalog at
`https://chatgpt.com/backend-api/codex/models?client_version=0.154.0`
returned default 272000 and maximum 872000 for Astra, Luna, Terra, Sol and
auto-review. GPT-5.5 returned 272000 for both. The deployed proxy missed
templates for `codex/`-prefixed IDs. The patch restores template lookup and
preserves the scoped routing slug. OpenCode now selects `max_context_window`.
Remote template updates remain enabled, so these values can change upstream.

The public OpenAI API advertises 1050000 context and 128000 output for Astra
and GPT-5.6. That context is not substituted for the subscription limit.
The existing Codex output value of 128000 is retained. The authenticated
catalog does not independently confirm output limits. Spark is absent from
that catalog; its existing 128000/128000 values remain unverified.

Anthropic's authenticated Models API confirms Sonnet 4.6 at 1000000/128000.
Archived official documentation gives Sonnet 3.7 at 200000/64000 and Haiku 3.5
at 200000/8192. The latter two are retired. Other listed Claude limits match
published values. Sonnet 4.5 retains the plain endpoint's 200000/64000 because
its 1M window requires the beta header. Fable 5.1 is 1000000/128000.

OpenRouter's model catalog reports context 1310720 for GLM 5.3 Flash and
DeepSeek V4 Flash 0731, with top-provider output limits 131072 and 943718.
Backend limits vary. MiniMax M3 Free is absent from the current catalog;
its retained fallback limits are not verified capabilities.

## Validation

The registry, config, service, Codex catalog, auth, Claude executor and helper
Go test packages passed. The CGO server build passed. Separate Fable 5.1
Standards and Spec reviews passed before image publication.

Argo CD reverted the initial live image change to the Git-pinned version.
This PR must be merged before rollout and final live verification can complete.
After deployment, verify `/v1/models?client_version=99.0.0` and
`opencode models cliproxy --verbose` on t3code. Restart existing OpenCode
processes to load the changed plugin.

After the new image is running, update the existing OpenRouter model entries
through the management API, preserving all other provider settings. Set
`max-completion-tokens: 131072` on `z-ai/glm-5.3-flash` and
`max-completion-tokens: 943718` on `deepseek/deepseek-v4-flash-0731`.
The old image silently discards this field. The first attempt was discarded
after Argo CD reverted the image, so these values are still pending.

## Sources

- https://developers.openai.com/api/docs/models/gpt-6-astra
- https://developers.openai.com/api/docs/models/gpt-5.5
- https://developers.openai.com/api/docs/models/gpt-5.6-sol
- https://platform.claude.com/docs/en/about-claude/models/overview
- https://platform.claude.com/docs/en/api/models/list
- https://web.archive.org/web/20250820094837/https://docs.anthropic.com/en/docs/about-claude/models/overview
- https://openrouter.ai/api/v1/models
