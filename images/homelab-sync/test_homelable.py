import unittest
import uuid

import build
import fixtures as fx
import homelable as hl

HOSTS = {"takaros": "root@192.168.10.10", "k3s-worker-61": "ubuntu@192.168.10.61",
         "pihole": "root@192.168.10.30", "truenas": "truenas_admin@192.168.10.200"}


def fresh_items():
    return build.build_items(fx.k8s(), fx.scans(), fx.repo(), set(), {"forgejo", "rackpeek", "loose"})


def canvas_of(body):
    return {"nodes": body["nodes"], "edges": body["edges"], "viewport": body["viewport"],
            "custom_style": body["custom_style"]}


def by_label(body):
    return {n["label"]: n for n in body["nodes"]}


class Homelable(unittest.TestCase):
    def test_node_id_stable(self):
        self.assertEqual(hl.node_id("App/Forgejo"), hl.node_id("app/forgejo"))
        uuid.UUID(hl.node_id("app/forgejo"))

    def test_ips(self):
        scans = fx.scans()
        scans["takaros"]["pve"]["resources"] += [
            {"type": "lxc", "node": "takaros", "vmid": 1077, "name": "static"},
            {"type": "qemu", "node": "takaros", "vmid": 1078, "name": "dhcp"},
            {"type": "qemu", "node": "takaros", "vmid": 1079, "name": "cloud"}]
        scans["takaros"]["pve"]["guests"].update({
            "1077": {"net0": "name=eth0,bridge=vmbr0,ip=192.168.10.77/24,gw=192.168.10.1"},
            "1078": {"net0": "virtio=AA:BB,bridge=vmbr0", "ipconfig0": "ip=dhcp"},
            "1079": {"ipconfig0": "ip=192.168.10.79/24,gw=192.168.10.1"}})
        got = hl.ips(HOSTS, scans)
        self.assertEqual(got["hw/takaros"], "192.168.10.10")
        self.assertEqual(got["vm/k3s-worker-61"], "192.168.10.61")
        self.assertEqual(got["lxc/pihole"], "192.168.10.30")
        self.assertEqual(got["vm/truenas"], "192.168.10.200")
        self.assertEqual(got["lxc/static"], "192.168.10.77")
        self.assertEqual(got["vm/cloud"], "192.168.10.79")
        self.assertNotIn("vm/dhcp", got)
        self.assertFalse(any(k.startswith("pve/") for k in got))

    def test_roundtrip(self):
        items = fresh_items()
        body = hl.payload(items, {}, {}, "d1")
        stored, raw = hl.stored_items(canvas_of(body))
        self.assertEqual(raw, [])
        want = {i["name"]: i for i in items if i["kind"] != "Service"}
        self.assertEqual({s["name"] for s in stored}, set(want))
        for s in stored:
            self.assertEqual(s["tags"], ["sync"])
            self.assertEqual(s["labels"], want[s["name"]]["labels"])

    def test_service_items_not_nodes(self):
        items = fresh_items()
        workloads = {i["name"] for i in items if i["kind"] == "Service"}
        self.assertIn("forgejo/deploy/forgejo", workloads)
        self.assertFalse(workloads & set(by_label(hl.payload(items, {}, {}, "d1"))))

    def test_guest_nested(self):
        nodes = by_label(hl.payload(fresh_items(), {}, {}, "d1"))
        self.assertEqual(nodes["vm/k3s-worker-61"]["parent_id"], hl.node_id("pve/takaros"))
        self.assertEqual(nodes["pve/takaros"]["type"], "proxmox")
        self.assertIs(nodes["pve/takaros"]["container_mode"], True)
        self.assertIsNone(nodes["app/forgejo"]["parent_id"])

    def test_ping_check(self):
        nodes = by_label(hl.payload(fresh_items(), {}, {"hw/takaros": "192.168.10.10"}, "d1"))
        self.assertEqual((nodes["hw/takaros"]["ip"], nodes["hw/takaros"]["check_method"],
                          nodes["hw/takaros"]["check_target"]), ("192.168.10.10", "ping", "192.168.10.10"))
        self.assertIsNone(nodes["pve/takaros"]["ip"])
        self.assertIsNone(nodes["pve/takaros"]["check_method"])

    def test_hand_made_kept(self):
        canvas = canvas_of(hl.payload(fresh_items(), {}, {}, "d1"))
        mine = {"id": "hand-1", "label": "my-switch", "type": "switch", "properties": [], "pos_x": 5, "pos_y": 6,
                "device_id": "dev-9", "notes": "mine"}
        edge = {"id": "e1", "source": "hand-1", "target": hl.node_id("hw/takaros"), "type": "ethernet"}
        canvas["nodes"].append(mine)
        canvas["edges"] = [edge]
        canvas["viewport"] = {"x": 1, "y": 2, "zoom": 0.5}
        canvas["custom_style"] = {"bg": "dark"}
        body = hl.payload(fresh_items(), canvas, {}, "d1")
        self.assertIn(mine, body["nodes"])
        self.assertEqual(body["edges"], [edge])
        self.assertEqual(body["viewport"], {"x": 1, "y": 2, "zoom": 0.5})
        self.assertEqual(body["custom_style"], {"bg": "dark"})

    def test_geometry_kept(self):
        canvas = canvas_of(hl.payload(fresh_items(), {}, {}, "d1"))
        n = next(x for x in canvas["nodes"] if x["label"] == "app/forgejo")
        n.update(pos_x=4321, pos_y=99, width=300, height=90, custom_colors={"bg": "#123"}, custom_icon="git",
                 device_id="dev-1")
        items = fresh_items()
        items.append(build.item("System", "app/brand-new", "k8s", ["k3s/epaflix"]))
        nodes = by_label(hl.payload(items, canvas, {}, "d1"))
        kept = nodes["app/forgejo"]
        self.assertEqual((kept["pos_x"], kept["pos_y"], kept["width"], kept["height"], kept["custom_colors"],
                          kept["custom_icon"], kept["device_id"]),
                         (4321, 99, 300, 90, {"bg": "#123"}, "git", "dev-1"))
        positions = [(x["pos_x"], x["pos_y"]) for x in nodes.values() if x["parent_id"] is None]
        self.assertEqual(len(positions), len(set(positions)))

    def test_workload_docs(self):
        docs = hl.workload_docs(fresh_items())
        self.assertIn("app/forgejo", docs)
        self.assertIn("## forgejo/deploy/forgejo", docs["app/forgejo"])
        self.assertIn("## truenas/app/silo", docs["vm/truenas"])
        self.assertFalse(any("t3code-work" in b for b in docs.values()))
        self.assertEqual(docs, hl.workload_docs(list(reversed(fresh_items()))))

    def test_seed_notes(self):
        resources = fx.stored() + [
            {"kind": "Service", "name": "forgejo/deploy/forgejo", "tags": ["sync"], "labels": {},
             "runsOn": ["app/forgejo"], "notes": "rootless image"},
            {"kind": "Service", "name": "old/deploy/x", "tags": ["sync"], "labels": {},
             "runsOn": ["app/old-thing"], "notes": "only one"}]
        seeded = {r["name"]: r for r in hl.seed_items(resources)}
        self.assertEqual(seeded["app/forgejo"]["notes"], "why\n\n### forgejo/deploy/forgejo\nrootless image")
        self.assertEqual(seeded["app/old-thing"]["notes"], "### old/deploy/x\nonly one")
        self.assertNotIn("forgejo/deploy/forgejo", seeded)


if __name__ == "__main__":
    unittest.main()
