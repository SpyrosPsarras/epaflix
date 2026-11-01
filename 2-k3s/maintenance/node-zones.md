# Node zones

Each k3s node carries `topology.kubernetes.io/zone` set to the Proxmox host that runs its VM, so the scheduler can spread replicas across hosts. The labels are set live, not by a manifest.

| Zone | Nodes |
| --- | --- |
| takaros | k3s-master-51, k3s-worker-61, k3s-worker-62 |
| evanthoulaki | k3s-master-53, k3s-worker-65 |
| patroklos | k3s-master-52, k3s-worker-63 |

```sh
kubectl label node k3s-master-51 k3s-worker-61 k3s-worker-62 topology.kubernetes.io/zone=takaros --overwrite
kubectl label node k3s-master-53 k3s-worker-65 topology.kubernetes.io/zone=evanthoulaki --overwrite
kubectl label node k3s-master-52 k3s-worker-63 topology.kubernetes.io/zone=patroklos --overwrite
kubectl get nodes -L topology.kubernetes.io/zone
```

If a VM moves to another Proxmox host, relabel its node with the new host name. Proxmox HA manages no VMs today, so VMs only move by hand.

A live migration of k3s-master-52 from takaros (Xeon E5-2623 v4) to patroklos (Xeon E5-2650 v3) failed with `cpu: host` ("Failed to set special registers: Invalid argument"). It succeeded after `qm set 1052 --cpu x86-64-v3` and a restart. k3s-worker-63 was set to `cpu: x86-64-v3`, restarted and live-migrated from evanthoulaki to patroklos; it now has 12 vCPU and 40 GiB RAM. The other k3s VMs still use `cpu: host`.

The workload using these labels is grafana in `2-k3s/10.observability/prometheus-values.yaml` with zone spread and `ScheduleAnyway`.
