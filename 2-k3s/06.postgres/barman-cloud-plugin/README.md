# Barman Cloud Plugin (imperative)

CNPG Barman Cloud Plugin operator + `ObjectStore` CRD, installed into
`cnpg-system`. Pinned to **v0.12.0**. Added to migrate postgres-cluster off
in-tree `barmanObjectStore` (issue #10).

Installed imperatively via `../03.install-barman-plugin.sh` — NOT under ArgoCD,
mirroring the CNPG operator (`../operator/cnpg-operator.yaml`). ArgoCD adoption of
operators/CRDs is tracked in issue #93.

Requires CNPG operator >= 1.26 and cert-manager (gRPC TLS).

Re-vendor a new version:

    curl -fsSL https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/<vX.Y.Z>/manifest.yaml \
      -o manifest.yaml

The single `kind: Secret` in this manifest is an empty cert-manager-managed
placeholder (no credentials) and is allowlisted in
`.github/hooks/check-sops-encrypted.sh`.
