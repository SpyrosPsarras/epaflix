import concurrent.futures
import datetime
import importlib.util
import json
import os
import pathlib
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import build
import homelable as hl

HERE = pathlib.Path(__file__).resolve().parent
SCANNER = next(p for p in (HERE / "homelab_scan.py", HERE.parent.parent / "1-proxmox/homelab-scan/homelab-scan.py") if p.exists())
_spec = importlib.util.spec_from_file_location("homelab_scan", SCANNER)
scanner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scanner)

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_LISTS = {
    "namespaces": "/api/v1/namespaces",
    "nodes": "/api/v1/nodes",
    "persistentvolumes": "/api/v1/persistentvolumes",
    "applications": "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications",
}
K8S_NAMESPACED = {
    "pods": "/api/v1/namespaces/{ns}/pods",
    "configmaps": "/api/v1/namespaces/{ns}/configmaps",
    "services": "/api/v1/namespaces/{ns}/services",
    "persistentvolumeclaims": "/api/v1/namespaces/{ns}/persistentvolumeclaims",
    "deployments": "/apis/apps/v1/namespaces/{ns}/deployments",
    "statefulsets": "/apis/apps/v1/namespaces/{ns}/statefulsets",
    "daemonsets": "/apis/apps/v1/namespaces/{ns}/daemonsets",
    "cronjobs": "/apis/batch/v1/namespaces/{ns}/cronjobs",
    "jobs": "/apis/batch/v1/namespaces/{ns}/jobs",
    "replicasets": "/apis/apps/v1/namespaces/{ns}/replicasets",
    "networkpolicies": "/apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies",
    "ingressroutes": "/apis/traefik.io/v1alpha1/namespaces/{ns}/ingressroutes",
}
SCAN_TIMEOUT = 600
SCAN_WORKERS = 6


class RealIO:
    def __init__(self, env):
        self.env = dict(env)
        self.indexed = []
        key = os.path.join(tempfile.mkdtemp(), "id")
        shutil.copyfile(self.env["SSH_KEY_FILE"], key)
        os.chmod(key, 0o600)
        self.env["SSH_KEY_FILE"] = key

    def _hl(self, method, path, body=None):
        req = urllib.request.Request(self.env["HL_URL"].rstrip("/") + "/api/v1/" + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-MCP-Service-Key": self.env["HL_SERVICE_KEY"],
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            data = r.read()
        return json.loads(data) if data else None

    def designs(self):
        return self._hl("GET", "designs")

    def create_design(self, name):
        return self._hl("POST", "designs", {"name": name, "design_type": "network"})["id"]

    def canvas(self, design_id):
        return self._hl("GET", "canvas?design_id=" + urllib.parse.quote(design_id))

    def save(self, body):
        self._hl("POST", "canvas/save", body)

    def documents(self):
        return self._hl("GET", "documents?kind=node")

    def document(self, doc_id):
        return self._hl("GET", "documents/" + urllib.parse.quote(doc_id))

    def create_document(self, body):
        self._hl("POST", "documents", body)

    def update_document(self, doc_id, body):
        self._hl("PATCH", "documents/" + urllib.parse.quote(doc_id), body)

    def git_sync(self):
        self._hl("POST", "git-sync")

    def rpk_get(self):
        req = urllib.request.Request(self.env["RPK_URL"].rstrip("/") + "/api/inventory",
                                     headers={"X-Api-Key": self.env["RPK_API_KEY"]})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)

    def _k8s(self, path):
        host = "https://kubernetes.default.svc"
        with open(f"{SA}/token") as f:
            token = f.read().strip()
        ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
        req = urllib.request.Request(host + path, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            return json.load(r).get("items", [])

    def k8s(self):
        out = {k: self._k8s(p) for k, p in K8S_LISTS.items()}
        for k in K8S_NAMESPACED:
            out[k] = []
        self.indexed = []
        for ns in out["namespaces"]:
            name = ns["metadata"]["name"]
            if build.excluded(name):
                continue
            try:
                lists = {k: self._k8s(p.format(ns=name)) for k, p in K8S_NAMESPACED.items()}
            except urllib.error.HTTPError as exc:
                if exc.code == 403:
                    continue
                raise
            self.indexed.append(name)
            for k, v in lists.items():
                out[k].extend(v)
        return out

    def clone_repo(self, dest):
        subprocess.run(["git", "clone", "-q", "--depth", "1", self.env["REPO_URL"], dest], check=True, timeout=300)

    def scan(self, alias, target):
        cmd = ["ssh", "-i", self.env["SSH_KEY_FILE"], "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", f"UserKnownHostsFile={self.env['KNOWN_HOSTS_FILE']}", "-o", "ConnectTimeout=10", target]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=SCAN_TIMEOUT)
        if res.returncode != 0:
            raise RuntimeError(f"ssh exit {res.returncode}: {res.stderr.strip()[-200:]}")
        return json.loads(res.stdout)

    def gitleaks(self, tree):
        report = os.path.join(tempfile.mkdtemp(), "r.json")
        subprocess.run(["gitleaks", "dir", tree, "--no-banner", "--exit-code", "0",
                        "--report-format", "json", "--report-path", report], check=True, timeout=600,
                       capture_output=True)
        with open(report) as f:
            found = json.load(f) or []
        return sorted({os.path.relpath(x["File"], tree) if os.path.isabs(x["File"]) else x["File"] for x in found})

    def push_live(self, tree, dirs, namespaces):
        work = tempfile.mkdtemp()
        askpass = os.path.join(work, "askpass")
        with open(askpass, "w") as f:
            f.write("#!/bin/sh\ncase \"$1\" in Username*) echo homelab-bot;; *) echo \"$HOMELAB_LIVE_TOKEN\";; esac\n")
        os.chmod(askpass, 0o700)
        env = dict(os.environ, GIT_ASKPASS=askpass, GIT_TERMINAL_PROMPT="0",
                   GIT_AUTHOR_NAME="homelab-sync", GIT_AUTHOR_EMAIL="homelab-bot@epaflix.com",
                   GIT_COMMITTER_NAME="homelab-sync", GIT_COMMITTER_EMAIL="homelab-bot@epaflix.com")
        repo = os.path.join(work, "live")
        url = self.env["HOMELAB_LIVE_URL"]

        def git(*args, check=True):
            return subprocess.run(["git", "-C", repo, *args], env=env, check=check, timeout=300,
                                  capture_output=True, text=True)

        res = subprocess.run(["git", "clone", "-q", "--depth", "1", url, repo], env=env, capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            os.makedirs(repo)
            git("init", "-q", "-b", "main")
            git("remote", "add", "origin", url)
        for entry in dirs:
            target = os.path.join(repo, entry)
            if os.path.isdir(target):
                shutil.rmtree(target)
            if os.path.isdir(os.path.join(tree, entry)):
                shutil.copytree(os.path.join(tree, entry), target)
        k8s_dir = os.path.join(repo, "k8s")
        if namespaces is not None and os.path.isdir(k8s_dir):
            for ns in os.listdir(k8s_dir):
                if ns not in namespaces:
                    shutil.rmtree(os.path.join(k8s_dir, ns))
        git("add", "-A")
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return
        git("commit", "-q", "-m", f"sync {datetime.date.today().isoformat()}")
        git("push", "-q", "origin", "HEAD:main")


def safe_rel(path):
    rel = os.path.normpath(path.lstrip("/"))
    if rel.startswith("..") or os.path.isabs(rel):
        raise ValueError(path)
    return rel


def mask_configmap(data):
    return {k: "***" if scanner.sensitive_key(k) else build.scrub(scanner.mask_text(v or ""))
            for k, v in (data or {}).items()}


def write_tree(tree, scans, k8s):
    idx_lines = {}
    for alias, scan in scans.items():
        base = os.path.join(tree, alias)
        os.makedirs(base, exist_ok=True)
        lines = []
        for f in scan.get("files", []):
            lines.append(f"{f['path']}\t{f.get('kind')}\t{f.get('sha256') or '-'}\t{'masked' if f.get('masked') else ''}")
            if f.get("content") is not None:
                dest = os.path.join(base, safe_rel(f["path"]))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w") as out:
                    out.write(build.scrub(f["content"]))
        with open(os.path.join(base, "FILES.tsv"), "w") as out:
            out.write("path\tkind\tsha256\tmasked\n" + "\n".join(sorted(lines)) + "\n")
        with open(os.path.join(base, "packages.txt"), "w") as out:
            out.write("\n".join(scan.get("packages") or []) + "\n")
        idx_lines[alias] = len(lines)
    for cm in (k8s or {}).get("configmaps", []):
        ns, name = cm["metadata"].get("namespace"), cm["metadata"]["name"]
        if build.excluded(ns):
            continue
        dest = os.path.join(tree, "k8s", ns, "configmaps", f"{name}.yaml")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        data = mask_configmap(cm.get("data"))
        with open(dest, "w") as out:
            json.dump({"name": name, "namespace": ns, "data": data}, out, indent=2, sort_keys=True)
    return idx_lines


def read_stored(io, seed):
    design = next((d for d in io.designs() if d.get("name") == hl.DESIGN), None)
    canvas = hl.check_canvas(io.canvas(design["id"])) if design else {}
    stored = hl.stored_items(canvas)[0]
    if stored or not seed:
        return design, canvas, stored, False
    return design, canvas, hl.seed_items(io.rpk_get().get("resources", [])), True


def write_docs(io, items, node_ids, log):
    existing = {d.get("node_id"): d for d in io.documents()}
    docs = hl.workload_docs(items)
    conflicts = 0
    for parent, body in sorted(docs.items()):
        nid = node_ids.get(parent.lower())
        if not nid:
            continue
        doc = existing.get(nid)
        if doc is None:
            io.create_document({"kind": "node", "title": f"{parent} workloads", "node_id": nid, "body": body})
        elif doc.get("title") != f"{parent} workloads":
            conflicts += 1
            log(f"homelable: {parent} already has another page, workloads not written")
        else:
            current = io.document(doc["id"]) or {}
            if current.get("title") != f"{parent} workloads":
                conflicts += 1
                log(f"homelable: {parent} page was renamed, workloads not written")
            elif current.get("body") != body:
                io.update_document(doc["id"], {"body": body, "revision_reason": "import"})
    log("homelable: workload pages written")
    return conflicts


def run(io, hosts, today, log, seed=False):
    exit_code = 0
    try:
        design, canvas, stored, seeded = read_stored(io, seed)
    except Exception as exc:
        log(f"homelable: read failed: {type(exc).__name__}: {exc}")
        return 1
    if seeded:
        log(f"homelable: {hl.DESIGN!r} has no sync nodes, seeding {len(stored)} items from RackPeek")
    work = tempfile.mkdtemp()
    repo = os.path.join(work, "repo")
    os.makedirs(repo)
    try:
        io.clone_repo(repo)
    except Exception as exc:
        log(f"repo: clone failed: {type(exc).__name__}: {exc}")
        return 1
    failed = set()
    k8s = {}
    try:
        k8s = io.k8s()
        log(f"k8s: ok, {sum(len(v) for v in k8s.values())} objects, indexed {len(getattr(io, 'indexed', []) or [])} namespaces")
    except Exception as exc:
        failed.add("k8s")
        log(f"k8s: failed: {type(exc).__name__}: {exc}")
    scans = {}
    with concurrent.futures.ThreadPoolExecutor(SCAN_WORKERS) as pool:
        futures = {pool.submit(io.scan, alias, target): alias for alias, target in hosts.items()}
        for fut in concurrent.futures.as_completed(futures):
            alias = futures[fut]
            try:
                scans[alias] = fut.result()
                log(f"{alias}: ok, {len(scans[alias].get('files', []))} files, {len(scans[alias].get('errors', []))} probe errors")
            except Exception as exc:
                failed.add(alias)
                log(f"{alias}: failed: {type(exc).__name__}: {exc}")
    tree = os.path.join(work, "live")
    os.makedirs(tree)
    write_tree(tree, scans, k8s)
    try:
        leaks = io.gitleaks(tree)
    except Exception as exc:
        log(f"gitleaks: failed: {type(exc).__name__}: {exc}")
        return 1
    for rel in leaks:
        path = os.path.join(tree, safe_rel(rel))
        if os.path.isfile(path):
            os.remove(path)
        log(f"gitleaks: dropped {rel}")
    indexed = set(getattr(io, "indexed", None) or []) if "k8s" not in failed else None
    namespaces = None if "k8s" in failed else {name_of_ns for name_of_ns in
                                               (n["metadata"]["name"] for n in k8s.get("namespaces", []))
                                               if not build.excluded(name_of_ns)}
    dirs = sorted(set(scans) | {f"k8s/{ns}" for ns in (indexed or set())})
    try:
        io.push_live(tree, dirs, namespaces)
        log("forgejo: pushed homelab-live")
    except Exception as exc:
        exit_code = 1
        log(f"forgejo: push failed: {type(exc).__name__}: {exc}")
    items = build.build_items(k8s, scans, repo, failed, indexed)
    ok_sources = ({"k8s"} | set(hosts)) - failed
    if design:
        try:
            canvas = hl.check_canvas(io.canvas(design["id"]))
            fresh_stored = hl.stored_items(canvas)[0]
            if fresh_stored or not seeded:
                stored = fresh_stored
        except Exception as exc:
            log(f"homelable: read failed: {type(exc).__name__}: {exc}")
            return 1
    resources = build.carry_over(items, stored, ok_sources, today)
    kept = {r["name"].lower() for r in resources}
    resources += [s for s in stored if s["name"].lower() not in kept]
    for r in resources:
        r["labels"] = {k: build.scrub(v) for k, v in (r.get("labels") or {}).items()}
        if r.get("notes"):
            r["notes"] = build.scrub(r["notes"])
    try:
        design_id = design["id"] if design else io.create_design(hl.DESIGN)
        body = hl.payload(resources, canvas, hl.ips(hosts, scans), design_id)
        io.save(body)
        log(f"homelable: saved {len(body['nodes'])} nodes")
    except Exception as exc:
        log(f"homelable: save failed: {type(exc).__name__}: {exc}")
        return 1
    try:
        if write_docs(io, resources, {hl._name(n).lower(): n["id"] for n in body["nodes"] if hl._is_sync(n)}, log):
            exit_code = 1
    except Exception as exc:
        exit_code = 1
        log(f"homelable: workload pages failed: {type(exc).__name__}: {exc}")
    try:
        io.git_sync()
        log("homelable: git sync pushed homelable-config")
    except Exception as exc:
        exit_code = 1
        log(f"homelable: git sync failed: {type(exc).__name__}: {exc}")
    return 1 if failed else exit_code


def main():
    needed = ("HL_URL", "HL_SERVICE_KEY", "HOMELAB_LIVE_URL", "HOMELAB_LIVE_TOKEN", "REPO_URL",
              "SSH_KEY_FILE", "KNOWN_HOSTS_FILE", "HOSTS")
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:
        print(f"missing env: {', '.join(missing)}", file=sys.stderr)
        return 1
    hosts = dict(pair.split("=", 1) for pair in os.environ["HOSTS"].split())
    today = datetime.date.today().isoformat()
    seed = bool(os.environ.get("RPK_URL") and os.environ.get("RPK_API_KEY"))
    return run(RealIO(os.environ), hosts, today, lambda line: print(line, flush=True), seed)


if __name__ == "__main__":
    sys.exit(main())
