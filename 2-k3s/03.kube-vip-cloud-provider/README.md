# kube-vip Cloud Provider Installation

This guide covers the installation and configuration of kube-vip cloud provider for k3s cluster with 3 control-plane nodes.

## Overview

The kube-vip cloud provider enables automatic IP address assignment for Services of type `LoadBalancer` in on-premises Kubernetes clusters, similar to cloud provider load balancers.

## Prerequisites

- k3s cluster with kube-vip installed for control-plane HA
- kubectl configured to access the cluster
- Available IP address range for LoadBalancer services

## Network Configuration

This cluster uses a **dual-network setup**:

- **External Network (`192.168.10.X`)**: Used for internet access, NFS connections, and **LoadBalancer services**
- **Internal Network (`10.0.0.X`)**: Used for internal cluster communication (flannel overlay network)
  - Node IPs: 10.0.0.51, 10.0.0.52, 10.0.0.53

### Important Notes:

1. **LoadBalancer IPs must use the external network (`192.168.10.X`)** - This is the network where clients connect from
2. kube-vip will advertise LoadBalancer IPs via ARP on the `192.168.10.X` network (eth0 interface)
3. The internal network (`10.0.0.X`) is already configured with flannel on eth1 and doesn't need changes
4. All IP pool configurations below should use IPs from the `192.168.10.X` range

## Installation Steps

### 1. Install the kube-vip Cloud Provider

Deploy the cloud provider controller:

```bash
kubectl apply -f https://raw.githubusercontent.com/kube-vip/kube-vip-cloud-provider/main/manifest/kube-vip-cloud-controller.yaml
```

Verify the deployment:

```bash
kubectl get pods -n kube-system | grep kube-vip-cloud-provider
```

### 2. Create IP Address Pool ConfigMap

The cloud provider uses a ConfigMap to manage IP address pools for LoadBalancer services. You can configure:

- **Global CIDR**: Available to all namespaces
- **Global Range**: IP range available to all namespaces
- **Namespace-specific CIDR**: Only available in specific namespace
- **Namespace-specific Range**: IP range only available in specific namespace

#### Current Configuration (Deployed)

```bash
kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: kubevip
  namespace: kube-system
data:
  range-traefik-system: 192.168.10.101-192.168.10.109  # Dedicated to Traefik namespace (9 IPs)
  range-global: 192.168.10.110-192.168.10.199           # Available to all other namespaces (90 IPs)
EOF
```

This splits the pool so Traefik always gets IPs in the .101-.109 range (currently uses .101), and all other services get .110+.

Verify the ConfigMap:

```bash
kubectl get configmap -n kube-system kubevip -o yaml
```

## Usage Examples

### Example 1: Basic LoadBalancer Service

Create a deployment and expose it as LoadBalancer:

```bash
# Create nginx deployment
kubectl create deployment nginx --image=nginx

# Expose as LoadBalancer (gets IP from pool automatically)
kubectl expose deployment nginx --port=80 --type=LoadBalancer --name=nginx-lb
```

Check the assigned IP:

```bash
kubectl get svc nginx-lb
```

### Example 2: LoadBalancer Service with YAML

Create a service definition:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: nginx-service
spec:
  type: LoadBalancer
  selector:
    app: nginx
  ports:
  - name: http
    port: 80
    targetPort: 80
    protocol: TCP
```

Apply it:

```bash
kubectl apply -f nginx-service.yaml
```

### Example 3: Service with Specific IP Address

Request a specific IP (must be within configured range or outside it):

```bash
kubectl expose deployment nginx --port=80 --type=LoadBalancer \
  --name=nginx-custom-ip --load-balancer-ip=192.168.10.150
```

Or via YAML:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: nginx-custom-ip
spec:
  type: LoadBalancer
  loadBalancerIP: "192.168.10.150"
  selector:
    app: nginx
  ports:
  - name: http
    port: 80
    targetPort: 80
    protocol: TCP
```

### Example 4: Complete Application Stack

Deploy a complete application with LoadBalancer:

```yaml
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: webapp
  labels:
    app: webapp
spec:
  replicas: 3
  selector:
    matchLabels:
      app: webapp
  template:
    metadata:
      labels:
        app: webapp
    spec:
      containers:
      - name: webapp
        image: nginx:alpine
        ports:
        - containerPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: webapp-lb
spec:
  type: LoadBalancer
  selector:
    app: webapp
  ports:
  - name: http
    port: 80
    targetPort: 80
    protocol: TCP
  - name: https
    port: 443
    targetPort: 443
    protocol: TCP
```

Apply it:

```bash
kubectl apply -f webapp-stack.yaml
```

Wait for IP assignment:

```bash
kubectl get svc webapp-lb -w
```

Test the service:

```bash
# Get the LoadBalancer IP
LB_IP=$(kubectl get svc webapp-lb -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

# Test HTTP access
curl http://$LB_IP
```

## Verification

### Check all LoadBalancer services:

```bash
kubectl get svc --all-namespaces -o wide | grep LoadBalancer
```

### Check cloud provider logs:

```bash
kubectl logs -n kube-system -l app=kube-vip-cloud-provider
```

### Check kube-vip pods:

```bash
kubectl get pods -n kube-system -l app.kubernetes.io/name=kube-vip
```

### View IP allocation:

```bash
kubectl get configmap -n kube-system kubevip -o yaml
```

## Troubleshooting

### Service stuck in Pending state

1. Check if cloud provider is running:
```bash
kubectl get pods -n kube-system | grep cloud-provider
```

2. Check cloud provider logs:
```bash
kubectl logs -n kube-system -l app=kube-vip-cloud-provider
```

3. Verify ConfigMap exists:
```bash
kubectl get configmap -n kube-system kubevip
```

4. Check if IP pool has available addresses:
```bash
kubectl get svc --all-namespaces | grep LoadBalancer
```

### LoadBalancer IP not accessible

1. Verify kube-vip is running:
```bash
kubectl get pods -n kube-system -l app.kubernetes.io/name=kube-vip
```

2. Check kube-vip logs:
```bash
kubectl logs -n kube-system -l app.kubernetes.io/name=kube-vip
```

3. Verify ARP is working (if using ARP mode):
```bash
# From another machine on the network
arping <LOADBALANCER_IP>
```

## Configuration Tips

1. **Choose appropriate IP ranges**: Ensure IPs don't conflict with DHCP or existing static IPs
2. **Use namespace-specific pools**: For multi-tenant environments
3. **Reserve IPs**: Keep some IPs for manual assignment using `loadBalancerIP`
4. **Monitor IP usage**: Regularly check available IPs in your pools

## References

- [kube-vip Cloud Provider Documentation](https://kube-vip.io/docs/usage/cloud-provider/)
- [kube-vip GitHub](https://github.com/kube-vip/kube-vip)
- [Kubernetes Services](https://kubernetes.io/docs/concepts/services-networking/service/)
