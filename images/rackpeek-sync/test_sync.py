import os
import tempfile
import unittest

import fixtures as fx
import sync


class FakeIO:
    def __init__(self, down_hosts=(), rpk_down=False, push_down=False, leaks=()):
        self.down_hosts = set(down_hosts)
        self.rpk_down = rpk_down
        self.push_down = push_down
        self.leaks = list(leaks)
        self.posts = []
        self.pushed = []
        self.log = []

    def rpk_get(self):
        if self.rpk_down:
            raise OSError("connection refused")
        return {"version": 5, "resources": fx.stored()}

    def rpk_post(self, body):
        self.posts.append(body)

    def k8s(self):
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

    def push_live(self, tree):
        if self.push_down:
            raise OSError("forgejo down")
        self.pushed.append(sorted(os.path.relpath(os.path.join(d, f), tree)
                                  for d, _, fs in os.walk(tree) for f in fs))


HOSTS = {"takaros": "root@192.168.10.10", "truenas": "truenas_admin@192.168.10.200"}


def run(io):
    return sync.run(io, HOSTS, fx.TODAY, io.log.append)


class Sync(unittest.TestCase):
    def test_happy_path_body(self):
        io = FakeIO()
        self.assertEqual(run(io), 0)
        body = io.posts[0]
        self.assertEqual(body["mode"], "Replace")
        self.assertEqual(body["json"]["version"], 5)
        self.assertNotIn("connections", body["json"])
        for r in body["json"]["resources"]:
            self.assertIn("sync", r["tags"])
        self.assertTrue(any("takaros/etc/hosts" == p for p in io.pushed[0]))
        self.assertTrue(any(p == "k8s/forgejo/configmaps/forgejo-cfg.yaml" for p in io.pushed[0]))

    def test_one_host_down(self):
        io = FakeIO(down_hosts={"truenas"})
        self.assertEqual(run(io), 1)
        names = {r["name"] for r in io.posts[0]["json"]["resources"]}
        self.assertIn("hw/takaros", names)
        self.assertNotIn("truenas/app/silo", names)
        self.assertTrue(any("truenas" in line and "TimeoutError" in line for line in io.log))

    def test_rackpeek_down_posts_nothing(self):
        io = FakeIO(rpk_down=True)
        self.assertEqual(run(io), 1)
        self.assertEqual(io.posts, [])
        self.assertEqual(io.pushed, [])

    def test_forgejo_down_still_posts(self):
        io = FakeIO(push_down=True)
        self.assertEqual(run(io), 1)
        self.assertEqual(len(io.posts), 1)

    def test_gitleaks_drops_file(self):
        io = FakeIO(leaks=["takaros/root/fan.sh"])
        run(io)
        self.assertNotIn("takaros/root/fan.sh", io.pushed[0])
        self.assertTrue(any("takaros/root/fan.sh" in line for line in io.log))

    def test_configmap_masked(self):
        tree = tempfile.mkdtemp()
        sync.write_tree(tree, {}, {"configmaps": [{"metadata": {"name": "c", "namespace": "n"},
                                                     "data": {"x": "password=hunter2"}}]})
        with open(os.path.join(tree, "k8s/n/configmaps/c.yaml")) as f:
            text = f.read()
        self.assertNotIn("hunter2", text)


class Main(unittest.TestCase):
    def test_missing_env_exits_1(self):
        for k in ("RPK_API_KEY", "HOSTS"):
            os.environ.pop(k, None)
        self.assertEqual(sync.main(), 1)


if __name__ == "__main__":
    unittest.main()
