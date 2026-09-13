#!/usr/bin/env python3
"""Unlink the redundant second name under the qBittorrent temp directory (#969).

699.7 G under /mnt/pool1/dataset01/downloads/temp is a second name for inodes
the library already holds. Unlinking those names frees 0 bytes, and that is the
point: `du` on temp stops lying (703 G -> ~2.4 G) and the library stops
depending on paths under a directory called `temp` (#842, closed by PR #967).

The whole job is one guard, applied per inode and never per path:

    unlink a temp name only if THAT inode still has a live name outside temp,
    re-stat'ed in the same breath as the unlink.

Everything else exists to keep that guard honest:

  - the candidate set is re-derived from live state before every batch - a
    fresh torrent list and a fresh filesystem walk - so temp names and library
    names always come from the same instant and no batch acts on a picture
    taken before the previous one ran;
  - identity is (st_dev, st_ino), never the path. A path that looks like the
    library counterpart proves nothing about which blocks sit behind it;
  - anything under a torrent qBittorrent currently holds is left alone, so a
    stalled download still being written, or anything still seeding, is never
    touched (#479);
  - qBittorrent's missingFiles/error set is compared against its value at the
    start of the run before each batch. If it grew, stop: something was in use
    after all.

Inodes whose every name is under temp are not candidates at all. Those are the
2.4 G of genuine partials and leftovers, and unlinking them destroys data.

Dry run by default. --apply is the only thing that unlinks.

Run on the TrueNAS box:

    QBT_URL=https://qbittorrent.epaflix.com QBT_USERNAME=... QBT_PASSWORD=... \
      sudo -E python3 temp-redundant-name-unlink.py [--apply]

    python3 temp-redundant-name-unlink.py --selftest   # no network, no pool

Exit 0 = nothing left to do (reported, or every candidate unlinked),
1 = stopped early with candidates remaining, 2 = could not run.
"""
import http.cookiejar
import json
import os
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30

BAD_STATES = {"missingFiles", "error"}

DATASET_ROOT = os.environ.get("DATASET_ROOT", "/mnt/pool1/dataset01")

TEMP_DIR = os.environ.get("TEMP_DIR", DATASET_ROOT + "/downloads/temp")

QBT_ROOT = os.environ.get("QBT_ROOT", "/media").rstrip("/")

QBT_URL = os.environ.get("QBT_URL", "http://qbittorrent:8080").rstrip("/")

BATCH = int(os.environ.get("BATCH", "10"))

GIB = 1024.0 ** 3


def fail(msg):
    """Abort LOUD. A failed read must never look like 'nothing to unlink'."""
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(2)


def http_status(exc):
    """Status code only - never echo a response body, it can carry a key."""
    return str(exc.code) if isinstance(exc, urllib.error.HTTPError) else "-"


def qbt_login():
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    body = urllib.parse.urlencode({
        "username": os.environ.get("QBT_USERNAME", ""),
        "password": os.environ.get("QBT_PASSWORD", ""),
    }).encode()
    req = urllib.request.Request(
        QBT_URL + "/api/v2/auth/login", data=body, headers={"Referer": QBT_URL})
    try:
        answer = opener.open(req, timeout=TIMEOUT).read().decode().strip()
    except Exception as exc:  # noqa: BLE001 - any failure is fatal here
        fail("qBittorrent login request failed (%s, http=%s)"
             % (type(exc).__name__, http_status(exc)))
    if answer and answer != "Ok.":
        fail("qBittorrent login rejected")
    return opener


def qbt_torrents(opener):
    try:
        with opener.open(QBT_URL + "/api/v2/torrents/info",
                         timeout=TIMEOUT) as resp:
            torrents = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            fail("qBittorrent rejected the session (http=403) - the WebUI "
                 "credential in QBT_USERNAME/QBT_PASSWORD is wrong or this "
                 "host is banned")
        fail("qBittorrent torrent list failed (HTTPError, http=%d)" % exc.code)
    except Exception as exc:  # noqa: BLE001
        fail("qBittorrent torrent list failed (%s, http=%s)"
             % (type(exc).__name__, http_status(exc)))
    if not isinstance(torrents, list):
        fail("qBittorrent torrent list was not a JSON array")
    return torrents


def bad_hashes(torrents):
    """Hashes currently in missingFiles/error, lowercased."""
    return {str(t.get("hash", "")).strip().lower()
            for t in torrents
            if str(t.get("state", "")) in BAD_STATES} - {""}


def held_paths(torrents):
    """Every torrent's content_path, translated into TrueNAS paths.

    content_path is the torrent root - the file itself for a single-file
    torrent - and it follows the data, so it names temp while a download still
    lives there. One prefix per torrent covers every file in it without a
    /torrents/files call per hash.

    qBittorrent sees the pool through the container mount (/media), the box
    sees /mnt/pool1/dataset01. A translation that silently matches nothing
    would disable the guard while looking like it passed, so a torrent list
    where nothing maps is a hard failure, not an empty set.
    """
    out = []
    for t in torrents:
        p = str(t.get("content_path") or "").strip()
        if under(p, QBT_ROOT):
            out.append(DATASET_ROOT + p[len(QBT_ROOT):])
    if torrents and not out:
        fail("none of the %d torrents reported a content_path under %s - the "
             "QBT_ROOT mapping is wrong, and without it nothing would be "
             "recognised as in use" % (len(torrents), QBT_ROOT))
    return out


def under(path, prefix):
    """True if path is prefix itself or sits inside it.

    A bare startswith would call /downloads/tempest a temp path.
    """
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def walk(root):
    """(st_dev, st_ino) -> {"bytes": int, "paths": [str, ...]} for every regular
    file under root, in a single pass.

    find already does this, and -printf hands back the stat fields without a
    stat(2) per path from Python. The NUL record separator keeps paths with
    newlines in them intact; %p is parsed last so tabs in a name survive too.
    """
    proc = subprocess.run(
        ["find", root, "-type", "f", "-printf", r"%D\t%i\t%b\t%p\0"],
        capture_output=True, check=False)
    if proc.returncode != 0:
        fail("find over %s failed (rc=%d): %s"
             % (root, proc.returncode,
                proc.stderr.decode("utf-8", "replace").strip()[-500:]))
    out = {}
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        try:
            dev, ino, blocks, path = record.split(b"\t", 3)
            key = (int(dev), int(ino))
        except ValueError:
            fail("unparseable find record: %r" % record[:200])
        entry = out.setdefault(key, {"bytes": int(blocks) * 512, "paths": []})
        entry["paths"].append(path.decode("utf-8", "surrogateescape"))
    return out


def classify(inodes, temp_dir, held):
    """Split the walk into the three groups that matter.

    redundant: at least one name under temp, at least one outside it, and no
        name claimed by a live torrent. Only these are ever unlinked, and only
        their temp names.
    in_use: the same shape, but a live torrent still holds one of the names.
        Reported, never touched.
    temp_only: every name is under temp. Unlinking one of these would drop the
        last name for real data, so it is not a candidate at all.

    Pure: takes the walk and the held prefixes, returns lists. The unlink loop
    does the re-stat'ing; this decides nothing that a later stat can contradict
    without being caught.
    """
    redundant, in_use, temp_only = [], [], []
    for key, entry in sorted(inodes.items()):
        temp_paths = [p for p in entry["paths"] if under(p, temp_dir)]
        if not temp_paths:
            continue
        lib_paths = [p for p in entry["paths"] if not under(p, temp_dir)]
        row = {"key": key, "bytes": entry["bytes"],
               "temp_paths": sorted(temp_paths), "lib_paths": sorted(lib_paths)}
        if not lib_paths:
            temp_only.append(row)
        elif any(under(p, h) for p in entry["paths"] for h in held):
            in_use.append(row)
        else:
            redundant.append(row)
    return redundant, in_use, temp_only


def counterpart(key, lib_paths):
    """A library name that still points at THIS inode, re-stat'ed now.

    The walk happened seconds ago. Between then and the unlink an import could
    have moved or replaced the library name, which would make the temp name the
    last one. The path is not the proof; (st_dev, st_ino) is.
    """
    for path in lib_paths:
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if ((st.st_dev, st.st_ino) == key and stat.S_ISREG(st.st_mode)
                and st.st_nlink > 1):
            return path
    return None


def unlink_temp_name(key, path, apply):
    """Remove one temp name, but only if it is still the inode we measured.

    Returns True if the name is gone (or was already), False if the path
    changed underneath us and was therefore left alone.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if (st.st_dev, st.st_ino) != key or not stat.S_ISREG(st.st_mode):
        return False
    if apply:
        os.unlink(path)
    return True


def total(rows):
    return sum(r["bytes"] for r in rows)


def report(label, rows):
    print("%-30s %8.1f G  %d inodes" % (label, total(rows) / GIB, len(rows)))


def selftest():
    assert under("/a/temp/x", "/a/temp")
    assert under("/a/temp", "/a/temp")
    assert under("/a/temp/x", "/a/temp/")
    assert not under("/a/tempest/x", "/a/temp")
    assert not under("/a/tem", "/a/temp")

    temp = "/pool/downloads/temp"
    inodes = {
        (1, 10): {"bytes": 2048, "paths": [temp + "/s01/e01.mkv",
                                           "/pool/tvshows/s01e01.mkv"]},
        (1, 11): {"bytes": 1024, "paths": [temp + "/part.mkv.!qB"]},
        (1, 12): {"bytes": 4096, "paths": [temp + "/a.mkv", temp + "/b.mkv"]},
        (1, 13): {"bytes": 512, "paths": ["/pool/movies/only.mkv"]},
        (1, 14): {"bytes": 8192, "paths": [temp + "/live.mkv",
                                           "/pool/tvshows/live.mkv"]},
    }
    held = [temp + "/live.mkv"]
    redundant, in_use, temp_only = classify(inodes, temp, held)
    assert [r["key"] for r in redundant] == [(1, 10)], redundant
    assert [r["key"] for r in in_use] == [(1, 14)], in_use
    assert [r["key"] for r in temp_only] == [(1, 11), (1, 12)], temp_only
    assert redundant[0]["lib_paths"] == ["/pool/tvshows/s01e01.mkv"]
    assert total(redundant) == 2048

    assert classify(inodes, temp, [temp])[0] == []

    assert bad_hashes([{"hash": "A" * 40, "state": "missingFiles"},
                       {"hash": "b" * 40, "state": "stalledUP"},
                       {"hash": "c" * 40, "state": "error"}]) == {
        "a" * 40, "c" * 40}

    with tempfile.TemporaryDirectory() as box:
        lib = os.path.join(box, "lib.mkv")
        tmp = os.path.join(box, "tmp.mkv")
        other = os.path.join(box, "other.mkv")
        with open(lib, "wb") as fh:
            fh.write(b"x")
        os.link(lib, tmp)
        key = (os.stat(lib).st_dev, os.stat(lib).st_ino)

        assert counterpart(key, [lib]) == lib
        assert counterpart(key, [os.path.join(box, "gone.mkv")]) is None
        with open(other, "wb") as fh:
            fh.write(b"y")
        assert counterpart(key, [other]) is None
        assert counterpart((key[0], key[1] + 10 ** 9), [lib]) is None

        assert unlink_temp_name(key, tmp, False) is True
        assert os.path.exists(tmp), "dry run must not unlink"
        assert unlink_temp_name(key, other, True) is False
        assert os.path.exists(other), "a different inode must not be unlinked"
        assert unlink_temp_name(key, tmp, True) is True
        assert not os.path.exists(tmp)
        assert os.path.exists(lib), "the library name must survive"
        assert os.stat(lib).st_nlink == 1

        assert counterpart(key, [lib]) is None, "last name is never a counterpart"

    print("selftest OK")
    return 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    apply = "--apply" in sys.argv

    if not os.path.isdir(TEMP_DIR):
        fail("%s is not a directory" % TEMP_DIR)

    baseline = None
    attempted = set()
    done = 0

    while True:
        torrents = qbt_torrents(qbt_login())
        bad = bad_hashes(torrents)
        if baseline is None:
            baseline = bad
            print("qbittorrent: %d torrents | %d already in "
                  "missingFiles/error" % (len(torrents), len(baseline)))
        elif bad - baseline:
            print("\nABORT: %d torrent(s) entered missingFiles/error during "
                  "this run: %s"
                  % (len(bad - baseline),
                     ", ".join(sorted(h[:8] for h in bad - baseline))),
                  file=sys.stderr)
            print("%d temp name(s) unlinked before the abort." % done,
                  file=sys.stderr)
            return 1

        held = held_paths(torrents)
        inodes = walk(DATASET_ROOT)
        redundant, in_use, temp_only = classify(inodes, TEMP_DIR, held)
        if not attempted:
            print("walked %s: %d inodes | %d content paths held"
                  % (DATASET_ROOT, len(inodes), len(held)))
            report("hardlinked into the library", redundant + in_use)
            report("  of which a torrent holds", in_use)
            report("exclusive to temp", temp_only)
            print("candidates: %d inodes, %d temp names, frees 0 bytes"
                  % (len(redundant),
                     sum(len(r["temp_paths"]) for r in redundant)))

        if not apply:
            for row in redundant:
                print("  would unlink %s\n            keeping %s"
                      % (row["temp_paths"][0], row["lib_paths"][0]))
            if not redundant:
                print("\nOK: nothing to unlink.")
                return 0
            print("\nDRY RUN: re-run with --apply to unlink %d temp name(s)."
                  % sum(len(r["temp_paths"]) for r in redundant))
            return 0

        # Only inodes this run has not already worked on: a candidate that was
        # skipped stays skipped, so a re-derivation that keeps returning it
        # cannot loop forever.
        batch = [r for r in redundant if r["key"] not in attempted][:BATCH]
        if not batch:
            break

        for row in batch:
            attempted.add(row["key"])
            for path in row["temp_paths"]:
                # Re-checked per name, not once per inode: an inode with two
                # temp names must not lose its second one on the strength of a
                # library name that was still there before the first.
                keep = counterpart(row["key"], row["lib_paths"])
                if keep is None:
                    print("  SKIP inode %d: no library name still points at it"
                          % row["key"][1])
                    break
                if unlink_temp_name(row["key"], path, True):
                    done += 1
                    print("  unlinked %s (kept %s)" % (path, keep))
                else:
                    print("  SKIP %s: changed since the walk" % path)

    if not done:
        print("\nOK: nothing to unlink.")
        return 0
    print("\nOK: unlinked %d temp name(s), freed 0 bytes, as expected." % done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
