# epaflix

A homelab as code. Four Proxmox hosts run the VMs: TrueNAS for storage and eight k3s nodes. Argo CD deploys everything under `2-k3s` from `main`. Secrets are SOPS files named `*.enc.yaml` that one age key decrypts.

| Folder | Holds |
| --- | --- |
| `0-truenas` | TrueNAS apps and scripts |
| `1-proxmox` | Host reinstall notes, Proxmox Backup Server config, Pi-hole DNS, SSH inventory |
| `2-k3s` | One folder per cluster app, numbered in install order |
| `3-devices` | Firmware for home devices outside the cluster, such as ESPHome boards |
| `images` | Container images built by CI |

All commands run from the repo root and need `kubectl`, `helm`, `kustomize`, `sops`, `ksops`, `openssl` and the age key. sops reads only `keys.txt` by default, so run `export SOPS_AGE_KEY_FILE=~/.config/sops/age/k3s-cluster.txt` first.

## Rebuild from zero

1. Proxmox hosts. Notes exist for [patroklos](1-proxmox/patroklos/README.md) and [shypowder](1-proxmox/shypowder/README.md). takaros and evanthoulaki have none, and no VM definitions are in git.
2. Guests. The k3s nodes are Ubuntu 24.04 VMs with `eth0` on `192.168.10.0/24` and `eth1` on `10.0.0.0/24`. Pi-hole comes first because the nodes resolve through `192.168.10.30`. takaros runs k3s-master-51, k3s-worker-61 and 62. evanthoulaki runs k3s-master-53, k3s-worker-63 and 65, and the LXC containers pihole and pegaprox. patroklos runs k3s-master-52, the truenas VM and the backup server in an LXC container. shypowder runs k3s-worker-64 with the GPU.
3. k3s `v1.36.5+k3s1`, the version in `2-k3s/maintenance/system-upgrade/plans`. Install with the upstream script. `/etc/rancher/k3s/config.yaml` on the first server:

   ```yaml
   cluster-init: true            # servers 52 and 53 use `server: https://192.168.10.100:6443` and a token instead
   node-ip: 10.0.0.51
   advertise-address: 10.0.0.51
   flannel-iface: eth1
   embedded-registry: true
   node-taint: ["node-role.kubernetes.io/control-plane:NoSchedule"]
   tls-san: ["192.168.10.100", "192.168.10.51", "192.168.10.52", "192.168.10.53", "10.0.0.51", "10.0.0.52", "10.0.0.53"]
   disable: [servicelb, traefik]
   etcd-arg: ["auto-compaction-mode=periodic", "auto-compaction-retention=1h", "quota-backend-bytes=8589934592"]
   etcd-expose-metrics: true
   kubelet-arg: ["image-gc-high-threshold=60", "image-gc-low-threshold=50", "resolv-conf=/etc/k3s-resolv.conf"]
   kube-controller-manager-arg: ["bind-address=0.0.0.0"]
   kube-scheduler-arg: ["bind-address=0.0.0.0"]
   ```
   `192.168.10.100` is the kube-vip address, so apply `2-k3s/01.kube-vip` before the other servers join. Agents run `k3s agent --node-ip 10.0.0.6x --flannel-iface eth1` with `K3S_URL=https://192.168.10.100:6443` and `K3S_TOKEN` in `/etc/systemd/system/k3s-agent.service.env`. Their `config.yaml` holds only a `kubelet-arg` list, and it differs from the servers: `image-gc-high-threshold=85`, `image-gc-low-threshold=80`, `system-reserved=cpu=250m,memory=512Mi`, `kube-reserved=cpu=250m,memory=512Mi`, `eviction-hard=memory.available<300Mi,nodefs.available<5%,imagefs.available<5%`, `resolv-conf=/etc/k3s-resolv.conf`. `/etc/k3s-resolv.conf` is `nameserver 192.168.10.30`. Workers need `open-iscsi` and `nfs-common`, a `registries.yaml` whose `mirrors` has an empty `"*"` entry, and k3s-worker-64 needs the label `gpu=nvidia`.
4. Apply `2-k3s/06.postgres/namespace.yaml` and create the two `postgres-system` Secrets from [Not in git](#not-in-git). Then install the Traefik CRDs and apply the add-ons in folder order. Kinds defined by a later folder fail on the first pass, so repeat the loop until it is clean. To recover the databases, switch `06.postgres` to recovery first, see [Postgres restore](#postgres-restore).
   ```sh
   v=$(sed -n 's/^ *version: "\(.*\)"$/\1/p' 2-k3s/05.traefik-deployment/kustomization.yaml)
   helm show crds traefik --repo https://traefik.github.io/charts --version "$v" | kubectl --context epaflix apply --server-side -f -
   for d in 01.kube-vip 02.cert-manager 03.kube-vip-cloud-provider 04.coredns 05.traefik-deployment \
            06.postgres/operator-kustomization 06.postgres 07.authentik-deployment 08.servarr 10.observability; do
     kustomize build --enable-helm --enable-alpha-plugins --enable-exec 2-k3s/$d |
       kubectl --context epaflix apply --server-side -f -
   done
   ```
5. Argo CD, next section. It then owns every folder that has an Application in `2-k3s/11.argocd/apps`.

## Argo CD bootstrap

```sh
2-k3s/11.argocd/install.sh
```

The script creates Secret `argocd/sops-age` with the key `keys.txt` from the age key file, or from `KEY_FILE`, and a random `argocd/argocd-redis`. It installs the CRDs from the chart version pinned in `kustomization.yaml`, applies that kustomization and then `apps/`. The repo server mounts `sops-age` to decrypt the `*.enc.yaml` files. Copies of the age key are listed in `.github/hooks/print-age-key-backup.sh`. Login is Authentik only. Patch `oidc.authentik.clientId` and `oidc.authentik.clientSecret` into Secret `argocd-secret` by hand, then restart `deploy/argocd-server`.

## Not in git

- Secrets created by hand, as of 2026-11-15, written as namespace/name:
  - Needed by step 4 and Argo CD: `argocd/sops-age`, `argocd/argocd-redis`, the two OIDC keys in `argocd/argocd-secret`, `postgres-system/postgres-superuser`, `postgres-system/postgres-app-user`.
  - Apps: `renovate/renovate-secrets`, `kube-system/aaaa-tripwire-ssh`, `servarr/lingarr-db-env`, `servarr/probe-key`, `remote-pi/cliproxy-free-canary`, `remote-pi/cliproxy-free-canary-config`, `vaultwarden/vaultwarden-org-setup`, `tailscale/subnet-router-state`.
  - MCP hub: `mcp-hub/mcp-hub-notion-grant`, and in `mcp-hub-homelab` the Secrets `mcp-hub-instance`, `mcp-hub-gmail`, `mcp-hub-google-upstream`, `mcp-hub-searxng`.
  - Agent runtime: in `t3code-homelab` the Secrets `t3-runtime-inputs`, `t3-runtime-grants`, `vaultwarden-agent`, `t3-build-kubeconfig`, `homelab-agent-kubeconfig`. Also `t3code/manager-kubeconfig`, `t3code/manager-agent-kubeconfig`, `t3-build-homelab/builder-token`.
- The Traefik CRDs. `05.traefik-deployment` sets `includeCRDs: false` and no Application renders them, so they stay at the version step 4 installed.
- Three `Endpoints` in `traefik-system`: `minio-external`, `pegaprox-ui`, `truenas-ui`. They are in `2-k3s/05.traefik-deployment/ingress/{minio,pegaprox,truenas}-proxy.yaml`, but Argo CD excludes the kind. Step 4 creates them. Apply later changes by hand.
- `2-k3s/18.cloudflare-tunnel` and `2-k3s/21.tailscale` have no Application. Run `2-k3s/18.cloudflare-tunnel/deploy.sh` and follow [the Tailscale README](2-k3s/21.tailscale/README.md).

## Postgres restore

`postgres-cluster` archives WAL and a daily base backup to the ObjectStore `postgres-minio-store` through the barman cloud plugin. The store is `s3://postgres-backups/` on MinIO and keeps 10 days. Test a restore in its own namespace:

```sh
k() { kubectl --context epaflix "$@"; }
move() { sed 's/namespace: postgres-system/namespace: pg-restore-test/' | k apply -f -; }
k -n postgres-system get objectstore postgres-minio-store -o jsonpath='{.status.serverRecoveryWindow}'
k create ns pg-restore-test
sops -d 2-k3s/06.postgres/minio-backup-credentials.enc.yaml | move
move < 2-k3s/06.postgres/cluster/postgres-object-store.yaml
k apply -f - <<'EOF'
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata: {name: pg-restore-test, namespace: pg-restore-test}
spec:
  instances: 1
  imageName: ghcr.io/cloudnative-pg/postgresql:16
  storage: {size: 20Gi, storageClass: local-path}
  bootstrap: {recovery: {source: live}}
  externalClusters:
    - name: live
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters: {barmanObjectName: postgres-minio-store, serverName: postgres-cluster}
EOF
k -n pg-restore-test wait --for=condition=Ready cluster/pg-restore-test --timeout=15m
k -n pg-restore-test exec pg-restore-test-1 -c postgres -- psql -U postgres -Atc 'select datname from pg_database'
k delete ns pg-restore-test
```

The first command must show one server, `postgres-cluster`, with a recent `lastSuccessfulBackupTime`. Run it again after the teardown. A second server name means the test cluster archived into the live backups. That is why the test cluster has no `plugins:` block, so do not add one. Compare the database list and `count(*)` on real tables with live. `n_live_tup` reads 0 on a fresh restore.

After a real loss, edit `cluster/postgres-cluster.yaml` before it is applied. Replace `bootstrap.initdb` with the `bootstrap.recovery` and `externalClusters` blocks above, and add `serverName: postgres-cluster-2` to the `plugins` parameters so new WAL goes to an empty path.

## Storage maintenance

Every `truenas-iscsi` volume is on the TrueNAS `apps` pool. That pool is one disk of the TrueNAS VM on patroklos, and evanthoulaki serves the disk over iSCSI. See [patroklos](1-proxmox/patroklos/README.md#truenas-apps-disk). If TrueNAS or evanthoulaki is away for more than 2 minutes, the app volumes go read-only. A cordon does not prevent that. Stop the consumers first. Vaultwarden is one of them, so fetch any secret you need beforehand. An evanthoulaki reboot also stops Pi-hole, k3s-master-53 and two workers.

```sh
k() { kubectl --context epaflix "$@"; }
for a in app-of-apps authentik discord gh-runner home-assistant remote-pi servarr vaultwarden; do
  k -n argocd patch application $a --type json -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
done
k -n vaultwarden patch cronjob vaultwarden-backup -p '{"spec":{"suspend":true}}'
scale() {
  k -n app-authentik scale deploy authentik-server authentik-worker --replicas=$1
  for n in discord-media discord-windmill; do k -n $n scale deploy openclaw --replicas=$1; done
  k -n gh-runner scale deploy gh-runner --replicas=$1
  k -n home-assistant scale deploy esphome home-assistant roborock --replicas=$1
  k -n remote-pi scale deploy cpa-manager-plus --replicas=$1
  k -n servarr scale deploy bazarr bazarr-autotranslate cleanuparr jellysweep lidarr lingarr navidrome neutarr \
    prowlarr qbittorrent radarr seerr sonarr streamystats --replicas=$1
  k -n vaultwarden scale deploy vaultwarden --replicas=$1
}
scale 0
k get volumeattachment | grep democratic-csi    # wait until this prints nothing, then reboot
```

When TrueNAS is back and `ssh truenas zpool status apps` is healthy, start again:

```sh
scale 1
k -n vaultwarden patch cronjob vaultwarden-backup -p '{"spec":{"suspend":false}}'
k -n argocd patch application app-of-apps --type merge -p '{"spec":{"syncPolicy":{"automated":{"selfHeal":true}}}}'
```

`app-of-apps` then restores the sync policy of the other Applications. The consumer list is from 2026-11-15. Recheck it with `k get pvc -A | grep truenas-iscsi`.
