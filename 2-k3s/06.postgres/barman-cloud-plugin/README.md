# Barman Cloud Plugin (imperative)

CNPG Barman Cloud Plugin operator + `ObjectStore` CRD, installed into
`cnpg-system`. Pinned to **v0.14.0**. Added to migrate postgres-cluster off
in-tree `barmanObjectStore` (issue #10).

Bootstrapped via `../03.install-barman-plugin.sh` (fresh-cluster only). Day-to-day
lifecycle is now under ArgoCD — adopted alongside the CNPG operator
(`../operator-kustomization/cnpg-operator.yaml`) via the `cnpg-operator` Application
(`../../11.argocd/apps/app-cnpg-operator.yaml`, rendered from
`../operator-kustomization/`) under issue #93.

Requires CNPG operator >= 1.26 and cert-manager (gRPC TLS).

Re-vendor a new version:

    curl -fsSL https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/<vX.Y.Z>/manifest.yaml \
      -o ../operator-kustomization/barman-manifest.yaml

The single `kind: Secret` in this manifest is an empty cert-manager-managed
placeholder (no credentials) and is allowlisted in
`.github/hooks/check-sops-encrypted.sh`.
