#!/usr/bin/env bash
# Renders the kured overlay and checks the reboot policy agreed in #1910.
set -euo pipefail
dir=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$dir/../.." && pwd)
out=$(mktemp -d); trap 'rm -rf "$out"' EXIT

kustomize build --enable-helm "$dir" >"$out/rendered.yaml"

python3 - "$out/rendered.yaml" "$root" <<'PY'
import os, re, sys, yaml

docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
root = sys.argv[2]
fail = []

ds = [d for d in docs if d["kind"] == "DaemonSet"]
if len(ds) != 1:
    sys.exit(f"FAIL: expected one kured DaemonSet, got {len(ds)}")
ds = ds[0]
if ds["metadata"].get("namespace") != "kube-system":
    fail.append("kured must run in kube-system")

pod = ds["spec"]["template"]["spec"]
args = {}
for a in pod["containers"][0].get("command", []) + pod["containers"][0].get("args", []):
    if a.startswith("--"):
        k, _, v = a[2:].partition("=")
        args[k] = v

def want(flag, value):
    if args.get(flag) != value:
        fail.append(f"--{flag} must be {value!r}, got {args.get(flag)!r}")

want("reboot-days", "sat")
want("start-time", "02:00")
want("end-time", "05:00")
want("time-zone", "UTC")
want("concurrency", "1")
want("drain-timeout", "5m")
want("drain-grace-period", "60")
want("prometheus-url", "http://kube-prometheus-stack-prometheus.observability.svc.cluster.local:9090")
want("alert-filter-match-only", "true")
if "force-reboot" in args and args["force-reboot"] != "false":
    fail.append("--force-reboot would reboot a node whose drain failed")

blocking = {"K3sUpgradeJobActive", "K3sNodeCordoned", "KubeNodeNotReady",
            "KubeNodeUnreachable", "etcdMembersDown", "etcdInsufficientMembers",
            "etcdNoLeader"}
regex = re.compile(args.get("alert-filter-regexp", "a^"))
for name in sorted(blocking):
    if not regex.search(name):
        fail.append(f"alert {name} must block reboots")
for name in ("Watchdog", "InfoInhibitor", "K3sNodeCordonedX", "XKubeNodeNotReady"):
    if regex.search(name):
        fail.append(f"alert {name} must not block reboots")

custom = open(f"{root}/2-k3s/10.observability/alertmanager-config/custom-alerts.yaml").read()
for name in ("K3sUpgradeJobActive", "K3sNodeCordoned"):
    if f"alert: {name}\n" not in custom:
        fail.append(f"blocking alert {name} no longer exists in custom-alerts.yaml")

tolerations = pod.get("tolerations", [])
if not any(t.get("key") == "node-role.kubernetes.io/control-plane" or t.get("operator") == "Exists" and "key" not in t
           for t in tolerations):
    fail.append("kured must tolerate the control-plane taint so masters get rebooted")

app_path = f"{root}/2-k3s/11.argocd/apps/app-kured.yaml"
app = yaml.safe_load(open(app_path)) if os.path.exists(app_path) else {}
if app.get("spec", {}).get("source", {}).get("path") != "2-k3s/28.kured":
    fail.append("app-kured.yaml must point at 2-k3s/28.kured")
apps = yaml.safe_load(open(f"{root}/2-k3s/11.argocd/apps/kustomization.yaml"))
if "app-kured.yaml" not in apps["resources"]:
    fail.append("app-kured.yaml is not listed in the apps kustomization")

if os.path.exists(f"{root}/2-k3s/maintenance/node-os-updater-cronjob.yaml"):
    fail.append("the SSH node-os-updater CronJob must be gone; kured owns reboots")
if "node-os-updater" in open(f"{root}/2-k3s/maintenance/kustomization.yaml").read():
    fail.append("the maintenance kustomization still applies node-os-updater")
cordoned = custom.split("alert: K3sNodeCordoned\n", 1)[-1].split("- alert:", 1)[0]
if "node-os-updater" in cordoned or "kured" not in cordoned:
    fail.append("K3sNodeCordoned must name kured, not node-os-updater, as the reboot cordoner")

if fail:
    print("\n".join("FAIL: " + f for f in fail), file=sys.stderr)
    sys.exit(1)
print("kured tests OK")
PY
