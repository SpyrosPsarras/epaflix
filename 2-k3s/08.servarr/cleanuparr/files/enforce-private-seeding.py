#!/usr/bin/env python3
"""Keep Cleanuparr's Download Cleaner within each private tracker's hit-and-run rule.

Cleanuparr removes a download when (max_ratio AND min_seed_time) are reached, OR
when max_seed_time is reached regardless of ratio. A rule at privacy_type='both'
therefore applies those thresholds to PRIVATE trackers too, and with max_ratio=1.0
that is mathematically incapable of ever producing an account ratio >= 1.0:
torrents that would earn a surplus are cut dead exactly at 1.0, and torrents that
never get there are cut at max_seed_time at whatever ratio they had.

That is what demoted the TorrentDay account to ratio 0.218 and suspended its
download privileges: 0 TorrentDay torrents were left seeding, and over one 3.7-day
window 49 of 83 removals fired on MAX_SEED_TIME with an unfinished ratio.

Cleanuparr 2.11.1 semantics, read from its v2.11.1 source: a tracker pattern
matches when a torrent tracker URL's host ends with it, case-insensitive, with no
dot boundary (SeedingRuleEvaluator, UriService.GetDomain); an empty pattern list
matches every tracker. Rules are tried in ascending priority and only the first
one matching category, tracker, tags and privacy applies, so the lowest number
wins. min_seed_time and max_seed_time are hours (TorrentDownloadService,
TimeSpan.FromHours). Download Cleaner only looks at completed torrents.

With no dot boundary, tracker-rules.json lists full announce hostnames, not
domains: a new announce host then falls to the private default, which is the
longest rule. A host that merely ends with a listed hostname ("notmagtre.com")
would still get that tracker's rule; torrent-hnr-check reports such hosts.

The private qBittorrent rules are rebuilt from tracker-rules.json: one rule per
tracker and category, named "<tracker> (<category>)", then a catch-all
"private default (<category>)" with the highest private priority number. Public
rules are left alone. No stall or slow rule that can match a private torrent
deletes it from the client or changes its category, whatever the rule is named,
because an incomplete private download deleted mid-way is a hit-and-run.

If a private rule must be created for a category with no public rule to copy
client settings from, nothing is changed and the init container fails: Cleanuparr stays down rather than run
with private rules missing.

This runs as an initContainer so it is enforced on every boot, including onto a
restored PVC. Idempotent: re-running changes nothing.
"""
import json
import os
import sqlite3
import sys
import uuid

DB = os.environ.get("CLEANUPARR_DB", "/config/cleanuparr.db")
RULES = os.environ.get("TRACKER_RULES", "/rules/tracker-rules.json")
SEEDING = "q_bit_seeding_rules"
JSON_COLUMNS = ("categories", "tracker_patterns", "tags_all", "tags_any")

TABLES = ("q_bit_seeding_rules", "deluge_seeding_rules",
          "transmission_seeding_rules", "u_torrent_seeding_rules",
          "r_torrent_seeding_rules")


def load_rules():
    with open(RULES) as f:
        return json.load(f)


def desired_seeding_rules(rules):
    cats, trackers = rules["categories"], rules["trackers"]
    out = []
    for ci, cat in enumerate(cats):
        # Every private rule waits the default hit-and-run time before ratio 5 can
        # delete: Cleanuparr gives a tracker rule to any torrent with one matching
        # host, including lookalike and mixed hosts whose real tracker is unknown.
        min_seed = float(rules["default_private"]["hnr"]["hours"])
        sources = [(f"{t['name']} ({cat})", t["hosts"], t["cleanuparr"], min_seed,
                    11 + ci * len(trackers) + ti) for ti, t in enumerate(trackers)]
        sources.append((f"private default ({cat})", [], rules["default_private"]["cleanuparr"],
                        min_seed, 11 + len(cats) * len(trackers) + ci))
        for name, hosts, limits, min_seed, priority in sources:
            out.append({"name": name, "privacy_type": "private", "categories": [cat],
                        "tracker_patterns": hosts, "max_ratio": float(limits["max_ratio"]),
                        "min_seed_time": min_seed, "max_seed_time": float(limits["max_seed_hours"]),
                        "priority": priority, "tags_all": [], "tags_any": [],
                        "min_seeders": 0, "max_inactive_days": -1.0,
                        "delete_source_files": True})
    return out


QUEUE_RULE_TABLES = ("stall_rules", "slow_rules")


def desired_stall_rule():
    return {"delete_private_torrents_from_client": 0, "change_category": 0}


def has_table(con, table):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone() is not None


def rows(con, sql, args=()):
    cur = con.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def differs(current, want):
    return {k: v for k, v in want.items() if k != "name" and (
        json.loads(current[k] or "[]") if k in JSON_COLUMNS else current[k]) != v}


def encode(values):
    return {k: json.dumps(v, separators=(",", ":")) if k in JSON_COLUMNS else v
            for k, v in values.items()}


def update(con, table, row_id, values):
    values = encode(values)
    con.execute(f"UPDATE {table} SET {','.join(f'{k}=?' for k in values)} WHERE id=?",
                [*values.values(), row_id])


def reconcile_privacy(con):
    """The pre-existing guard: a 'both' rule applies public limits to private torrents."""
    changes = 0
    for table in TABLES:
        if has_table(con, table):
            n = con.execute(
                f"UPDATE {table} SET privacy_type='public' WHERE privacy_type='both'").rowcount
            if n:
                print(f"{table}: {n} rule(s) at privacy 'both' set to public")
            changes += n
    con.commit()
    return changes


def reconcile_seeding(con, rules):
    changes = 0
    if not has_table(con, SEEDING):
        return changes

    templates = {}
    for r in rows(con, f"SELECT * FROM {SEEDING} WHERE privacy_type='public' ORDER BY priority"):
        for cat in json.loads(r["categories"] or "[]"):
            templates.setdefault(cat, r)
    desired = desired_seeding_rules(rules)
    wanted = {w["name"] for w in desired}
    existing = {r["name"] for r in rows(
        con, f"SELECT name FROM {SEEDING} WHERE privacy_type='private'")}
    missing = sorted({w["categories"][0] for w in desired
                      if w["name"] not in existing and w["categories"][0] not in templates})
    if missing:
        raise SystemExit(f"{SEEDING}: no public rule to copy for categories {missing}; "
                         "refusing to change private rules")
    current = {}
    for r in rows(con, f"SELECT * FROM {SEEDING} WHERE privacy_type='private' ORDER BY priority"):
        name = r["name"]
        if name in wanted and name not in current:
            current[name] = r
            continue
        con.execute(f"DELETE FROM {SEEDING} WHERE id=?", (r["id"],))
        print(f"{SEEDING}: removed {name} (not in tracker-rules.json or duplicate)")
        changes += 1
    for want in desired:
        want = dict(want, action="delete")
        name, cat = want["name"], want["categories"][0]
        if name in current:
            diff = differs(current[name], want)
            if diff:
                update(con, SEEDING, current[name]["id"], diff)
                print(f"{SEEDING}: updated {name}: {diff}")
                changes += 1
        else:
            rec = dict(templates[cat], **encode(want), id=str(uuid.uuid4()).upper())
            con.execute(f"INSERT INTO {SEEDING} ({','.join(rec)}) VALUES "
                        f"({','.join('?' * len(rec))})", list(rec.values()))
            print(f"{SEEDING}: created {name}")
            changes += 1
    return changes


def reconcile_queue_rules(con, rules):
    want = desired_stall_rule()
    changes = 0
    # An older restored DB lacks the column; Cleanuparr migrates it on start.
    if has_table(con, "queue_cleaner_configs") and "errored_delete_private" in {
            r[1] for r in con.execute("PRAGMA table_info(queue_cleaner_configs)")}:
        n = con.execute("UPDATE queue_cleaner_configs SET errored_delete_private=0 "
                        "WHERE errored_delete_private<>0").rowcount
        if n:
            print("queue_cleaner_configs: errored_delete_private set to 0")
        changes += n
    # Queue Cleaner deletes a torrent whose files are all skipped, private or not;
    # the known private hosts and the hourly private-guard tag keep them out of it.
    if has_table(con, "queue_cleaner_configs") and "ignored_downloads" in {
            r[1] for r in con.execute("PRAGMA table_info(queue_cleaner_configs)")}:
        wanted = [h for t in rules["trackers"] for h in t["hosts"]] + ["private-guard"]
        for row_id, raw in con.execute(
                "SELECT id, ignored_downloads FROM queue_cleaner_configs").fetchall():
            have = json.loads(raw or "[]")
            add = [x for x in wanted if x.lower() not in {h.lower() for h in have}]
            if add:
                con.execute("UPDATE queue_cleaner_configs SET ignored_downloads=? WHERE id=?",
                            (json.dumps(have + add), row_id))
                print(f"queue_cleaner_configs: ignored_downloads added {len(add)} entries")
                changes += 1
    # Metadata strikes delete a magnet before qBittorrent knows whether it is private.
    if has_table(con, "queue_cleaner_configs") and "downloading_metadata_max_strikes" in {
            r[1] for r in con.execute("PRAGMA table_info(queue_cleaner_configs)")}:
        n = con.execute("UPDATE queue_cleaner_configs SET downloading_metadata_max_strikes=0 "
                        "WHERE downloading_metadata_max_strikes<>0").rowcount
        if n:
            print("queue_cleaner_configs: downloading_metadata_max_strikes set to 0")
        changes += n
    for table in QUEUE_RULE_TABLES:
        if not has_table(con, table):
            continue
        for r in rows(con, f"SELECT * FROM {table} WHERE privacy_type<>'public'"):
            diff = differs(r, {k: v for k, v in want.items() if k in r})
            if diff:
                update(con, table, r["id"], diff)
                print(f"{table}: updated {r['name']}: {diff}")
                changes += 1
    return changes


def reconcile(con, rules, commit=True):
    # Always applied, also in report mode: it predates the seeding policy.
    guarded = reconcile_privacy(con)
    try:
        changes = guarded + reconcile_seeding(con, rules) + reconcile_queue_rules(con, rules)
    except BaseException:
        con.rollback()
        raise
    con.commit() if commit else con.rollback()
    return changes


def main():
    rules = load_rules()
    if not os.path.exists(DB):
        print(f"{DB} absent (fresh install) - nothing to enforce")
        return 0
    # Argo rolls this Deployment on merge; writes wait for the rollout PR.
    enforce = os.environ.get("SEEDING_POLICY", "") == "enforce"
    if not enforce:
        print("SEEDING_POLICY is not 'enforce': planned changes below are rolled back")
    con = sqlite3.connect(DB)
    try:
        if not reconcile(con, rules, commit=enforce):
            print("private-tracker seeding rules already correct - no change")
        if has_table(con, SEEDING):
            for name, priv, ratio, seed, patterns in con.execute(
                    f"SELECT name,privacy_type,max_ratio,max_seed_time,tracker_patterns "
                    f"FROM {SEEDING} ORDER BY priority"):
                # Pattern count only: a live pattern can be a pasted announce URL.
                n = len(json.loads(patterns or "[]"))
                print(f"  {name} | {priv} | ratio<={ratio} | seed<={seed}h | {n} pattern(s)")
    finally:
        con.close()
    return 0


SCHEMA = """
CREATE TABLE q_bit_seeding_rules (id TEXT PRIMARY KEY, download_client_config_id TEXT,
  name TEXT, privacy_type TEXT, max_ratio REAL, min_seed_time REAL, max_seed_time REAL,
  delete_source_files INTEGER, categories TEXT, priority INTEGER, tags_all TEXT,
  tags_any TEXT, tracker_patterns TEXT, min_seeders INTEGER, max_inactive_days REAL,
  action TEXT);
CREATE TABLE stall_rules (id TEXT PRIMARY KEY, reset_strikes_on_progress INTEGER,
  minimum_progress TEXT, queue_cleaner_config_id TEXT, name TEXT, enabled INTEGER,
  max_strikes INTEGER, privacy_type TEXT, min_completion_percentage INTEGER,
  max_completion_percentage INTEGER, delete_private_torrents_from_client INTEGER,
  change_category INTEGER);
CREATE TABLE queue_cleaner_configs (id TEXT PRIMARY KEY, errored_delete_private INTEGER,
  downloading_metadata_max_strikes INTEGER, ignored_downloads TEXT);
CREATE TABLE slow_rules (id TEXT PRIMARY KEY, name TEXT, enabled INTEGER, privacy_type TEXT,
  max_strikes INTEGER, delete_private_torrents_from_client INTEGER, change_category INTEGER);
"""


def _fixture(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for i, cat in enumerate(("radarr", "tv-sonarr", "animes", "music")):
        for name, priv, ratio, seed, prio in ((cat, "public", 1.0, 400.0, i + 1),
                                              (f"{cat} (private)", "private", 5.0, 720.0, i + 11)):
            con.execute(
                "INSERT INTO q_bit_seeding_rules VALUES "
                "(?,'CLIENT',?,?,?,0,?,1,?,?,'[\"needs-tag\"]','[]','[]',3,7,'delete')",
                (f"{name}-id", name, priv, ratio, seed, json.dumps([cat]), prio))
    for name, priv, lo, hi, delete, move in (("Stall", "public", 0, 100, 1, 1),
                                             ("private stalled", "private", 0, 99, 1, 1),
                                             ("renamed 0-99", "both", 0, 99, 1, 1),
                                             ("private stalled 99-100", "private", 99, 100, 0, 0)):
        con.execute("INSERT INTO stall_rules VALUES (?,1,'0','QC',?,1,30,?,?,?,?,?)",
                    (f"{name}-id", name, priv, lo, hi, delete, move))
    con.execute("INSERT INTO slow_rules VALUES ('slow-id','Slow-rule',1,'both',3,1,0)")
    con.execute("INSERT INTO queue_cleaner_configs VALUES ('qc-id',1,3,'[\"mine\"]')")
    con.commit()
    return con


def selftest():
    import tempfile
    rules = load_rules()
    cats = rules["categories"]
    with tempfile.TemporaryDirectory() as tmp:
        con = _fixture(os.path.join(tmp, "cleanuparr.db"))
        stall_before = con.execute(
            "SELECT * FROM stall_rules WHERE name='private stalled 99-100'").fetchone()
        assert reconcile(con, rules) > 0
        con.row_factory = sqlite3.Row
        rows = {r["name"]: dict(r) for r in con.execute("SELECT * FROM q_bit_seeding_rules")}

        # test_one_rule_per_tracker_and_category
        private = {n: r for n, r in rows.items() if r["privacy_type"] == "private"}
        assert len(private) == (len(rules["trackers"]) + 1) * len(cats), sorted(private)
        for cat in cats:
            default = private[f"private default ({cat})"]
            for t in rules["trackers"]:
                r = private[f"{t['name']} ({cat})"]
                assert json.loads(r["tracker_patterns"]) == t["hosts"], r
                assert json.loads(r["categories"]) == [cat], r
                assert r["max_ratio"] == t["cleanuparr"]["max_ratio"], r
                assert r["max_seed_time"] == t["cleanuparr"]["max_seed_hours"], r
                assert r["min_seed_time"] == rules["default_private"]["hnr"]["hours"], r
                assert r["priority"] < default["priority"], (r, default)
                assert (r["download_client_config_id"], r["delete_source_files"], r["action"]) \
                    == ("CLIENT", 1, "delete"), r

        # test_unknown_private_host_gets_default
        for cat in cats:
            default = private[f"private default ({cat})"]
            assert json.loads(default["tracker_patterns"]) == [], default
            assert default["min_seed_time"] == rules["default_private"]["hnr"]["hours"], default
            assert default["max_ratio"] == rules["default_private"]["cleanuparr"]["max_ratio"]
            assert default["max_seed_time"] == \
                rules["default_private"]["cleanuparr"]["max_seed_hours"]
            assert default["priority"] > max(
                r["priority"] for n, r in private.items() if not n.startswith("private default"))

        # test_public_rules_untouched
        for i, cat in enumerate(cats):
            r = rows[cat]
            assert (r["privacy_type"], r["max_ratio"], r["max_seed_time"], r["priority"]) == \
                ("public", 1.0, 400.0, i + 1), r

        # test_old_generic_private_rules_replaced
        assert not [n for n in rows if n.endswith("(private)")], sorted(rows)

        # private rules carry no inherited tag, seeder or inactivity filter
        for r in private.values():
            assert (r["tags_all"], r["tags_any"], r["min_seeders"], r["max_inactive_days"]) \
                == ("[]", "[]", 0, -1), r

        # test_stall_rule: every rule that can match a private torrent, whatever its name
        con.row_factory = None
        for name in ("private stalled", "renamed 0-99"):
            assert con.execute(
                "SELECT delete_private_torrents_from_client, change_category FROM stall_rules "
                "WHERE name=?", (name,)).fetchone() == (0, 0), name
        assert con.execute("SELECT delete_private_torrents_from_client, change_category "
                           "FROM stall_rules WHERE name='Stall'").fetchone() == (1, 1)
        assert con.execute("SELECT delete_private_torrents_from_client FROM slow_rules"
                           ).fetchone() == (0,)
        assert con.execute("SELECT errored_delete_private, downloading_metadata_max_strikes "
                           "FROM queue_cleaner_configs").fetchone() == (0, 0)
        ignored = json.loads(con.execute(
            "SELECT ignored_downloads FROM queue_cleaner_configs").fetchone()[0])
        assert ignored[0] == "mine" and "private-guard" in ignored and \
            "tracker.avistaz.to" in ignored, ignored
        assert con.execute(
            "SELECT * FROM stall_rules WHERE name='private stalled 99-100'").fetchone() \
            == stall_before

        # test_idempotent
        assert reconcile(con, rules) == 0
        con.execute("UPDATE q_bit_seeding_rules SET action='stop', delete_source_files=0 "
                    "WHERE name=?", (f"private default ({cats[0]})",))
        con.commit()
        assert reconcile(con, rules) == 1
        assert con.execute("SELECT action, delete_source_files FROM q_bit_seeding_rules "
                           "WHERE name=?", (f"private default ({cats[0]})",)).fetchone() \
            == ("delete", 1)
        con.execute("UPDATE q_bit_seeding_rules SET max_ratio=1.0, tracker_patterns='[]' "
                    "WHERE name=?", (f"{rules['trackers'][0]['name']} ({cats[0]})",))
        con.commit()
        assert reconcile(con, rules) == 1
        assert reconcile(con, rules) == 0
        con.close()

        # older DB without errored_delete_private: no failure
        con = _fixture(os.path.join(tmp, "old.db"))
        con.execute("DROP TABLE queue_cleaner_configs")
        con.execute("CREATE TABLE queue_cleaner_configs (id TEXT PRIMARY KEY)")
        con.commit()
        reconcile(con, rules)
        con.close()

        # report mode: planned changes are rolled back, the old 'both' guard still applies
        con = _fixture(os.path.join(tmp, "report.db"))
        con.execute("UPDATE q_bit_seeding_rules SET privacy_type='both' WHERE name='radarr'")
        con.commit()
        before = con.execute("SELECT * FROM q_bit_seeding_rules WHERE name<>'radarr' "
                             "ORDER BY id").fetchall()
        assert reconcile(con, rules, commit=False) > 0
        assert con.execute("SELECT * FROM q_bit_seeding_rules WHERE name<>'radarr' "
                           "ORDER BY id").fetchall() == before
        assert con.execute("SELECT privacy_type FROM q_bit_seeding_rules WHERE name='radarr'"
                           ).fetchone() == ("public",)
        con.close()

        # missing public template: refuse, change nothing
        con = _fixture(os.path.join(tmp, "missing.db"))
        con.execute("DELETE FROM q_bit_seeding_rules WHERE name='music'")
        con.commit()
        before = con.execute("SELECT * FROM q_bit_seeding_rules ORDER BY id").fetchall()
        try:
            reconcile(con, rules)
            raise AssertionError("missing template must fail")
        except SystemExit:
            pass
        assert con.execute("SELECT * FROM q_bit_seeding_rules ORDER BY id").fetchall() == before
        con.close()

        # template removed after a successful migration: existing rows still reconcile
        con = _fixture(os.path.join(tmp, "migrated.db"))
        reconcile(con, rules)
        con.execute("DELETE FROM q_bit_seeding_rules WHERE name='music'")
        con.execute("UPDATE q_bit_seeding_rules SET max_ratio=1.0 WHERE name=?",
                    (f"private default (music)",))
        con.commit()
        assert reconcile(con, rules) == 1
        con.close()
    print("selftest OK")
    return 0


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else main())
