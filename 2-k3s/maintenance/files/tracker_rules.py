#!/usr/bin/env python3
"""Read the Git-owned tracker-rule table (tracker-rules.json) and classify torrents.

rule_for() classifies a tracker URL the way Cleanuparr 2.11.1 does, so the
limits set here match the rule Cleanuparr applies: the URL's host, compared
case-insensitively, ends with one of a tracker's hosts, with no dot boundary;
trackers are tried in table order and anything unmatched gets the private
default. Stdlib only. Run with --selftest.
"""
import json
import os
import sys
import urllib.parse


def load_rules(path):
    with open(path) as f:
        return json.load(f)


def rule_for(tracker_url, rules):
    try:
        host = urllib.parse.urlsplit(tracker_url).hostname or ""
    except ValueError:
        host = ""
    for tracker in rules["trackers"]:
        if host and any(host.endswith(h.lower()) for h in tracker["hosts"]):
            return tracker
    return rules["default_private"]


def rule_for_trackers(tracker_urls, rules):
    """Cleanuparr matches a rule when any of the torrent's trackers matches it."""
    order = rules["trackers"] + [rules["default_private"]]
    return min((rule_for(u, rules) for u in tracker_urls), key=order.index,
               default=rules["default_private"])


def rule_for_indexer(name, rules):
    return next((t for t in rules["trackers"] if t["prowlarr"] == name),
                rules["default_private"])


def seed_goal_minutes(rule):
    return (rule["cleanuparr"]["max_seed_hours"] + 24) * 60


def hnr_met(torrent, rule):
    """True when the client's ratio or seeding time meets the rule; None without a rule."""
    hnr = rule["hnr"]
    if hnr is None:
        return None
    hours = hnr["hours"]
    if "large_gb" in hnr:
        # total_size is the release size; "size" counts only selected files.
        # AvistaZ states sizes in decimal GB.
        size_gb = torrent.get("total_size", torrent["size"]) / 1e9
        hours = (hours + hnr["hours_per_gb"] * size_gb if size_gb < hnr["large_gb"]
                 else hnr["large_hours"])
    # ratio null: an unreviewed tracker may require seed time whatever the ratio.
    return ((hnr["ratio"] is not None and torrent["ratio"] >= hnr["ratio"])
            or torrent["seeding_time"] / 3600 >= hours)


def selftest():
    rules = load_rules(os.environ.get("TRACKER_RULES", "/rules/tracker-rules.json"))
    by_name = {t["name"]: t for t in rules["trackers"]}
    default = rules["default_private"]

    assert rule_for("https://routing.bgp.technology/abc/announce", rules) is by_name["IPTorrents"]
    assert rule_for("http://TD.JumboHostPro.EU:2710/x/announce", rules) is by_name["TorrentDay"]
    assert rule_for("https://tracker.avistaz.to/k/announce", rules) is by_name["AvistaZ"]
    assert rule_for("https://magtre.com/announce.php?passkey=x", rules) is by_name["Mpellas"]
    assert rule_for("udp://tracker.example.org:1337/announce", rules) is default
    assert rule_for("** [DHT] **", rules) is default
    assert rule_for("", rules) is default
    assert rule_for("http://[bad/announce", rules) is default
    # Cleanuparr has no dot boundary either; torrent-hnr-check reports these hosts.
    assert rule_for("https://notmagtre.com/announce", rules) is by_name["Mpellas"]
    partial = {"ratio": 0, "seeding_time": 200 * 3600, "size": 40 * 2**30,
               "total_size": 60 * 2**30}
    assert hnr_met(partial, by_name["AvistaZ"]) is False, "60 GB release needs 1440 h"
    # Every private Cleanuparr rule waits default hours before ratio 5 can delete;
    # a tracker needing more seed time than that would need a higher minimum.
    for t in rules["trackers"]:
        if t["hnr"]:
            need = max(t["hnr"]["hours"], t["hnr"].get("large_hours", 0))
            assert need <= default["hnr"]["hours"], (t["name"], need)
    one_hour = {"ratio": 1.0, "seeding_time": 3600, "size": 2**30}
    assert hnr_met(one_hour, default) is False, "unknown tracker: time only"
    assert hnr_met(dict(one_hour, seeding_time=1440 * 3600), default) is True

    assert rule_for_trackers([], rules) is default
    assert rule_for_trackers(["** [DHT] **", "https://magtre.com/a",
                              "https://async.empirehost.me/a"], rules) is by_name["IPTorrents"]

    assert rule_for_indexer("IPTorrents", rules) is by_name["IPTorrents"]
    assert rule_for_indexer("Mpellas", rules) is by_name["Mpellas"]
    assert rule_for_indexer("iptorrents", rules) is default
    assert rule_for_indexer("SomeNewTracker", rules) is default

    assert seed_goal_minutes(by_name["IPTorrents"]) == 131040
    assert seed_goal_minutes(by_name["AvistaZ"]) == 131040
    assert seed_goal_minutes(default) == 131040

    def t(ratio=0.0, hours=0.0, gb=1):
        return {"ratio": ratio, "seeding_time": hours * 3600, "size": gb * 10**9}
    assert hnr_met(t(ratio=1.0), by_name["IPTorrents"]) is True
    assert hnr_met(t(ratio=0.99, hours=335.9), by_name["IPTorrents"]) is False
    assert hnr_met(t(hours=336), by_name["IPTorrents"]) is True
    assert hnr_met(t(hours=91.9, gb=10), by_name["AvistaZ"]) is False
    assert hnr_met(t(hours=92, gb=10), by_name["AvistaZ"]) is True
    assert hnr_met(t(hours=1439, gb=60), by_name["AvistaZ"]) is False
    assert hnr_met(t(hours=1440, gb=60), by_name["AvistaZ"]) is True
    assert hnr_met(t(ratio=0.9, gb=60), by_name["AvistaZ"]) is True
    assert hnr_met(t(), by_name["Mpellas"]) is None
    assert hnr_met(t(hours=1439), default) is False
    print("selftest OK")
    return 0


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else 0)
