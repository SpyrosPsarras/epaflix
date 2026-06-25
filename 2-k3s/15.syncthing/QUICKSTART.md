# Syncthing — post-deploy checklist

One-time hardening after the first ArgoCD sync. Do these in the GUI at `https://syncthing.epaflix.com` (Authentik login required). `kubectl exec` is not available in this cluster — use the GUI only.

## GUI hardening (Settings → Connections)

- [ ] Uncheck **Global Discovery**
- [ ] Uncheck **Enable Relaying**
- [ ] Uncheck **Enable NAT traversal**
- [ ] Leave **Local Discovery** on
- [ ] Confirm Sync Protocol Listen Address: `tcp://0.0.0.0:22000`

## GUI hardening (Settings → General)

- [ ] Usage Reporting → **No**

## GUI hardening (Settings → GUI)

- [ ] Set a GUI username and password (defense-in-depth behind Authentik)

## Verify

- [ ] Storage: GUI shows data path under `/var/syncthing`; free space ≈ 1 TiB
- [ ] Note the device ID. Peers connect via `tcp://192.168.10.101:22000`
- [ ] Persistence: delete the pod (`kubectl --context epaflix delete pod -n syncthing -l app=syncthing`); after restart on `k3s-worker-63`, confirm config and device ID survive
- [ ] Peer sync: add a second device using `tcp://192.168.10.101:22000`; share a small folder; confirm sync and `.stversions` appear after an edit
- [ ] Backup: after the next 01:00 PBS run (or trigger manually with `vzdump 1063 --storage pbs-backup-local --mode snapshot`), confirm a snapshot of VM `1063` including the data disk exists in PBS

## Follow-ups (open `gh issue` for each)

- Add Renovate config entry for `syncthing/syncthing` image bumps
- TrueNAS `pool1` is a non-redundant stripe — track remediation now that PBS no longer relies on it
