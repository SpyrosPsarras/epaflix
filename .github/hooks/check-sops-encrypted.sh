#!/usr/bin/env bash
# Pre-commit hook: refuse to commit any staged YAML that declares
# `kind: Secret` unless it has been sops-encrypted (contains a `sops:`
# block) OR is explicitly in the project's "imperative Secret" allowlist.
#
# Wire up via .github/hooks/install-hooks.sh (one-shot).
set -euo pipefail

# Files allowed to remain unencrypted Secret YAML — placeholder manifests
# that ArgoCD already excludes from sync by kustomization comments.
# Each entry is a relative path from repo root.
ALLOWLIST=(
  "2-k3s/11.argocd/oidc-secret.yaml"
  "2-k3s/06.postgres/operator-kustomization/barman-manifest.yaml"
  "2-k3s/12.renovate/secret-app.yaml"
  # Same shape as the renovate one: placeholder-only, never in any
  # kustomization `resources:`, so ArgoCD never applies it. It only tripped
  # the hook once #461 touched it — it had not been staged since the hook
  # landed.
  "2-k3s/07.authentik-deployment/secret-app.yaml"
)

fail=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  case "$f" in
    *.yaml|*.yml) ;;
    *) continue ;;
  esac
  # Skip deleted files
  [ -e "$f" ] || continue

  # Is the staged version a Secret?
  if git show ":$f" 2>/dev/null | grep -qE '^kind:[[:space:]]+Secret[[:space:]]*$'; then
    # Allowlisted?
    skip=0
    for a in "${ALLOWLIST[@]}"; do
      [ "$f" = "$a" ] && skip=1
    done
    [ $skip -eq 1 ] && continue

    # Encrypted?
    if git show ":$f" 2>/dev/null | grep -q '^sops:'; then
      continue
    fi
    echo "ERROR: $f is a plaintext k8s Secret. Encrypt with: sops -e -i $f && mv $f ${f%.yaml}.enc.yaml" >&2
    fail=1
  fi
done < <(git diff --cached --name-only --diff-filter=ACMR)

exit $fail
