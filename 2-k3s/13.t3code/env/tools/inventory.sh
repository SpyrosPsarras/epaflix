#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
variant=${T3_RUNTIME_VARIANT:-mixed}
for tool in tree kubectl gh helm kustomize argocd sops git curl ssh python3; do
  command -v "$tool" >/dev/null
done
[[ -x /usr/sbin/sshd ]]
if compgen -G '/etc/ssh/ssh_host_*' >/dev/null; then exit 1; fi
kubectl version --client >/dev/null
case "$variant" in
  mixed)
    command -v az >/dev/null
    az version >/dev/null
    command -v bw >/dev/null
    ;;
  homelab)
    if command -v az >/dev/null || command -v bw >/dev/null || command -v kubelogin >/dev/null; then exit 1; fi
    [[ -d /etc/codex && ! -e /scripts/work-aks-auth.py ]]
    [[ ! -e /etc/apt/sources.list.d/azure-cli.list && ! -e /usr/share/keyrings/microsoft.gpg ]]
    for file in /scripts/aks-auth.sh /scripts/aks-auth-central.sh /scripts/vault-run.py /scripts/vault-ssh-agent.py; do
      [[ ! -e $file ]]
    done
    ;;
  work)
    command -v az >/dev/null
    az version >/dev/null
    [[ -d /etc/codex && -x /scripts/work-aks-auth.py ]]
    if command -v bw >/dev/null || command -v kubelogin >/dev/null; then exit 1; fi
    for file in /scripts/aks-auth.sh /scripts/aks-auth-central.sh /scripts/vault-run.py /scripts/vault-ssh-agent.py; do
      [[ ! -e $file ]]
    done
    ;;
  *) exit 1 ;;
esac
source "$root/versions.env"
[[ $(gh --version | head -1) == "gh version $GH_VERSION "* ]]
pin() { node -p "require('/tools/package.json').dependencies['$1']"; }
ver() { timeout 60 "$1" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?' | head -1; }
for pair in t3:t3 @anthropic-ai/claude-code:claude @openai/codex:codex @earendil-works/pi-coding-agent:pi; do
  want=$(pin "${pair%%:*}"); got=$(ver "/tools/node_modules/.bin/${pair##*:}")
  [[ $got == "$want" ]] || { echo "inventory: ${pair##*:} version mismatch" >&2; exit 1; }
done
pair_help=$(timeout 60 t3 pair --help)
for flag in --base-dir --scope --ttl; do
  grep -q -- "$flag" <<<"$pair_help"
done
echo "inventory: $variant tools, pinned CLI versions and pairing flags PASS"
