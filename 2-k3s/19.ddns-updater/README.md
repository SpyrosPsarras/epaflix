# ddns-updater

Argo syncs everything in `kustomization.yaml` — the Cloudflare token Secret
is decrypted by the ksops generator, same as searxng. A local
`kubectl apply -k` needs the ksops plugin and the sops age key.
