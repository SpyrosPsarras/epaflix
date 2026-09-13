# CLIProxyAPI auth-selection backport

The deployed v7.2.148 build preserves Claude PR #5411 and backports the
auth-selection portion of upstream commit `5ab0bca040cdfe17997c6f34da4247fa99518093`.
The two modified auth files match that upstream patch exactly.

## Failure and verification

On 2026-09-12, OpenRouter rejected the retired MiniMax free model with 404.
Its failed model state could mark the shared credential unavailable. Prefixed
GLM requests passed model-specific validation, then the built-in selector
checked aggregate credential availability again with an empty model name.
It rejected healthy GLM with `auth_unavailable` without contacting OpenRouter.
Unprefixed GLM used the scheduler path and succeeded, clearing the aggregate
state and temporarily restoring prefixed requests.

`openrouter_prefix_incident_test.go` reproduces that distinction through
`Manager.ExecuteStream`: the bare route passes and the prefixed route fails
before the backport; both pass afterward. The auth, executor, and executor/helps
test packages passed with the existing Claude patch included. Independent
Fable 5.1 Standards and Spec reviews passed before image publication.

The exact production transition that recomputed aggregate availability was
not captured. The failed model, premature local rejection, recovery pattern,
and code-level reproduction were verified independently.

## Rebuild

Requires Git, Go 1.26, a C compiler, tar, and `crane`. Run from a fresh temporary directory.
CGO must be enabled for the dynamic plugin loader. Check the binary's required
GLIBC versions against the base image before publication. The verified build
requires at most GLIBC 2.34; the base image provides 2.36.
Set `RECIPE_DIR` to this directory's absolute path first. These commands do not
need production credentials until the final registry push.

```sh
git clone https://github.com/router-for-me/CLIProxyAPI.git source
cd source
git checkout --detach d577e630b18bcc13e852888c9c0cc34b92cff72d
git fetch origin refs/pull/5411/head
git cat-file -e da1b2efabc8957f55c7406ff0da0e67c41e309a6
GIT_COMMITTER_NAME=Builder GIT_COMMITTER_EMAIL=builder@localhost \
  git merge --no-commit --no-ff da1b2efabc8957f55c7406ff0da0e67c41e309a6
cp "$RECIPE_DIR/openrouter_prefix_incident_test.go" sdk/cliproxy/auth/
# Expected failure for the prefixed route only:
go test ./sdk/cliproxy/auth -run '^TestOpenRouterPrefixIncident$' -count=1 -v
git show 5ab0bca040cdfe17997c6f34da4247fa99518093 --format= \
  -- sdk/cliproxy/auth/conductor_selection.go sdk/cliproxy/auth/selector.go \
  | git apply
go test ./sdk/cliproxy/auth -run '^TestOpenRouterPrefixIncident$' -count=1 -v
go test ./sdk/cliproxy/auth ./internal/runtime/executor ./internal/runtime/executor/helps -count=1
CGO_ENABLED=1 go build -trimpath \
  -ldflags '-s -w -X main.Version=v7.2.148-pr5411-authfix -X main.Commit=d577e63+da1b2ef+5ab0bca-auth -X main.BuildDate=2026-09-12T18:20:00Z' \
  -o CLIProxyAPI ./cmd/server
tar -cf ../authfix-layer.tar --transform='s|^CLIProxyAPI$|CLIProxyAPI/CLIProxyAPI|' CLIProxyAPI
crane append \
  --base ghcr.io/spyrospsarras/cli-proxy-api@sha256:889632d564efe313c1a334690fc0ddad1a0545ff78c48aab595f0e2028dbe8e7 \
  --new_layer ../authfix-layer.tar \
  --new_tag ghcr.io/spyrospsarras/cli-proxy-api:pr-5411-authfix-cgo-20260912 \
  --output ../authfix-image.tar
# Authenticate with a registry-write credential before publishing a new tag.
crane push ../authfix-image.tar ghcr.io/spyrospsarras/cli-proxy-api:YOUR_NEW_TAG
```

The published image adds the replacement binary to the previous image, retaining
its libraries, paths, and startup command. Binary SHA-256 from this build:
`e2610308749a26cd3c65177b6db0de930b87272c54368b35f84366712febe8c8`.
Before rollout, this binary loaded and registered all three existing plugins
in an isolated process inside the production base container: pi-bridge,
opencode-cloak, and cliproxyapi-copilot. The initial CGO-disabled build failed
that check after rollout and was replaced by this CGO-enabled image.
Image layer timestamps can change the image digest on rebuild; always pin the
digest returned by the registry.

## Retire or roll back

Return to an upstream release only after it contains both the prevalidated
auth-candidate fix and Claude `thinking.block_binding` support. v7.2.159 has
the auth fix but lacks the latter. Rollback is the previous image digest
listed in the rebuild command, which restores the known auth-selection bug.
