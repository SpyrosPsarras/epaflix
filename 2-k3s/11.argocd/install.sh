#!/usr/bin/env bash

: "${KUBECTL_CONTEXT:=epaflix}"
kubectl() { command kubectl --context "$KUBECTL_CONTEXT" "$@"; }

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_VERSION="9.5.14"   # app v3.4.2 — pin to avoid surprise upgrades.

echo ">>> 1/4  apply namespace"
kubectl apply -f "${SCRIPT_DIR}/namespace.yaml"

echo ">>> 2/4  helm upgrade --install argocd (chart ${CHART_VERSION})"
helm repo add argo https://argoproj.github.io/argo-helm 2>/dev/null || true
helm repo update argo
helm upgrade --install argocd argo/argo-cd \
  --namespace argocd \
  --version "${CHART_VERSION}" \
  -f "${SCRIPT_DIR}/helm-values.yaml" \
  --wait --timeout 5m

echo ">>> 3/4  apply Traefik IngressRoute"
kubectl apply -f "${SCRIPT_DIR}/ingress.yaml"

echo ">>> 4/4  done. Next steps:"
cat <<EOF

  • Add DNS:  argocd.epaflix.com → 192.168.10.101  (Pi-hole dnsmasq.d)
  • Create the Authentik OIDC provider + group + binding per
    2-k3s/05.traefik-deployment/examples/app-with-native-oidc-authentik.md
  • Merge-patch OIDC client-id/secret into argocd-secret (do NOT apply
    oidc-secret.yaml — that would clobber the helm-managed server.secretkey).
    Replace VALUES below with real ones from .github/instructions/secrets.enc.yaml:
        kubectl -n argocd patch secret argocd-secret --type=merge \\
          -p '{"stringData":{"oidc.authentik.clientId":"<CID>","oidc.authentik.clientSecret":"<CSEC>"}}'
        kubectl -n argocd rollout restart deploy/argocd-server
  • Image bumps are delivered by Renovate (2-k3s/12.renovate/), not
    Image Updater — nothing to install here (retired in #192/#265).
  • Get the initial admin password (only valid before OIDC works).
    Capture, never print in a recorded shell (#602):
        PW=\$(kubectl --context epaflix -n argocd get secret argocd-initial-admin-secret \\
          -o jsonpath='{.data.password}' | base64 -d)

EOF
