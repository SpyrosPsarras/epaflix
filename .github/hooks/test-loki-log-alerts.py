#!/usr/bin/env python3
"""Replay CRI fixtures through shipped Alloy stages and evaluate real LogQL in Loki.

Optional arguments: rule ConfigMap path, Alloy values path, for baseline checks.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[2]
OBSERVABILITY = ROOT / "2-k3s/10.observability"
# Same releases as the observability charts, pinned to immutable digests.
LOKI_IMAGE = "grafana/loki:3.6.11@sha256:44148ad243c07957113baea950ee5ba9b0c8eff8d7e38ff3cc1c0af705790945"
ALLOY_IMAGE = "grafana/alloy:v1.20.1@sha256:2aa2099af76c0098d4af7a4d6e48f86cb66dc1a000222ad927a1c67c6542d13f"
CONFIG = """
auth_enabled: false
analytics:
  reporting_enabled: false
server:
  http_listen_port: 3100
common:
  instance_addr: 127.0.0.1
  path_prefix: /tmp/loki
  storage:
    filesystem:
      chunks_directory: /tmp/loki/chunks
      rules_directory: /tmp/loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory
ingester:
  lifecycler:
    min_ready_duration: 0
schema_config:
  configs:
    - from: "2024-01-01"
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h
"""


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def main():
    rule_path = Path(sys.argv[1]) if len(sys.argv) > 1 else OBSERVABILITY / "alertmanager-config/loki-log-alerts.yaml"
    alloy_path = Path(sys.argv[2]) if len(sys.argv) > 2 else OBSERVABILITY / "alloy-values.yaml"
    rules = yaml.safe_load(yaml.safe_load(rule_path.read_text())["data"]["log-alerts.yaml"])
    rule = next(r for g in rules["groups"] for r in g["rules"] if r["alert"] == "ServarrErrorSpike")
    cases = yaml.safe_load((ROOT / ".github/hooks/fixtures/servarr-log-levels.yaml").read_text())
    shipped = yaml.safe_load(alloy_path.read_text())["alloy"]["configMap"]["content"]
    # Keep every shipped process stage, substituting only discovery and the sink.
    process = 'loki.process "pods" {' + shipped.split('loki.process "pods" {', 1)[1].split('\nloki.write "default"', 1)[0]
    with tempfile.TemporaryDirectory(prefix="loki-alert-test-") as tmp:
        directory = Path(tmp)
        directory.chmod(0o755)
        (directory / "loki.yaml").write_text(CONFIG)
        targets = []
        expected_lines = {}
        now = time.time_ns()
        for case in cases:
            name = case["name"]
            assert case.get("expected_entries") in (None, 1), f"{name}: expected_entries supports only 1"
            events = case.get("lines", [case.get("line")])
            raw_events = ['[Info] Worker: Booting']
            entries = ['[Info] Worker: Booting']
            for i in range(case.get("count", 26)):
                event = events[i % len(events)].rstrip("\n")
                if "before" in case:
                    raw_events.append(case["before"])
                    entries.append(case["before"])
                raw_events.append(event)
                entries.extend(case.get("entries", [event]))
            if case.get("expected_entries") == 1:
                entries = ["\n".join(raw_events)]
            expected_lines[name] = entries
            cri_lines = []
            for i, line in enumerate("\n".join(raw_events).splitlines()):
                stamp = now - 60_000_000_000 + i * 1_000_000
                date = datetime.fromtimestamp(stamp // 1_000_000_000, timezone.utc)
                timestamp = date.strftime("%Y-%m-%dT%H:%M:%S") + f".{stamp % 1_000_000_000:09d}Z"
                cri_lines.append(f"{timestamp} stdout F {line}\n")
            (directory / f"{name}.log").write_text("".join(cri_lines))
            targets.append('{__path__ = "/fixtures/' + name + '.log", namespace = ' +
                           json.dumps(case.get("namespace", "servarr")) + ', container = ' + json.dumps(name) + '}')
        source = 'loki.source.file "fixtures" {\n targets = [' + ',\n'.join(targets) + ',]\n forward_to = [loki.process.pods.receiver]\n}\n'
        sink = 'loki.write "default" {\n endpoint {\n url = "http://loki:3100/loki/api/v1/push"\n }\n}\n'
        (directory / "config.alloy").write_text(source + process + sink)
        network = docker("network", "create", directory.name)
        containers = []
        try:
            loki = docker(
                "run", "--detach", "--rm", "--network", network, "--network-alias", "loki",
                "--publish", "127.0.0.1::3100", "--mount", f"type=bind,src={tmp},dst=/fixtures,readonly",
                LOKI_IMAGE, "-config.file=/fixtures/loki.yaml",
            )
            containers.append(loki)
            port = docker("inspect", "--format", '{{(index (index .NetworkSettings.Ports "3100/tcp") 0).HostPort}}', loki)
            base = f"http://127.0.0.1:{port}"

            def request(path):
                with urllib.request.urlopen(base + path, timeout=5) as response:
                    return response.read()

            for _ in range(60):
                try:
                    request("/ready")
                    break
                except (urllib.error.URLError, TimeoutError, ConnectionError):
                    time.sleep(0.5)
            else:
                raise AssertionError("Local Loki did not become ready")
            containers.append(docker(
                "run", "--detach", "--rm", "--network", network,
                "--mount", f"type=bind,src={tmp},dst=/fixtures,readonly",
                ALLOY_IMAGE, "run", "--disable-reporting", "/fixtures/config.alloy",
            ))

            def query(expr):
                params = urllib.parse.urlencode({"query": expr, "time": str(time.time_ns())})
                result = json.loads(request("/loki/api/v1/query?" + params))
                assert result["status"] == "success", "Local Loki query failed"
                return {r["metric"]["container"]: float(r["value"][1]) for r in result["data"]["result"]}

            # Allow the real 3s multiline timeout and 1s write batch to flush.
            time.sleep(5)
            for _ in range(40):
                totals = query('sum by (container) (count_over_time({namespace=~"servarr|elsewhere"}[5m]))')
                if set(totals) == set(expected_lines):
                    break
                time.sleep(0.25)
            else:
                raise AssertionError(f"Fixture ingestion incomplete: {set(expected_lines) - set(totals)}")
            params = urllib.parse.urlencode({"query": '{namespace=~"servarr|elsewhere"}',
                                            "start": str(now - 120_000_000_000),
                                            "end": str(time.time_ns()), "direction": "forward", "limit": 5000})
            result = json.loads(request("/loki/api/v1/query_range?" + params))
            # Loki's detected_level metadata can split one container's query result.
            emitted_values = {}
            for stream in result["data"]["result"]:
                emitted_values.setdefault(stream["stream"]["container"], []).extend(stream["values"])
            emitted = {name: [value[1] for value in sorted(values, key=lambda value: int(value[0]))]
                       for name, values in emitted_values.items()}
            actual = query(rule["expr"])
            failed = []
            marker = "Inspect in Grafana → Explore → Loki:"
            assert marker in rule["annotations"]["description"], "Grafana runbook query marker is required"
            runbook = rule["annotations"]["description"].split(marker, 1)[1].strip()
            for case in cases:
                name = case["name"]
                expected = float(case.get("expected_matches", case.get("count", 26))) if case["matches"] else None
                example = runbook.replace("{{ $labels.container }}", name)
                runbook_count = query(f"sum by (container) (count_over_time({example} [5m]))").get(name)
                runbook_expected = case.get("expected_runbook_matches", expected)
                runbook_ok = runbook_count == runbook_expected
                print(f"{'PASS' if runbook_ok else 'FAIL'} Grafana query {name}: expected={runbook_expected}, actual={runbook_count}")
                if not runbook_ok:
                    failed.append(f"Grafana/{name}")
                got = actual.get(name)
                lines_ok = emitted.get(name) == expected_lines[name]
                ok = got == expected and lines_ok
                print(f"{'PASS' if ok else 'FAIL'} {name}: severity expected={expected}, actual={got}; "
                      f"Alloy entries expected={len(expected_lines[name])}, actual={len(emitted.get(name, []))}, exact={lines_ok}")
                if not ok:
                    print(f"  first emitted entries: {emitted.get(name, [])[:2]!r}"[:300])
                    failed.append(name)
            assert not (set(actual) - set(expected_lines)), "Unexpected alert labels"
            assert not failed, f"{len(failed)} Alloy/LogQL regression cases failed"
            print(f"PASS: {len(cases)} raw CRI cases, shipped Alloy v1.20.1 process, Loki 3.6.11 LogQL")
        except Exception:
            for container in containers:
                print(docker("logs", container)[-4000:])
            raise
        finally:
            for container in reversed(containers):
                docker("rm", "--force", container)
            docker("network", "rm", network)


if __name__ == "__main__":
    main()
