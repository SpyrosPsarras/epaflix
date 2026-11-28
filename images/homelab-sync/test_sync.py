import json
import os
import tempfile
import unittest

import fixtures as fx
import homelable as hl
import sync

DESIGN_ID = "d-index"


def stored_canvas():
    body = hl.payload(hl.seed_items(fx.stored()), {}, {}, DESIGN_ID)
    return {"nodes": body["nodes"], "edges": body["edges"], "viewport": {}, "custom_style": None}


class FakeIO:
    def __init__(self, down_hosts=(), hl_down=False, push_down=False, leaks=(), git_sync_down=False,
                 save_down=False, design=True, rpk_down=False, k8s_down=False, docs=None):
        self.down_hosts = set(down_hosts)
        self.hl_down = hl_down
        self.push_down = push_down
        self.leaks = list(leaks)
        self.git_sync_down = git_sync_down
        self.save_down = save_down
        self.rpk_down = rpk_down
        self.k8s_down = k8s_down
        self.design = design
        self.docs = dict(docs or {})
        self.saves = []
        self.created_designs = []
        self.created_docs = []
        self.updated_docs = []
        self.git_syncs = 0
        self.canvas_reads = 0
        self.indexed = ["forgejo", "rackpeek", "loose"]
        self.pushed = []
        self.log = []

    def designs(self):
        if self.hl_down:
            raise OSError("connection refused")
        return [{"id": "other", "name": "My map"}] + ([{"id": DESIGN_ID, "name": hl.DESIGN}] if self.design else [])

    def create_design(self, name):
        self.created_designs.append(name)
        return DESIGN_ID

    def canvas(self, design_id):
        self.canvas_reads += 1
        canvas = stored_canvas()
        if self.canvas_reads > 1:
            next(n for n in canvas["nodes"] if n["label"] == "app/forgejo")["notes"] = "edited during run"
        return canvas

    def save(self, body):
        if self.save_down:
            raise OSError("save failed")
        self.saves.append(body)

    def documents(self):
        return [{"id": doc_id, "node_id": nid, "title": title} for nid, (doc_id, _, title) in self.docs.items()]

    def document(self, doc_id):
        return next({"id": d, "body": b, "title": t} for d, b, t in self.docs.values() if d == doc_id)

    def create_document(self, body):
        self.created_docs.append(body)

    def update_document(self, doc_id, body):
        self.updated_docs.append((doc_id, body))

    def git_sync(self):
        if self.git_sync_down:
            raise RuntimeError("502")
        self.git_syncs += 1

    def rpk_get(self):
        if self.rpk_down:
            raise OSError("rackpeek down")
        return {"version": 5, "resources": fx.stored() + [
            {"kind": "Service", "name": "forgejo/deploy/forgejo", "tags": ["sync"], "labels": {},
             "runsOn": ["app/forgejo"], "notes": "rootless"}]}

    def k8s(self):
        if self.k8s_down:
            raise OSError("api down")
        return fx.k8s()

    def clone_repo(self, dest):
        src = fx.repo()
        os.rmdir(dest)
        os.rename(src, dest)

    def scan(self, alias, target):
        if alias in self.down_hosts:
            raise TimeoutError("timed out")
        return fx.scans()[alias]

    def gitleaks(self, tree):
        return self.leaks

    def push_live(self, tree, dirs, namespaces):
        if self.push_down:
            raise OSError("forgejo down")
        self.dirs = sorted(dirs)
        self.namespaces = namespaces
        self.pushed.append(sorted(os.path.relpath(os.path.join(d, f), tree)
                                  for d, _, fs in os.walk(tree) for f in fs))


HOSTS = {"takaros": "root@192.168.10.10", "truenas": "truenas_admin@192.168.10.200"}


def run(io, seed=False):
    return sync.run(io, HOSTS, fx.TODAY, io.log.append, seed)


def nodes(io):
    return {n["label"]: n for n in io.saves[0]["nodes"]}


class Sync(unittest.TestCase):
    def test_happy_path_body(self):
        io = FakeIO()
        self.assertEqual(run(io), 0)
        self.assertEqual(len(io.saves), 1)
        self.assertEqual(io.saves[0]["design_id"], DESIGN_ID)
        self.assertIn({"key": "managed-by", "value": "homelab-sync", "icon": None, "visible": False},
                      nodes(io)["hw/takaros"]["properties"])
        self.assertEqual(nodes(io)["hw/takaros"]["check_target"], "192.168.10.10")
        self.assertEqual(io.git_syncs, 1)
        self.assertEqual(io.created_designs, [])
        self.assertTrue(any("takaros/etc/hosts" == p for p in io.pushed[0]))
        self.assertTrue(any(p == "k8s/forgejo/configmaps/forgejo-cfg.yaml" for p in io.pushed[0]))

    def test_gone_and_hand_made(self):
        io = FakeIO()
        run(io)
        got = nodes(io)
        gone = {p["key"]: p["value"] for p in got["app/old-thing"]["properties"]}
        self.assertEqual(gone["gone-since"], fx.TODAY)
        self.assertIn("my-switch-notes", got)
        self.assertNotIn("managed-by", {p["key"] for p in got["my-switch-notes"]["properties"]})

    def test_fresh_read_before_save(self):
        io = FakeIO()
        run(io)
        self.assertEqual(nodes(io)["app/forgejo"]["notes"], "edited during run")

    def test_homelable_down_saves_nothing(self):
        io = FakeIO(hl_down=True)
        self.assertEqual(run(io), 1)
        self.assertEqual(io.saves, [])
        self.assertEqual(io.created_docs + io.updated_docs, [])
        self.assertEqual(io.pushed, [])

    def test_seed_from_rackpeek(self):
        io = FakeIO(design=False)
        self.assertEqual(run(io, seed=True), 0)
        self.assertEqual(io.created_designs, [hl.DESIGN])
        self.assertEqual(nodes(io)["app/forgejo"]["notes"], "why\n\n### forgejo/deploy/forgejo\nrootless")
        self.assertIn("VM/Truenas", nodes(io))

    def test_seed_rackpeek_down(self):
        io = FakeIO(design=False, rpk_down=True)
        self.assertEqual(run(io, seed=True), 1)
        self.assertEqual(io.created_designs, [])
        self.assertEqual(io.saves, [])

    def test_no_seed_without_rpk_env(self):
        io = FakeIO(design=False)
        self.assertEqual(run(io), 0)
        self.assertEqual(io.created_designs, [hl.DESIGN])
        self.assertIsNone(nodes(io)["app/forgejo"]["notes"])

    def test_workload_docs_created(self):
        io = FakeIO()
        run(io)
        doc = next(d for d in io.created_docs if d["node_id"] == nodes(io)["app/forgejo"]["id"])
        self.assertEqual(doc["kind"], "node")
        self.assertIn("## forgejo/deploy/forgejo", doc["body"])

    def test_doc_unchanged_not_written(self):
        first = FakeIO()
        run(first)
        docs = {d["node_id"]: (f"doc-{i}", d["body"], d["title"]) for i, d in enumerate(first.created_docs)}
        io = FakeIO(docs=docs)
        run(io)
        self.assertEqual(io.created_docs, [])
        self.assertEqual(io.updated_docs, [])

    def test_doc_changed_updated(self):
        first = FakeIO()
        run(first)
        nid = nodes(first)["app/forgejo"]["id"]
        io = FakeIO(docs={nid: ("doc-1", "old", "app/forgejo workloads")})
        run(io)
        self.assertEqual(io.updated_docs[0][0], "doc-1")
        self.assertEqual(io.updated_docs[0][1]["revision_reason"], "import")

    def test_hand_written_page_not_overwritten(self):
        first = FakeIO()
        run(first)
        nid = nodes(first)["app/forgejo"]["id"]
        io = FakeIO(docs={nid: ("doc-1", "my runbook", "Forgejo runbook")})
        self.assertEqual(run(io), 1)
        self.assertEqual(io.updated_docs, [])
        self.assertFalse(any(d["node_id"] == nid for d in io.created_docs))

    def test_page_renamed_after_list_not_written(self):
        first = FakeIO()
        run(first)
        nid = nodes(first)["app/forgejo"]["id"]
        io = FakeIO(docs={nid: ("doc-1", "old", "app/forgejo workloads")})
        io.document = lambda doc_id: {"id": doc_id, "body": "old", "title": "My runbook"}
        self.assertEqual(run(io), 1)
        self.assertEqual(io.updated_docs, [])

    def test_truenas_page_kept_when_truenas_scan_fails(self):
        first = FakeIO(design=False)
        run(first)
        saved = first.saves[0]
        nid = nodes(first)["vm/truenas"]["id"]
        io = FakeIO(down_hosts={"truenas"}, docs={nid: ("doc-1", "## truenas/app/silo\n", "vm/truenas workloads")})
        io.canvas = lambda design_id: json.loads(json.dumps(
            {"nodes": saved["nodes"], "edges": [], "viewport": {}, "custom_style": None}))
        self.assertEqual(run(io), 1)
        self.assertIn("vm/truenas", nodes(io))
        self.assertEqual(io.updated_docs, [])

    def test_renamed_sync_node_still_synced(self):
        first = FakeIO()
        run(first)
        saved = json.loads(json.dumps(first.saves[0]))
        node = next(n for n in saved["nodes"] if n["label"] == "app/forgejo")
        node["label"] = "Forgejo (git)"
        next(p for p in node["properties"] if p["key"] == "health")["value"] = "Stale"
        io = FakeIO()
        io.canvas = lambda design_id: json.loads(json.dumps(
            {"nodes": saved["nodes"], "edges": [], "viewport": {}, "custom_style": None}))
        self.assertEqual(run(io), 0)
        got = next(n for n in io.saves[0]["nodes"] if n["id"] == hl.node_id("app/forgejo"))
        self.assertEqual({p["key"]: p["value"] for p in got["properties"]}["health"], "Healthy")
        self.assertNotIn("changed_facts", got)

    def test_unindexed_namespace_page_kept(self):
        first = FakeIO(design=False)
        run(first)
        saved = first.saves[0]
        nid = nodes(first)["app/loose"]["id"]
        io = FakeIO(docs={nid: ("doc-1", "## loose/deploy/stray\n", "app/loose workloads")})
        io.canvas = lambda design_id: json.loads(json.dumps(
            {"nodes": saved["nodes"], "edges": [], "viewport": {}, "custom_style": None}))
        io.indexed = ["forgejo", "rackpeek"]
        io.k8s = lambda: dict(fx.k8s(), deployments=[d for d in fx.k8s()["deployments"] if d["metadata"]["name"] != "stray"])
        run(io)
        self.assertEqual(io.updated_docs, [])

    def test_incomplete_canvas_saves_nothing(self):
        io = FakeIO()
        io.canvas = lambda design_id: {}
        self.assertEqual(run(io), 1)
        self.assertEqual(io.saves, [])

    def test_incomplete_second_read_saves_nothing(self):
        io = FakeIO()
        orig = io.canvas
        io.canvas = lambda design_id: orig(design_id) if io.canvas_reads == 0 else {"nodes": []}
        self.assertEqual(run(io), 1)
        self.assertEqual(io.saves, [])

    def test_seed_again_when_design_empty(self):
        io = FakeIO()
        io.canvas = lambda design_id: {"nodes": [], "edges": [], "viewport": {}, "custom_style": None}
        self.assertEqual(run(io, seed=True), 0)
        self.assertEqual(io.created_designs, [])
        self.assertEqual(nodes(io)["app/forgejo"]["notes"], "why\n\n### forgejo/deploy/forgejo\nrootless")

    def test_failed_host_keeps_guest_in_box(self):
        first = FakeIO()
        run(first)
        saved = first.saves[0]
        io = FakeIO(down_hosts={"takaros"})
        io.canvas = lambda design_id: json.loads(json.dumps(
            {"nodes": saved["nodes"], "edges": [], "viewport": {}, "custom_style": None}))
        self.assertEqual(run(io), 1)
        got = nodes(io)
        before = {n["label"]: n for n in saved["nodes"]}
        self.assertEqual(got["lxc/pihole"]["parent_id"], before["pve/takaros"]["id"])
        for name in ("lxc/pihole", "pve/takaros", "hw/takaros"):
            self.assertEqual({k: v for k, v in got[name].items() if k != "properties"},
                             {k: v for k, v in before[name].items() if k != "properties"})

    def test_k8s_down_keeps_docs(self):
        io = FakeIO(k8s_down=True)
        self.assertEqual(run(io), 1)
        self.assertFalse(any(d["title"].startswith("app/") for d in io.created_docs))
        self.assertEqual(io.updated_docs, [])
        self.assertIn("app/forgejo", nodes(io))
        self.assertNotIn("gone-since", {p["key"] for p in nodes(io)["app/forgejo"]["properties"]})

    def test_git_sync_failure_exits_1(self):
        io = FakeIO(git_sync_down=True)
        self.assertEqual(run(io), 1)
        self.assertEqual(len(io.saves), 1)

    def test_save_failure_no_docs(self):
        io = FakeIO(save_down=True)
        self.assertEqual(run(io), 1)
        self.assertEqual(io.created_docs, [])
        self.assertEqual(io.git_syncs, 0)

    def test_one_host_down(self):
        io = FakeIO(down_hosts={"truenas"})
        self.assertEqual(run(io), 1)
        self.assertIn("hw/takaros", nodes(io))
        self.assertFalse(any(d["title"].startswith("vm/truenas") for d in io.created_docs))
        self.assertTrue(any("truenas" in line and "TimeoutError" in line for line in io.log))
        self.assertNotIn("truenas", io.dirs)
        self.assertIn("takaros", io.dirs)
        self.assertIn("k8s/forgejo", io.dirs)
        self.assertNotIn("k8s/t3code-work", io.dirs)
        self.assertIn("loose", io.namespaces)

    def test_final_payload_scrubbed(self):
        io = FakeIO()
        orig = io.canvas

        def canvas(design_id):
            c = orig(design_id)
            n = next(x for x in c["nodes"] if x["label"] == "app/forgejo")
            n["properties"].append({"key": "uses", "value": "x.t3code-work.svc", "icon": None, "visible": False})
            return c

        io.canvas = canvas
        run(io)
        self.assertNotIn("t3code-work", json.dumps(io.saves[0]))
        self.assertFalse(any("t3code-work" in d["body"] for d in io.created_docs))

    def test_forgejo_down_still_saves(self):
        io = FakeIO(push_down=True)
        self.assertEqual(run(io), 1)
        self.assertEqual(len(io.saves), 1)

    def test_gitleaks_drops_file(self):
        io = FakeIO(leaks=["takaros/root/fan.sh"])
        run(io)
        self.assertNotIn("takaros/root/fan.sh", io.pushed[0])
        self.assertTrue(any("takaros/root/fan.sh" in line for line in io.log))

    def test_configmap_masked(self):
        tree = tempfile.mkdtemp()
        sync.write_tree(tree, {}, {"configmaps": [{"metadata": {"name": "c", "namespace": "n"},
                                                     "data": {"x": "password=hunter2", "DB_PASSWORD": "bare1",
                                                              "url": "http://a.t3code-work.svc"}}]})
        with open(os.path.join(tree, "k8s/n/configmaps/c.yaml")) as f:
            text = f.read()
        for bad in ("hunter2", "bare1", "t3code-work"):
            self.assertNotIn(bad, text)


class Main(unittest.TestCase):
    def test_missing_env_exits_1(self):
        for k in ("HL_SERVICE_KEY", "HOSTS"):
            os.environ.pop(k, None)
        self.assertEqual(sync.main(), 1)


if __name__ == "__main__":
    unittest.main()
