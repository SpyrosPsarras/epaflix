#!/usr/bin/env bash
# Brings the t3code guest to the versions in versions.env. Run as root on the
# guest. A stamp of the last applied versions.env makes the daily run a no-op
# unless Renovate changed a pin. Restarts t3code.service when t3 changed.
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
T3_USER=${T3_USER:-spyros}
STAMP=/var/lib/t3code/versions.applied
. "$DIR/versions.env"

if [[ -f $STAMP ]] && cmp -s "$STAMP" "$DIR/versions.env"; then
  echo "t3code already at pinned versions"
  exit 0
fi
prev_t3=$(sed -n 's/^T3_VERSION=\([^ ]*\).*/\1/p' "$STAMP" 2>/dev/null || true)

npm install -g "t3@$T3_VERSION" "@anthropic-ai/claude-code@$CLAUDE_CODE_VERSION" "opencode-ai@$OPENCODE_VERSION"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
helm_tgz="helm-v${HELM_VERSION}-linux-amd64.tar.gz"
curl -fsSL -o "$tmp/$helm_tgz" "https://get.helm.sh/$helm_tgz"
curl -fsSL -o "$tmp/$helm_tgz.sha256sum" "https://get.helm.sh/$helm_tgz.sha256sum"
(cd "$tmp" && sha256sum -c "$helm_tgz.sha256sum" >/dev/null)
tar -xzf "$tmp/$helm_tgz" -C "$tmp"
install -m 0755 "$tmp/linux-amd64/helm" /usr/local/bin/helm
curl -fsSL "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv${KUSTOMIZE_VERSION}/kustomize_v${KUSTOMIZE_VERSION}_linux_amd64.tar.gz" | tar -xzf - -C "$tmp" kustomize
install -m 0755 "$tmp/kustomize" /usr/local/bin/kustomize
curl -fsSL -o "$tmp/argocd" "https://github.com/argoproj/argo-cd/releases/download/v${ARGOCD_VERSION}/argocd-linux-amd64"
install -m 0755 "$tmp/argocd" /usr/local/bin/argocd
curl -fsSL -o "$tmp/sops" "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64"
install -m 0755 "$tmp/sops" /usr/local/bin/sops

apt-get update -q >/dev/null
DEBIAN_FRONTEND=noninteractive apt-get install -y -q kubectl >/dev/null

install -d -m 0755 /var/lib/t3code
install -m 0644 "$DIR/versions.env" "$STAMP"

if [[ $prev_t3 != "$T3_VERSION" ]] && id "$T3_USER" >/dev/null 2>&1; then
  sudo -u "$T3_USER" -H env XDG_RUNTIME_DIR="/run/user/$(id -u "$T3_USER")" \
    systemctl --user try-restart t3code.service
  echo "t3 ${prev_t3:-none} -> $T3_VERSION, service restarted"
fi
echo "t3code at: t3=$T3_VERSION claude=$CLAUDE_CODE_VERSION opencode=$OPENCODE_VERSION helm=$HELM_VERSION kustomize=$KUSTOMIZE_VERSION argocd=$ARGOCD_VERSION sops=$SOPS_VERSION"
