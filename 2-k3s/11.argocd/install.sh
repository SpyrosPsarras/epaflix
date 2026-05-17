#!/usr/bin/env bash
# Install / upgrade ArgoCD on the k3s cluster.
#
# Order:
#   1. namespace.yaml
#   2. oidc-secret.yaml (placeholder — real values from secrets.yml)
#   3. helm upgrade --install argocd … helm-values.yaml
#   4. ingress.yaml (after Service exists)
#   5. image-updater install (separate script)
#
# Re-run is safe: helm upgrade is idempotent.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_VERSION="9.5.14"   # app v3.4.2 — pin to avoid surprise upgrades.

echo ">>> 1/4  apply namespace"
kubectl apply -f "${SCRIPT_DIR}/namespace.yaml"

echo ">>> 2/4  helm upgrade --install argocd (chart ${CHART_VERSION})"
# The chart creates `argocd-secret` itself with a random server.secretkey;
# OIDC client-id/secret are merge-patched AFTER install (see step 4 notes).
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
    Replace VALUES below with real ones from .github/instructions/secrets.yml:
        kubectl -n argocd patch secret argocd-secret --type=merge \\
          -p '{"stringData":{"oidc.authentik.clientId":"<CID>","oidc.authentik.clientSecret":"<CSEC>"}}'
        kubectl -n argocd rollout restart deploy/argocd-server
  • Install argocd-image-updater:  ${SCRIPT_DIR}/image-updater/install.sh
  • Get the initial admin password (only valid before OIDC works):
        kubectl -n argocd get secret argocd-initial-admin-secret \\
          -o jsonpath='{.data.password}' | base64 -d ; echo

EOF
