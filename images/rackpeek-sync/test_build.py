import json
import unittest

import build
import fixtures as fx


def by_name(items):
    return {i["name"]: i for i in items}


class Build(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = fx.repo()
        cls.items = build.build_items(fx.k8s(), fx.scans(), cls.repo, set(),
                                      indexed={"forgejo", "rackpeek"})
        cls.byname = by_name(cls.items)

    def test_names_and_runs_on(self):
        b = self.byname
        self.assertEqual(b["hw/takaros"]["kind"], "Server")
        self.assertEqual(b["pve/takaros"]["type"], "hypervisor")
        self.assertEqual(b["pve/takaros"]["runsOn"], ["hw/takaros"])
        self.assertEqual(b["vm/truenas"]["type"], "vm")
        self.assertEqual(b["vm/truenas"]["runsOn"], ["pve/takaros"])
        self.assertEqual(b["lxc/pihole"]["type"], "container")
        self.assertEqual(b["lxc/pihole"]["labels"]["os"], "Debian")
        self.assertEqual(b["k3s/epaflix"]["type"], "cluster")
        self.assertIn("vm/k3s-worker-61", b["k3s/epaflix"]["runsOn"])
        self.assertEqual(b["app/forgejo"]["labels"]["kind"], "argo-app")
        self.assertEqual(b["forgejo/deploy/forgejo"]["kind"], "Service")
        self.assertEqual(b["forgejo/deploy/forgejo"]["runsOn"], ["app/forgejo"])
        self.assertEqual(b["loose/deploy/stray"]["runsOn"], ["app/loose"])
        self.assertEqual(b["app/loose"]["labels"]["kind"], "namespace")
        self.assertEqual(b["truenas/app/silo"]["runsOn"], ["vm/truenas"])

    def test_every_item_tagged_and_sourced(self):
        for item in self.items:
            self.assertIn("sync", item["tags"], item["name"])
            self.assertIn("source", item["labels"], item["name"])
            for v in item["labels"].values():
                self.assertIsInstance(v, str)

    def test_workload_labels(self):
        l = self.byname["forgejo/deploy/forgejo"]["labels"]
        self.assertIn("codeberg.org/forgejo/forgejo", l["images"])
        self.assertEqual(l["nodes"], "k3s-worker-61")
        self.assertEqual(l["updates"], "renovate")
        self.assertIn("k8s/forgejo/configmaps/forgejo-cfg.yaml", l["configmaps"])
        self.assertEqual(l["secrets"], "forgejo-secrets: DB_PASSWD, SECRET_KEY")
        self.assertEqual(l["ingress"], "git.epaflix.com (internal)")
        self.assertEqual(l["auth"], "authentik-forwardauth")
        self.assertIn("postgres-cluster-rw.postgres-system.svc", l["uses"])
        self.assertIn("rackpeek.rackpeek.svc", l["uses"])
        self.assertIn("ns/postgres-system", l["uses"])
        self.assertNotIn("ns/kube-system", l["uses"])
        self.assertEqual(l["git"], "managed by app/forgejo (2-k3s/38.forgejo)")
        self.assertNotIn("ENC[", json.dumps(l))

    def test_nodes_follow_owners(self):
        self.assertEqual(self.byname["rackpeek/deploy/rackpeek"]["labels"]["nodes"], "k3s-worker-63")

    def test_not_indexed(self):
        self.assertEqual(self.byname["app/loose"]["labels"]["indexed"], "no")
        self.assertNotIn("indexed", self.byname["app/forgejo"]["labels"])

    def test_disk_chain_extended(self):
        self.assertEqual(self.byname["forgejo/deploy/forgejo"]["labels"]["disk-chain"],
                         "forgejo-data -> pv/pvc-111 -> truenas:apps/k8s/iscsi/vols/pvc-111 -> pool apps")
        self.assertEqual(self.byname["loose/deploy/stray"]["labels"]["disk-chain"],
                         "stray-data -> pv/pvc-222 -> local:/var/lib/rancher/k3s/storage/pvc-222 -> vm/k3s-worker-61")

    def test_live_only(self):
        self.assertEqual(self.byname["loose/deploy/stray"]["labels"]["git"], "live only")

    def test_no_duplicate_names(self):
        names = [i["name"].lower() for i in self.items]
        self.assertEqual(len(names), len(set(names)))

    def test_excluded_namespaces(self):
        blob = json.dumps(self.items)
        self.assertNotIn("t3code-work", blob)
        self.assertNotIn("mcp-hub-work", blob)
        self.assertNotIn("pvc-999", blob)
        self.assertNotIn("hub.", self.byname["forgejo/deploy/forgejo"]["labels"]["uses"])
        self.assertNotIn("secret-db", self.byname["forgejo/deploy/forgejo"]["labels"]["uses"])
        self.assertNotIn("evil", blob)

    def test_scrub_inside_tokens(self):
        self.assertEqual(build.scrub("a FOO_t3code-work_BAR b"), "a *** b")
        self.assertEqual(build.scrub("x.mcp-hub-work.svc"), "***")

    def test_no_namespace_readable(self):
        items = by_name(build.build_items(fx.k8s(), {}, fx.repo(), set(), indexed=set()))
        self.assertEqual(items["app/forgejo"]["labels"]["indexed"], "no")

    def test_host_labels(self):
        self.assertEqual(self.byname["hw/takaros"]["labels"]["model"], "HP ProLiant DL380 Gen9")
        l = self.byname["pve/takaros"]["labels"]
        self.assertEqual(l["files"], "2 (1 in Git, 1 masked)")
        self.assertEqual(l["live-config"], "homelab-live/takaros/")


class GitStatus(unittest.TestCase):
    def test_file(self):
        idx = {"aa": "1-proxmox/takaros/hosts"}
        self.assertEqual(build.git_status("aa", idx), "in Git: 1-proxmox/takaros/hosts")
        self.assertEqual(build.git_status("zz", idx), "not in Git")
        self.assertEqual(build.git_status(None, idx), "not in Git")

    def test_repo_index(self):
        idx = build.repo_index(fx.repo())
        self.assertIn("1-proxmox/takaros/hosts", idx.values())


class CarryOver(unittest.TestCase):
    def setUp(self):
        self.new = [{"kind": "System", "name": "App/Forgejo", "type": "other", "tags": ["sync"],
                     "labels": {"source": "k8s", "kind": "argo-app"}}]

    def run_carry(self, ok):
        return by_name(build.carry_over(self.new, fx.stored(), ok, fx.TODAY))

    def test_carry_notes_and_tags(self):
        out = self.run_carry({"k8s", "nick"})
        self.assertEqual(out["App/Forgejo"]["notes"], "why")
        self.assertEqual(sorted(out["App/Forgejo"]["tags"]), ["critical", "sync"])

    def test_non_sync_untouched(self):
        self.assertNotIn("my-switch-notes", self.run_carry({"k8s"}))

    def test_gone_marked(self):
        out = self.run_carry({"k8s"})
        self.assertEqual(out["app/old-thing"]["labels"]["gone-since"], fx.TODAY)
        self.assertEqual(out["app/older"]["labels"]["gone-since"], "2026-11-01")

    def test_back_clears_gone(self):
        stored = fx.stored()
        stored[0]["labels"]["gone-since"] = "2026-11-01"
        out = by_name(build.carry_over(self.new, stored, {"k8s"}, fx.TODAY))
        self.assertNotIn("gone-since", out["App/Forgejo"]["labels"])

    def test_manual_name_collision_skipped(self):
        new = [{"kind": "System", "name": "vm/truenas", "type": "vm", "tags": ["sync"], "labels": {"source": "takaros"}}]
        out = build.carry_over(new, fx.stored(), {"takaros"}, fx.TODAY)
        self.assertNotIn("vm/truenas", {i["name"].lower(): i for i in out} and [i["name"] for i in out])

    def test_failed_guest_scan_keeps_facts(self):
        new = [{"kind": "System", "name": "lxc/pihole", "type": "container", "tags": ["sync"],
                "labels": {"source": "takaros", "vmid": "1030"}}]
        out = by_name(build.carry_over(new, fx.stored(), {"takaros"}, fx.TODAY))
        self.assertEqual(out["lxc/pihole"]["labels"]["os"], "OldOS")
        self.assertEqual(out["lxc/pihole"]["labels"]["vmid"], "1030")

    def test_fresh_scan_drops_old_errors(self):
        new = [{"kind": "System", "name": "lxc/pihole", "type": "container", "tags": ["sync"],
                "labels": {"source": "takaros", "os": "Debian", "live-config": "homelab-live/pihole/"}}]
        stored = fx.stored()
        stored[-1]["labels"]["scan-errors"] = "3"
        out = by_name(build.carry_over(new, stored, {"takaros"}, fx.TODAY))
        self.assertNotIn("scan-errors", out["lxc/pihole"]["labels"])
        self.assertEqual(out["lxc/pihole"]["labels"]["os"], "Debian")

    def test_fresh_scan_keeps_notes_and_tags(self):
        new = [{"kind": "System", "name": "app/forgejo", "type": "other", "tags": ["sync"],
                "labels": {"source": "k8s", "live-config": "x"}}]
        out = by_name(build.carry_over(new, fx.stored(), {"k8s"}, fx.TODAY))
        self.assertEqual(out["app/forgejo"]["notes"], "why")
        self.assertIn("critical", out["app/forgejo"]["tags"])

    def test_scrub_long_token_fast(self):
        import time
        start = time.time()
        build.scrub("a" * 65536)
        self.assertLess(time.time() - start, 0.5)

    def test_nested_secret_not_in_uses(self):
        k8s = fx.k8s()
        k8s["configmaps"][0]["data"]["app.yaml"] = "db:\n  password: hidden-db.postgres-system.svc\n"
        items = by_name(build.build_items(k8s, {}, fx.repo(), set(), indexed=None))
        self.assertNotIn("hidden-db", items["forgejo/deploy/forgejo"]["labels"]["uses"])

    def test_split_line_secret_not_in_uses(self):
        k8s = fx.k8s()
        k8s["configmaps"][0]["data"]["app.yaml"] = "db:\n  password:\n    split-db.postgres-system.svc\n"
        items = by_name(build.build_items(k8s, {}, fx.repo(), set(), indexed=None))
        self.assertNotIn("split-db", items["forgejo/deploy/forgejo"]["labels"]["uses"])

    def test_long_env_value_fast(self):
        import time
        k8s = fx.k8s()
        k8s["deployments"][0]["spec"]["template"]["spec"]["containers"][0]["env"].append(
            {"name": "BIG", "value": "z" * 65536})
        start = time.time()
        build.build_items(k8s, {}, fx.repo(), set())
        self.assertLess(time.time() - start, 1.0)

    def test_failed_source_untouched(self):
        self.assertNotIn("vm/nick", self.run_carry({"k8s"}))


if __name__ == "__main__":
    unittest.main()
