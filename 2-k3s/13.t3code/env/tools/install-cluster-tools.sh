#!/usr/bin/env bash
# Pins from ../../versions.env. This image targets the existing amd64 workers.
set -euo pipefail
source "${T3_VERSIONS_FILE:-$(dirname "$0")/versions.env}"
[[ $(dpkg --print-architecture) == amd64 ]]
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
helm_tgz="helm-v${HELM_VERSION}-linux-amd64.tar.gz"
curl -fsSL -o "$tmp/$helm_tgz" "https://get.helm.sh/$helm_tgz"
curl -fsSL -o "$tmp/$helm_tgz.sha256sum" "https://get.helm.sh/$helm_tgz.sha256sum"
(cd "$tmp" && sha256sum -c "$helm_tgz.sha256sum")
tar -xzf "$tmp/$helm_tgz" -C "$tmp"
install -m 0755 "$tmp/linux-amd64/helm" /usr/local/bin/helm
kustomize_url="https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv${KUSTOMIZE_VERSION}"
archive="kustomize_v${KUSTOMIZE_VERSION}_linux_amd64.tar.gz"
curl -fsSL -o "$tmp/$archive" "$kustomize_url/$archive"
curl -fsSL -o "$tmp/checksums.txt" "$kustomize_url/checksums.txt"
(cd "$tmp" && sha256sum --check --ignore-missing checksums.txt)
tar -xz -f "$tmp/$archive" -C "$tmp" kustomize
install -m 0755 "$tmp/kustomize" /usr/local/bin/kustomize
curl -fsSL -o "$tmp/argocd-linux-amd64" "https://github.com/argoproj/argo-cd/releases/download/v${ARGOCD_VERSION}/argocd-linux-amd64"
curl -fsSL -o "$tmp/cli_checksums.txt" "https://github.com/argoproj/argo-cd/releases/download/v${ARGOCD_VERSION}/cli_checksums.txt"
(cd "$tmp" && sha256sum --check --ignore-missing cli_checksums.txt)
curl -fsSL -o "$tmp/sops-v${SOPS_VERSION}.linux.amd64" "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64"
curl -fsSL -o "$tmp/sops-checksums.txt" "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.checksums.txt"
(cd "$tmp" && sha256sum --check --ignore-missing sops-checksums.txt)
install -m 0755 "$tmp/argocd-linux-amd64" /usr/local/bin/argocd
install -m 0755 "$tmp/sops-v${SOPS_VERSION}.linux.amd64" /usr/local/bin/sops
