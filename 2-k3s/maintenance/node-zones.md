# Node zones

Each k3s node carries `topology.kubernetes.io/zone` set to the Proxmox host that runs its VM, so the scheduler can spread replicas across hosts. The labels are set live, not by a manifest.

| Zone | Nodes |
| --- | --- |
| takaros | k3s-master-51, k3s-master-52, k3s-worker-61, k3s-worker-62 |
| evanthoulaki | k3s-master-53, k3s-worker-63, k3s-worker-65 |

```sh
kubectl label node k3s-master-51 k3s-master-52 k3s-worker-61 k3s-worker-62 topology.kubernetes.io/zone=takaros --overwrite
kubectl label node k3s-master-53 k3s-worker-63 k3s-worker-65 topology.kubernetes.io/zone=evanthoulaki --overwrite
kubectl get nodes -L topology.kubernetes.io/zone
```

If a VM moves to another Proxmox host, relabel its node with the new host name. Proxmox HA manages no VMs today, so VMs only move by hand.

The workload using these labels is grafana in `2-k3s/10.observability/prometheus-values.yaml` with zone spread and `ScheduleAnyway`.
