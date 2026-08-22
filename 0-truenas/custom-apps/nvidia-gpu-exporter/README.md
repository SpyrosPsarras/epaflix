# nvidia_gpu_exporter, a TrueNAS custom app (#916)

GPU metrics for the RTX 2070 SUPER in `192.168.10.200`, scraped by the cluster
Prometheus through `2-k3s/10.observability/truenas-exporters.yaml` (#917) and
alerted on by the vendored `truenas-gpu` group in
`2-k3s/10.observability/alertmanager-config/custom-alerts.yaml`.

| Fact | Value |
|---|---|
| App name | `nvidia-gpu-exporter` (custom app, `custom_app: true`) |
| Compose | `docker-compose.yaml` in this directory, the tracked source of truth |
| Image | `docker.io/utkuozdemir/nvidia_gpu_exporter:1.14.0-nvml`, index digest `sha256:82acc3fc60a5a709846ea9757bbccb170fd141039fa47e5899c55fe9a60f56fe` (resolved 2026-08-22) |
| Listens | `192.168.10.200:9835` (host networking) |
| Backend | native NVML (`-nvml` image flavor defaults to it, no flag needed) |
| Alert group | `truenas-gpu`, five expressions vendored from upstream `v1.14.0` |

## The backend is load-bearing, not a preference

Upstream `docs/CONFIGURE.md` at `v1.14.0` states the XID families
(`nvidia_smi_xid_errors_total`, `nvidia_smi_xid_last_timestamp_seconds`) exist
**only** on the nvml backend. The default exec backend shells out to
`nvidia-smi`, which cannot report them. `NvidiaGpuXidCritical` queries
`nvidia_smi_xid_last_timestamp_seconds`, so on the plain `1.14.0` image that
alert can never fire on real hardware.

If the nvml flavor does not come up on this box (upstream marks it experimental:
Linux x86_64, glibc, cgo build), the fallback is the plain `1.14.0` tag, and
that is a **recorded downgrade, not a silent one**: the Xid rule then has
synthetic promtool evidence only, and the deploy-gate step in
`2-k3s/10.observability/README.md` opens the follow-up issue that says so.
Check which backend is live:

```bash
ssh truenas_admin@192.168.10.200 'curl -4 -m5 -s http://localhost:9835/metrics | grep -c "^nvidia_smi_nvml_return_code"'
# want: 1 on the nvml backend. The exec backend reports
# nvidia_smi_command_exit_code instead. Grep for that as the control.
```

## Install / re-create

The app registration lives in TrueNAS middleware config, **not** in this repo.
This is the recipe, and it is what to run after a fresh install:

```bash
scp 0-truenas/custom-apps/nvidia-gpu-exporter/docker-compose.yaml truenas_admin@192.168.10.200:/tmp/
ssh truenas_admin@192.168.10.200 \
  'sudo midclt call app.create "$(jq -n --rawfile c /tmp/docker-compose.yaml \
     "{custom_app:true, app_name:\"nvidia-gpu-exporter\", custom_compose_config_string:\$c}")"'
```

Verify from the host, with its negative control in the same session:

```bash
ssh truenas_admin@192.168.10.200 'curl -4 -m5 -s http://localhost:9835/metrics | grep -c "^nvidia_smi_gpu_info"'
# want: >= 1
ssh truenas_admin@192.168.10.200 'curl -4 -m5 -s http://localhost:9836/metrics'
# want: FAILURE (connection refused)
```

Delete (rollback):

```bash
ssh truenas_admin@192.168.10.200 'sudo midclt call app.delete nvidia-gpu-exporter'
```

## Driver-update caveat

The image bundles no NVIDIA components: the NVIDIA Container Toolkit injects the
driver libraries matched to the host driver when the container starts (upstream
`docs/INSTALL.md`). Measured 2026-08-22 on the box: docker `28.3.1`, runtimes
include `nvidia` (`/usr/bin/nvidia-container-runtime`) and the **default**
runtime is `nvidia`; host driver `570.172.08`.

A driver update that changes those paths breaks collection while the exporter
keeps serving. The exporter stays UP and `nvidia_smi_last_collect_success` goes
to 0. That fires `NvidiaGpuExporterCollectionFailing`, which is the condition the
#916 decision names as the one that **must never be silenced**. It is the same
shape as the 2026-08-09 incident, so the alert is doing its job; do not silence
it, fix the injection.

## Update survival

Custom apps are middleware-managed and their compose lives on the ix-apps pool
dataset (`/mnt/.ix-apps/app_configs/<app>`), so a TrueNAS SCALE update redeploys
them; nothing is installed on the immutable boot pool. Measured 2026-08-22:
SCALE `25.10.0.1`. After the next update, re-run the from-host curl pair above
plus the backend check, and confirm `up{job="truenas-gpu-exporter"}` is 1
(tracked as its own follow-up issue).
