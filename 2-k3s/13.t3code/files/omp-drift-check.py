#!/usr/bin/env python3
"""Compare the sops omp-api-key snapshot against the live cliproxy config in Postgres."""
import hashlib, json, os, re, subprocess, sys

os.environ["SOPS_AGE_KEY_FILE"] = os.path.expanduser("~/.config/sops/age/k3s-cluster.txt")
dec = subprocess.run(["sops", "-d", "2-k3s/17.remote-pi/cliproxy/cliproxy-secrets.enc.yaml"],
                     capture_output=True, text=True, check=True).stdout
import yaml
docs = list(yaml.safe_load_all(dec))
db = next(d for d in docs if d and "username" in d.get("stringData", {}))
proxy = next(d for d in docs if d and "omp-api-key" in d.get("stringData", {}))
omp = proxy["stringData"]["omp-api-key"]
print(f"sops omp-api-key fp: {hashlib.sha256(omp.encode()).hexdigest()[:8]} ({len(omp)} chars)")

pgpass = db["stringData"]["password"]
out = subprocess.run(
    ["kubectl", "exec", "-n", "postgres-system", "postgres-cluster-10", "--",
     "env", f"PGPASSWORD={pgpass}", "psql", "-h", "127.0.0.1", "-U",
     db["stringData"]["username"], "-d", "cliproxy", "-Atc",
     "SELECT content FROM public.config_store WHERE id='config'"],
    capture_output=True, text=True, check=True)
content = out.stdout
if not content.strip():
    sys.exit("psql returned empty (check pod/credentials)")

m = re.search(r"^api-keys:\s*\n((?:[ \t]+-.*\n?)+)", content, re.M)
live = re.findall(r"-\s*(\S+)", m.group(1)) if m else []
print(f"live api-keys in config_store: {len(live)}")
match = False
for k in live:
    fp = hashlib.sha256(k.encode()).hexdigest()[:8]
    print(f"live key fp: {fp} ({len(k)} chars)")
    if fp == hashlib.sha256(omp.encode()).hexdigest()[:8]:
        match = True
        print("MATCH: sops snapshot is current")
        open("/tmp/live-omp-key", "w").write(k)
if not match:
    print("MISMATCH: sops omp-api-key is stale vs live config")
    if live:
        open("/tmp/live-omp-key", "w").write(live[0])