# Kube-VIP Installation for K3s HA

## Prerequisites
- K3s master node already installed
- Identify the correct network interface for the VIP

## IMPORTANT: Node Netplan Configuration

**Do NOT include `search: epaflix.com`** in node netplan configs. This causes DNS issues where external domains like `code.visualstudio.com` get resolved as `code.visualstudio.com.epaflix.com` and hit the Pi-hole wildcard catchall.

Example correct netplan (`/etc/netplan/50-cloud-init.yaml`):
```yaml
network:
  version: 2
  ethernets:
    eth0:
      match:
        macaddress: "bc:24:11:xx:xx:xx"
      addresses:
      - "192.168.10.51/24"
      nameservers:
        addresses:
        - 192.168.10.30
        # NO search domain here!
      set-name: "eth0"
      routes:
      - to: "default"
        via: "192.168.10.1"
    eth1:
      match:
        macaddress: "bc:24:11:xx:xx:xx"
      addresses:
      - "10.0.0.51/24"
      nameservers:
        addresses:
        - 192.168.10.30
      set-name: "eth1"
```

## Step 1: Find the External Network Interface

```bash
# Connect to master-1
ssh ubuntu@192.168.10.51

# Find interface with external IP (192.168.10.x) and note the CIDR
ip -o addr show | grep "192.168.10"
# Example output: 2: ens18    inet 192.168.10.51/24 ...
# Use 'ens18' as interface and '24' as the subnet mask
```

## Step 2: Delete Old Manifest (if exists)

```bash
sudo rm -f /var/lib/rancher/k3s/server/manifests/kube-vip.yaml
```

## Step 3: Create Kube-VIP Manifest

**IMPORTANT:**
- Replace `ens18` with your actual interface name from Step 1
- VIP address must include CIDR notation (`192.168.10.100/24`)

```bash
sudo tee /var/lib/rancher/k3s/server/manifests/kube-vip.yaml << 'EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: kube-vip
  namespace: kube-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  annotations:
    rbac.authorization.kubernetes.io/autoupdate: "true"
  name: system:kube-vip-role
rules:
  - apiGroups: [""]
    resources: ["services", "services/status", "nodes", "endpoints"]
    verbs: ["list","get","watch", "update"]
  - apiGroups: ["coordination.k8s.io"]
    resources: ["leases"]
    verbs: ["list", "get", "watch", "update", "create"]
---
kind: ClusterRoleBinding
apiVersion: rbac.authorization.k8s.io/v1
metadata:
  name: system:kube-vip-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:kube-vip-role
subjects:
- kind: ServiceAccount
  name: kube-vip
  namespace: kube-system
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: kube-vip-ds
  namespace: kube-system
spec:
  selector:
    matchLabels:
      name: kube-vip-ds
  template:
    metadata:
      labels:
        name: kube-vip-ds
    spec:
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: node-role.kubernetes.io/master
                operator: Exists
            - matchExpressions:
              - key: node-role.kubernetes.io/control-plane
                operator: Exists
      containers:
      - args:
        - manager
        env:
        - name: vip_arp
          value: "true"
        - name: port
          value: "6443"
        - name: vip_interface
          value: eth0
        - name: vip_cidr
          value: "32"
        - name: cp_enable
          value: "true"
        - name: cp_namespace
          value: kube-system
        - name: vip_ddns
          value: "false"
        - name: svc_enable
          value: "true"
        - name: vip_leaderelection
          value: "true"
        - name: vip_leaseduration
          value: "5"
        - name: vip_renewdeadline
          value: "3"
        - name: vip_retryperiod
          value: "1"
        - name: address
          value: "192.168.10.100"
        - name: prometheus_server
          value: :2112
        image: ghcr.io/kube-vip/kube-vip:v0.8.7
        imagePullPolicy: IfNotPresent
        name: kube-vip
        resources: {}
        securityContext:
          capabilities:
            add:
            - NET_ADMIN
            - NET_RAW
            - SYS_TIME
      hostNetwork: true
      serviceAccountName: kube-vip
      tolerations:
      - effect: NoSchedule
        operator: Exists
      - effect: NoExecute
        operator: Exists
EOF
```

```bash
# Wait for the daemonset to be ready
kubectl rollout status daemonset/kube-vip-ds -n kube-system --timeout=120s

# Check pod status
kubectl get pods -n kube-system -l name=kube-vip-ds

# View logs (should show success messages)
kubectl logs -n kube-system -l name=kube-vip-ds --tail=50
```
```
k3sup install \
  --skip-install \
  --host 192.168.10.51 \
  --user ubuntu \
  --local-path ~/.kube/config

sed -i 's|https://192.168.10.51:6443|https://192.168.10.100:6443|' ~/.kube/config
```

**Expected successful log output:**
```
INFO Starting Kube-vip Manager with the ARP engine
INFO [ARP manager] starting ARP/NDP advertisement
INFO successfully acquired lease kube-system/plndr-cp-lock
INFO Broadcasting ARP update for 192.168.10.100
```

## Step 5: Verify VIP is Active

```bash
# Check if VIP is assigned to the interface
ip addr show eth0 | grep 192.168.10.100

# Ping the VIP
ping -c 3 192.168.10.100

# Test API access through VIP
curl -k https://192.168.10.100:6443/livez

# Should return: ok

# Check which node holds the VIP lease
kubectl get lease -n kube-system plndr-cp-lock -o yaml
```

## Step 6: Update Kubeconfig (on local machine)

```bash
# Update kubeconfig to use VIP instead of direct master IP
sed -i 's|https://192.168.10.51:6443|https://192.168.10.100:6443|' ~/.kube/config

# Test connection
kubectl get nodes
```

## Step 7: Fix CoreDNS to Forward Directly to Pi-hole

**IMPORTANT:** k3s CoreDNS forwards to the node's `/etc/resolv.conf` by default. If nodes have search domains, this breaks external DNS resolution.

```bash
# Patch CoreDNS to forward directly to Pi-hole
kubectl patch configmap coredns -n kube-system --type=json \
  -p='[{"op":"replace","path":"/data/Corefile","value":".:53 {\n    errors\n    health\n    ready\n    kubernetes cluster.local in-addr.arpa ip6.arpa {\n      pods insecure\n      fallthrough in-addr.arpa ip6.arpa\n    }\n    hosts /etc/coredns/NodeHosts {\n      ttl 60\n      reload 15s\n      fallthrough\n    }\n    prometheus :9153\n    cache 30\n    loop\n    reload\n    loadbalance\n    import /etc/coredns/custom/*.override\n    forward . 192.168.10.30\n}\nimport /etc/coredns/custom/*.server\n"}]'

# Restart CoreDNS
kubectl rollout restart deployment/coredns -n kube-system

# Verify pods can access external HTTPS
kubectl run test-dns --image=busybox:latest --restart=Never --rm -it -- wget -q --spider https://google.com && echo "DNS OK"
```

## Troubleshooting

### Invalid CIDR Error (v1.0.3 bug)
```
ERROR invalid CIDR: "192.168.10.100/"
```
**Solution:** Use v0.8.7 instead of `:latest` and use `address` + `vip_cidr` env vars

### Pod CrashLoopBackOff

```bash
# Check detailed logs
kubectl logs -n kube-system -l name=kube-vip-ds --tail=100

# Common issues:
# 1. Wrong interface name - verify with: ip link show
# 2. VIP already in use - check: ping 192.168.10.100
# 3. Port 6443 blocked - verify: nc -zv 192.168.10.51 6443
```

### VIP Not Responding

```bash
# Verify pod is running
kubectl get pods -n kube-system -l name=kube-vip-ds -o wide

# Check if VIP is on the interface
ssh ubuntu@192.168.10.51 'ip addr show eth0 | grep 192.168.10.100'

# Check lease holder
kubectl get lease -n kube-system plndr-cp-lock -o jsonpath='{.spec.holderIdentity}'

# Check ARP cache from another machine
arping -c 3 192.168.10.100
```

### Testing VIP Failover

```bash
# Identify current leader
kubectl get lease -n kube-system plndr-cp-lock -o jsonpath='{.spec.holderIdentity}'

# Stop K3s on leader node to trigger failover
ssh ubuntu@<leader-ip> 'sudo systemctl stop k3s'

# VIP should move to another master (check in ~15 seconds)
kubectl get lease -n kube-system plndr-cp-lock -o jsonpath='{.spec.holderIdentity}'

# Verify VIP is still accessible
curl -k https://192.168.10.100:6443/livez
```
