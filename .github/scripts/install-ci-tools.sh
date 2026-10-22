#!/usr/bin/env bash
set -euo pipefail

dl() {
  url=$1; out=$2; n=0
  until curl -fsSL -o "$out" "$url"; do
    n=$((n+1))
    if [ "$n" -ge 3 ]; then
      echo "download failed after 3 attempts: $url" >&2
      return 1
    fi
    sleep $((n*5))
  done
}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

for tool in "$@"; do
  case "$tool" in
    helm)
      : "${HELM_VERSION:?}"
      helm_tgz="helm-v${HELM_VERSION}-linux-amd64.tar.gz"
      dl "https://get.helm.sh/${helm_tgz}" "$tmp/${helm_tgz}"
      dl "https://get.helm.sh/${helm_tgz}.sha256sum" "$tmp/${helm_tgz}.sha256sum"
      ( cd "$tmp" && sha256sum -c "${helm_tgz}.sha256sum" )
      tar -xzf "$tmp/${helm_tgz}" -C "$tmp"
      sudo mv "$tmp/linux-amd64/helm" /usr/local/bin/helm
      helm version
      ;;
    kustomize)
      : "${KUSTOMIZE_VERSION:?}"
      dl "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv${KUSTOMIZE_VERSION}/kustomize_v${KUSTOMIZE_VERSION}_linux_amd64.tar.gz" "$tmp/kustomize.tar.gz"
      tar -xzf "$tmp/kustomize.tar.gz" -C "$tmp"
      sudo mv "$tmp/kustomize" /usr/local/bin/kustomize
      kustomize version
      ;;
    promtool)
      : "${PROMTOOL_VERSION:?}"
      prom_tgz="prometheus-${PROMTOOL_VERSION}.linux-amd64.tar.gz"
      prom_url="https://github.com/prometheus/prometheus/releases/download/v${PROMTOOL_VERSION}"
      dl "${prom_url}/${prom_tgz}" "$tmp/${prom_tgz}"
      dl "${prom_url}/sha256sums.txt" "$tmp/prom-sha256sums.txt"
      ( cd "$tmp" && grep " ${prom_tgz}\$" prom-sha256sums.txt | sha256sum -c - )
      tar -xzf "$tmp/${prom_tgz}" -C "$tmp" "prometheus-${PROMTOOL_VERSION}.linux-amd64/promtool"
      sudo mv "$tmp/prometheus-${PROMTOOL_VERSION}.linux-amd64/promtool" /usr/local/bin/promtool
      promtool --version
      ;;
    *)
      echo "Unknown tool: $tool" >&2
      exit 1
      ;;
  esac
done
