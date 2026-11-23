import hashlib
import os
import tempfile

X_SHA = hashlib.sha256(b"x").hexdigest()

TODAY = "2026-11-24"


def meta(name, ns=None, **extra):
    m = {"name": name}
    if ns:
        m["namespace"] = ns
    m.update(extra)
    return m


def pod_spec(image, cm=None, secret=None, env=None, pvc=None):
    c = {"name": "main", "image": image, "env": env or []}
    if secret:
        c["envFrom"] = [{"secretRef": {"name": secret}}]
    vols = []
    if cm:
        vols.append({"name": "cfg", "configMap": {"name": cm}})
    if pvc:
        vols.append({"name": "data", "persistentVolumeClaim": {"claimName": pvc}})
    return {"metadata": {"labels": {"app": "x"}}, "spec": {"containers": [c], "volumes": vols}}


def k8s():
    forgejo_tpl = pod_spec("codeberg.org/forgejo/forgejo:17.0.2-rootless@sha256:abc", cm="forgejo-cfg",
                           secret="forgejo-secrets", pvc="forgejo-data",
                           env=[{"name": "DB", "value": "postgres-cluster-rw.postgres-system.svc:5432"}])
    forgejo_tpl["metadata"]["labels"] = {"app": "forgejo"}
    hidden_tpl = pod_spec("evil:1")
    return {
        "namespaces": [{"metadata": meta(n)} for n in ("forgejo", "rackpeek", "t3code-work", "mcp-hub-work", "loose")],
        "nodes": [{"metadata": meta("k3s-worker-61")}, {"metadata": meta("k3s-master-51")}],
        "applications": [
            {"metadata": meta("forgejo", "argocd"),
             "spec": {"source": {"path": "2-k3s/38.forgejo"}, "destination": {"namespace": "forgejo"}},
             "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"},
                        "resources": [{"kind": "Deployment", "namespace": "forgejo", "name": "forgejo"},
                                      {"kind": "ConfigMap", "namespace": "forgejo", "name": "forgejo-cfg"}]}},
            {"metadata": meta("t3code-work", "argocd"),
             "spec": {"source": {"path": "2-k3s/13.t3code-work"}, "destination": {"namespace": "t3code-work"}},
             "status": {"resources": [{"kind": "StatefulSet", "namespace": "t3code-work", "name": "t3code-0"}]}},
        ],
        "deployments": [
            {"metadata": meta("forgejo", "forgejo"), "spec": {"template": forgejo_tpl}},
            {"metadata": meta("Forgejo", "forgejo"), "spec": {"template": forgejo_tpl}},
            {"metadata": meta("stray", "loose"), "spec": {"template": pod_spec("nginx:1")}},
        ],
        "statefulsets": [{"metadata": meta("secret-pod", "t3code-work"), "spec": {"template": hidden_tpl}}],
        "daemonsets": [],
        "cronjobs": [],
        "pods": [{"metadata": meta("forgejo-6dfb-abcde", "forgejo"), "spec": {"nodeName": "k3s-worker-61"}},
                 {"metadata": meta("secret-pod-0", "t3code-work"), "spec": {"nodeName": "k3s-worker-65"}}],
        "configmaps": [{"metadata": meta("forgejo-cfg", "forgejo"), "data": {"a": "url=http://rackpeek.rackpeek.svc:8080"}}],
        "services": [{"metadata": meta("forgejo", "forgejo"), "spec": {"selector": {"app": "forgejo"}}}],
        "ingressroutes": [{"metadata": meta("forgejo-https", "forgejo"),
                           "spec": {"entryPoints": ["internal"],
                                    "routes": [{"match": "Host(`git.epaflix.com`)",
                                                "middlewares": [{"name": "authentik-forwardauth"}],
                                                "services": [{"name": "forgejo"}]}]}}],
        "networkpolicies": [{"metadata": meta("forgejo-egress", "forgejo"),
                             "spec": {"podSelector": {"matchLabels": {"app": "forgejo"}},
                                      "egress": [{"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "postgres-system"}}}]},
                                                 {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}]}]}}],
        "persistentvolumeclaims": [{"metadata": meta("forgejo-data", "forgejo"), "spec": {"volumeName": "pvc-111"}}],
        "persistentvolumes": [
            {"metadata": meta("pvc-111"), "spec": {"claimRef": {"namespace": "forgejo", "name": "forgejo-data"},
                                                   "csi": {"driver": "org.democratic-csi.iscsi", "volumeHandle": "pvc-111"}}},
            {"metadata": meta("pvc-999"), "spec": {"claimRef": {"namespace": "mcp-hub-work", "name": "x"},
                                                   "csi": {"volumeHandle": "pvc-999"}}},
        ],
    }


def scans():
    return {
        "takaros": {"schema": 1, "host": "takaros", "os": {"name": "Debian GNU/Linux 13", "kernel": "6.17"},
                    "hardware": {"vendor": "HP", "product": "ProLiant DL380 Gen9", "serial": "S1", "cpu": "Xeon",
                                 "sockets": 2, "dimms": [{"size": "16 GB"}, {"size": "16 GB"}]},
                    "packages": ["pve-manager 9.2"],
                    "files": [{"path": "/etc/hosts", "sha256": X_SHA, "content": "x", "kind": "text", "masked": False, "size": 1, "mtime": 0},
                              {"path": "/root/fan.sh", "sha256": "bb", "content": "pw=***", "kind": "text", "masked": True, "size": 1, "mtime": 0}],
                    "pve": {"cluster_reader": True,
                            "resources": [{"type": "node", "node": "takaros", "status": "online"},
                                          {"type": "qemu", "node": "takaros", "vmid": 1200, "name": "truenas"},
                                          {"type": "lxc", "node": "takaros", "vmid": 1030, "name": "pihole.epaflix.com"},
                                          {"type": "qemu", "node": "takaros", "vmid": 1061, "name": "k3s-worker-61"}],
                            "guests": {"1200": {"cores": 4, "memory": "16384", "scsi0": "local-lvm:vm-1200-disk-0,size=32G"},
                                       "1030": {"cores": 1, "memory": "512"},
                                       "1061": {"cores": 8, "memory": "32768", "scsi0": "ssd-raid5:vm-1061-disk-0,size=200G"}},
                            "storage": []},
                    "truenas": None, "errors": []},
        "pihole": {"schema": 1, "host": "pihole", "os": {"name": "Debian"}, "hardware": {}, "packages": ["a 1"],
                   "files": [], "pve": None, "truenas": None, "errors": []},
        "truenas": {"schema": 1, "host": "truenas", "os": {"name": "TrueNAS"}, "hardware": {}, "packages": [], "files": [],
                    "pve": None,
                    "truenas": {"pools": [], "datasets": [{"name": "apps/k8s/iscsi/vols/pvc-111", "type": "VOLUME"}],
                                "nfs": [], "iscsi_extents": [], "apps": [{"name": "silo", "state": "RUNNING", "version": "1"}]},
                    "errors": []},
    }


def repo():
    root = tempfile.mkdtemp()

    def w(rel, text):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    w(".github/renovate.json", '{"packageRules": []}')
    w("2-k3s/38.forgejo/kustomization.yaml",
      "images:\n  - name: codeberg.org/forgejo/forgejo\n    newTag: 17.0.2-rootless@sha256:abc\n")
    w("2-k3s/38.forgejo/secrets.enc.yaml",
      "apiVersion: v1\nkind: Secret\nmetadata:\n    name: forgejo-secrets\nstringData:\n    DB_PASSWD: ENC[AES256_GCM,data:zz]\n    SECRET_KEY: ENC[AES256_GCM,data:yy]\nsops:\n    version: 3\n")
    w("1-proxmox/takaros/hosts", "x")
    return root


def stored():
    return [
        {"kind": "System", "name": "app/forgejo", "type": "other", "tags": ["sync", "critical"],
         "labels": {"source": "k8s"}, "notes": "why"},
        {"kind": "System", "name": "app/old-thing", "type": "other", "tags": ["sync"], "labels": {"source": "k8s"}},
        {"kind": "System", "name": "app/older", "type": "other", "tags": ["sync"],
         "labels": {"source": "k8s", "gone-since": "2026-11-01"}},
        {"kind": "System", "name": "vm/nick", "type": "vm", "tags": ["sync"], "labels": {"source": "nick"}},
        {"kind": "Server", "name": "my-switch-notes", "tags": ["mine"], "labels": {}},
    ]
