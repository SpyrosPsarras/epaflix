# SearXNG AirVPN Egress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** All SearXNG outbound requests leave through an AirVPN WireGuard tunnel, and nothing leaves when the tunnel is down.

**Architecture:** A gluetun Deployment `airvpn-proxy` in the `searxng` namespace holds the tunnel and serves an HTTP proxy. SearXNG uses it through `outgoing.proxies`. NetworkPolicies stop SearXNG from reaching anything else.

**Tech Stack:** k3s, kustomize with KSOPS, SOPS/age, gluetun, Argo CD.

**Spec:** `docs/superpowers/specs/2026-11-19-searxng-airvpn-egress-design.md`

## Global Constraints

- Namespace `searxng`. App folder `2-k3s/14.searxng/`. Labels: `app: airvpn-proxy` and the existing `app: searxng`.
- Image `qmcgaw/gluetun` with tag `v3.41.3` set only in `kustomization.yaml` `images:`. The manifest uses the bare name.
- Proxy port 8888 (`http`), health port 9999 (`health`).
- gluetun env: `VPN_SERVICE_PROVIDER=airvpn`, `VPN_TYPE=wireguard`, `SERVER_COUNTRIES=Netherlands,Germany,Sweden`, `HTTPPROXY=on`, `HEALTH_SERVER_ADDRESS=:9999`, `FIREWALL_INPUT_PORTS=8888,9999`, `FIREWALL_OUTBOUND_SUBNETS=10.42.0.0/16`. WireGuard keys come from Secret `airvpn-wireguard` with `envFrom`.
- Secret keys: `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_PRESHARED_KEY`, `WIREGUARD_ADDRESSES` (IPv4 `/32` only). The AirVPN device name is `searxng`.
- No key material in output, logs or Git. Only the top-level session handles the Secret.
- No comments in manifests unless a line cannot explain itself.

## Review Focus

1. A pod on another node calls the proxy: replies must route back over `eth0`, not into the tunnel. Covered by the cross-node call in Task 3.
2. The kubelet probes the gluetun health port from the node IP. The pod must turn Ready. Covered in Task 3.
3. The tunnel drops while SearXNG runs: searches fail, with no direct egress. Covered by scaling the proxy to 0 in Task 3.
4. The Discord bots, MCP hubs and search-guard still reach SearXNG on 8080. Task 2 adds no ingress policy to SearXNG, and Task 3 runs a search from another namespace.
5. Pod restart with a stale `ip rule` (gluetun wiki, Kubernetes section): the strategy is `Recreate` and a `postStart` deletes table 51820 rules. Covered by deleting the pod once in Task 3.

---

### Task 1: WireGuard Secret (top-level session only)

**Files:**
- Create: `2-k3s/14.searxng/airvpn-wireguard.enc.yaml`
- Modify: `2-k3s/14.searxng/ksops-generator.yaml` (add the file to `files:`)

- [ ] **Step 1:** Inside one `vault run` command that supplies the AirVPN API key, fetch `https://airvpn.org/api/generator/?protocols=wireguard_3_udp_1637&servers=nl&device=searxng&key=…`, build a `Secret` named `airvpn-wireguard` in namespace `searxng` with the three `stringData` keys, write it to the target path and run `sops -e -i` on it. Print nothing from the config.
- [ ] **Step 2:** Verify that `grep -c ENC\\[ 2-k3s/14.searxng/airvpn-wireguard.enc.yaml` shows 3 and that the address differs from Default's: `WIREGUARD_ADDRESSES` must not equal the `Default` session's `vpn_ipv4` reported by `userinfo`. Compare the values inside the command and print only `differs`.

### Task 2: Manifests

**Files:**
- Create: `2-k3s/14.searxng/airvpn-proxy.yaml` (Deployment, Service)
- Create: `2-k3s/14.searxng/network-policy.yaml` (NetworkPolicies `searxng-egress` and `airvpn-proxy-ingress`)
- Modify: `2-k3s/14.searxng/kustomization.yaml` (resources, `images:` entry `qmcgaw/gluetun` `v3.41.3`)
- Modify: `2-k3s/14.searxng/configmap.yaml` (add `outgoing.proxies`)

**Interfaces:**
- Consumes: Secret `airvpn-wireguard` from Task 1.
- Produces: Service `airvpn-proxy` port 8888 `http`, used by `settings.yml` as `http://airvpn-proxy.searxng.svc.cluster.local:8888`.

- [ ] **Step 1:** Deployment `airvpn-proxy`: 1 replica, `strategy: Recreate`, container `gluetun` with the env from Global Constraints, `envFrom` Secret `airvpn-wireguard`, capability `NET_ADMIN` added, `postStart` `/bin/sh -c "(ip rule del table 51820; ip -6 rule del table 51820) || true"`, startup and liveness `httpGet` on `health`, readiness `tcpSocket` on `http`, requests `cpu: 50m` and `memory: 64Mi`, `dnsPolicy: None` with `dnsConfig.nameservers: [127.0.0.1]` (the cluster search domains with `ndots:5` add about 6 s to every lookup through gluetun's DNS). Service `airvpn-proxy` ClusterIP port 8888. NetworkPolicy `airvpn-proxy-ingress`: podSelector `app: airvpn-proxy`, `policyTypes: [Ingress]`, allow TCP 8888 from pods with `app: searxng` in the same namespace, and TCP 9999 with no `from` restriction so kubelet probes work.
- [ ] **Step 2:** NetworkPolicy `searxng-egress`: podSelector `app: searxng`, `policyTypes: [Egress]`, allow kube-dns UDP and TCP 53 (copy the selector from `2-k3s/35.discord/media/network-policy.yaml`) and TCP 8888 to pods with `app: airvpn-proxy`.
- [ ] **Step 3:** In `settings.yml` add:

```yaml
outgoing:
  proxies:
    all://:
      - http://airvpn-proxy.searxng.svc.cluster.local:8888
```

- [ ] **Step 4:** Verify: `kubectl kustomize --enable-alpha-plugins --enable-exec 2-k3s/14.searxng` renders (needs the age key through `vault run --name sops-age-k3s-cluster`), and `kubectl apply --dry-run=server` on the rendered output passes. Expected: no errors, the image is `qmcgaw/gluetun:v3.41.3`.

### Task 3: Scratch verification (top-level session)

Run the render from Task 2 in a scratch namespace `searxng-scratch` (rewrite the namespace and the proxy URL, apply, and delete the namespace afterwards).

- [ ] **Step 1:** `airvpn-proxy` becomes Ready. `curl -x http://airvpn-proxy:8888 https://airvpn.org/api/whatismyip/` from a pod with `app: searxng` returns `"airvpn": true`.
- [ ] **Step 2:** From the SearXNG pod, `GET /search?q=kubernetes&format=json` returns at least 1 result. Record `unresponsive_engines` for the PR.
- [ ] **Step 3:** From the SearXNG pod, a direct `curl -m 5 https://1.1.1.1` fails.
- [ ] **Step 4:** From a pod in another namespace on a different node, the SearXNG search on 8080 succeeds.
- [ ] **Step 5:** Delete the proxy pod once. It becomes Ready again.
- [ ] **Step 6:** Scale `airvpn-proxy` to 0. The search returns 0 results and a direct `curl` still fails.
- [ ] **Step 7:** Delete namespace `searxng-scratch` and confirm through `userinfo` that no `searxng` session remains before the PR merges.

### Task 4: Review gate, PR, sync

- [ ] **Step 1:** Run review-gate (Standards and Spec reviewers) on the branch diff and the Task 2 and 3 output.
- [ ] **Step 2:** Commit `feat(searxng): send searches only through AirVPN`, rebase onto `origin/main`, push and open the PR.
- [ ] **Step 3:** After Spyros merges: run an Argo sync of `searxng` at the merged SHA (dry-run first), delete the SearXNG pod once so it reads the new `settings.yml` (the init container copies it only at start, and the namespace has no Reloader), then repeat the Task 3 Steps 1–3 checks against the live namespace.
