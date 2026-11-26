#!/usr/bin/env python3
import fnmatch
import hashlib
import json
import os
import re
import socket
import subprocess

SCHEMA = 1
MAX_CONTENT = 64 * 1024
PROBE_TIMEOUT = 20

NAME_ONLY_DIRS = ("/etc/ssh", "/etc/pve/priv", "/etc/rancher", "/etc/proxmox-backup", "/etc/proxmox-datacenter-manager",
                  "/etc/pihole", "/etc/wireguard", "/etc/ssl/private")
NAME_ONLY_GLOBS = ("/etc/shadow*", "/etc/gshadow*", "*.key", "*.pem", "*.pw", "id_*")
SKIP_DIRS = ("/var/lib",)
WALK_DIRS = ("/usr/local", "/etc/systemd/system", "/etc/cron.d", "/etc/cron.daily",
             "/etc/cron.hourly", "/etc/cron.weekly", "/etc/cron.monthly",
             "/var/spool/cron/crontabs")
WALK_FILES = ("/etc/crontab", "/etc/fstab", "/etc/hosts", "/etc/sysctl.conf",
              "/etc/network/interfaces", "/etc/netplan")
WALK_LIMIT = 5000

KEY_SEP = re.compile(r"(?<![A-Za-z0-9_.-])[\"']?(?P<key>[A-Za-z0-9_.-]+)[\"']?\s*[:=]\s*")
SENSITIVE = re.compile(r"pass|pwhash|secret|token|key|credential|auth", re.IGNORECASE)
BLOCK = re.compile(r"^[|>](?:[1-9][+-]?|[+-][1-9]?)?$")
GUEST_FIELDS = ("cores", "sockets", "memory", "name", "ostype", "hostname")
DISK_KEY = re.compile(r"^(scsi|virtio|sata|ide|efidisk|tpmstate|unused|mp)\d*$|^rootfs$")
CLI_FLAG = re.compile(
    r"(?P<pre>(?<![A-Za-z0-9_-])--?[A-Za-z0-9-]*(?:pass|secret|token|key|credential)[A-Za-z0-9-]*[ =])(?P<val>\"[^\"\n]*\"|'[^'\n]*'|[^\s;]+)",
    re.IGNORECASE)
URL_TOKEN = re.compile(r"(?P<pre>://[^\s\"']*?/)[A-Za-z0-9_-]{32,}(?=[/?#\s\"']|$)", re.MULTILINE)
URL_PASSWORD = re.compile(r"(?P<pre>(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(?P<at>@)")


def under(path, prefix):
    return path == prefix or path.startswith(prefix + "/")


def sensitive_key(key):
    return bool(SENSITIVE.search(key))


def mask_line(line):
    if ":" not in line and "=" not in line:
        return line, None
    m = next((m for m in KEY_SEP.finditer(line) if sensitive_key(m.group("key"))), None)
    if not m:
        return line, None
    rest = line[m.end():]
    if rest.strip() == "" or rest.lstrip().startswith("#"):
        return line, len(line) - len(line.lstrip())
    if rest.strip() in ("***", '"***"', "'***'"):
        return line, None
    if BLOCK.match(rest.split("#")[0].strip()):
        return line, len(line) - len(line.lstrip())
    quote = rest[0] if rest[:1] in ("'", '"') else ""
    return line[:m.end()] + quote + "***", None


def mask_text(text):
    text = URL_PASSWORD.sub(lambda m: m.group("pre") + "***" + m.group("at"), text)
    while True:
        masked = URL_TOKEN.sub(lambda m: m.group("pre") + "***", text)
        if masked == text:
            break
        text = masked
    text = CLI_FLAG.sub(lambda m: m.group("pre") + "***", text)
    out, block_indent, cont = [], None, False
    for line in text.split("\n"):
        if cont:
            if line.lstrip().startswith(("#", ";")):
                out.append(mask_line(line)[0])
                continue
            out.append("***" if line.strip() else line)
            cont = line.rstrip().endswith("\\")
            continue
        if block_indent is not None:
            if not line.strip():
                out.append(line)
                continue
            if line.lstrip().startswith("#"):
                out.append(mask_line(line)[0])
                continue
            if len(line) - len(line.lstrip()) > block_indent:
                out.append(" " * (block_indent + 2) + "***")
                continue
            block_indent = None
        masked, block_indent = mask_line(line)
        cont = masked != line and line.rstrip().endswith("\\")
        out.append(masked)
    return "\n".join(out)


def guest_config(cfg):
    out = {k: cfg[k] for k in GUEST_FIELDS if k in cfg}
    for k, v in cfg.items():
        if DISK_KEY.match(k) and isinstance(v, str):
            parts = v.split(",")
            out[k] = ",".join([parts[0]] + [p for p in parts[1:] if p.startswith("size=")])
    return out


def classify(path):
    if any(under(path, d) for d in SKIP_DIRS) or "/.git/" in path + "/":
        return "skip"
    if any(under(path, d) for d in NAME_ONLY_DIRS):
        return "name-only"
    if path.startswith("/root/") and any(part.startswith(".") for part in path[len("/root/"):].split("/")):
        return "name-only"
    base = os.path.basename(path)
    for pattern in NAME_ONLY_GLOBS:
        if fnmatch.fnmatch(path if pattern.startswith("/") else base, pattern):
            return "name-only"
    return "read"


def has_symlink(root, path):
    cur = root
    for part in path.strip("/").split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return True
    return False


def file_entry(path, root):
    full = os.path.join(root, path.lstrip("/"))
    if has_symlink(root, path):
        return {"path": path, "size": 0, "mtime": 0, "sha256": None, "content": None,
                "masked": False, "kind": "symlink"}
    st = os.stat(full)
    entry = {"path": path, "size": st.st_size, "mtime": int(st.st_mtime),
             "sha256": None, "content": None, "masked": False, "kind": "name-only"}
    if classify(path) != "read":
        return entry
    with open(full, "rb") as f:
        raw = f.read()
    entry["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None or "\x00" in text:
        entry["kind"] = "binary"
        return entry
    entry["kind"] = "text"
    if len(raw) <= MAX_CONTENT:
        masked = mask_text(text)
        entry["content"] = masked
        entry["masked"] = masked != text
    return entry


def walk(root, start, out):
    start_len = len(out)
    base = os.path.join(root, start.lstrip("/"))
    if os.path.isfile(base):
        out.append(start)
        return
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = "/" + os.path.relpath(dirpath, root) if os.path.relpath(dirpath, root) != "." else "/"
        dirnames[:] = [d for d in dirnames if classify(os.path.join(rel_dir, d)) != "skip"]
        for name in filenames:
            rel = os.path.join(rel_dir, name)
            if os.path.isfile(os.path.join(dirpath, name)) and classify(rel) != "skip":
                out.append(rel)
            if len(out) - start_len >= WALK_LIMIT:
                return


def collect_paths(root, pve):
    out = []
    for start in WALK_DIRS + WALK_FILES:
        walk(root, start, out)
    root_dir = os.path.join(root, "root")
    if os.path.isdir(root_dir):
        walk(root, "/root", out)
    if pve:
        walk(root, "/etc/pve", out)
    return sorted(set(out))


def run(cmd, errors, text=True):
    try:
        res = subprocess.run(cmd, capture_output=True, text=text, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"{cmd[0]}: {type(exc).__name__}")
        return None
    if res.returncode != 0:
        errors.append(f"{' '.join(cmd[:3])}: exit {res.returncode}")
        return None
    return res.stdout


def run_json(cmd, errors):
    out = run(cmd, errors)
    if out is None:
        return None
    try:
        return json.loads(out)
    except ValueError:
        errors.append(f"{' '.join(cmd[:3])}: not json")
        return None


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def os_info(root):
    release = {}
    for line in (read(os.path.join(root, "etc/os-release")) or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            release[k] = v.strip('"')
    uptime = (read("/proc/uptime") or "0").split()[0]
    return {"name": release.get("PRETTY_NAME"), "kernel": os.uname().release,
            "uptime_s": int(float(uptime))}


def dmi_section(text, title):
    blocks, cur = [], None
    for line in text.splitlines():
        if line and not line.startswith("\t"):
            cur = {} if line.strip() == title else None
            if cur is not None:
                blocks.append(cur)
        elif cur is not None and ":" in line:
            k, v = line.strip().split(":", 1)
            cur[k.strip()] = v.strip()
    return blocks


def hardware(errors):
    out = {}
    dmi = run(["dmidecode", "-t", "system", "-t", "processor", "-t", "memory"], errors)
    if dmi:
        system = (dmi_section(dmi, "System Information") or [{}])[0]
        cpus = dmi_section(dmi, "Processor Information")
        dimms = [d for d in dmi_section(dmi, "Memory Device") if d.get("Size", "").split(" ")[0].isdigit()]
        out.update({"vendor": system.get("Manufacturer"), "product": system.get("Product Name"),
                    "serial": system.get("Serial Number"),
                    "cpu": cpus[0].get("Version") if cpus else None, "sockets": len(cpus),
                    "dimms": [{"size": d.get("Size"), "type": d.get("Type"), "locator": d.get("Locator")} for d in dimms]})
    out["disks"] = run_json(["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MODEL,SERIAL,MOUNTPOINT"], errors)
    out["nics"] = run_json(["ip", "-j", "addr"], errors)
    return out


def packages(errors):
    out = run(["dpkg-query", "-W", "-f", "${Package} ${Version}\n"], errors)
    return sorted(out.splitlines()) if out else []


def modified_conffiles(root):
    status = os.path.join(root, "var/lib/dpkg/status")
    changed = []
    try:
        with open(status) as f:
            text = f.read()
    except OSError:
        return changed
    for block in re.findall(r"^Conffiles:\n((?: .*\n)+)", text, re.M):
        for line in block.splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[-1] == "obsolete" or not parts[0].startswith("/etc/"):
                continue
            path, digest = parts[0], parts[1]
            if has_symlink(root, path) or classify(path) != "read":
                continue
            try:
                with open(os.path.join(root, path.lstrip("/")), "rb") as f:
                    actual = hashlib.md5(f.read()).hexdigest()
            except OSError:
                continue
            if actual != digest:
                changed.append(path)
    return sorted(set(changed))


def unowned_etc(root, errors):
    candidates = []
    walk(root, "/etc", candidates)
    owned = set()
    for i in range(0, len(candidates), 200):
        chunk = candidates[i:i + 200]
        try:
            res = subprocess.run(["dpkg", "-S"] + chunk, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"dpkg -S: {type(exc).__name__}")
            return []
        for line in res.stdout.splitlines():
            if ": " in line:
                owned.update(p.strip() for p in line.split(": ", 1)[1].split(","))
    return [p for p in candidates if p not in owned]


def pve_info(errors):
    info = {"pvs": run_json(["pvs", "--reportformat", "json"], errors),
            "lvs": run_json(["lvs", "--reportformat", "json"], errors),
            "zpool": run(["zpool", "status", "-P"], errors)}
    resources = run_json(["pvesh", "get", "/cluster/resources", "--output-format", "json"], errors) or []
    nodes = sorted(r["node"] for r in resources if r.get("type") == "node" and r.get("status") == "online")
    info["cluster_reader"] = bool(nodes) and nodes[0] == socket.gethostname()
    if info["cluster_reader"]:
        info["resources"] = resources
        guests = {}
        for r in resources:
            if r.get("type") in ("qemu", "lxc"):
                path = f"/nodes/{r['node']}/{r['type']}/{r['vmid']}/config"
                cfg = run_json(["pvesh", "get", path, "--output-format", "json"], errors)
                if cfg is not None:
                    guests[str(r["vmid"])] = guest_config(cfg)
        info["guests"] = guests
        info["storage"] = run_json(["pvesh", "get", "/storage", "--output-format", "json"], errors)
    return info


def truenas_info(errors):
    def call(method):
        return run_json(["midclt", "call", method], errors)

    datasets = call("pool.dataset.query") or []
    return {
        "pools": [{"name": p.get("name"), "status": p.get("status"),
                   "topology": [{"type": v.get("type"), "disks": len(v.get("children") or [])}
                                for v in (p.get("topology") or {}).get("data", [])]}
                  for p in call("pool.query") or []],
        "datasets": [{"name": d.get("name"), "type": d.get("type"),
                      "used": (d.get("used") or {}).get("parsed"),
                      "available": (d.get("available") or {}).get("parsed"),
                      "encrypted": d.get("encrypted")} for d in datasets],
        "nfs": [{"path": s.get("path") or s.get("paths"), "enabled": s.get("enabled")} for s in call("sharing.nfs.query") or []],
        "iscsi_extents": [{"name": e.get("name"), "disk": e.get("disk"), "path": e.get("path")} for e in call("iscsi.extent.query") or []],
        "apps": [{"name": a.get("name"), "state": a.get("state"), "version": a.get("version")} for a in call("app.query") or []],
    }


def scan():
    root = os.environ.get("RACKPEEK_SCAN_ROOT", "/")
    errors = []
    pve = os.path.isdir(os.path.join(root, "etc/pve"))
    truenas = bool(run(["sh", "-c", "command -v midclt"], []))
    paths = set(collect_paths(root, pve))
    if os.path.isfile(os.path.join(root, "var/lib/dpkg/status")):
        paths.update(modified_conffiles(root))
        paths.update(unowned_etc(root, errors))
    files = []
    for path in sorted(paths):
        try:
            files.append(file_entry(path, root))
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{path}: {type(exc).__name__}")
    return {"schema": SCHEMA, "host": socket.gethostname(), "os": os_info(root),
            "hardware": hardware(errors), "packages": packages(errors), "files": files,
            "pve": pve_info(errors) if pve else None,
            "truenas": truenas_info(errors) if truenas else None, "errors": errors}


if __name__ == "__main__":
    print(json.dumps(scan(), sort_keys=True))
