import hashlib
import json
import os
import re
import sys

EXCLUDED = ("t3code-work", "mcp-hub-work")
DESKTOPS = ("shypowder",)
KIND_SHORT = {"deployments": ("Deployment", "deploy"), "statefulsets": ("StatefulSet", "sts"),
              "daemonsets": ("DaemonSet", "ds"), "cronjobs": ("CronJob", "cronjob")}
HOST_RE = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:svc(?:\.cluster\.local)?|epaflix\.com)\b")
SECRET_DOC = re.compile(r"^kind:\s*Secret\s*$", re.M)
EXCLUDED_REF = re.compile(r"[^\s\"',;]*(?:" + "|".join(EXCLUDED) + r")[^\s\"',;]*", re.IGNORECASE)
SENSITIVE_KEY = re.compile(r"pass|pwhash|secret|token|key|credential|auth", re.IGNORECASE)
SCAN_LABELS = ("os", "kernel", "files", "live-config", "packages", "scan-errors")


def excluded(ns):
    return ns in EXCLUDED


def scrub(text):
    return EXCLUDED_REF.sub("***", text) if isinstance(text, str) else text


def ns_of(obj):
    return obj.get("metadata", {}).get("namespace")


def name_of(obj):
    return obj.get("metadata", {}).get("name")


def item(kind, name, source, runs_on=None, **fields):
    out = {"kind": kind, "name": name, "tags": ["sync"], "labels": {"source": source},
           "runsOn": runs_on or []}
    out.update({k: v for k, v in fields.items() if v is not None})
    return out


def label(it, key, value):
    if value not in (None, "", []):
        it["labels"][key] = value if isinstance(value, str) else ", ".join(value)


def repo_index(repo):
    idx = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                with open(full, "rb") as f:
                    idx.setdefault(hashlib.sha256(f.read()).hexdigest(), os.path.relpath(full, repo))
            except OSError:
                continue
    return idx


def git_status(sha256, idx):
    return f"in Git: {idx[sha256]}" if sha256 and sha256 in idx else "not in Git"


def sops_secret_keys(repo, path):
    keys = {}
    base = os.path.join(repo, path or "")
    if not path or not os.path.isdir(base):
        return keys
    for dirpath, _, filenames in os.walk(base):
        for fn in filenames:
            if not fn.endswith(".enc.yaml"):
                continue
            with open(os.path.join(dirpath, fn)) as f:
                text = f.read()
            for doc in text.split("\n---"):
                if not SECRET_DOC.search(doc):
                    continue
                m = re.search(r"^metadata:\s*\n(?:\s+.*\n)*?\s+name:\s*(\S+)", doc, re.M)
                block = re.search(r"^(?:stringData|data):\s*\n((?:[ \t]+.*\n?)*)", doc, re.M)
                if not m or not block:
                    continue
                indent = None
                names = []
                for line in block.group(1).splitlines():
                    stripped = line.lstrip()
                    cur = len(line) - len(stripped)
                    if indent is None:
                        indent = cur
                    if cur == indent and ":" in stripped:
                        names.append(stripped.split(":", 1)[0])
                keys[m.group(1)] = names
    return keys


def updates(images, app_path, repo):
    rules = {}
    try:
        with open(os.path.join(repo, ".github/renovate.json")) as f:
            for rule in json.load(f).get("packageRules", []):
                for pkg in rule.get("matchPackageNames", []):
                    rules[pkg] = rule
    except (OSError, ValueError):
        pass
    kust_text = ""
    if app_path and os.path.isdir(os.path.join(repo, app_path)):
        for dirpath, _, filenames in os.walk(os.path.join(repo, app_path)):
            for fn in filenames:
                if fn in ("kustomization.yaml", "values.yaml") or fn.endswith("-values.yaml"):
                    with open(os.path.join(dirpath, fn)) as f:
                        kust_text += f.read()
    result = []
    for image in images:
        repo_name = image.split("@")[0].rsplit(":", 1)[0] if ":" in image.split("/")[-1] else image.split("@")[0]
        short = repo_name.split("/")[-1]
        wf = os.path.join(repo, ".github/workflows", f"build-{short}.yml")
        if repo_name.startswith("ghcr.io/spyrospsarras/") and os.path.exists(wf):
            result.append(f"ci: build-{short}.yml")
        elif repo_name in kust_text:
            rule = rules.get(repo_name, {})
            result.append("manual" if rule.get("enabled") is False else
                          "renovate (review)" if rule.get("automerge") is False else "renovate")
        else:
            result.append("manual")
    return ", ".join(sorted(set(result))) or None


def pod_refs(template):
    spec = template.get("spec", {})
    containers = spec.get("containers", []) + spec.get("initContainers", [])
    images = [c.get("image", "") for c in spec.get("containers", [])]
    cms, secrets, env_values = set(), set(), []
    for v in spec.get("volumes", []):
        if "configMap" in v:
            cms.add(v["configMap"].get("name"))
        if "secret" in v:
            secrets.add(v["secret"].get("secretName"))
        for src in (v.get("projected") or {}).get("sources", []):
            if "configMap" in src:
                cms.add(src["configMap"].get("name"))
            if "secret" in src:
                secrets.add(src["secret"].get("name"))
    for c in containers:
        for ef in c.get("envFrom", []):
            if "configMapRef" in ef:
                cms.add(ef["configMapRef"].get("name"))
            if "secretRef" in ef:
                secrets.add(ef["secretRef"].get("name"))
        for e in c.get("env", []):
            if "value" in e and not SENSITIVE_KEY.search(e.get("name", "")):
                env_values.append(e["value"])
            ref = e.get("valueFrom", {})
            if "configMapKeyRef" in ref:
                cms.add(ref["configMapKeyRef"].get("name"))
            if "secretKeyRef" in ref:
                secrets.add(ref["secretKeyRef"].get("name"))
    pvcs = [v["persistentVolumeClaim"]["claimName"] for v in spec.get("volumes", []) if "persistentVolumeClaim" in v]
    return images, sorted(x for x in cms if x), sorted(x for x in secrets if x), env_values, pvcs


def template_of(kind, obj):
    if kind == "cronjobs":
        return obj.get("spec", {}).get("jobTemplate", {}).get("spec", {}).get("template", {})
    return obj.get("spec", {}).get("template", {})


def selects(selector, labels):
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def owners(k8s):
    rs_owner = {(ns_of(r), name_of(r)): o.get("name") for r in k8s.get("replicasets", [])
                for o in r.get("metadata", {}).get("ownerReferences", []) if o.get("kind") == "Deployment"}
    job_owner = {(ns_of(j), name_of(j)): o.get("name") for j in k8s.get("jobs", [])
                 for o in j.get("metadata", {}).get("ownerReferences", []) if o.get("kind") == "CronJob"}
    nodes = {}
    for p in k8s.get("pods", []):
        ns, node = ns_of(p), p.get("spec", {}).get("nodeName")
        if excluded(ns) or not node:
            continue
        for o in p.get("metadata", {}).get("ownerReferences", []):
            kind, name = o.get("kind"), o.get("name")
            if kind == "ReplicaSet" and (ns, name) in rs_owner:
                key = ("Deployment", ns, rs_owner[(ns, name)])
            elif kind == "Job" and (ns, name) in job_owner:
                key = ("CronJob", ns, job_owner[(ns, name)])
            elif kind in ("StatefulSet", "DaemonSet"):
                key = (kind, ns, name)
            else:
                continue
            nodes.setdefault(key, set()).add(node)
    return nodes


def pv_node(pv):
    for term in (((pv.get("spec", {}).get("nodeAffinity") or {}).get("required") or {}).get("nodeSelectorTerms") or []):
        for expr in term.get("matchExpressions") or []:
            if expr.get("key") == "kubernetes.io/hostname" and expr.get("values"):
                return expr["values"][0]
    return None


def build_k8s(k8s, repo, idx, zvols, guests, indexed):
    out = []
    apps = {}
    owner = {}
    for app in k8s.get("applications", []):
        dest = app.get("spec", {}).get("destination", {}).get("namespace")
        if excluded(dest):
            continue
        name = name_of(app)
        path = app.get("spec", {}).get("source", {}).get("path") or ""
        apps[name] = {"path": path, "dest": dest}
        it = item("System", f"app/{name}", "k8s", ["k3s/epaflix"], type="other")
        label(it, "kind", "argo-app")
        label(it, "namespace", dest)
        label(it, "path", path)
        status = app.get("status", {})
        label(it, "sync", (status.get("sync") or {}).get("status"))
        label(it, "health", (status.get("health") or {}).get("status"))
        if indexed is not None and dest not in indexed:
            it["labels"]["indexed"] = "no"
        out.append(it)
        for r in status.get("resources", []):
            if not excluded(r.get("namespace")):
                owner[(r.get("kind"), r.get("namespace"), r.get("name"))] = name
    for ns in k8s.get("namespaces", []):
        n = name_of(ns)
        if excluded(n) or n in apps or any(a["dest"] == n for a in apps.values()):
            continue
        it = item("System", f"app/{n}", "k8s", ["k3s/epaflix"], type="other")
        label(it, "kind", "namespace")
        if indexed is not None and n not in indexed:
            it["labels"]["indexed"] = "no"
        out.append(it)

    cm_data = {(ns_of(c), name_of(c)): {k: v for k, v in (c.get("data") or {}).items() if not SENSITIVE_KEY.search(k)}
               for c in k8s.get("configmaps", []) if not excluded(ns_of(c))}
    pvcs = {(ns_of(p), name_of(p)): p for p in k8s.get("persistentvolumeclaims", []) if not excluded(ns_of(p))}
    pvs = {name_of(p): p for p in k8s.get("persistentvolumes", [])
           if not excluded((p.get("spec", {}).get("claimRef") or {}).get("namespace"))}
    services = [s for s in k8s.get("services", []) if not excluded(ns_of(s))]
    routes = [r for r in k8s.get("ingressroutes", []) if not excluded(ns_of(r))]
    netpols = [n for n in k8s.get("networkpolicies", []) if not excluded(ns_of(n))]
    pod_nodes = owners(k8s)
    secret_keys_cache = {}

    for key, (kind_name, short) in KIND_SHORT.items():
        for obj in k8s.get(key, []):
            ns, name = ns_of(obj), name_of(obj)
            if excluded(ns):
                continue
            app = owner.get((kind_name, ns, name))
            app_item = f"app/{app}" if app else (f"app/{next((a for a, v in apps.items() if v['dest'] == ns), ns)}")
            tpl = template_of(key, obj)
            labels = (tpl.get("metadata") or {}).get("labels") or {}
            images, cms, secrets, env_values, claim_names = pod_refs(tpl)
            it = item("Service", f"{ns}/{short}/{name}", "k8s", [app_item])
            label(it, "images", sorted(set(images)))
            label(it, "nodes", sorted(pod_nodes.get((kind_name, ns, name), ())))
            app_path = apps.get(app, {}).get("path") if app else None
            label(it, "updates", updates(images, app_path, repo))
            label(it, "configmaps", [f"k8s/{ns}/configmaps/{c}.yaml" for c in cms])
            if app_path not in secret_keys_cache:
                secret_keys_cache[app_path] = sops_secret_keys(repo, app_path)
            sk = secret_keys_cache[app_path]
            label(it, "secrets", "; ".join(f"{s}: {', '.join(sk[s])}" if s in sk else f"{s}: live only" for s in secrets))
            svc_names = {name_of(s) for s in services if ns_of(s) == ns and selects(s.get("spec", {}).get("selector"), labels)}
            hosts, auth = [], set()
            for r in routes:
                if ns_of(r) != ns:
                    continue
                eps = ",".join(r.get("spec", {}).get("entryPoints", []))
                for route in r.get("spec", {}).get("routes", []):
                    if any(s.get("name") in svc_names for s in route.get("services", [])):
                        for h in re.findall(r"Host\(`([^`]+)`\)", route.get("match", "")):
                            hosts.append(f"{h} ({eps})")
                        auth.update(m.get("name") for m in route.get("middlewares", []) if "auth" in m.get("name", ""))
            label(it, "ingress", sorted(set(hosts)))
            label(it, "auth", sorted(auth))
            uses = set()
            for text in env_values + [v for c in cms for v in cm_data.get((ns, c), {}).values()]:
                uses.update(h for h in HOST_RE.findall(text or "") if not EXCLUDED_REF.search(h))
            for np in netpols:
                if ns_of(np) != ns or not selects((np.get("spec", {}).get("podSelector") or {}).get("matchLabels"), labels):
                    continue
                for rule in np.get("spec", {}).get("egress", []):
                    for peer in rule.get("to", []):
                        target = ((peer.get("namespaceSelector") or {}).get("matchLabels") or {}).get("kubernetes.io/metadata.name")
                        if target and target not in (ns, "kube-system") and not excluded(target):
                            uses.add(f"ns/{target}")
            label(it, "uses", sorted(u.replace(".cluster.local", "") for u in uses))
            label(it, "git", f"managed by app/{app} ({app_path})" if app else "live only")
            chains = []
            for c in claim_names:
                pvc = pvcs.get((ns, c))
                if not pvc:
                    continue
                pv_name = pvc.get("spec", {}).get("volumeName")
                pv = pvs.get(pv_name)
                link = f"{c} -> pv/{pv_name}"
                if pv:
                    handle = (pv.get("spec", {}).get("csi") or {}).get("volumeHandle")
                    local = (pv.get("spec", {}).get("local") or pv.get("spec", {}).get("hostPath") or {}).get("path")
                    if handle and handle in zvols:
                        link += f" -> truenas:{zvols[handle]} -> pool {zvols[handle].split('/')[0]}"
                    elif local:
                        link += f" -> local:{local}"
                        node = pv_node(pv)
                        if node and node in guests:
                            link += f" -> vm/{node}"
                chains.append(link)
            label(it, "disk-chain", chains)
            out.append(it)

    nodes = sorted(name_of(n) for n in k8s.get("nodes", []))
    cluster = item("System", "k3s/epaflix", "k8s", [f"vm/{n}" for n in nodes], type="cluster")
    label(cluster, "nodes", nodes)
    out.append(cluster)
    return out


def host_labels(it, alias, scan, idx):
    files = scan.get("files", [])
    in_git = sum(1 for f in files if git_status(f.get("sha256"), idx) != "not in Git")
    masked = sum(1 for f in files if f.get("masked"))
    label(it, "files", f"{len(files)} ({in_git} in Git, {masked} masked)")
    label(it, "live-config", f"homelab-live/{alias}/")
    os_info = scan.get("os") or {}
    label(it, "os", os_info.get("name"))
    label(it, "kernel", os_info.get("kernel"))
    if scan.get("packages"):
        label(it, "packages", str(len(scan["packages"])))
    if scan.get("errors"):
        label(it, "scan-errors", str(len(scan["errors"])))


def build_hosts(scans, idx):
    out, guests_by_name, zvols = [], {}, {}
    for alias, scan in sorted(scans.items()):
        pve = scan.get("pve")
        if pve:
            hw = scan.get("hardware") or {}
            kind = "Desktop" if alias in DESKTOPS else "Server"
            h = item(kind, f"hw/{alias}", alias)
            label(h, "model", " ".join(x for x in (hw.get("vendor"), hw.get("product")) if x))
            label(h, "serial", hw.get("serial"))
            label(h, "cpu", f"{hw.get('sockets')} x {hw.get('cpu')}" if hw.get("cpu") else None)
            label(h, "memory", f"{len(hw.get('dimms') or [])} DIMMs" if hw.get("dimms") else None)
            out.append(h)
            p = item("System", f"pve/{alias}", alias, [f"hw/{alias}"], type="hypervisor")
            host_labels(p, alias, scan, idx)
            out.append(p)
            if pve.get("cluster_reader"):
                for r in pve.get("resources", []):
                    if r.get("type") not in ("qemu", "lxc"):
                        continue
                    prefix = "vm" if r["type"] == "qemu" else "lxc"
                    short = r["name"].split(".")[0]
                    g = item("System", f"{prefix}/{short}", alias, [f"pve/{r['node']}"],
                             type="vm" if prefix == "vm" else "container")
                    cfg = (pve.get("guests") or {}).get(str(r["vmid"])) or {}
                    label(g, "vmid", str(r["vmid"]))
                    if cfg.get("cores"):
                        g["cores"] = int(cfg["cores"])
                    if cfg.get("memory"):
                        g["ram"] = round(int(cfg["memory"]) / 1024, 1)
                    disks = [f"{k}={v.split(',')[0]}" for k, v in cfg.items()
                             if re.match(r"^(scsi|virtio|sata|ide|rootfs|mp)\d*$", k) and isinstance(v, str)]
                    label(g, "disks", sorted(disks))
                    guests_by_name[short] = g
                    out.append(g)
        tn = scan.get("truenas")
        if tn:
            for d in tn.get("datasets", []):
                if d.get("type") == "VOLUME":
                    zvols[d["name"].rsplit("/", 1)[-1]] = d["name"]
            for a in tn.get("apps", []):
                s = item("Service", f"truenas/app/{a['name']}", alias, [f"vm/{scan.get('host') or alias}"])
                label(s, "state", a.get("state"))
                label(s, "version", a.get("version"))
                out.append(s)
    for alias, scan in scans.items():
        if scan.get("pve"):
            continue
        g = guests_by_name.get((scan.get("host") or "").split(".")[0]) or guests_by_name.get(alias)
        if g:
            host_labels(g, alias, scan, idx)
    return out, zvols, guests_by_name


def build_items(k8s, scans, repo, failed, indexed=None):
    idx = repo_index(repo)
    host_items, zvols, guests = build_hosts({a: s for a, s in scans.items() if a not in failed}, idx)
    items = host_items + (build_k8s(k8s, repo, idx, zvols, guests, indexed) if "k8s" not in failed else [])
    for it in items:
        it["labels"] = {k: scrub(v) for k, v in it["labels"].items()}
    seen, out = set(), []
    for it in items:
        key = it["name"].lower()
        if key in seen:
            print(f"warning: duplicate name {it['name']} dropped", file=sys.stderr)
            continue
        seen.add(key)
        out.append(it)
    return out


def carry_over(new, stored, ok_sources, today):
    stored_by = {s["name"].lower(): s for s in stored}
    out, seen = [], set()
    for it in new:
        old = stored_by.get(it["name"].lower())
        if old and "sync" not in (old.get("tags") or []):
            print(f"warning: {it['name']} exists without tag sync, left untouched", file=sys.stderr)
            seen.add(it["name"].lower())
            continue
        if old and "live-config" not in it["labels"]:
            for k in SCAN_LABELS:
                if k in (old.get("labels") or {}):
                    it["labels"][k] = old["labels"][k]
            if old.get("notes"):
                it["notes"] = old["notes"]
            it["tags"] = sorted(set(it["tags"]) | {t for t in old.get("tags") or [] if t != "sync"})
        it["labels"].pop("gone-since", None)
        seen.add(it["name"].lower())
        out.append(it)
    for s in stored:
        if "sync" not in (s.get("tags") or []) or s["name"].lower() in seen:
            continue
        if (s.get("labels") or {}).get("source") not in ok_sources:
            continue
        s = json.loads(json.dumps(s))
        s.setdefault("labels", {}).setdefault("gone-since", today)
        out.append(s)
    return out
