#!/usr/bin/env python3
"""Reconcile the CLIProxyAPI config row in Postgres before the proxy starts.

CLIProxyAPI keeps its whole config.yaml as one text row in `config_store`. A few
values have no environment variable, so without this they exist ONLY as live
database state and a rebuild from git loses them - the thing CLAUDE.md forbids.
See #861.

Two different contracts, on purpose:

  ENFORCED  - policy that git owns. Overwritten every start if it drifts.
              remote-management.allow-remote : the internal hostname is useless
                                               without it, every non-localhost
                                               caller is refused regardless of key.
              auth-dir                       : must sit on the mounted emptyDir.
                                               After #862 the rootfs is read-only,
                                               so a stale path here means adding a
                                               provider account fails at write time.

  SEEDED    - set only when absent or still the upstream example. Never clobbered,
              because the owner may rotate it through the management UI.
              api-keys

If the row does not exist yet this exits 0 and does nothing: the proxy seeds it
from config.example.yaml on first start, and the next restart reconciles it. That
is deliberate - guessing a whole 37KB config here would rot against upstream.
"""
import os
import sys

import yaml

EXAMPLE_MARKERS = ("your-api-key", "example", "changeme", "sk-123456")


def is_placeholder(keys):
    if not keys:
        return True
    return all(any(m in str(k).lower() for m in EXAMPLE_MARKERS) for k in keys)


def reconcile(cfg, want_auth_dir, want_api_key):
    """Pure transform. Returns (cfg, changes). Kept separate from the database so
    it can be self-tested without one - run this file with --self-test."""
    changes = []

    rm = cfg.setdefault("remote-management", {})
    if rm.get("allow-remote") is not True:
        rm["allow-remote"] = True
        changes.append("remote-management.allow-remote -> true")

    if cfg.get("auth-dir") != want_auth_dir:
        changes.append(f"auth-dir -> {want_auth_dir}")
        cfg["auth-dir"] = want_auth_dir

    if want_api_key and is_placeholder(cfg.get("api-keys")):
        cfg["api-keys"] = [want_api_key]
        changes.append("api-keys seeded (was absent or the upstream example)")

    return cfg, changes


def self_test():
    real = "omp-realkey0123456789"

    cfg, ch = reconcile({"remote-management": {"allow-remote": False},
                         "auth-dir": "/CLIProxyAPI/pgstore/auths",
                         "api-keys": ["your-api-key-1"]}, "/want", real)
    assert cfg["remote-management"]["allow-remote"] is True
    assert cfg["auth-dir"] == "/want"
    assert cfg["api-keys"] == [real], cfg["api-keys"]
    assert len(ch) == 3, ch

    # a key the owner rotated through the UI must NEVER be clobbered
    cfg, ch = reconcile({"remote-management": {"allow-remote": True},
                         "auth-dir": "/want",
                         "api-keys": ["owner-rotated-this-key"]}, "/want", real)
    assert cfg["api-keys"] == ["owner-rotated-this-key"], cfg["api-keys"]
    assert ch == [], ch

    # empty list counts as absent, so it seeds
    cfg, _ = reconcile({"api-keys": []}, "/want", real)
    assert cfg["api-keys"] == [real]

    # no desired key configured -> never touch api-keys
    cfg, _ = reconcile({"api-keys": ["your-api-key-1"]}, "/want", "")
    assert cfg["api-keys"] == ["your-api-key-1"]

    # allow-remote absent entirely still gets enforced
    cfg, _ = reconcile({}, "/want", "")
    assert cfg["remote-management"]["allow-remote"] is True

    print("self-test passed")
    return 0


def main():
    DSN = os.environ["RECONCILE_DSN"]
    WANT_AUTH_DIR = os.environ["WANT_AUTH_DIR"]
    WANT_API_KEY = os.environ.get("WANT_API_KEY", "")
    import psycopg2
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(
        "SELECT to_regclass('public.config_store') IS NOT NULL"
    )
    if not cur.fetchone()[0]:
        print("config_store does not exist yet - first boot, the proxy will seed it")
        return 0

    cur.execute("SELECT content FROM public.config_store WHERE id = 'config'")
    row = cur.fetchone()
    if row is None:
        print("no config row yet - first boot, the proxy will seed it")
        return 0

    cfg = yaml.safe_load(row[0])
    if not isinstance(cfg, dict):
        print("config row is not a YAML mapping, refusing to touch it", file=sys.stderr)
        return 1

    cfg, changes = reconcile(cfg, WANT_AUTH_DIR, WANT_API_KEY)

    if not changes:
        print("config already matches git - nothing to do")
        return 0

    cur.execute(
        "UPDATE public.config_store SET content = %s, updated_at = now() WHERE id = 'config'",
        (yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),),
    )
    conn.commit()
    for c in changes:
        print(f"applied: {c}")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    sys.exit(main())
