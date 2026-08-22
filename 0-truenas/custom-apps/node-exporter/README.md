# node-exporter, a TrueNAS custom app (#918)

Host and ZFS-ARC metrics for `192.168.10.200`, scraped by the cluster Prometheus
through `2-k3s/10.observability/truenas-exporters.yaml` (#917).

| Fact | Value |
|---|---|
| App name | `node-exporter` (custom app, `custom_app: true`) |
| Compose | `docker-compose.yaml` in this directory, the tracked source of truth |
| Image | `docker.io/prom/node-exporter:v1.12.1`, index digest `sha256:1b4e4438faca4dd7e001dd445d161a4a2091b0fededa84093b3a8dfeae1f1be0` (resolved 2026-08-22) |
| Listens | `192.168.10.200:9100` (host networking) |
| Series the alerts need | `node_zfs_arc_size`, `node_zfs_arc_c_max`, `node_zfs_arc_memory_throttle_count` |
| Alert group | `truenas-memory` in `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml` |

## Install / re-create

The app registration lives in TrueNAS middleware config, **not** in this repo.
Same posture as the POSTINIT `initshutdownscript` for `../../scripts/gpu-persistenced.sh`.
This is the recipe, and it is what to run after a fresh install:

```bash
scp 0-truenas/custom-apps/node-exporter/docker-compose.yaml truenas_admin@192.168.10.200:/tmp/
ssh truenas_admin@192.168.10.200 \
  'sudo midclt call app.create "$(jq -n --rawfile c /tmp/docker-compose.yaml \
     "{custom_app:true, app_name:\"node-exporter\", custom_compose_config_string:\$c}")"'
```

Verify from the host itself. The from-host curl is the reliable shape, and it
only counts alongside its negative control in the same session:

```bash
ssh truenas_admin@192.168.10.200 'curl -4 -m5 -s http://localhost:9100/metrics | grep -c "^node_zfs_arc_size"'
# want: >= 1
ssh truenas_admin@192.168.10.200 'curl -4 -m5 -s http://localhost:9101/metrics'
# want: FAILURE (connection refused). A probe that cannot tell listening from
# not-listening has not proven anything with its 9100 "yes".
```

Delete (rollback):

```bash
ssh truenas_admin@192.168.10.200 'sudo midclt call app.delete node-exporter'
```

## Update survival

Custom apps are middleware-managed and their compose lives on the ix-apps pool
dataset (`/mnt/.ix-apps/app_configs/<app>`), so a TrueNAS SCALE update redeploys
them. Nothing lands on the immutable boot pool. An `apt`/`pip` install there is
what would not survive. `wg-easy` already proves this lifecycle on this box.
Measured 2026-08-22: SCALE `25.10.0.1`, five custom apps already registered
(`jellyfin`, `ddns-updater`, `proxmox`, `tdarr-dovi`, `tdarr-denix`).

Verify after the next update (tracked as its own follow-up issue): re-run the
from-host curl pair above and check `up{job="truenas-node-exporter"}` in
Prometheus.
