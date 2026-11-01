# Node settings

These settings were applied by hand and verified live on 2026-11-01. Restore them when rebuilding a node.

| Role | Nodes |
| --- | --- |
| Masters | k3s-master-51, k3s-master-52, k3s-master-53 |
| Workers | k3s-worker-61, k3s-worker-62, k3s-worker-63, k3s-worker-65 |

## Kubelet image cleanup

On every node, `/etc/rancher/k3s/config.yaml` has exactly these `kubelet-arg` entries:

```yaml
kubelet-arg:
  - image-gc-high-threshold=60
  - image-gc-low-threshold=50
  - resolv-conf=/etc/k3s-resolv.conf
```

The default image garbage collection threshold is 85 %. At that threshold, the masters kept 7 to 9 GB of unused images. With 60/50, kubelet deletes unused images when root filesystem use passes 60 %, until it reaches 50 %. Within 5 minutes of the change, worker-65 went from 77 % to 44 % and worker-61 from 82 % to 73 %.

Restart one node's service at a time. Use `systemctl restart k3s` on masters and `systemctl restart k3s-agent` on workers. Running pods survive because the units use `KillMode=process`.

Check the effective settings for each node:

```sh
kubectl get --raw /api/v1/nodes/<node>/proxy/configz
```

Confirm `imageGCHighThresholdPercent` is `60` and `imageGCLowThresholdPercent` is `50`.

## Journal size

On every node, `/etc/systemd/journald.conf.d/size.conf` contains:

```ini
[Journal]
SystemMaxUse=500M
```

Apply the cap and remove old logs:

```sh
systemctl restart systemd-journald
journalctl --vacuum-size=500M
```

Logs had used 2.9 GB on k3s-master-51 and on k3s-master-52.

## VM disk discard

The k3s VM disks use `scsi0` on the `local-raid` LVM-thin pool. Set `discard=on,ssd=1` so Ubuntu's weekly `fstrim.timer`, enabled by default, returns freed blocks to the thin pool.

| Nodes | Discard status on 2026-11-01 |
| --- | --- |
| k3s-master-51, k3s-master-52, k3s-master-53 | Enabled |
| k3s-worker-62, k3s-worker-65 | Already enabled |
| k3s-worker-61 | Not yet enabled. Planned for retirement. |
| k3s-worker-63 | Not yet enabled. Enable when it moves host. |

Changing discard needs a full VM stop and start. A reboot inside the guest is not enough.

On the masters, enabling discard and running `fstrim -v /` once dropped thin LV allocation from about 94 % to about 30 %.

Only the masters showed that drop. Workers 62 and 65 already had discard, and a manual `fstrim -v /` barely changed their thin LV allocation: 92.7 % to 92.5 % and 97.0 % to 96.8 %, although their filesystems were less full. The cause is not known. Do not expect discard alone to shrink a worker disk.

## Master sizing

After 2026-11-01, each master has 4 vCPU, 6 GiB RAM, no balloon and a 30 GB disk. Measured use was about 2.1 to 2.5 GB RAM and 8 to 12 GB disk per master.

Change one master at a time. Etcd keeps quorum with 2 of 3 masters. Before changing the next master, check that the API reports `[+]etcd ok` and all nodes are Ready:

```sh
kubectl get --raw='/readyz?verbose'
kubectl get nodes
```

## Existing alerts

Kube-prometheus node filesystem alerts already cover full filesystems. Proxmox thin pools have `ProxmoxThinPoolDataNearFull` and `ProxmoxThinPoolDataCritical` in `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml`. No new alerts were added.
