#!/usr/bin/env python3
"""Import finished SeaDexArr batches into Sonarr.

Sonarr only follows downloads it grabbed itself. Torrents that the seadexarr
CronJob adds finish and then sit in qBittorrent, and their names often carry
no episode numbers. For each finished torrent tagged seadexarr this job:

  1. lists the files with Sonarr's manual import (GET /manualimport?folder=),
  2. re-parses them against the series in the seadexarr-sonarr-<id> tag
     (POST /manualimport, the call Sonarr's Manual Import dialog makes when
     you pick a series),
  3. imports every file that has no Sonarr rejection and whose episodes no
     other file claims (ManualImport command, copy mode, so the torrent keeps
     seeding),
  4. tags the torrent seadexarr-imported when the import worked and the
     series has no missing episodes left (or another open seadexarr torrent
     of the series may still fill them), else seadexarr-manual.

Exit 1 while any torrent carries seadexarr-manual, so SeadexarrImportFailed
stays firing until someone imports it by hand and removes the tag or the
torrent. Exit 2 when a read or write fails. Run with --selftest for the pure
logic.
"""
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 300  # Sonarr runs mediainfo on every file of the batch
IMPORT_WAIT = 1500

SONARR_URL = os.environ.get(
    "SONARR_URL", "http://sonarr.servarr.svc.cluster.local:8989"
).rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")
QBT_URL = os.environ.get("QBT_URL", "http://qbittorrent:8080").rstrip("/")
QBT_USERNAME = os.environ.get("QBT_USERNAME", "")
QBT_PASSWORD = os.environ.get("QBT_PASSWORD", "")
QBT_CATEGORY = os.environ.get("QBT_CATEGORY", "tv-sonarr")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

TAG = "seadexarr"
TAG_DONE = "seadexarr-imported"
TAG_MANUAL = "seadexarr-manual"
SERIES_TAG_RE = re.compile(r"^seadexarr-sonarr-(\d+)$")


def fail(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(2)


def tags_of(torrent):
    return {t.strip() for t in str(torrent.get("tags") or "").split(",")
            if t.strip()}


def series_id(torrent):
    """The one series id in the tags, or None when there are zero or several."""
    ids = {int(m.group(1)) for m in map(SERIES_TAG_RE.match, tags_of(torrent))
           if m}
    return ids.pop() if len(ids) == 1 else None


def is_open(torrent):
    """Tagged seadexarr and not yet imported or handed to a human."""
    tags = tags_of(torrent)
    return TAG in tags and not tags & {TAG_DONE, TAG_MANUAL}


def pending(torrents):
    """Finished open torrents. A missing or ambiguous series tag is left to
    handle(), which sends it to manual instead of guessing."""
    return [t for t in torrents if is_open(t)
            and float(t.get("progress") or 0) >= 1.0 and t.get("content_path")]


def others_open(torrents, torrent, sid):
    """Other open torrents for the same series, finished or not."""
    return [t for t in torrents if t.get("hash") != torrent.get("hash")
            and is_open(t) and series_id(t) == sid]


def episode_ids(item):
    ids = [ep.get("id") for ep in item.get("episodes") or []]
    if ids and all(isinstance(i, int) and not isinstance(i, bool) and i > 0
                   for i in ids):
        return ids
    return []


def select_files(items, sid):
    """Split re-parsed manual import items into (import, skipped).

    A file is imported only when Sonarr maps it to the tagged series and to
    valid episode ids, has no rejection, and shares no episode with another
    file of the batch. Two files on one episode means a misparse (seen live:
    Sonarr read the CRC "[E6B6C6CF]" of Lucky Star 09 as E06), so both are
    skipped.
    """
    claims = {}
    for item in items:
        for e in episode_ids(item):
            claims[e] = claims.get(e, 0) + 1
    ok, skipped = [], []
    for item in items:
        ep_ids = episode_ids(item)
        reasons = [str(r.get("reason")) for r in item.get("rejections") or []]
        if item.get("seriesId") != sid:
            reasons.append("series %s is not the tagged series %d"
                           % (item.get("seriesId"), sid))
        if not ep_ids:
            reasons.append("no episode")
        if any(claims[e] > 1 for e in ep_ids):
            reasons.append("another file maps to the same episode")
        if reasons:
            skipped.append((item.get("path"), reasons))
        else:
            ok.append(item)
    return ok, skipped


def import_payload(items, download_id):
    files = []
    for i in items:
        f = {
            "path": i["path"],
            "seriesId": i["seriesId"],
            "episodeIds": episode_ids(i),
            "quality": i.get("quality"),
            "languages": i.get("languages"),
            "releaseGroup": i.get("releaseGroup"),
            "indexerFlags": i.get("indexerFlags") or 0,
            "downloadId": download_id,
        }
        if i.get("releaseType") is not None:
            f["releaseType"] = i["releaseType"]
        files.append(f)
    return {"name": "ManualImport", "importMode": "copy", "files": files}


def final_tag(command_status, missing, others):
    """Tag for a handled torrent.

    Imported when the ManualImport command completed (or nothing was left to
    import) and the series has no missing episodes. A series that still has
    gaps is only judged by the last of its open torrents, so a SeaDex pick
    split over several torrents does not alert halfway. A failed import is
    always manual.
    """
    if command_status not in (None, "completed"):
        return TAG_MANUAL
    if missing == 0 or others:
        return TAG_DONE
    return TAG_MANUAL


def sonarr(path, payload=None, query=None):
    url = SONARR_URL + "/api/v3/" + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"X-Api-Key": SONARR_API_KEY, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode() or "null")


def qbt_login():
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    body = urllib.parse.urlencode(
        {"username": QBT_USERNAME, "password": QBT_PASSWORD}).encode()
    req = urllib.request.Request(QBT_URL + "/api/v2/auth/login", data=body,
                                 headers={"Referer": QBT_URL})
    try:
        answer = opener.open(req, timeout=60).read().decode().strip()
    except Exception as exc:  # noqa: BLE001
        fail("qBittorrent login failed (%s)" % type(exc).__name__)
    if answer and answer != "Ok.":
        fail("qBittorrent login rejected")
    return opener


def qbt_torrents(opener):
    query = urllib.parse.urlencode({"category": QBT_CATEGORY, "tag": TAG})
    try:
        with opener.open(QBT_URL + "/api/v2/torrents/info?" + query,
                         timeout=60) as resp:
            torrents = json.load(resp)
    except Exception as exc:  # noqa: BLE001
        fail("qBittorrent torrent list failed (%s)" % type(exc).__name__)
    if not isinstance(torrents, list):
        fail("qBittorrent torrent list was not a JSON array")
    return torrents


def qbt_tag(opener, torrent_hash, tag):
    body = urllib.parse.urlencode({"hashes": torrent_hash, "tags": tag}).encode()
    req = urllib.request.Request(QBT_URL + "/api/v2/torrents/addTags",
                                 data=body, headers={"Referer": QBT_URL})
    opener.open(req, timeout=60).read()


def wait_command(cmd_id):
    deadline = time.time() + IMPORT_WAIT
    while time.time() < deadline:
        cmd = sonarr("command/%d" % cmd_id)
        if cmd.get("status") in ("completed", "failed", "aborted", "cancelled"):
            return cmd.get("status")
        time.sleep(10)
    return "timeout"


def series_missing(sid):
    stats = sonarr("series/%d" % sid).get("statistics") or {}
    return int(stats.get("episodeCount") or 0) - int(
        stats.get("episodeFileCount") or 0)


def handle(opener, torrent, torrents):
    """Import one torrent. Returns the tag it ended with.

    Safe to repeat: when a run dies after the import but before the tag,
    filterExistingFiles drops the imported files, nothing is re-imported and
    the series check below decides the tag.
    """
    sid = series_id(torrent)
    name = torrent.get("name")
    if sid is None:
        print("torrent %s: needs exactly one seadexarr-sonarr-<id> tag" % name)
        tag = TAG_MANUAL
    else:
        print("torrent %s -> series %d" % (name, sid))
        listed = sonarr("manualimport", query={
            "folder": torrent["content_path"], "filterExistingFiles": "true"})
        reprocess = [{
            "id": i["id"], "path": i["path"], "seriesId": sid,
            "quality": i.get("quality"), "languages": i.get("languages"),
            "releaseGroup": i.get("releaseGroup"),
            "indexerFlags": i.get("indexerFlags"),
            "releaseType": i.get("releaseType"),
        } for i in listed or []]
        items = sonarr("manualimport", payload=reprocess) if reprocess else []
        ok, skipped = select_files(items or [], sid)
        for path, reasons in skipped:
            print("    skip %s: %s"
                  % (os.path.basename(str(path)), "; ".join(reasons)))
        print("    %d file(s) to import, %d skipped" % (len(ok), len(skipped)))
        if DRY_RUN:
            print("    DRY_RUN - nothing imported or tagged")
            return None
        status = None
        if ok:
            cmd = sonarr("command", payload=import_payload(
                ok, str(torrent.get("hash") or "").upper()))
            status = wait_command(cmd["id"])
            print("    ManualImport command %s: %s" % (cmd["id"], status))
        missing = series_missing(sid)
        others = others_open(torrents, torrent, sid)
        print("    series %d: %d episode(s) still missing, %d other open "
              "seadexarr torrent(s)" % (sid, missing, len(others)))
        tag = final_tag(status, missing, others)
    if DRY_RUN:
        return None
    qbt_tag(opener, torrent["hash"], tag)
    print("    tagged %s" % tag)
    return tag


def selftest():
    def t(tags, progress=1.0, path="/media/x"):
        return {"tags": tags, "progress": progress, "content_path": path}

    assert series_id(t("seadexarr, seadexarr-sonarr-406")) == 406
    assert series_id(t("seadexarr")) is None
    assert series_id(t("seadexarr,seadexarr-sonarr-1,seadexarr-sonarr-2")) \
        is None
    got = pending([
        t("seadexarr,seadexarr-sonarr-1"),
        t("seadexarr,seadexarr-sonarr-2", progress=0.5),
        t("seadexarr,seadexarr-sonarr-3,seadexarr-imported"),
        t("seadexarr,seadexarr-sonarr-4,seadexarr-manual"),
        t("seadexarr"),
        t("seadexarr-sonarr-6"),
        t("seadexarr,seadexarr-sonarr-7", path=""),
    ])
    # the tagless one is kept so handle() sends it to manual
    assert [x["tags"] for x in got] == \
        ["seadexarr,seadexarr-sonarr-1", "seadexarr"], got

    def h(hash_, tags, progress=1.0):
        return dict(t(tags, progress), hash=hash_)

    mine = h("a", "seadexarr,seadexarr-sonarr-9")
    torrents = [mine,
                h("b", "seadexarr,seadexarr-sonarr-9", progress=0.3),
                h("c", "seadexarr,seadexarr-sonarr-9,seadexarr-imported"),
                h("d", "seadexarr,seadexarr-sonarr-8")]
    assert [x["hash"] for x in others_open(torrents, mine, 9)] == ["b"]

    def item(path, eps, rejections=(), sid=406):
        return {"path": path, "seriesId": sid,
                "episodes": [{"id": e} for e in eps],
                "rejections": [{"reason": r} for r in rejections]}

    ok, skipped = select_files([
        item("01.mkv", [1]),
        item("02.mkv", [2]),
        item("06a.mkv", [6]),
        item("06b.mkv", [6]),
        item("nc.mkv", []),
        item("03.mkv", [3], rejections=["Not an upgrade"]),
        item("04.mkv", [4], sid=None),
        item("other.mkv", [11], sid=407),
        item("null.mkv", [None]),
        item("bool.mkv", [True]),
        item("05-07.mkv", [5, 7]),
    ], 406)
    assert [i["path"] for i in ok] == ["01.mkv", "02.mkv", "05-07.mkv"], ok
    assert sorted(p for p, _ in skipped) == sorted(
        ["03.mkv", "04.mkv", "06a.mkv", "06b.mkv", "nc.mkv", "other.mkv",
         "null.mkv", "bool.mkv"]), skipped

    payload = import_payload(ok[:1], "ABC")
    assert payload["importMode"] == "copy"
    assert payload["files"][0]["episodeIds"] == [1]
    assert payload["files"][0]["downloadId"] == "ABC"
    assert "releaseType" not in payload["files"][0]
    assert import_payload([dict(ok[0], releaseType="seasonPack")], "A")[
        "files"][0]["releaseType"] == "seasonPack"

    # completed + complete series, or nothing left to import (re-run after
    # a lost tag) -> imported
    assert final_tag("completed", 0, []) == TAG_DONE
    assert final_tag(None, 0, []) == TAG_DONE
    # partial import with gaps and no other torrent to fill them -> manual
    assert final_tag("completed", 2, []) == TAG_MANUAL
    assert final_tag(None, 2, []) == TAG_MANUAL
    # gaps while another torrent of the same series is open -> wait for it
    assert final_tag("completed", 2, [mine]) == TAG_DONE
    # failed import is always manual
    assert final_tag("failed", 0, []) == TAG_MANUAL
    assert final_tag("timeout", 0, [mine]) == TAG_MANUAL
    print("selftest OK")
    return 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if not SONARR_API_KEY or not QBT_USERNAME:
        fail("SONARR_API_KEY / QBT_USERNAME are empty")

    opener = qbt_login()
    torrents = qbt_torrents(opener)
    todo = pending(torrents)
    print("%d seadexarr torrent(s), %d finished and not yet imported"
          % (len(torrents), len(todo)))
    # One torrent per run keeps IMPORT_WAIT inside the Job deadline, so an
    # import is never cut off before its tag is written. Runs are 15 min apart.
    for torrent in todo[:1]:
        try:
            handle(opener, torrent, torrents)
        except urllib.error.HTTPError as exc:
            fail("Sonarr/qBittorrent call failed for %s (http=%d)"
                 % (torrent.get("name"), exc.code))
        except Exception as exc:  # noqa: BLE001
            fail("import of %s failed (%s)"
                 % (torrent.get("name"), type(exc).__name__))

    manual = [t.get("name") for t in qbt_torrents(opener)
              if TAG_MANUAL in tags_of(t)]
    if manual:
        print("\nFAIL: %d torrent(s) need a manual import in Sonarr "
              "(series page > Manual Import), then remove the %s tag or the "
              "torrent:" % (len(manual), TAG_MANUAL))
        for name in manual:
            print("    %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
