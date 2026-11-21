#!/usr/bin/env python3
"""Keep the Cleanuparr and qBittorrent download policy in git. Stdlib only.

Why: on 2026-09-25 Cleanuparr's Blacklist Sync pushed the upstream
malware list into qBittorrent's "Excluded file names". That list skips
*.rar and *.r00-*.r04 but not *.r05+, so 18 scene releases downloaded
half their RAR volumes, qBittorrent called them complete, unpackerr had
nothing to extract and Sonarr sat on "No files found are eligible for
import". The 2026-06-13 fix (#182) had only cleaned the MalwareBlocker
list, and nothing in git could see the Blacklist Sync setting.

Policy (a torrent is accepted or rejected whole, never half-downloaded):
  - Blacklist Sync off; qBittorrent excluded file names off.
  - MalwareBlocker blocks executables only (/blocklists, mounted from
    git), rejects the whole torrent on any hit, skips private trackers.
  - QueueCleaner treats "No files found are eligible for import" as a
    failed import: after its strikes the item is removed from the arr
    and blocklisted, so the arr grabs another release. Private torrents
    stay in qBittorrent (deletePrivate=false) to finish seeding.

Private seeding (a private torrent is never cut before its tracker's
hit-and-run rule, see tracker-rules.json):
  - Cleanuparr's private qBittorrent seeding rules equal the table: one
    "<tracker> (<category>)" rule per tracker and category, then
    "private default (<category>)" below them. Private rules the table does
    not name are deleted; a rule at privacy Both becomes Public. Same rule
    set as the boot-time writer enforce-private-seeding.py (the test
    script compares both). Cleanuparr 2.11.1 API: GET/POST
    /api/seeding-rules/{clientId}, PUT/DELETE /api/seeding-rules/{id},
    PUT /api/seeding-rules/{clientId}/reorder. PUT cannot set a priority
    and reorder renumbers every rule from 1, so only the private rules'
    relative order is enforced, not their exact numbers.
  - No stall or slow rule that can match a private torrent deletes it from
    qBittorrent or changes its category (PUT /api/queue-rules/stall|slow/{id}).
  - qBittorrent global ratio, seeding-time and inactive-seeding-time limits
    are off, so torrents outside the Cleanuparr categories keep seeding.
  - Every private torrent in a Cleanuparr category gets no ratio limit and
    a seeding time limit 24 h above its Cleanuparr max, so qBittorrent never
    stops it before Cleanuparr removes it.
  - An incomplete private torrent in radarr or tv-sonarr that is in no
    Radarr or Sonarr queue (removed from it by hand or by a failed import;
    Queue Cleaner ignores private torrents) moves to private-stalled, outside
    the Download Cleaner, for a manual decision.
  - Every private or semi-private Prowlarr torrent indexer has no seed
    ratio and a seed time and pack seed time equal to the qBittorrent seed
    goal for its tracker (matched by indexer name), so new grabs get the
    same limits. Public indexers are not touched. Prowlarr (checked against
    master 847d487, deployed 2.x): GET /api/v1/indexer returns "fields" as
    [{"name", "value"}], with nulls omitted, and the nested settings as
    torrentBaseSettings.seedRatio (double?), .seedTime and .packSeedTime
    (int? minutes); "privacy" is public, semiPrivate or private. PUT
    /api/v1/indexer/{id} gets the GET body with only those three values
    changed: a Password or ApiKey field returned as "********" keeps its
    stored value (SchemaBuilder.ReadFromSchema), and Cardigann extra fields
    come back unmasked and are stored as sent.

Each run reads the live config, puts back anything that drifted, and
prints what it changed.
Exit 0 = already correct, 1 = drift found and corrected (a finding: the
log names each field), 2 = could not run. Per-torrent share-limit writes
print as SET lines and do not count: Radarr and Sonarr omit ratioLimit when
Prowlarr's seed ratio is empty, so every new private grab reads ratio -2 and
share-limit action "Default" until this job sets it.
With DRY_RUN=1 nothing is written and drift lines say "would correct".
Until SEEDING_POLICY=enforce, the seeding steps (seeding_rules, queue_rules,
share_limits, set_aside, prowlarr) only print PENDING lines and do not count
as drift.
Run with --selftest to check the merge logic offline.
"""
import contextlib
import copy
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from tracker_rules import load_rules, rule_for_indexer, rule_for_trackers, seed_goal_minutes

TIMEOUT = 20
CLEANUPARR_URL = os.environ.get(
    "CLEANUPARR_URL", "http://cleanuparr.servarr.svc.cluster.local:30263").rstrip("/")
QBT_URL = os.environ.get("QBT_URL", "http://qbittorrent:8080").rstrip("/")
RADARR_URL = os.environ.get("RADARR_URL", "http://radarr:7878").rstrip("/")
SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989").rstrip("/")
PROWLARR_URL = os.environ.get("PROWLARR_URL", "http://prowlarr:9696").rstrip("/")
RULES = os.environ.get("TRACKER_RULES", "/rules/tracker-rules.json")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
# The seeding steps write only once the owner has seen a dry run: Argo syncs
# this CronJob on merge, before the rollout preflight.
SEEDING_ENFORCED = os.environ.get("SEEDING_POLICY", "") == "enforce"

QBT_EXISTING = ("excluded_file_names_enabled",)
QBT_OFF = ("excluded_file_names_enabled", "max_ratio_enabled",
           "max_seeding_time_enabled", "max_inactive_seeding_time_enabled")
# animes and music also get torrents from Lidarr and seadexarr, which have no
# Radarr or Sonarr queue entry.
SET_ASIDE_CATEGORIES = ("radarr", "tv-sonarr")
FRESH_GRAB_SECONDS = 900
STALLED_STATES = ("stalledDL", "metaDL", "forcedMetaDL", "stoppedDL", "pausedDL")
RULE_FIELDS = ("name", "privacyType", "categories", "trackerPatterns", "maxRatio",
               "minSeedTime", "maxSeedTime", "tagsAll", "tagsAny", "minSeeders",
               "maxInactiveDays", "deleteSourceFiles", "action")
# Deleting torrent and payload is what frees disk; Stop would keep both.
PRIVATE_ACTION = "Delete"

BLOCKLIST = "/blocklists/malware-blocklist.txt"
NO_FILES = "No files found are eligible for import"
ARR_LIST = {"enabled": True, "blocklistType": "Blacklist", "blocklistPath": BLOCKLIST}

# Desired subset per Cleanuparr config endpoint. Keys not listed keep
# their live value. malware_blocker goes last: Cleanuparr rejects its PUT
# until the Deployment mounting /blocklists has rolled out, and that must
# not skip the other sections.
DESIRED = {
    "blacklist_sync": {"enabled": False},
    "queue_cleaner": {
        "failedImport": {
            "maxStrikes": 3,
            # Include: a matching pattern earns a strike. Exclude would
            # turn NO_FILES into an exemption.
            "patternMode": "Include",
            "ignorePrivate": False,
            "deletePrivate": False,
        },
    },
    "malware_blocker": {
        "enabled": True,
        "ignorePrivate": True,
        "deletePrivate": False,
        "deleteIfAnyFileBlocked": True,
        "sonarr": ARR_LIST,
        "radarr": ARR_LIST,
        "lidarr": ARR_LIST,
    },
}


def merge(current, desired, path=()):
    """Return (merged copy, [changed field paths])."""
    out = copy.deepcopy(current)
    changes = []
    for key, want in desired.items():
        if isinstance(want, dict) and isinstance(out.get(key), dict):
            out[key], sub = merge(out[key], want, path + (key,))
            changes += sub
        elif out.get(key) != want:
            # Desired value only: a live value can be a URL carrying a token.
            changes.append("%s: changed to %r" % (".".join(path + (key,)), want))
            out[key] = copy.deepcopy(want)
    return out, changes


# Part of the seeding policy, so it waits for SEEDING_POLICY=enforce.
# An errored private download deleted from the client is a hit-and-run.
# Metadata strikes delete a magnet before qBittorrent knows whether it is private.
SEEDING_DESIRED = {"queue_cleaner": {"errored": {"deletePrivate": False},
                                     "downloadingMetadataMaxStrikes": 0}}


PRIVATE_TAG = "private-guard"


def queue_ignore_wanted(rules):
    # Queue Cleaner deletes a torrent whose files are all skipped, private or not,
    # with no setting to stop it (QBitServiceQC, v2.11.1). Private torrents are kept
    # out of it: known hosts at once, any private torrent via the hourly tag.
    return [h for t in rules["trackers"] for h in t["hosts"]] + [PRIVATE_TAG]


def plan_queue_ignore(cfg, rules):
    have = list(cfg.get("ignoredDownloads") or [])
    lower = {x.lower() for x in have}
    add = [x for x in queue_ignore_wanted(rules) if x.lower() not in lower]
    return (dict(cfg, ignoredDownloads=have + add) if add else None,
            ["queue_cleaner.ignoredDownloads: added %s" % x for x in add])


CATEGORY_LIMIT_KEYS = ("ratio_limit", "seeding_time_limit", "inactive_seeding_time_limit")


def plan_category_limits(categories, rules):
    """Managed categories must inherit the (disabled) global limits: qBittorrent 5.3
    applies a category's limits and action to a fresh grab before this job runs."""
    ops, changes = [], []
    for cat in limit_categories(rules):
        c = categories.get(cat)
        if c is None:
            continue
        bad = [k for k in CATEGORY_LIMIT_KEYS if c.get(k, -2) != -2]
        bad += [k for k in ("share_limit_action", "share_limits_mode")
                if c.get(k, "Default") != "Default"]
        if bad:
            ops.append({"category": cat, "ratioLimit": -2, "seedingTimeLimit": -2,
                        "inactiveSeedingTimeLimit": -2, "shareLimitAction": "Default",
                        "shareLimitsMode": "Default"})
            changes.append("qbittorrent.category %s: %s changed to inherit global"
                           % (cat, ", ".join(bad)))
    return ops, changes


def plan_private_tags(torrents):
    hashes = [t["hash"] for t in torrents if t["private"] and PRIVATE_TAG not in
              [x.strip() for x in (t.get("tags") or "").split(",")]]
    return hashes, ["qbittorrent.tag %s: added %s" % (h[:8], PRIVATE_TAG) for h in hashes]


def reconcile(section, current, desired=None):
    cfg, changes = merge(current, (desired or DESIRED)[section], (section,))
    if desired is SEEDING_DESIRED:
        return cfg, changes
    if section == "queue_cleaner":
        # Append, never replace: patterns someone added in the UI stay.
        patterns = cfg["failedImport"].get("patterns") or []
        if NO_FILES not in patterns:
            cfg["failedImport"]["patterns"] = patterns + [NO_FILES]
            changes.append("queue_cleaner.failedImport.patterns: added %r" % NO_FILES)
    return cfg, changes


class PolicyError(RuntimeError):
    """A refusal whose message holds no secret and is safe to log."""


def api_key(snake):
    return re.sub(r"_(\w)", lambda m: m.group(1).upper(), snake)


def desired_seeding_rules(rules):
    """Port of enforce-private-seeding.py; torrent-policy.test.sh checks they agree."""
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


def plan_seeding(live, rules):
    """Return ([(method, id, body)], [change lines]) that make live equal the table."""
    live = sorted(live, key=lambda r: (r["priority"], r["id"]))
    body_of = lambda r: {k: v for k, v in r.items() if k not in ("id", "priority")}  # noqa: E731
    ops, changes = [], []
    for r in live:
        if r["privacyType"] == "Both":
            ops.append(("PUT", r["id"], dict(body_of(r), privacyType="Public")))
            changes.append("seeding_rules.%s: privacyType 'Both' -> 'Public'" % r["name"])
    templates = {}
    for r in live:
        if r["privacyType"] != "Private":
            for cat in r["categories"]:
                templates.setdefault(cat, r)
    desired = [dict({api_key(k): v for k, v in w.items()}, privacyType="Private",
                    action=PRIVATE_ACTION) for w in desired_seeding_rules(rules)]
    names = {w["name"] for w in desired}
    current, survivors = {}, []
    for r in live:
        if r["privacyType"] != "Private":
            survivors.append(r)
        elif r["name"] in names and r["name"] not in current:
            current[r["name"]] = r
            survivors.append(r)
        else:
            ops.append(("DELETE", r["id"], None))
            changes.append("seeding_rules.%s: removed (not in tracker-rules.json or duplicate)"
                           % r["name"])
    missing = sorted({w["categories"][0] for w in desired
                      if w["name"] not in current and w["categories"][0] not in templates})
    if missing:
        raise PolicyError("no public seeding rule to copy for categories %s; refusing to "
                          "change private rules" % missing)
    taken = {r["priority"] for r in survivors}
    for w in desired:
        r = current.get(w["name"])
        if r is None:
            tpl = templates[w["categories"][0]]
            body = body_of(w)
            # Cleanuparr rejects a used priority; without one it appends, and the
            # next pass reorders.
            if w["priority"] not in taken:
                body["priority"] = w["priority"]
                taken.add(w["priority"])
            ops.append(("POST", None, body))
            changes.append("seeding_rules.%s: created" % w["name"])
            continue
        diff = {k: w[k] for k in RULE_FIELDS if r.get(k) != w[k]}
        if diff:
            ops.append(("PUT", r["id"], dict(body_of(r), **diff)))
            # Field names only: a live trackerPatterns entry could be a pasted
            # announce URL carrying a passkey.
            changes.append("seeding_rules.%s: %s changed" % (w["name"], ", ".join(diff)))
    order = [w["name"] for w in sorted(desired, key=lambda w: w["priority"])]
    if not ops and [r["name"] for r in live if r["privacyType"] == "Private"] != order:
        ids = [r["id"] for r in live if r["privacyType"] != "Private"]
        ops.append(("REORDER", ids + [current[n]["id"] for n in order], None))
        changes.append("seeding_rules: private rule order restored (tracker rules before "
                       "private default)")
    return ops, changes


def plan_queue_rules(kind, live):
    ops, changes = [], []
    for r in live:
        if r["privacyType"] == "Public":
            continue
        diff = {k: False for k in ("deletePrivateTorrentsFromClient", "changeCategory")
                if r.get(k)}
        if diff:
            ops.append(("PUT", r["id"], dict({k: v for k, v in r.items() if k != "id"}, **diff)))
            changes.append("%s_rules.%s: %s" % (kind, r["name"], ", ".join(
                "%s True -> False" % k for k in diff)))
    return ops, changes


def qbt_globals(prefs, flags=None):
    form = {k: False for k in (QBT_OFF if flags is None else flags) if prefs.get(k)}
    return form, ["qbittorrent.%s: True -> False" % k for k in form]


def limit_categories(rules):
    # Set-aside torrents keep safe limits: one that later completes must not
    # stop on a short limit from grab time.
    return list(rules["categories"]) + [rules["stalled_category"]]


SHARE_LIMIT_ACTION = "Stop"
SHARE_LIMITS_MODE = "MatchAny"


def share_limits_form(hashes, ratio, seed, inactive):
    # qBittorrent 5.3 rejects setShareLimits without shareLimitAction and
    # shareLimitsMode (torrentscontroller.cpp, release-5.3.0).
    return {"hashes": "|".join(hashes), "ratioLimit": ratio, "seedingTimeLimit": seed,
            "inactiveSeedingTimeLimit": inactive, "shareLimitAction": SHARE_LIMIT_ACTION,
            "shareLimitsMode": SHARE_LIMITS_MODE}


def share_limit_plan(torrents, urls_by_hash, rules):
    """Return ({(ratio, seed min, inactive min): [hashes]}, [change lines])."""
    groups, changes = {}, []
    for t in torrents:
        if not t.get("private") or t.get("category") not in limit_categories(rules):
            continue
        rule = rule_for_trackers(urls_by_hash.get(t["hash"], []), rules)
        want = (-1, seed_goal_minutes(rule), -1)
        have = (t.get("ratio_limit"), t.get("seeding_time_limit"),
                t.get("inactive_seeding_time_limit"))
        # qBittorrent 5.3 stores the action per torrent; Stop never removes data.
        if t.get("share_limit_action") not in (None, SHARE_LIMIT_ACTION) or \
                t.get("share_limits_mode") not in (None, SHARE_LIMITS_MODE):
            have = have + (t.get("share_limit_action"), t.get("share_limits_mode"))
        if have != want:
            groups.setdefault(want, []).append(t["hash"])
            changes.append("qbittorrent.share_limits %s: ratio/seed/inactive %s -> %s"
                           % (t["hash"][:8], have, want))
    return groups, changes


def torrent_list(info):
    """Validate the fields scope decisions use; a bad reply is "could not run"."""
    if not isinstance(info, list) or not all(
            isinstance(t, dict) and isinstance(t.get("hash"), str) and t["hash"]
            and "private" in t and isinstance(t.get("category"), str) for t in info):
        raise PolicyError("qBittorrent torrents/info reply lacks hash/private/category")
    if any(t["private"] is not None and not isinstance(t["private"], bool) for t in info):
        raise PolicyError("qBittorrent torrents/info private field is not a boolean")
    # private is null while a magnet waits for metadata: nothing to limit yet.
    return [t for t in info if t["private"] is not None]


def set_aside_plan(torrents, queued, rules, now=None, checked=None):
    """checked: hashes whose share limits this run reconciled; only they may leave scope."""
    now = time.time() if now is None else now
    hashes, changes = [], []
    for t in torrents:
        if checked is not None and t["hash"] not in checked:
            continue
        # A fresh grab can reach qBittorrent before the arr's next client poll
        # lists it in the queue; moving it then would orphan an active grab.
        # Tagged torrents were added by another tool (alias search, seadexarr)
        # that imports them itself; only a stalled download is a candidate.
        tags = [x.strip() for x in (t.get("tags") or "").split(",") if x.strip()]
        if (t.get("private") and t.get("category") in SET_ASIDE_CATEGORIES
                and not [x for x in tags if x != PRIVATE_TAG]
                and t.get("state") in STALLED_STATES
                and t.get("progress", 1) < 1 and t["hash"].lower() not in queued
                and t.get("added_on", 0) < now - FRESH_GRAB_SECONDS):
            hashes.append(t["hash"])
            changes.append("qbittorrent.category %s: %r -> %r (incomplete, in no arr queue)"
                           % (t["hash"][:8], t["category"], rules["stalled_category"]))
    return hashes, changes


PROWLARR_SEED_FIELDS = tuple("torrentBaseSettings." + k
                             for k in ("seedRatio", "seedTime", "packSeedTime"))


def plan_prowlarr(indexers, rules):
    """Return ([(id, PUT body)], [change lines]) for drifted private torrent indexers."""
    if not isinstance(indexers, list):
        raise PolicyError("prowlarr indexer reply is not a list")
    ops, changes = [], []
    for ix in indexers:
        if ix.get("protocol") != "torrent" or ix.get("privacy") not in ("private",
                                                                         "semiPrivate"):
            continue
        rule = rule_for_indexer(ix["name"], rules)
        if rule is rules["default_private"]:
            # Its hosts are not in Queue Cleaner's ignore list until someone adds them.
            changes.append(FINDING + "prowlarr.%s: private indexer missing from "
                           "tracker-rules.json" % ix["name"])
        goal = seed_goal_minutes(rule)
        want = dict(zip(PROWLARR_SEED_FIELDS, (None, goal, goal)))
        body = copy.deepcopy(ix)
        fields = {f["name"]: f for f in body.get("fields", []) if f["name"] in want}
        if len(fields) != len(want):
            raise PolicyError("prowlarr indexer %s has no torrentBaseSettings seed fields; "
                              "refusing to guess" % ix["name"])
        diff = [n for n in PROWLARR_SEED_FIELDS if fields[n].get("value") != want[n]]
        if diff:
            for n in diff:
                fields[n]["value"] = want[n]
            ops.append((ix["id"], body))
            changes.append("prowlarr.%s: %s changed" % (
                ix["name"], ", ".join(n.split(".", 1)[1] for n in diff)))
    return ops, changes


def queued_hashes(page):
    if not (isinstance(page, dict) and isinstance(page.get("records"), list)
            and type(page.get("totalRecords")) is int and page["totalRecords"] >= 0):
        raise PolicyError("queue reply has no records/totalRecords; refusing to set "
                          "torrents aside")
    records = page["records"]
    if page["totalRecords"] > len(records):
        raise PolicyError("queue has %d records, read %d; refusing to set torrents aside"
                          % (page["totalRecords"], len(records)))
    return {r["downloadId"].lower() for r in records if r.get("downloadId")}


def request(url, headers, method="GET", data=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = resp.read().decode()
    try:
        return json.loads(body) if body.strip() else None
    except ValueError:
        # qBittorrent answers some writes with plain text such as "Ok.".
        return body


def cleanuparr_api(path, key, method="GET", body=None):
    headers = {"X-Api-Key": key, "Content-Type": "application/json"}
    data = None if body is None else json.dumps(body).encode()
    return request(CLEANUPARR_URL + path, headers, method, data)


def prowlarr(path, key, method="GET", body=None):
    headers = {"X-Api-Key": key, "Content-Type": "application/json"}
    data = None if body is None else json.dumps(body).encode()
    return request(PROWLARR_URL + path, headers, method, data)


def cleanuparr(section, key, body=None):
    return cleanuparr_api("/api/configuration/" + section, key,
                          "GET" if body is None else "PUT", body)


def qbt(path, key, form=None):
    headers = {"Authorization": "Bearer " + key}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    return request(QBT_URL + path, headers, "POST" if data else "GET", data)


def arr_queue(base, key, unknown_param):
    # Unknown items stay in the arr queue for a manual import, so they must not move.
    url = "%s/api/v3/queue?pageSize=1000&%s=true" % (base, unknown_param)
    return queued_hashes(request(url, {"X-Api-Key": key}))


def selftest_policy_sections():
    live = {"enabled": True, "ignorePrivate": False, "deleteIfAnyFileBlocked": False,
            "deletePrivate": False, "cronExpression": "0 0/5 * ? * * *",
            "sonarr": {"enabled": True, "blocklistType": "Blacklist",
                       "blocklistPath": "/config/custom-blocklist-sonarr.txt"},
            "radarr": {"enabled": True, "blocklistType": "Blacklist",
                       "blocklistPath": "https://raw.githubusercontent.com/x/blacklist"},
            "lidarr": {"enabled": False, "blocklistType": "Blacklist", "blocklistPath": None},
            "readarr": {"enabled": False, "blocklistType": "Blacklist", "blocklistPath": None}}
    cfg, changes = reconcile("malware_blocker", live)
    assert cfg["ignorePrivate"] and cfg["deleteIfAnyFileBlocked"], cfg
    assert cfg["sonarr"]["blocklistPath"] == BLOCKLIST
    assert cfg["radarr"]["blocklistPath"] == BLOCKLIST
    assert cfg["cronExpression"] == live["cronExpression"], "unlisted keys must survive"
    assert cfg["lidarr"]["blocklistPath"] == BLOCKLIST
    assert cfg["lidarr"]["enabled"] is True
    assert cfg["readarr"] == live["readarr"], "unlisted arrs must survive"
    assert len(changes) == 6, changes
    assert live["sonarr"]["blocklistPath"] != BLOCKLIST, "input must not be mutated"
    assert reconcile("malware_blocker", cfg)[1] == [], "second pass must be a no-op"

    qc = {"enabled": True, "failedImport": {"maxStrikes": 3, "patternMode": "Include",
          "ignorePrivate": False, "deletePrivate": False,
          "patterns": ["Not an upgrade for existing"]}}
    cfg, changes = reconcile("queue_cleaner", qc)
    assert cfg["failedImport"]["patterns"] == ["Not an upgrade for existing", NO_FILES]
    assert len(changes) == 1, changes
    assert reconcile("queue_cleaner", cfg)[1] == []

    flipped = copy.deepcopy(cfg)
    flipped["failedImport"].update(patternMode="Exclude", maxStrikes=0)
    cfg2, changes = reconcile("queue_cleaner", flipped)
    assert cfg2 == cfg and len(changes) == 2, changes

    cfg, changes = reconcile("queue_cleaner", {"failedImport": {"patterns": None}})
    assert cfg["failedImport"]["patterns"] == [NO_FILES], cfg

    cfg, changes = reconcile("blacklist_sync", {"enabled": True, "blacklistPath": "u"})
    assert cfg == {"enabled": False, "blacklistPath": "u"} and len(changes) == 1


def _live_seeding(rules):
    """What GET /api/seeding-rules/{client} returns once the rules are correct."""
    live = [{"id": "pub-%s" % cat, "name": cat, "privacyType": "Public", "categories": [cat],
             "trackerPatterns": [], "tagsAny": [], "tagsAll": [], "priority": i + 1,
             "maxRatio": 1.0, "minSeedTime": 0.0, "maxSeedTime": 400.0, "minSeeders": 0,
             "maxInactiveDays": -1.0, "deleteSourceFiles": True, "action": "Delete"}
            for i, cat in enumerate(rules["categories"])]
    for want in desired_seeding_rules(rules):
        rule = {api_key(k): v for k, v in want.items()}
        rule.update(id="id-" + want["name"], privacyType="Private",
                    deleteSourceFiles=True, action="Delete")
        live.append(rule)
    return live


class FakeCleanuparr:
    """Applies plan_seeding() ops the way SeedingRulesController does."""

    def __init__(self, live):
        self.rules = copy.deepcopy(live)

    def apply(self, ops):
        for method, ident, body in ops:
            if method == "DELETE":
                self.rules = [r for r in self.rules if r["id"] != ident]
            elif method == "PUT":
                rule = next(r for r in self.rules if r["id"] == ident)
                rule.update({k: v for k, v in body.items() if k != "priority"})
            elif method == "POST":
                taken = {r["priority"] for r in self.rules}
                prio = body.get("priority") or max(taken | {0}) + 1
                assert prio not in taken, "Cleanuparr rejects a used priority"
                self.rules.append(dict(body, id="new-" + body["name"], priority=prio))
            elif method == "REORDER":
                assert sorted(ident) == sorted(r["id"] for r in self.rules), "all ids"
                for prio, rid in enumerate(ident, 1):
                    next(r for r in self.rules if r["id"] == rid)["priority"] = prio


def selftest_seeding_rules_exact(rules):
    good = _live_seeding(rules)
    assert plan_seeding(good, rules) == ([], [])

    live = copy.deepcopy(good)
    edited = next(r for r in live if r["name"] == "IPTorrents (tv-sonarr)")
    edited["maxRatio"] = 3.0
    live.append(dict(edited, id="extra", name="tv-sonarr (private)", priority=99))
    live = [r for r in live if r["name"] != "private default (music)"]
    live[0]["privacyType"] = "Both"
    ops, changes = plan_seeding(live, rules)
    assert {(m, i) for m, i, _ in ops} >= {("DELETE", "extra"), ("PUT", edited["id"])}, ops
    assert [b["name"] for m, _, b in ops if m == "POST"] == ["private default (music)"], ops
    assert "seeding_rules.IPTorrents (tv-sonarr): maxRatio changed" in changes, changes
    stopped = copy.deepcopy(good)
    rule = next(r for r in stopped if r["name"] == "private default (radarr)")
    rule.update(action="Stop", deleteSourceFiles=False)
    assert plan_seeding(stopped, rules)[1] == [
        "seeding_rules.private default (radarr): deleteSourceFiles, action changed"]
    leaky = copy.deepcopy(good)
    next(r for r in leaky if r["name"] == "AvistaZ (radarr)")["trackerPatterns"] = [
        "https://tracker.avistaz.to/SECRETPASSKEY/announce"]
    assert not any("SECRETPASSKEY" in c for c in plan_seeding(leaky, rules)[1]), "no live values"
    assert any("tv-sonarr (private)" in c for c in changes), changes
    assert any("Both" in c for c in changes), changes
    put = next(b for m, i, b in ops if i == edited["id"])
    assert put["maxRatio"] == 5.0 and put["action"] == "Delete" and "id" not in put, put
    post = next(b for m, _, b in ops if m == "POST")
    assert post["priority"] == 30 and post["privacyType"] == "Private", post
    assert post["trackerPatterns"] == [] and post["maxSeedTime"] == 2160.0, post
    fake = FakeCleanuparr(live)
    fake.apply(ops)
    assert plan_seeding(fake.rules, rules) == ([], []), "second pass must be a no-op"

    # UI reorder puts the default above a tracker rule: one reorder, then no-op.
    live = copy.deepcopy(good)
    for r in live:
        if r["name"] == "private default (radarr)":
            r["priority"] = 5
    ops, changes = plan_seeding(live, rules)
    assert [m for m, _, _ in ops] == ["REORDER"] and len(changes) == 1, (ops, changes)
    fake = FakeCleanuparr(live)
    fake.apply(ops)
    assert plan_seeding(fake.rules, rules) == ([], [])

    # New rule whose priority is taken: POST without one, reorder on the next pass.
    live = [r for r in copy.deepcopy(good) if r["name"] != "IPTorrents (radarr)"]
    for r in live:
        if r["name"] == "private default (music)":
            r["priority"] = 11
    fake = FakeCleanuparr(live)
    ops, _ = plan_seeding(fake.rules, rules)
    assert [b.get("priority") for m, _, b in ops if m == "POST"] == [None], ops
    fake.apply(ops)
    ops, _ = plan_seeding(fake.rules, rules)
    assert [m for m, _, _ in ops] == ["REORDER"], ops
    fake.apply(ops)
    assert plan_seeding(fake.rules, rules) == ([], [])

    # No public rule to copy client settings from: refuse before any write.
    live = [r for r in copy.deepcopy(good)
            if r["name"] not in ("music", "Mpellas (music)")]
    try:
        plan_seeding(live, rules)
        raise AssertionError("missing template must fail")
    except RuntimeError:
        pass


def selftest_queue_rules():
    live = [{"id": "a", "name": "Stall", "privacyType": "Public", "maxStrikes": 30,
             "deletePrivateTorrentsFromClient": True, "changeCategory": True},
            {"id": "b", "name": "private stalled", "privacyType": "Private", "maxStrikes": 30,
             "deletePrivateTorrentsFromClient": True, "changeCategory": False},
            {"id": "c", "name": "renamed", "privacyType": "Both", "maxStrikes": 3,
             "deletePrivateTorrentsFromClient": False, "changeCategory": True},
            {"id": "d", "name": "ok", "privacyType": "Private", "maxStrikes": 3,
             "deletePrivateTorrentsFromClient": False, "changeCategory": False}]
    ops, changes = plan_queue_rules("stall", live)
    assert [i for _, i, _ in ops] == ["b", "c"] and len(changes) == 2, (ops, changes)
    for _, _, body in ops:
        assert not body["deletePrivateTorrentsFromClient"] and not body["changeCategory"]
        assert body["maxStrikes"] in (30, 3), "every other field is sent back unchanged"
    assert live[1]["deletePrivateTorrentsFromClient"] is True, "input must not be mutated"


def selftest_qbt_globals():
    off = {"excluded_file_names_enabled": False, "max_ratio_enabled": False,
           "max_seeding_time_enabled": False, "max_inactive_seeding_time_enabled": False}
    assert qbt_globals(off) == ({}, [])
    form, changes = qbt_globals(dict(off, max_ratio_enabled=True))
    assert form == {"max_ratio_enabled": False} and len(changes) == 1, (form, changes)
    for flag in QBT_OFF:
        form, changes = qbt_globals(dict(off, **{flag: True}))
        assert form == {flag: False} and len(changes) == 1, flag
    form, changes = qbt_globals({k: True for k in off})
    assert set(form) == set(off) and len(changes) == 4


def _torrent(h, category, private=True, tracker="https://async.empirehost.me/a",
             ratio_limit=1, seeding_time_limit=43200, progress=1, state="stalledDL",
             tags=""):
    return {"hash": h, "category": category, "private": private, "progress": progress,
            "ratio_limit": ratio_limit, "seeding_time_limit": seeding_time_limit,
            "inactive_seeding_time_limit": -2, "urls": [tracker], "state": state,
            "tags": tags}


def _limits(torrents, rules):
    return share_limit_plan(torrents, {t["hash"]: t["urls"] for t in torrents}, rules)


def selftest_per_torrent_limits(rules):
    torrents = [_torrent("a" * 40, "tv-sonarr"), _torrent("b" * 40, "radarr"),
                _torrent("c" * 40, "private-stalled"), _torrent("d" * 40, "ratio-seed"),
                _torrent("e" * 40, "tv-sonarr", private=False)]
    groups, changes = _limits(torrents, rules)
    # private-stalled keeps safe limits so a later completion cannot stop early
    assert groups == {(-1, 131040, -1): ["a" * 40, "b" * 40, "c" * 40]}, groups
    assert len(changes) == 3 and all(c.startswith("qbittorrent.") for c in changes), changes
    assert not any("a" * 9 in c for c in changes), "log hash prefixes only"
    done = [dict(t, ratio_limit=-1, seeding_time_limit=131040, inactive_seeding_time_limit=-1)
            for t in torrents]
    assert _limits(done, rules) == ({}, [])


def selftest_unknown_host(rules):
    t = _torrent("f" * 40, "animes", tracker="https://tracker.example.org/announce")
    groups, _ = _limits([t], rules)
    assert groups == {(-1, 131040, -1): ["f" * 40]}, groups


def selftest_set_aside_stalled(rules):
    stalled = _torrent("A" * 40, "tv-sonarr", progress=0.4)
    hashes, changes = set_aside_plan([stalled], set(), rules)
    assert hashes == ["A" * 40] and len(changes) == 1, (hashes, changes)
    assert "private-stalled" in changes[0] and "A" * 9 not in changes[0], changes
    assert set_aside_plan([stalled], {"a" * 40}, rules) == ([], []), "queued: keep"
    fresh = dict(stalled, added_on=1000)
    assert set_aside_plan([fresh], set(), rules, now=1000 + 600) == ([], []), "fresh grab"
    assert set_aside_plan([fresh], set(), rules, now=1000 + 901)[0] == ["A" * 40]
    others = [_torrent("1" * 40, "radarr", progress=1),
              _torrent("2" * 40, "radarr", private=False, progress=0.4),
              _torrent("3" * 40, "animes", progress=0.4),
              _torrent("4" * 40, "music", progress=0.4),
              _torrent("5" * 40, "private-stalled", progress=0.4)]
    assert set_aside_plan(others, set(), rules) == ([], [])


def selftest_arr_queue():
    page = {"totalRecords": 2, "records": [{"downloadId": "ABC"}, {"downloadId": None}]}
    assert queued_hashes(page) == {"abc"}
    try:
        queued_hashes({"totalRecords": 1001, "records": [{"downloadId": "A"}] * 1000})
        raise AssertionError("a truncated queue must fail the step")
    except RuntimeError:
        pass
    for bad in ({}, None, "Ok.", {"records": None, "totalRecords": 0},
                {"records": [], "totalRecords": "0"}, {"records": [], "totalRecords": False},
                {"records": [], "totalRecords": -1}):
        try:
            queued_hashes(bad)
            raise AssertionError("an invalid queue reply must fail the step: %r" % (bad,))
        except RuntimeError:
            pass


def _indexer(ident, name, privacy, ratio=1, seed=43200, pack=None, protocol="torrent"):
    fields = [{"name": "baseUrl", "value": "https://example.org/"},
              {"name": "username", "value": "someone"},
              {"name": "password", "value": "********", "privacy": "password"},
              {"name": "torrentBaseSettings.appMinimumSeeders"},
              {"name": "torrentBaseSettings.seedRatio", "value": ratio},
              {"name": "torrentBaseSettings.seedTime", "value": seed},
              {"name": "torrentBaseSettings.packSeedTime", "value": pack},
              {"name": "torrentBaseSettings.preferMagnetUrl", "value": False}]
    return {"id": ident, "name": name, "privacy": privacy, "protocol": protocol,
            "implementation": "Cardigann", "enable": True, "tags": [1],
            "fields": [{k: v for k, v in f.items() if v is not None} for f in fields]}


def _seed_values(body):
    return {f["name"].split(".", 1)[1]: f.get("value") for f in body["fields"]
            if f["name"] in PROWLARR_SEED_FIELDS}


def selftest_prowlarr_private_goals(rules):
    live = [_indexer(1, "IPTorrents", "private"),
            _indexer(2, "PublicOne", "public"),
            _indexer(3, "NewPrivate", "semiPrivate"),
            _indexer(4, "SomeUsenet", "private", protocol="usenet")]
    ops, changes = plan_prowlarr(live, rules)
    assert [i for i, _ in ops] == [1, 3], ops
    assert _seed_values(ops[0][1]) == {"seedRatio": None, "seedTime": 131040,
                                       "packSeedTime": 131040}, ops[0][1]
    assert _seed_values(ops[1][1]) == {"seedRatio": None, "seedTime": 131040,
                                       "packSeedTime": 131040}, ops[1][1]
    assert changes == ["prowlarr.IPTorrents: seedRatio, seedTime, packSeedTime changed",
                       FINDING + "prowlarr.NewPrivate: private indexer missing from tracker-rules.json",
                       "prowlarr.NewPrivate: seedRatio, seedTime, packSeedTime changed"], changes
    assert _seed_values(live[0])["seedRatio"] == 1, "input must not be mutated"
    done = [body for _, body in ops] + live[1:2]
    assert plan_prowlarr(done, rules) == (
        [], [FINDING + "prowlarr.NewPrivate: private indexer missing from tracker-rules.json"]), \
        "second pass writes nothing; the missing indexer stays a finding"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert run_steps([("prowlarr", lambda: [FINDING + "prowlarr.X: missing"], True)]) == 1
    assert "FINDING (not corrected): prowlarr.X: missing" in out.getvalue(), out.getvalue()
    assert "back to policy" not in out.getvalue(), out.getvalue()
    partly = [b for _, b in ops][:1]
    next(f for f in partly[0]["fields"]
         if f["name"] == "torrentBaseSettings.seedTime")["value"] = 60
    assert plan_prowlarr(partly, rules)[1] == ["prowlarr.IPTorrents: seedTime changed"]
    broken = _indexer(5, "Odd", "private")
    broken["fields"] = broken["fields"][:3]
    try:
        plan_prowlarr([broken], rules)
        raise AssertionError("a private torrent indexer without seed fields must fail")
    except RuntimeError:
        pass


def selftest_prowlarr_body_untouched(rules):
    live = _indexer(1, "AvistaZ", "private", ratio=2.5, seed=10, pack=20)
    (_, body), = plan_prowlarr([live], rules)[0]
    strip = lambda b: dict(b, fields=[f for f in b["fields"]  # noqa: E731
                                      if f["name"] not in PROWLARR_SEED_FIELDS])
    assert json.dumps(strip(body)) == json.dumps(strip(live)), body
    assert [f["name"] for f in body["fields"]] == [f["name"] for f in live["fields"]]
    assert any(f.get("value") == "********" for f in body["fields"]), "mask sent back as-is"
    assert _seed_values(body) == {"seedRatio": None, "seedTime": 131040,
                                  "packSeedTime": 131040}, body
    assert not any("someone" in c or "****" in c for c in plan_prowlarr([live], rules)[1])


def selftest_step_isolation():
    ran = []

    def broken():
        raise urllib.error.URLError("refused")

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        steps = [("broken", broken, True),
                 ("after", lambda: ran.append(1) or ["x: 1 -> 2"], True)]
        assert run_steps(steps) == 2 and ran == [1]
        assert run_steps([("ok", lambda: [], True)]) == 0
        assert run_steps([("drift", lambda: ["x: 1 -> 2"], True)]) == 1
        assert run_steps([("routine", lambda: ["x: 1 -> 2"], False)]) == 0
        assert run_steps([("gated", lambda: ["x: 1 -> 2"], True, True)]) == 0, "pending"
    assert "ERROR broken:" in err.getvalue() and "DRIFT corrected: x: 1 -> 2" in out.getvalue()
    assert "SET corrected: x: 1 -> 2" in out.getvalue()
    assert "PENDING (SEEDING_POLICY not enforce): x: 1 -> 2" in out.getvalue()


def selftest_category_limits(rules):
    safe = {"radarr": {"name": "radarr", "save_path": "/d"}, "tv-sonarr": {
        "ratio_limit": -2, "seeding_time_limit": -2, "inactive_seeding_time_limit": -2,
        "share_limit_action": "Default", "share_limits_mode": "Default"}}
    assert plan_category_limits(safe, rules) == ([], [])
    unsafe = dict(safe, radarr={"inactive_seeding_time_limit": 10,
                                "share_limit_action": "RemoveWithContent"})
    ops, changes = plan_category_limits(unsafe, rules)
    assert ops == [{"category": "radarr", "ratioLimit": -2, "seedingTimeLimit": -2,
                    "inactiveSeedingTimeLimit": -2, "shareLimitAction": "Default",
                    "shareLimitsMode": "Default"}], ops
    assert changes == ["qbittorrent.category radarr: inactive_seeding_time_limit, "
                       "share_limit_action changed to inherit global"], changes


def selftest_private_guard(rules):
    cfg, changes = plan_queue_ignore({"ignoredDownloads": ["mine"], "enabled": True}, rules)
    assert cfg["ignoredDownloads"][0] == "mine" and PRIVATE_TAG in cfg["ignoredDownloads"]
    assert "tracker.avistaz.to" in cfg["ignoredDownloads"] and cfg["enabled"] is True
    assert plan_queue_ignore(cfg, rules) == (None, []), "second pass no-op"
    torrents = [{"hash": "a" * 40, "private": True, "tags": ""},
                {"hash": "b" * 40, "private": True, "tags": "x, private-guard"},
                {"hash": "c" * 40, "private": False, "tags": ""}]
    assert plan_private_tags(torrents)[0] == ["a" * 40]
    guarded = dict(_torrent("A" * 40, "tv-sonarr", progress=0.4), tags=PRIVATE_TAG)
    assert set_aside_plan([guarded], set(), rules)[0] == ["A" * 40], "guard tag is not a tool tag"


def selftest_final_review_fixes(rules):
    form = share_limits_form(["a" * 40, "b" * 40], -1, 131040, -1)
    assert form == {"hashes": "a" * 40 + "|" + "b" * 40, "ratioLimit": -1,
                    "seedingTimeLimit": 131040, "inactiveSeedingTimeLimit": -1,
                    "shareLimitAction": "Stop", "shareLimitsMode": "MatchAny"}, form
    removing = dict(_torrent("a" * 40, "radarr"), ratio_limit=-1, seeding_time_limit=131040,
                    inactive_seeding_time_limit=-1, share_limit_action="RemoveWithContent",
                    share_limits_mode="MatchAny")
    assert _limits([removing], rules)[0] == {(-1, 131040, -1): ["a" * 40]}, "action drift"
    fresh = dict(_torrent("f" * 40, "radarr"), ratio_limit=-2, seeding_time_limit=131040,
                 inactive_seeding_time_limit=-2, share_limit_action="Default",
                 share_limits_mode="Default")
    assert _limits([fresh], rules)[0] == {(-1, 131040, -1): ["f" * 40]}, "fresh grab"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert run_steps([("share_limits", lambda: ["x"], False, False)]) == 0
    stalled = _torrent("A" * 40, "tv-sonarr", progress=0.4)
    for keep in (dict(stalled, tags="alias-search-526"), dict(stalled, state="downloading")):
        assert set_aside_plan([keep], set(), rules) == ([], []), keep
    assert set_aside_plan([stalled], set(), rules, checked=set()) == ([], []), "unchecked"
    assert set_aside_plan([stalled], set(), rules, checked={"A" * 40})[0] == ["A" * 40]
    assert torrent_list([{"hash": "a", "private": None, "category": "radarr"}]) == []
    for bad in ([{}], [{"hash": "a", "private": "yes", "category": "radarr"}], {}, None):
        try:
            torrent_list(bad)
            raise AssertionError("bad torrents/info must fail: %r" % (bad,))
        except PolicyError:
            pass
    try:
        plan_prowlarr({}, rules)
        raise AssertionError("a non-list Prowlarr reply must fail")
    except PolicyError:
        pass
    _, changes = merge({"radarr": {"blocklistPath": "https://x/list?token=SECRET"}},
                       {"radarr": {"blocklistPath": BLOCKLIST}})
    assert changes and not any("SECRET" in c for c in changes), changes
    cfg, changes = reconcile("queue_cleaner", {"failedImport": {"patterns": [NO_FILES]},
                                               "errored": {"deletePrivate": True}},
                             SEEDING_DESIRED)
    assert cfg["errored"]["deletePrivate"] is False, cfg
    assert cfg["downloadingMetadataMaxStrikes"] == 0, cfg


def selftest():
    rules = load_rules(RULES)
    selftest_policy_sections()
    selftest_seeding_rules_exact(rules)
    selftest_queue_rules()
    selftest_qbt_globals()
    selftest_per_torrent_limits(rules)
    selftest_unknown_host(rules)
    selftest_set_aside_stalled(rules)
    selftest_arr_queue()
    selftest_prowlarr_private_goals(rules)
    selftest_prowlarr_body_untouched(rules)
    selftest_step_isolation()
    selftest_final_review_fixes(rules)
    selftest_private_guard(rules)
    selftest_category_limits(rules)
    print("selftest OK")
    return 0


# A change line with this prefix is reported, not corrected: it needs a Git change.
FINDING = "FINDING "


def run_steps(steps):
    """steps: [(name, fn returning change lines, counts as drift)]. Returns the exit code."""
    verb = "would correct (DRY_RUN)" if DRY_RUN else "corrected"
    drift, failed, pending_lines, findings = 0, 0, 0, 0
    for name, step, is_drift, *gated in steps:
        pending = bool(gated and gated[0])
        # One broken step must not hide the others, and any failure is exit 2.
        try:
            changes = step()
        except urllib.error.HTTPError as exc:
            # Status only: a response body or redirect URL can echo a key.
            print("ERROR %s: HTTP %d" % (name, exc.code), file=sys.stderr)
            failed += 1
            continue
        except (urllib.error.URLError, TimeoutError) as exc:
            print("ERROR %s: %s" % (name, type(exc).__name__), file=sys.stderr)
            failed += 1
            continue
        except PolicyError as exc:
            print("ERROR %s: %s" % (name, exc), file=sys.stderr)
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 - anything else is also "could not run"
            # Type only: messages such as "Invalid header value" embed the key.
            print("ERROR %s: %s" % (name, type(exc).__name__), file=sys.stderr)
            failed += 1
            continue
        for line in changes:
            if pending:
                print("PENDING (SEEDING_POLICY not enforce): %s" % line)
            elif line.startswith(FINDING):
                print("FINDING (not corrected): %s" % line[len(FINDING):])
                findings += 1
            else:
                print("%s %s: %s" % ("DRIFT" if is_drift else "SET", verb, line))
        if is_drift and not pending:
            drift += len(changes)
        if pending:
            pending_lines += len(changes)

    if failed:
        print("FAIL: %d step(s) could not run, see ERROR lines above." % failed)
        return 2
    if not drift:
        print("OK: no drift outside the seeding policy." if pending_lines else
              "OK: Cleanuparr, qBittorrent and Prowlarr match the download policy.")
        if pending_lines:
            print("%d seeding-policy change(s) PENDING until SEEDING_POLICY=enforce."
                  % pending_lines)
        return 0
    if findings == drift:
        print("FINDING lines need a change in "
              "2-k3s/08.servarr/cleanuparr/files/tracker-rules.json via branch+PR.")
        return 1
    print("%s; if the change was wanted, edit"
          % ("DRY_RUN: nothing was written. These differ from policy" if DRY_RUN else
             "Someone or something changed these outside git, or a private download was "
             "set aside for a manual decision. They are back to policy")
          + " 2-k3s/maintenance/files/cleanuparr-policy.py or "
          "2-k3s/08.servarr/cleanuparr/files/tracker-rules.json via branch+PR.")
    return 1


def main():
    if "--selftest" in sys.argv:
        return selftest()
    keys = {name: os.environ.get(name, "").strip() for name in (
        "CLEANUPARR_API_KEY", "QBT_WEBAPI_KEY", "RADARR_API_KEY", "SONARR_API_KEY",
        "PROWLARR_API_KEY")}
    required = ("CLEANUPARR_API_KEY", "QBT_WEBAPI_KEY")
    if not all(keys[k] for k in required):
        print("ERROR: empty: %s" % ", ".join(k for k in required if not keys[k]),
              file=sys.stderr)
        return 2
    cu_key, qbt_key = keys["CLEANUPARR_API_KEY"], keys["QBT_WEBAPI_KEY"]
    WRITE = SEEDING_ENFORCED and not DRY_RUN
    try:
        rules = load_rules(RULES)
    except (OSError, ValueError) as exc:
        print("ERROR: cannot read %s: %s" % (RULES, type(exc).__name__), file=sys.stderr)
        return 2

    def qbt_step(flags):
        form, changes = qbt_globals(qbt("/api/v2/app/preferences", qbt_key), flags)
        if form and (WRITE if flags != QBT_EXISTING else not DRY_RUN):
            qbt("/api/v2/app/setPreferences", qbt_key, {"json": json.dumps(form)})
        return changes

    def errored_step():
        cfg, changes = reconcile("queue_cleaner", cleanuparr("queue_cleaner", cu_key),
                                 SEEDING_DESIRED)
        if changes and WRITE:
            cleanuparr("queue_cleaner", cu_key, cfg)
        return changes

    def queue_ignore_step():
        cfg, changes = plan_queue_ignore(cleanuparr("queue_cleaner", cu_key), rules)
        if cfg and WRITE:
            cleanuparr("queue_cleaner", cu_key, cfg)
        return changes

    def category_limits_step():
        categories = qbt("/api/v2/torrents/categories", qbt_key)
        if not isinstance(categories, dict):
            raise PolicyError("qBittorrent categories reply is not an object")
        ops, changes = plan_category_limits(categories, rules)
        for form in ops if WRITE else []:
            qbt("/api/v2/torrents/editCategory", qbt_key, form)
        return changes

    def private_tag_step():
        hashes, changes = plan_private_tags(torrent_list(qbt("/api/v2/torrents/info", qbt_key)))
        if hashes and WRITE:
            qbt("/api/v2/torrents/addTags", qbt_key,
                {"hashes": "|".join(hashes), "tags": PRIVATE_TAG})
        return changes

    def cleanuparr_step(section):
        cfg, changes = reconcile(section, cleanuparr(section, cu_key))
        if changes and not DRY_RUN:
            cleanuparr(section, cu_key, cfg)
        return changes

    def seeding_step():
        clients = [c["id"] for c in cleanuparr_api("/api/configuration/download_client",
                                                   cu_key)["clients"]
                   if c.get("typeName") == "qBittorrent"]
        if not clients:
            raise PolicyError("Cleanuparr has no qBittorrent download client")
        changes = []
        for client in clients:
            # A created rule may need a reorder, which the next pass plans.
            for _ in range(3):
                live = cleanuparr_api("/api/seeding-rules/%s" % client, cu_key)
                ops, found = plan_seeding(live, rules)
                changes += found
                if not ops or not WRITE:
                    break
                for method, ident, body in ops:
                    if method == "POST":
                        cleanuparr_api("/api/seeding-rules/%s" % client, cu_key, "POST", body)
                    elif method == "REORDER":
                        cleanuparr_api("/api/seeding-rules/%s/reorder" % client, cu_key, "PUT",
                                       {"orderedIds": ident})
                    else:
                        cleanuparr_api("/api/seeding-rules/" + urllib.parse.quote(ident),
                                       cu_key, method, body)
        return changes

    def queue_rules_step():
        changes = []
        for kind in ("stall", "slow"):
            ops, found = plan_queue_rules(kind, cleanuparr_api("/api/queue-rules/" + kind, cu_key))
            changes += found
            for method, ident, body in ops if WRITE else []:
                cleanuparr_api("/api/queue-rules/%s/%s" % (kind, urllib.parse.quote(ident)),
                              cu_key, method, body)
        return changes

    limits = {"done": False, "hashes": set()}

    def set_aside_step():
        if not limits["done"]:
            raise PolicyError("share limits were not reconciled this run; not moving "
                              "torrents out of scope")
        if not (keys["RADARR_API_KEY"] and keys["SONARR_API_KEY"]):
            raise PolicyError("RADARR_API_KEY or SONARR_API_KEY is empty")
        torrents = torrent_list(qbt("/api/v2/torrents/info", qbt_key))
        queued = (arr_queue(RADARR_URL, keys["RADARR_API_KEY"], "includeUnknownMovieItems")
                  | arr_queue(SONARR_URL, keys["SONARR_API_KEY"], "includeUnknownSeriesItems"))
        hashes, changes = set_aside_plan(torrents, queued, rules, checked=limits["hashes"])
        if hashes and WRITE:
            category = rules["stalled_category"]
            if category not in qbt("/api/v2/torrents/categories", qbt_key):
                try:
                    qbt("/api/v2/torrents/createCategory", qbt_key,
                        {"category": category, "savePath": ""})
                except urllib.error.HTTPError as exc:
                    if exc.code != 409:
                        raise
            qbt("/api/v2/torrents/setCategory", qbt_key,
                {"hashes": "|".join(hashes), "category": category})
        return changes

    def share_limits_step():
        torrents = [t for t in torrent_list(qbt("/api/v2/torrents/info", qbt_key))
                    if t["private"] and t["category"] in limit_categories(rules)]
        urls = {}
        for t in torrents:
            try:
                urls[t["hash"]] = [tr["url"] for tr in qbt(
                    "/api/v2/torrents/trackers?hash=" + t["hash"], qbt_key)]
            except urllib.error.HTTPError as exc:
                # Removed between the two reads (404); nothing left to limit.
                if exc.code != 404:
                    raise
        torrents = [t for t in torrents if t["hash"] in urls]
        groups, changes = share_limit_plan(torrents, urls, rules)
        for (ratio, seed, inactive), hashes in groups.items() if WRITE else []:
            qbt("/api/v2/torrents/setShareLimits", qbt_key,
                share_limits_form(hashes, ratio, seed, inactive))
        limits.update(done=True, hashes={t["hash"] for t in torrents})
        return changes

    def prowlarr_step():
        key = keys["PROWLARR_API_KEY"]
        if not key:
            raise PolicyError("PROWLARR_API_KEY is empty")
        ops, changes = plan_prowlarr(prowlarr("/api/v1/indexer", key), rules)
        for ident, body in ops if WRITE else []:
            # forceSave skips the live indexer test, so a tracker outage cannot block
            # a settings write; validation errors still reject it.
            prowlarr("/api/v1/indexer/%d?forceSave=true" % ident, key, "PUT", body)
        return changes

    gated = not SEEDING_ENFORCED
    return run_steps(
        [("qbittorrent", lambda: qbt_step(QBT_EXISTING), True)]
        + [(s, lambda s=s: cleanuparr_step(s), True) for s in DESIRED]
        + [(n, fn, drift, gated) for n, fn, drift in (
            ("qbittorrent_share_globals",
             lambda: qbt_step(tuple(k for k in QBT_OFF if k not in QBT_EXISTING)), True),
            ("queue_cleaner_errored", errored_step, True),
            ("queue_cleaner_ignore", queue_ignore_step, True),
            ("qbittorrent_category_limits", category_limits_step, True),
            # Routine: every new private grab needs the tag.
            ("private_tag", private_tag_step, False),
            ("seeding_rules", seeding_step, True), ("queue_rules", queue_rules_step, True),
            # Routine, not drift: Radarr and Sonarr cannot send "no ratio limit", so
            # every new private grab arrives with ratio -2 and needs this write.
            ("share_limits", share_limits_step, False),
            ("set_aside", set_aside_step, True), ("prowlarr", prowlarr_step, True))])


if __name__ == "__main__":
    sys.exit(main())
