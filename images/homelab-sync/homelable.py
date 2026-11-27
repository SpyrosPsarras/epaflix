import re
import uuid

import build

DESIGN = "Homelab index"
NS = uuid.UUID("5d0f3c3e-6a51-4f0b-9a57-3c2b9e1f7a10")
MARK = ("managed-by", "homelab-sync")
SYNC_KEYS = frozenset({MARK[0], "tags", "source", "gone-since", "indexed", "auth", "configmaps", "cpu", "disk-chain",
                       "disks", "files", "git", "health", "images", "ingress", "kernel", "kind", "live-config", "memory",
                       "model", "namespace", "nodes", "os", "packages", "path", "scan-errors", "secrets", "serial",
                       "state", "sync", "updates", "uses", "version", "vmid"})
TYPES = {"hw": "server", "pve": "proxmox", "vm": "vm", "lxc": "lxc"}
ROWS = ("hw", "pve", "app", "k3s")
OWNED = ("label", "type", "ip", "check_method", "check_target", "properties", "notes",
         "parent_id", "container_mode", "cpu_count", "ram_gb")
STEP_X, ROW_Y, GUEST_X, GUEST_Y, GUEST_COLS = 260, 700, 220, 120, 3
BOX_W = GUEST_COLS * GUEST_X + 40
IP = re.compile(r"(?:^|,)ip=(\d{1,3}(?:\.\d{1,3}){3})(?:/\d+)?(?:,|$)")


def node_id(name):
    return str(uuid.uuid5(NS, name.lower()))


def node_type(name):
    return TYPES.get(name.split("/", 1)[0].lower(), "generic")


def ips(hosts, scans):
    out = {}
    for alias, target in hosts.items():
        addr = target.rsplit("@", 1)[-1]
        if (scans.get(alias) or {}).get("pve"):
            out[f"hw/{alias}"] = addr
    by_name = {}
    for scan in scans.values():
        pve = (scan or {}).get("pve") or {}
        for r in pve.get("resources", []):
            if r.get("type") not in ("qemu", "lxc"):
                continue
            prefix = "vm" if r["type"] == "qemu" else "lxc"
            short = r["name"].split(".")[0]
            cfg = (pve.get("guests") or {}).get(str(r["vmid"])) or {}
            by_name[short] = f"{prefix}/{short}"
            for key in ("net0", "ipconfig0"):
                m = IP.search(str(cfg.get(key) or ""))
                if m:
                    out[f"{prefix}/{short}"] = m.group(1)
                    break
    for alias, target in hosts.items():
        if alias in by_name:
            out[by_name[alias]] = target.rsplit("@", 1)[-1]
    return out


def _props(it):
    props = [{"key": MARK[0], "value": MARK[1]}] if "sync" in (it.get("tags") or []) else []
    other = sorted(t for t in it.get("tags") or [] if t != "sync")
    if other:
        props.append({"key": "tags", "value": ", ".join(other)})
    props += [{"key": k, "value": v} for k, v in sorted((it.get("labels") or {}).items())]
    return [dict(p, icon=None, visible=False) for p in props]


def _is_sync(node):
    return node.get("id") == node_id(node.get("label") or "") and any(
        (p.get("key"), p.get("value")) == MARK for p in node.get("properties") or [])


def check_canvas(canvas):
    if not (isinstance(canvas, dict) and isinstance(canvas.get("nodes"), list) and isinstance(canvas.get("edges"), list)
            and isinstance(canvas.get("viewport"), dict) and "custom_style" in canvas):
        raise ValueError("incomplete canvas response")
    return canvas


def _merge_props(new, old):
    old_by = {p.get("key"): p for p in old or []}
    out = [dict(p, icon=(old_by.get(p["key"]) or {}).get("icon"), visible=(old_by.get(p["key"]) or {}).get("visible", False))
           for p in new]
    keys = {p["key"] for p in new}
    return out + [p for p in old or [] if p.get("key") not in keys and p.get("key") not in SYNC_KEYS]


def stored_items(canvas):
    items, raw = [], []
    for n in canvas.get("nodes") or []:
        if not _is_sync(n):
            raw.append(n)
            continue
        props = {p.get("key"): p.get("value") for p in n.get("properties") or []}
        tags = ["sync"] + [t.strip() for t in (props.pop("tags", "") or "").split(",") if t.strip()]
        props.pop(MARK[0], None)
        items.append({"name": n["label"], "tags": tags, "labels": props, "notes": n.get("notes"), "runsOn": [], "node": n})
    return items, raw


def seed_items(resources):
    resources = [dict(r, notes=build.scrub(r["notes"]) if r.get("notes") else r.get("notes")) for r in resources
                 if not build.has_excluded(r.get("name") or "")
                 and not any(build.has_excluded(x) for x in r.get("runsOn") or [])]
    by_name = {r["name"].lower(): dict(r) for r in resources if r.get("kind") != "Service"}
    for r in sorted((r for r in resources if r.get("kind") == "Service" and r.get("notes")), key=lambda r: r["name"]):
        parent = by_name.get(((r.get("runsOn") or [""])[0]).lower())
        if parent is None:
            continue
        section = f"### {r['name']}\n{r['notes']}"
        parent["notes"] = f"{parent['notes']}\n\n{section}" if parent.get("notes") else section
    return list(by_name.values())


def _row(name):
    prefix = name.split("/", 1)[0].lower()
    return ROWS.index(prefix) if prefix in ROWS else len(ROWS)


def _step(row):
    return BOX_W + 40 if ROWS[row:row + 1] == ("pve",) else STEP_X


def _place(name, parent, taken, children):
    if parent:
        i = children.get(parent, 0)
        children[parent] = i + 1
        return 20 + (i % GUEST_COLS) * GUEST_X, 60 + (i // GUEST_COLS) * GUEST_Y
    row, col = _row(name), 0
    while (row, col) in taken:
        col += 1
    taken.add((row, col))
    return col * _step(row), row * ROW_Y


def payload(items, canvas, ip_map, design_id):
    stored = {n["id"]: n for n in canvas.get("nodes") or [] if _is_sync(n)}
    raw = [n for n in canvas.get("nodes") or [] if not _is_sync(n)]
    raw_ids = {n["id"] for n in raw}
    nodes_items = [it for it in items if it.get("kind") != "Service"]
    names = {it["name"].lower() for it in nodes_items}
    guests = {}
    for it in nodes_items:
        host = ((it.get("runsOn") or [""])[0] or "").lower()
        if node_type(it["name"]) in ("vm", "lxc") and host.startswith("pve/"):
            guests[host] = guests.get(host, 0) + 1
    taken, children, nodes = set(), {}, []
    for n in stored.values():
        if n.get("parent_id"):
            children[n["parent_id"]] = children.get(n["parent_id"], 0) + 1
        else:
            row = _row(n.get("label") or "")
            taken.add((row, round((n.get("pos_x") or 0) / _step(row))))
    for it in nodes_items:
        name = it["name"]
        nid = node_id(name)
        if nid in raw_ids:
            continue
        old = stored.get(nid)
        if old and "node" in it:
            node = dict(old)
            node["properties"] = _merge_props(_props(it), old.get("properties"))
            node["notes"] = it.get("notes")
            nodes.append(node)
            continue
        base = {k: v for k, v in (old or {}).items() if k not in OWNED}
        base["id"] = nid
        parent = None
        host = (it.get("runsOn") or [None])[0] or ""
        if node_type(name) in ("vm", "lxc") and host.lower().startswith("pve/") and host.lower() in names:
            parent = node_id(host)
        if not old:
            base["pos_x"], base["pos_y"] = _place(name, parent, taken, children)
            if node_type(name) == "proxmox":
                base["width"] = BOX_W
                base["height"] = 80 + -(-guests.get(name.lower(), 0) // GUEST_COLS) * GUEST_Y
        ip = ip_map.get(name)
        base.update({
            "label": name,
            "type": node_type(name),
            "ip": ip,
            "check_method": "ping" if ip else None,
            "check_target": ip,
            "properties": _merge_props(_props(it), (old or {}).get("properties")),
            "notes": it.get("notes"),
            "parent_id": parent,
            "container_mode": node_type(name) == "proxmox",
            "cpu_count": it.get("cores"),
            "ram_gb": it.get("ram"),
        })
        nodes.append(base)
    ids = {n["id"] for n in nodes} | raw_ids
    for n in nodes:
        if n["parent_id"] not in ids:
            n["parent_id"] = None
    missing = set(stored) - ids
    if missing:
        raise ValueError(f"{len(missing)} stored nodes would be deleted")
    return {"design_id": design_id, "nodes": nodes + raw, "edges": canvas.get("edges") or [],
            "viewport": canvas.get("viewport") or {}, "custom_style": canvas.get("custom_style")}


def workload_docs(items):
    by_parent = {}
    for it in sorted((i for i in items if i.get("kind") == "Service"), key=lambda i: i["name"].lower()):
        parent = (it.get("runsOn") or [None])[0]
        if not parent:
            continue
        lines = [f"## {it['name']}", ""]
        lines += [f"- **{k}**: {v}" for k, v in sorted((it.get("labels") or {}).items())]
        by_parent.setdefault(parent, []).append("\n".join(lines))
    return {p: "\n\n".join(parts) + "\n" for p, parts in by_parent.items()}
