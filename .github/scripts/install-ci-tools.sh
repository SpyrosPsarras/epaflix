#!/usr/bin/env bash
set -euo pipefail

cache=${CI_TOOLS_CACHE:-$HOME/.cache/ci-tools}

dl() {
  url=$1; out=$2; n=0
  until curl -fsSL -o "$out" "$url"; do
    n=$((n+1))
    if [ "$n" -ge 6 ]; then
      echo "download failed after 6 attempts: $url" >&2
      return 1
    fi
    sleep $((n*10))
  done
}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

fetch() {
  local tool=$1 version=$2
  case "$tool" in
    helm)
      helm_tgz="helm-v${version}-linux-amd64.tar.gz"
      dl "https://get.helm.sh/${helm_tgz}" "$tmp/${helm_tgz}"
      dl "https://get.helm.sh/${helm_tgz}.sha256sum" "$tmp/${helm_tgz}.sha256sum"
      ( cd "$tmp" && sha256sum -c "${helm_tgz}.sha256sum" )
      tar -xzf "$tmp/${helm_tgz}" -C "$tmp"
      mv "$tmp/linux-amd64/helm" "$tmp/bin"
      ;;
    kustomize)
      dl "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv${version}/kustomize_v${version}_linux_amd64.tar.gz" "$tmp/kustomize.tar.gz"
      tar -xzf "$tmp/kustomize.tar.gz" -C "$tmp"
      mv "$tmp/kustomize" "$tmp/bin"
      ;;
    promtool)
      prom_tgz="prometheus-${version}.linux-amd64.tar.gz"
      prom_url="https://github.com/prometheus/prometheus/releases/download/v${version}"
      dl "${prom_url}/${prom_tgz}" "$tmp/${prom_tgz}"
      dl "${prom_url}/sha256sums.txt" "$tmp/prom-sha256sums.txt"
      ( cd "$tmp" && grep " ${prom_tgz}\$" prom-sha256sums.txt | sha256sum -c - )
      tar -xzf "$tmp/${prom_tgz}" -C "$tmp" "prometheus-${version}.linux-amd64/promtool"
      mv "$tmp/prometheus-${version}.linux-amd64/promtool" "$tmp/bin"
      ;;
  esac
}

for tool in "$@"; do
  case "$tool" in
    helm) version=${HELM_VERSION:?} ;;
    kustomize) version=${KUSTOMIZE_VERSION:?} ;;
    promtool) version=${PROMTOOL_VERSION:?} ;;
    *)
      echo "Unknown tool: $tool" >&2
      exit 1
      ;;
  esac
  cached="$cache/$tool-$version"
  if [ -x "$cached" ]; then
    echo "$tool $version from cache"
  else
    fetch "$tool" "$version"
    mkdir -p "$cache"
    mv "$tmp/bin" "$cached"
  fi
  sudo install -m 0755 "$cached" "/usr/local/bin/$tool"
  case "$tool" in
    helm) helm version ;;
    kustomize) kustomize version ;;
    promtool) promtool --version ;;
  esac
done
