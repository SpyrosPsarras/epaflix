#!/usr/bin/env python3
"""Private torrent hit-and-run check. Stdlib only. Run with --selftest.

--mode early-stop: a private torrent in a Cleanuparr category is stopped
  before the hit-and-run rule of every tracker on it is met, or has a host
  that only looks like a listed host, or mixes listed and unlisted hosts
  (an unlisted host is outside Queue Cleaner's ignore list until the hourly
  private-guard tag; add it to tracker-rules.json).
--mode tracker: for some tracker on a private torrent in a Cleanuparr
  category, a URL fails (status 4, 5 or 6) and none works, so that site
  counts no seed time.

Read-only: POST /api/v2/auth/login, GET /api/v2/torrents/info and
/api/v2/torrents/trackers. Logs hash prefix, category and reason only.
Exit 0 = clean, 1 = findings, 2 = could not run.
"""
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from tracker_rules import hnr_met, load_rules

TIMEOUT = 30
QBT_URL = os.environ.get("QBT_URL", "http://qbittorrent:8080").rstrip("/")
RULES = os.environ.get("TRACKER_RULES", "/rules/tracker-rules.json")
STOPPED = {"stoppedUP", "stoppedDL", "pausedUP", "pausedDL"}
# qBittorrent 5.2+: 4 not working, 5 tracker error, 6 unreachable.
NOT_WORKING = (4, 5, 6)


def fail(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(2)


def http_status(exc):
    """Status code only: a response body can carry a key."""
    return str(exc.code) if isinstance(exc, urllib.error.HTTPError) else "-"


def qbt_login():
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    body = urllib.parse.urlencode({
        "username": os.environ.get("QBT_USERNAME", ""),
        "password": os.environ.get("QBT_PASSWORD", ""),
    }).encode()
    req = urllib.request.Request(QBT_URL + "/api/v2/auth/login", data=body,
                                 headers={"Referer": QBT_URL})
    try:
        answer = opener.open(req, timeout=TIMEOUT).read().decode().strip()
    except Exception as exc:  # noqa: BLE001
        fail("qBittorrent login request failed (%s, http=%s)"
             % (type(exc).__name__, http_status(exc)))
    if answer and answer != "Ok.":
        fail("qBittorrent login rejected")
    return opener


class Gone(Exception):
    """The torrent was removed between the list and tracker reads."""


def qbt_get(opener, path, gone_ok=False):
    try:
        with opener.open(QBT_URL + path, timeout=TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if gone_ok and exc.code == 404:
            raise Gone() from None
        fail("qBittorrent %s failed (%s, http=%s)"
             % (path.split("?")[0], type(exc).__name__, http_status(exc)))
    except Exception as exc:  # noqa: BLE001
        fail("qBittorrent %s failed (%s, http=%s)"
             % (path.split("?")[0], type(exc).__name__, http_status(exc)))
    if not isinstance(data, list):
        fail("qBittorrent %s was not a JSON array" % path.split("?")[0])
    return data


def in_scope(t, rules):
    return bool(t.get("private")) and t.get("category") in rules["categories"]


def real_trackers(trackers):
    return [tr for tr in trackers if not str(tr.get("url", "")).startswith("** [")]


def host_of(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_lookalike(host, rules):
    listed = [h.lower() for t in rules["trackers"] for h in t["hosts"]]
    return any(host.endswith(h) and not (host == h or host.endswith("." + h))
               for h in listed)


def tracker_of(host, rules):
    """The listed tracker this host belongs to (equal or subdomain), else None."""
    return next((t for t in rules["trackers"] for h in t["hosts"]
                 if host == h.lower() or host.endswith("." + h.lower())), None)


def early_stop_findings(torrents, trackers_by_hash, rules):
    """[(hash, reason)] for stopped-early, lookalike or partly unknown private torrents."""
    found = []
    for t in torrents:
        if not in_scope(t, rules):
            continue
        urls = [tr["url"] for tr in real_trackers(trackers_by_hash.get(t["hash"], []))]
        hosts = [host_of(u) for u in urls]
        owners = [tracker_of(h, rules) for h in hosts]
        if any(is_lookalike(h, rules) for h in hosts):
            found.append((t["hash"], "lookalike host"))
        elif any(owners) and not all(owners):
            # The unknown host is unreviewed: its tracker's hit-and-run rule is not in
            # tracker-rules.json yet.
            found.append((t["hash"], "known and unknown tracker hosts"))
        elif t.get("state") in STOPPED:
            # Every tracker on the torrent must be satisfied; unknown hosts use the
            # private default.
            applicable = {id(o): o for o in owners if o} or {0: rules["default_private"]}
            if any(hnr_met(t, r) is False for r in applicable.values()):
                found.append((t["hash"], "%s before hit-and-run rule (ratio %.2f, seeded %.1f h)"
                              % (t["state"], t["ratio"], t["seeding_time"] / 3600)))
    return found


def tracker_findings(torrents, trackers_by_hash, rules):
    found = []
    for t in torrents:
        if not in_scope(t, rules):
            continue
        groups = {}
        for tr in real_trackers(trackers_by_hash.get(t["hash"], [])):
            owner = tracker_of(host_of(tr.get("url", "")), rules)
            groups.setdefault(owner["name"] if owner else host_of(tr.get("url", "")),
                              []).append(tr.get("status"))
        # Per tracker: a backup URL of the same tracker may cover a failing one, a
        # different tracker may not; a never-contacted URL (1) covers nothing.
        if any(any(s in NOT_WORKING for s in st) and 2 not in st for st in groups.values()):
            found.append((t["hash"], "no tracker working"))
    return found


def report_lines(torrents, found):
    category = {t["hash"]: t.get("category", "") for t in torrents}
    return ["    %-8s  %-10s  %s" % (h[:8], category[h], reason) for h, reason in found]


def selftest_boundaries(rules):
    names = {t["name"]: t for t in rules["trackers"]}

    def met(name, ratio=0.0, hours=0.0, gb=1):
        return hnr_met({"ratio": ratio, "seeding_time": hours * 3600, "size": gb * 10**9},
                       names[name])
    assert met("IPTorrents", ratio=1.0) is True
    assert met("IPTorrents", ratio=0.99, hours=335.9) is False
    assert met("IPTorrents", ratio=0.99, hours=336.0) is True
    assert met("TorrentDay", hours=72) is True
    assert met("AvistaZ", hours=91.9, gb=10) is False
    assert met("AvistaZ", hours=92, gb=10) is True
    assert met("AvistaZ", hours=1439.9, gb=60) is False
    assert met("AvistaZ", hours=1440, gb=60) is True
    assert met("AvistaZ", ratio=0.9, gb=60) is True
    assert met("Mpellas") is None


MALFORMED = (KeyError, TypeError, AttributeError, ValueError)


def scan(info, fetch_trackers, rules, check):
    """Return (in-scope torrents, findings); raises MALFORMED on a bad reply."""
    if not isinstance(info, list) or not all(
            isinstance(t, dict) and isinstance(t.get("hash"), str) and t["hash"]
            and isinstance(t.get("private", 0), (bool, type(None)))
            and "private" in t and isinstance(t.get("category"), str)
            for t in info):
        raise TypeError("torrents/info lacks hash/private/category")
    # private is null while a magnet waits for metadata; in_scope skips it.
    torrents = [t for t in info if in_scope(t, rules)]
    trackers = {}
    for t in torrents:
        try:
            trackers[t["hash"]] = fetch_trackers(t["hash"])
        except Gone:
            pass
    # A torrent removed mid-scan (e.g. a finished cleanup) has nothing left to check.
    torrents = [t for t in torrents if t["hash"] in trackers]
    return torrents, check(torrents, trackers, rules)


def _torrent(h, state="stoppedUP", category="radarr", private=True, ratio=0.1, hours=1.0):
    return {"hash": h, "state": state, "category": category, "private": private,
            "ratio": ratio, "seeding_time": hours * 3600, "size": 2**30,
            "name": "Secret.Name.2026"}


def _trackers(url="https://async.empirehost.me/a", status=2):
    return [{"url": "** [DHT] **", "status": 0}, {"url": "** [PeX] **", "status": 0},
            {"url": url, "status": status}]


def selftest_early_stop(rules):
    stopped = [_torrent(c * 40, state=s) for c, s in
               zip("1234", ("pausedDL", "pausedUP", "stoppedDL", "stoppedUP"))]
    others = [_torrent("5" * 40, state="stalledUP"),
              _torrent("6" * 40, category="private-stalled"),
              _torrent("7" * 40, category="ratio-seed"),
              _torrent("8" * 40, private=False),
              _torrent("9" * 40, hours=336)]
    torrents = stopped + others
    found = early_stop_findings(torrents, {t["hash"]: _trackers() for t in torrents}, rules)
    assert [h for h, _ in found] == [t["hash"] for t in stopped], found


def selftest_unknown_host(rules):
    unknown = _trackers("udp://tracker.example.org:1337/announce")
    found = early_stop_findings([_torrent("a" * 40, hours=1439)], {"a" * 40: unknown}, rules)
    assert [h for h, _ in found] == ["a" * 40], found
    found = early_stop_findings([_torrent("a" * 40, hours=1440)], {"a" * 40: unknown}, rules)
    assert found == [], found


def selftest_tracker_mode(rules):
    torrents = [_torrent("a" * 40), _torrent("b" * 40), _torrent("c" * 40, private=False),
                _torrent("d" * 40, category="private-stalled")]
    dead = _trackers(status=4)
    trackers = {"a" * 40: dead, "c" * 40: dead, "d" * 40: dead,
                "b" * 40: dead + [{"url": "https://routing.bgp.technology/a", "status": 2}]}
    found = tracker_findings(torrents, trackers, rules)
    assert [h for h, _ in found] == ["a" * 40], found
    for status in (5, 6):
        t = _torrent("e" * 40)
        assert tracker_findings([t], {t["hash"]: _trackers(status=status)}, rules) == \
            [("e" * 40, "no tracker working")], status
    t = _torrent("g" * 40)
    unreachable_and_untried = _trackers(status=6) + [{"url": "https://b.example/a", "status": 1}]
    assert tracker_findings([t], {t["hash"]: unreachable_and_untried}, rules) == \
        [("g" * 40, "no tracker working")]
    assert tracker_findings([t], {t["hash"]: _trackers(status=6) + _trackers(status=2)[2:]},
                            rules) == []


def selftest_lookalike_host(rules):
    torrents = [_torrent("a" * 40, state="stalledUP"), _torrent("b" * 40, state="stalledUP"),
                _torrent("c" * 40, state="stalledUP")]
    trackers = {"a" * 40: _trackers("https://notmagtre.com/announce"),
                "b" * 40: _trackers("https://magtre.com/announce"),
                "c" * 40: _trackers("https://www.magtre.com/announce")}
    found = early_stop_findings(torrents, trackers, rules)
    assert found == [("a" * 40, "lookalike host")], found
    mixed = _trackers("https://async.empirehost.me/a") + [
        {"url": "https://tracker.example.org/announce", "status": 2}]
    t = _torrent("d" * 40, state="stalledUP")
    assert early_stop_findings([t], {t["hash"]: mixed}, rules) == \
        [("d" * 40, "known and unknown tracker hosts")]
    two = _trackers("https://async.empirehost.me/a") + [
        {"url": "https://tracker.avistaz.to/a", "status": 4}]
    big = dict(_torrent("e" * 40, ratio=0.1, hours=400), size=60 * 2**30)
    assert [r for _, r in early_stop_findings([big], {big["hash"]: two}, rules)] == \
        ["stoppedUP before hit-and-run rule (ratio 0.10, seeded 400.0 h)"], "AvistaZ 1440 h"
    assert tracker_findings([big], {big["hash"]: two}, rules) == \
        [("e" * 40, "no tracker working")], "working IPT must not mask failing AvistaZ"


def selftest_no_names(rules):
    t = _torrent("abcdef0123456789" * 2 + "abcdef01")
    found = early_stop_findings([t], {t["hash"]: _trackers()}, rules)
    lines = report_lines([t], found)
    assert lines and all("abcdef01" in ln and "radarr" in ln for ln in lines), lines
    assert not any("Secret" in ln or "empirehost" in ln or t["hash"] in ln for ln in lines), lines


def selftest_malformed(rules):
    def fetch(h):
        if h == "x" * 40:
            raise Gone()
        return _trackers()
    gone, kept = _torrent("x" * 40), _torrent("y" * 40)
    torrents, found = scan([gone, kept], fetch, rules, early_stop_findings)
    assert [t["hash"] for t in torrents] == ["y" * 40] and [h for h, _ in found] == ["y" * 40]
    pending = [{"hash": "m" * 40, "private": None, "category": "radarr", "state": "metaDL"}]
    assert scan(pending, lambda h: _trackers(), rules, early_stop_findings) == ([], [])
    for info in ([{"private": True, "category": "radarr", "state": "stoppedUP"}], [{}],
                 [{"private": True, "category": "radarr", "hash": None}], [None], {}):
        try:
            scan(info, lambda h: _trackers(), rules, early_stop_findings)
            raise AssertionError("malformed reply must not scan: %r" % (info,))
        except MALFORMED:
            pass


def selftest():
    rules = load_rules(RULES)
    for test in (selftest_boundaries, selftest_early_stop, selftest_unknown_host,
                 selftest_tracker_mode, selftest_lookalike_host, selftest_no_names,
                 selftest_malformed):
        test(rules)
    print("selftest OK")
    return 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv[:-1] else ""
    if mode not in ("early-stop", "tracker"):
        fail("usage: torrent-hnr-check.py --mode early-stop|tracker")
    try:
        rules = load_rules(RULES)
    except (OSError, ValueError) as exc:
        fail("cannot read %s (%s)" % (RULES, type(exc).__name__))

    opener = qbt_login()
    check = early_stop_findings if mode == "early-stop" else tracker_findings
    try:
        torrents, found = scan(
            qbt_get(opener, "/api/v2/torrents/info"),
            lambda h: qbt_get(opener, "/api/v2/torrents/trackers?hash=" + h, gone_ok=True),
            rules, check)
    except MALFORMED as exc:
        # Malformed data must never read as a finding (exit 1); it is "could not run".
        fail("unexpected qBittorrent reply (%s)" % type(exc).__name__)
    print("torrent-hnr-check --mode %s: %d private torrents in %s, %d findings"
          % (mode, len(torrents), ",".join(rules["categories"]), len(found)))
    for line in report_lines(torrents, found):
        print(line)
    if found:
        print("FAIL: REPORT ONLY. Find each torrent by hash prefix in the qBittorrent WebUI.")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
