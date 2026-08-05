#!/usr/bin/env python3
"""Self-checks for the scoring rule and the atomic publish.

Run inside the built image by test.sh. Plain asserts, no framework - the whole
point is that a broken ranking or a non-atomic write fails the build.

The fixture is the real 2026-07-31 measurement from the design doc, so these
are the spec's own sanity anchors, not invented numbers.
"""

import glob
import json
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone

import vpn_picker as vp


def cfg(**overrides):
    os.environ["AIRVPN_API_KEY"] = "x"
    c = vp.Config()
    # Tests commonly redirect only ranking.json after Config construction. The
    # runtime derives both paths from env in one pass; keep the fixture's private
    # base journal beside its redirected ranking unless a test names it.
    if "output_path" in overrides and "base_state_path" not in overrides:
        overrides["base_state_path"] = overrides["output_path"] + ".base"
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


# 2026-07-31, from the design doc. `bw` is back-calculated from the published
# load percent so headroom comes out as the doc states it.
FIXTURE = [
    # The 20 Gbit box the design says must win. ~17 Gbit spare.
    {"public_name": "Dedalus", "country_code": "nl", "health": "ok",
     "currentload": 15, "bw": 3199, "bw_max": 20000, "ip_v4_in1": "109.235.50.5"},
    # AirVPN's own `server_best` for nl, health ok, and 22% measured loss on our
    # line. 2 Gbit box, ~1.2 Gbit spare.
    {"public_name": "Anser", "country_code": "nl", "health": "ok",
     "currentload": 39, "bw": 780, "bw_max": 2000, "ip_v4_in1": "109.235.50.7"},
    # Excluded at stage 1 on health. `error` means CLOSED to new connections.
    {"public_name": "Cygnus", "country_code": "nl", "health": "error",
     "currentload": 10, "bw": 100, "bw_max": 20000, "ip_v4_in1": "1.2.3.4"},
    # Excluded on country - not in nl,de,se.
    {"public_name": "Achernar", "country_code": "ch", "health": "ok",
     "currentload": 5, "bw": 100, "bw_max": 20000, "ip_v4_in1": "1.2.3.5"},
    # Excluded on load - the class of box `quick` keeps picking.
    {"public_name": "Overloaded", "country_code": "se", "health": "ok",
     "currentload": 78, "bw": 1560, "bw_max": 2000, "ip_v4_in1": "1.2.3.6"},
    # Filler so the top-5 cap is actually exercised.
    {"public_name": "Felix", "country_code": "nl", "health": "ok",
     "currentload": 30, "bw": 6000, "bw_max": 20000, "ip_v4_in1": "1.2.3.7"},
    {"public_name": "Menkent", "country_code": "de", "health": "ok",
     "currentload": 25, "bw": 5000, "bw_max": 20000, "ip_v4_in1": "1.2.3.8"},
    {"public_name": "Sheliak", "country_code": "se", "health": "ok",
     "currentload": 40, "bw": 8000, "bw_max": 20000, "ip_v4_in1": "1.2.3.9"},
    {"public_name": "Piautos", "country_code": "nl", "health": "ok",
     "currentload": 50, "bw": 10000, "bw_max": 20000, "ip_v4_in1": "1.2.3.10"},
]


def test_shortlist():
    picks = [s["public_name"] for s in vp.shortlist(FIXTURE, cfg())]

    assert "Cygnus" in [s["public_name"] for s in FIXTURE], "fixture lost Cygnus"
    assert "Cygnus" not in picks, "health=error must be excluded, got %s" % picks
    assert "Achernar" not in picks, "country outside nl,de,se must be excluded"
    assert "Overloaded" not in picks, "currentload 78 must fail the <=75 ceiling"
    assert len(picks) == 5, "top 5 expected, got %s" % picks

    # Ranked by absolute headroom, not bare load percent.
    assert picks == ["Dedalus", "Menkent", "Felix", "Sheliak", "Piautos"], picks
    assert picks.index("Dedalus") < picks.index("Felix"), "headroom order broken"

    # A 20 Gbit box must sit above a 2 Gbit one even when the 2 Gbit box has the
    # lower load percent. This is the whole reason we rank on headroom.
    assert vp.headroom(FIXTURE[0]) > vp.headroom(FIXTURE[1])
    print("ok  shortlist: health/country/load filters + headroom order + top-5 cap")


def test_probe_gate():
    probed = [
        {"name": "Anser", "entry_ip": "109.235.50.7", "loss_pct": 22.0,
         "rtt_ms": 27.0, "load": 39, "bw_max": 2000, "headroom": 1220},
        {"name": "Dedalus", "entry_ip": "109.235.50.5", "loss_pct": 0.0,
         "rtt_ms": 26.4, "load": 15, "bw_max": 20000, "headroom": 16801},
        {"name": "Felix", "entry_ip": "1.2.3.7", "loss_pct": 0.0,
         "rtt_ms": 31.2, "load": 30, "bw_max": 20000, "headroom": 14000},
        {"name": "Menkent", "entry_ip": "1.2.3.8", "loss_pct": 0.33,
         "rtt_ms": 26.0, "load": 25, "bw_max": 20000, "headroom": 15000},
    ]
    ranked = [s["name"] for s in vp.rank_survivors(probed, cfg().max_loss_pct)]

    assert "Anser" not in ranked, "22%% loss must be rejected by the gate, got %s" % ranked
    assert ranked[0] == "Dedalus", "Dedalus must beat Anser and Felix, got %s" % ranked
    # Equal loss falls through to RTT, then load.
    assert ranked == ["Dedalus", "Felix", "Menkent"], ranked
    print("ok  probe gate: Anser rejected at 22%, Dedalus wins, loss>rtt>load order")


def test_parse_ping():
    iputils = """PING 109.235.50.5 (109.235.50.5) 56(84) bytes of data.

--- 109.235.50.5 ping statistics ---
300 packets transmitted, 294 received, 2% packet loss, time 60102ms
rtt min/avg/max/mdev = 26.439/29.278/71.601/5.670 ms
"""
    loss, rtt = vp.parse_ping(iputils)
    # 6/300 is exactly 2.0, but the point is we compute it instead of trusting
    # the integer-rounded "2%" field, which cannot express the 1% gate.
    assert loss == 2.0, loss
    assert rtt == 29.28, rtt

    total_loss = """--- 1.2.3.4 ping statistics ---
300 packets transmitted, 0 received, 100% packet loss, time 60000ms
"""
    loss, rtt = vp.parse_ping(total_loss)
    assert loss == 100.0 and rtt == float("inf"), (loss, rtt)

    fine_grained = """--- x ping statistics ---
300 packets transmitted, 298 received, 0% packet loss, time 60000ms
rtt min/avg/max/mdev = 26.0/27.0/28.0/1.0 ms
"""
    loss, _ = vp.parse_ping(fine_grained)
    assert loss == 0.67, "sub-1%% loss must survive the integer rounding, got %s" % loss
    print("ok  parse_ping: exact loss from counts, inf RTT on total loss")


def test_document_contract():
    survivors = [{"name": "Dedalus", "entry_ip": "109.235.50.5", "loss_pct": 0.0,
                  "rtt_ms": 26.4, "load": 19, "bw_max": 20000, "headroom": 16801}]
    doc = json.loads(vp.build_document(survivors, cfg()))
    assert doc["schema"] == 1, doc
    assert doc["ttl_seconds"] == 2100, doc
    assert doc["generated_at"].endswith("Z") and len(doc["generated_at"]) == 20, doc
    assert set(doc["servers"][0]) == {
        "name", "entry_ip", "loss_pct", "rtt_ms", "load", "bw_max", "headroom"
    }, doc["servers"][0]
    print("ok  contract: schema 1, RFC3339 UTC stamp, exact server fields")


def test_atomic_publish():
    """A reader must never see a partial document, and no temp file may survive.

    Written as a race on purpose: an in-place `open(path, "w")` write passes a
    single-shot check and fails this one.
    """
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "ranking.json")
        small = json.dumps({"schema": 1, "servers": []}).encode()
        # Big enough that a non-atomic write spans several page flushes.
        big = json.dumps({"schema": 1, "servers": [{"n": "x" * 200}] * 500}).encode()
        vp.publish(small, path)

        stop = threading.Event()
        partial = []

        def reader():
            while not stop.is_set():
                try:
                    with open(path, "rb") as fh:
                        json.loads(fh.read())
                except FileNotFoundError:
                    partial.append("missing")
                except Exception as exc:
                    partial.append(str(exc))

        t = threading.Thread(target=reader)
        t.start()
        for i in range(300):
            vp.publish(big if i % 2 else small, path)
        stop.set()
        t.join()

        assert not partial, "reader saw %s partial/absent reads, first: %s" % (
            len(partial), partial[0])
        leftovers = glob.glob(os.path.join(d, "*"))
        assert leftovers == [path], "temp files left behind: %s" % leftovers

        # A failed publish must clean up after itself too.
        try:
            vp.publish(b"{}", os.path.join(d, "nope", "\x00bad"))
        except Exception:
            pass
        assert glob.glob(os.path.join(d, "nope", "*")) == [], "temp survived a failed publish"
        print("ok  atomic publish: 300 swaps under a live reader, zero partial reads")
    finally:
        shutil.rmtree(d)


def _verdict(path, producer, server, observed_at, source="vpn-agent", ttl=21600,
             loss=8.0, bad_windows=3):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump({
            "schema": 1,
            "source": source,
            "producer": producer,
            "server": server,
            "observed_at": observed_at,
            "ttl_seconds": ttl,
            "loss_pct": loss,
            "bad_windows": bad_windows,
        }, fh)


def test_agent_verdicts_eject_servers_with_a_bounded_fail_open_cap():
    """#792: sustained in-tunnel evidence removes, but cannot empty, ranking.

    Two producers share /media and publish separate files. The scorer must merge
    them, validate the source/schema/timestamp, and cap ejection at half of the
    otherwise-good ranking. Malformed and stale verdicts fail open.
    """
    d = tempfile.mkdtemp()
    try:
        verdict_dir = os.path.join(d, "verdicts")
        c = cfg(output_path=os.path.join(d, "ranking.json"),
                verdict_dir=verdict_dir, max_ejection_fraction=0.5,
                verdict_max_ttl_seconds=21600, verdict_future_skew_seconds=60)
        now = time.time()
        stamp = lambda seconds=0: datetime.fromtimestamp(
            now + seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # A clean scorer verdict is NOT an authority for in-tunnel health (#767).
        _verdict(os.path.join(verdict_dir, "wrong-source.json"),
                 "scorer", "Menkent", stamp(), source="vpn-picker")
        # Finite TTL: an old agent verdict no longer bans a recovered server.
        _verdict(os.path.join(verdict_dir, "stale.json"),
                 "old-pod", "Dedalus", stamp(-21601))

        probed = [
            {"name": "Dalim", "entry_ip": "1.1.1.1", "loss_pct": 0.0,
             "rtt_ms": 20.0, "load": 10, "bw_max": 20000, "headroom": 18000},
            {"name": "Piautos", "entry_ip": "1.1.1.2", "loss_pct": 0.0,
             "rtt_ms": 21.0, "load": 11, "bw_max": 20000, "headroom": 17900},
            {"name": "Menkent", "entry_ip": "1.1.1.3", "loss_pct": 0.0,
             "rtt_ms": 22.0, "load": 12, "bw_max": 20000, "headroom": 17800},
            {"name": "Dedalus", "entry_ip": "1.1.1.4", "loss_pct": 0.0,
             "rtt_ms": 23.0, "load": 13, "bw_max": 20000, "headroom": 17700},
        ]
        api_rows = [
            {"public_name": row["name"], "country_code": "nl", "health": "ok",
             "currentload": row["load"], "bw": 2000, "bw_max": 20000,
             "ip_v4_in1": row["entry_ip"]}
            for row in probed
        ]
        old_fetch, old_probe = vp.fetch_servers, vp.probe_all
        vp.fetch_servers = lambda _cfg: api_rows
        vp.probe_all = lambda _candidates, _cfg: list(probed)
        try:
            state = vp.State()
            assert vp.run_cycle(state, c) is True
        finally:
            vp.fetch_servers, vp.probe_all = old_fetch, old_probe

        with open(c.output_path, "rb") as fh:
            baseline = json.load(fh)
        assert [s["name"] for s in baseline["servers"]] == [
            "Dalim", "Piautos", "Menkent", "Dedalus"
        ], baseline

        # Both qBittorrent instances contribute AFTER the 15-minute score cycle.
        # The short verdict reconciliation must update the same ranking bytes
        # without waiting for another API call/probe, and without restamping the
        # base data (which would defeat ranking's own TTL).
        _verdict(os.path.join(verdict_dir, "cluster-dalim.json"),
                 "cluster-default", "Dalim", stamp())
        _verdict(os.path.join(verdict_dir, "nick-piautos.json"),
                 "nick", "Piautos", stamp(-1), loss=7.0)
        refresh = getattr(vp, "refresh_ejections", None)
        assert refresh is not None, (
            "the agents published authoritative in-tunnel verdicts after the "
            "score cycle, but the scorer has no short reconciliation path - a "
            "pod recreation can still boot from the old ranking for 15 minutes "
            "and land straight back on the rejected server (#792)")
        assert refresh(state, c) is True, \
            "a new verdict waited for the next 15-minute scorer cycle"
        with open(c.output_path, "rb") as fh:
            published = json.load(fh)
        names = [s["name"] for s in published["servers"]]
        assert published["generated_at"] == baseline["generated_at"], \
            "verdict refresh restamped old probe data and can keep it fresh forever"
        assert names == ["Menkent", "Dedalus"], (
            "the scorer published %s after cluster-default measured Dalim lossy "
            "and nick measured Piautos lossy - it is still making the entry-IP "
            "probe the arbiter and can put either consumer back on a server its "
            "in-tunnel agent already rejected (#792)" % names)
        assert state.snapshot()[5:7] == (2, 2), state.snapshot()

        # Unknown schema and a timestamp past the bounded future-skew allowance
        # are malformed, not authority. They must fail open.
        invalid = {
            "schema": 2, "source": "vpn-agent", "producer": "cluster-default",
            "server": "Dalim", "observed_at": stamp(), "ttl_seconds": 21600,
            "loss_pct": 8.0, "bad_windows": 3,
        }
        try:
            vp.parse_verdict(json.dumps(invalid).encode(), c, now_epoch=now)
        except ValueError:
            pass
        else:
            raise AssertionError("unknown verdict schema was accepted")
        invalid["schema"] = 1
        invalid["observed_at"] = stamp(c.verdict_future_skew_seconds + 1)
        try:
            vp.parse_verdict(json.dumps(invalid).encode(), c, now_epoch=now)
        except ValueError:
            pass
        else:
            raise AssertionError("future verdict timestamp was accepted")

        # Expiry restores the measured base ranking promptly and still preserves
        # its generated_at. This is a temporary ejection, never a permanent ban.
        _verdict(os.path.join(verdict_dir, "cluster-dalim.json"),
                 "cluster-default", "Dalim", stamp(-21601))
        _verdict(os.path.join(verdict_dir, "nick-piautos.json"),
                 "nick", "Piautos", stamp(-21601), loss=7.0)
        assert vp.refresh_ejections(state, c) is True
        with open(c.output_path, "rb") as fh:
            restored = json.load(fh)
        assert [s["name"] for s in restored["servers"]] == [
            "Dalim", "Piautos", "Menkent", "Dedalus"
        ], restored
        assert restored["generated_at"] == baseline["generated_at"]

        # Make all four verdicts valid. The newest half are ejected; the oldest
        # half stay in their original rank order. Never empty ranking on a rough
        # day, and never make `quick` the only fallback.
        _verdict(os.path.join(verdict_dir, "cluster-dalim.json"),
                 "cluster-default", "Dalim", stamp())
        _verdict(os.path.join(verdict_dir, "nick-piautos.json"),
                 "nick", "Piautos", stamp(-1), loss=7.0)
        _verdict(os.path.join(verdict_dir, "wrong-source.json"),
                 "cluster-default", "Menkent", stamp(-2))
        _verdict(os.path.join(verdict_dir, "stale.json"),
                 "nick", "Dedalus", stamp(-3))
        kept, ejected, capped = vp.apply_ejections(
            list(probed), vp.load_active_verdicts(c, now_epoch=now),
            c.max_ejection_fraction)
        assert [s["name"] for s in kept] == ["Menkent", "Dedalus"], kept
        assert [v["server"] for v in ejected] == ["Dalim", "Piautos"], ejected
        assert {v["server"] for v in capped} == {"Menkent", "Dedalus"}, capped
        assert len(kept) >= 1, "max-ejection cap emptied the ranking"

        one, one_ejected, one_capped = vp.apply_ejections(
            [probed[0]], [vp.load_active_verdicts(c, now_epoch=now)[0]],
            c.max_ejection_fraction)
        assert one == [probed[0]] and one_ejected == [] and len(one_capped) == 1, \
            "a verdict emptied a one-server ranking: %s" % ((one, one_ejected),)
        print("ok  #792: two producers merge; stale/malformed fail open; half-cap keeps ranking non-empty")
    finally:
        shutil.rmtree(d)


def test_verdict_refresh_publish_failure_keeps_consumers_identical_and_retries():
    """#792 review: an NFS failure cannot split file and HTTP consumers.

    The shared file and State.payload are one publication contract. If the
    atomic file replace fails, the HTTP state must stay on the old bytes so the
    next reconciliation still sees a change and retries. Committing memory on a
    failed write leaves k3s on the old ranking, Nick on the new one, and makes
    the transient failure permanent until another score cycle.
    """
    d = tempfile.mkdtemp()
    old_publish = vp.publish
    try:
        verdict_dir = os.path.join(d, "verdicts")
        c = cfg(output_path=os.path.join(d, "ranking.json"),
                verdict_dir=verdict_dir, max_ejection_fraction=0.5)
        base = [
            {"name": "Dalim", "entry_ip": "1.1.1.1", "loss_pct": 0.0,
             "rtt_ms": 20.0, "load": 10, "bw_max": 20000, "headroom": 18000},
            {"name": "Dedalus", "entry_ip": "1.1.1.2", "loss_pct": 0.0,
             "rtt_ms": 21.0, "load": 11, "bw_max": 20000, "headroom": 17900},
        ]
        generated_at = "2026-08-05T12:00:00Z"
        baseline = vp.build_document(base, c, generated_at=generated_at)
        old_publish(baseline, c.output_path)
        state = vp.State()
        with state.lock:
            state.payload = baseline
            state.generated_epoch = datetime.strptime(
                generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc).timestamp()
            state.passing = len(base)
            state.base_servers = list(base)
            state.base_generated_at = generated_at

        now = time.time()
        _verdict(
            os.path.join(verdict_dir, "cluster-dalim.json"),
            "cluster-default", "Dalim",
            datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

        attempts = []

        def fail_publish(payload, path):
            attempts.append((payload, path))
            raise OSError("transient NFS failure")

        vp.publish = fail_publish
        changed = vp.refresh_ejections(state, c)
        assert changed is False, (
            "refresh reported success after atomic publication failed - file and "
            "HTTP consumers cannot have received one ranking (#792 review)")
        assert state.snapshot()[0] == baseline, (
            "failed file publication still committed the ejected payload to HTTP "
            "state, splitting the two consumers (#792 review)")
        with open(c.output_path, "rb") as fh:
            assert fh.read() == baseline
        assert len(attempts) == 1

        # The in-memory bytes stayed old, so restoring the writer must retry and
        # converge both consumers without waiting for another 15-minute score.
        vp.publish = old_publish
        assert vp.refresh_ejections(state, c) is True, (
            "the reconciliation did not retry after a transient publish failure")
        with open(c.output_path, "rb") as fh:
            on_disk = fh.read()
        assert state.snapshot()[0] == on_disk, \
            "successful retry still left file and HTTP payloads different"
        assert [s["name"] for s in json.loads(on_disk)["servers"]] == ["Dedalus"]
        print("ok  #792 publish failure: file/HTTP stay identical and next refresh retries")
    finally:
        vp.publish = old_publish
        shutil.rmtree(d)


def test_restart_restores_servers_when_verdict_expires_or_breaks():
    """#792 review: an ejection lease must stay finite across scorer restart.

    ranking.json is already ejected, so it cannot be used as the pre-ejection
    base on startup. The scorer must persist the measured base separately and
    recover it after a restart; otherwise an expired or malformed verdict keeps
    the hidden server banned throughout an API/probe outage.
    """
    d = tempfile.mkdtemp()
    try:
        verdict_dir = os.path.join(d, "verdicts")
        c = cfg(
            output_path=os.path.join(d, "ranking.json"),
            base_state_path=os.path.join(d, "ranking-base.json"),
            verdict_dir=verdict_dir, max_ejection_fraction=0.5,
        )
        probed = [
            {"name": "Dalim", "entry_ip": "1.1.1.1", "loss_pct": 0.0,
             "rtt_ms": 20.0, "load": 10, "bw_max": 20000, "headroom": 18000},
            {"name": "Dedalus", "entry_ip": "1.1.1.2", "loss_pct": 0.0,
             "rtt_ms": 21.0, "load": 11, "bw_max": 20000, "headroom": 17900},
        ]
        api_rows = [
            {"public_name": row["name"], "country_code": "nl", "health": "ok",
             "currentload": row["load"], "bw": 2000, "bw_max": 20000,
             "ip_v4_in1": row["entry_ip"]}
            for row in probed
        ]
        now = time.time()
        stamp = lambda seconds=0: datetime.fromtimestamp(
            now + seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        verdict_path = os.path.join(verdict_dir, "cluster-dalim.json")
        _verdict(verdict_path, "cluster-default", "Dalim", stamp())

        old_fetch, old_probe = vp.fetch_servers, vp.probe_all
        vp.fetch_servers = lambda _cfg: api_rows
        vp.probe_all = lambda _candidates, _cfg: list(probed)
        try:
            state = vp.State()
            assert vp.run_cycle(state, c) is True
        finally:
            vp.fetch_servers, vp.probe_all = old_fetch, old_probe
        with open(c.output_path, "rb") as fh:
            ejected = fh.read()
        assert [s["name"] for s in json.loads(ejected)["servers"]] == ["Dedalus"]

        def restart_and_restore(reason):
            restarted = vp.State()
            vp.load_existing(restarted, c)
            assert [s["name"] for s in restarted.base_snapshot()[0]] == [
                "Dalim", "Dedalus"
            ], (
                "after scorer restart, ranking.json's already-ejected servers "
                "were adopted as the measured base; a %s verdict can no longer "
                "restore Dalim without a successful API/probe cycle (#792 review)"
                % reason)
            assert vp.refresh_ejections(restarted, c) is True, (
                "a %s verdict did not restore the hidden server after scorer "
                "restart during an API/probe outage (#792 review)" % reason)
            with open(c.output_path, "rb") as fh:
                restored = fh.read()
            assert restarted.snapshot()[0] == restored
            assert [s["name"] for s in json.loads(restored)["servers"]] == [
                "Dalim", "Dedalus"
            ]
            return restarted

        # Expiry after the process dies must restore from the separately
        # persisted measured base, not from the ejected public document.
        _verdict(verdict_path, "cluster-default", "Dalim", stamp(-21601))
        restored = restart_and_restore("expired")

        # Re-eject without a new score, then corrupt the verdict and repeat the
        # restart. Malformed input fails open in the same finite-lease shape.
        _verdict(verdict_path, "cluster-default", "Dalim", stamp())
        assert vp.refresh_ejections(restored, c) is True
        with open(verdict_path, "w") as fh:
            fh.write('{"schema": 1, "source": "vpn-agent"')
        restart_and_restore("malformed")
        print("ok  #792 restart: expired/malformed verdict restores the persisted measured base")
    finally:
        shutil.rmtree(d)


def test_missing_or_corrupt_base_state_fails_open_without_guessing():
    """#792 second review: unavailable restoration data must never be invented.

    This is explicitly a post-upgrade loss: ranking.json is already the ejected
    public view and ranking-base.json existed before it was deleted/corrupted.
    The hidden row cannot be reconstructed from public bytes. Safe fail-open is
    therefore bounded and honest: keep the last public bytes with their original
    timestamp, disable verdict reconciliation, let consumers reject them at the
    ranking TTL, and recover only after a fresh API/probe cycle writes a new
    journal. Adopting the filtered public list as a measured base would make the
    hidden server impossible to restore and could apply still more ejections.
    """
    d = tempfile.mkdtemp()
    try:
        c = cfg(
            output_path=os.path.join(d, "ranking.json"),
            base_state_path=os.path.join(d, "ranking-base.json"),
            verdict_dir=os.path.join(d, "verdicts"),
            api_url="http://127.0.0.1:1/unreachable",
        )
        generated_at = "2026-08-05T12:00:00Z"
        base = [
            {"name": "Dalim", "entry_ip": "1.1.1.1", "loss_pct": 0.0,
             "rtt_ms": 20.0, "load": 10, "bw_max": 20000, "headroom": 18000},
            {"name": "Dedalus", "entry_ip": "1.1.1.2", "loss_pct": 0.0,
             "rtt_ms": 21.0, "load": 11, "bw_max": 20000, "headroom": 17900},
        ]
        public = vp.build_document([base[1]], c, generated_at=generated_at)
        journal = vp.build_base_state(base, generated_at)
        vp.publish(public, c.output_path)
        vp.publish(journal, c.base_state_path)
        # A stale file proves the verdict channel existed, but cannot supply the
        # omitted ranking row. It must not be treated as restoration data.
        _verdict(
            os.path.join(c.verdict_dir, "cluster-dalim.json"),
            "cluster-default", "Dalim", "2026-08-04T00:00:00Z",
        )

        for failure in ("missing", "corrupt"):
            vp.publish(journal, c.base_state_path)
            if failure == "missing":
                os.unlink(c.base_state_path)
            else:
                with open(c.base_state_path, "wb") as fh:
                    fh.write(b'{"schema":1,"bases":[')

            state = vp.State()
            vp.load_existing(state, c)
            assert state.snapshot()[0] == public
            assert state.base_snapshot() == ([], None), (
                "a post-upgrade %s base journal made the ejected public ranking "
                "its own measured base; hidden Dalim can never be restored "
                "during an outage (#792 second review): %s" %
                (failure, state.base_snapshot()))
            assert vp.refresh_ejections(state, c) is False, (
                "verdict reconciliation ran without a measured base after a %s "
                "journal; fail-open must preserve public bytes until their TTL" %
                failure)
            assert state.snapshot()[0] == public
            with open(c.output_path, "rb") as fh:
                assert fh.read() == public
            assert vp.run_cycle(state, c) is False
            assert state.snapshot()[0] == public
            assert json.loads(public)["generated_at"] == generated_at
        print("ok  #792 unavailable base: preserve timestamped public bytes, disable reconciliation, never guess")
    finally:
        shutil.rmtree(d)


def test_base_state_requires_the_complete_ranking_row_contract():
    """#792 second review: hidden rows must be safe to publish later.

    A journal row is not merely an identifier. Expiry republishes it into the
    public ranking, so every hidden row must satisfy the exact schema-1 server
    contract before the journal is trusted. Name-only, wrong-type and extra-key
    rows are all corruption and fail open by disabling restoration.
    """
    valid = {"name": "Dedalus", "entry_ip": "1.1.1.2", "loss_pct": 0.0,
             "rtt_ms": 21.0, "load": 11, "bw_max": 20000,
             "headroom": 17900}
    malformed = [
        {"name": "Dalim"},
        dict(valid, loss_pct="0.0"),
        dict(valid, undocumented="accepted-by-name-only-validation"),
    ]
    for hidden in malformed:
        payload = json.dumps({
            "schema": 1,
            "bases": [{
                "generated_at": "2026-08-05T12:00:00Z",
                "servers": [valid, hidden],
            }],
        }).encode()
        try:
            vp.parse_base_state(payload)
        except ValueError:
            pass
        else:
            raise AssertionError(
                "a malformed hidden ranking row was accepted and can be "
                "published into schema 1 when a verdict expires (#792 second "
                "review): keys=%s" % sorted(hidden))
    print("ok  #792 base journal: every hidden row satisfies the complete public contract")


def test_stale_ranking_keeps_its_timestamp():
    """An API outage must not restamp the last good ranking."""
    d = tempfile.mkdtemp()
    try:
        c = cfg(output_path=os.path.join(d, "ranking.json"),
                api_url="http://127.0.0.1:1/unreachable")
        good = vp.build_document(
            [{"name": "Dedalus", "entry_ip": "109.235.50.5", "loss_pct": 0.0,
              "rtt_ms": 26.4, "load": 19, "bw_max": 20000, "headroom": 16801}],
            c, generated_at="2026-07-31T12:00:00Z")
        vp.publish(good, c.output_path)

        state = vp.State()
        vp.load_existing(state, c)
        assert state.payload == good, "existing ranking was not adopted on start"

        assert vp.run_cycle(state, c) is False, "unreachable API must not report success"
        payload, _, _, _, ok, _, _ = state.snapshot()
        assert payload == good, "stale cycle overwrote the last good ranking"
        assert json.loads(payload)["generated_at"] == "2026-07-31T12:00:00Z", \
            "stale ranking was restamped, the consumer TTL can never expire it"
        assert ok is False, "scrape_success must report the failed cycle"
        assert json.loads(open(c.output_path, "rb").read())["servers"], \
            "must never publish an empty list"
        print("ok  API outage: last good ranking kept, original generated_at, never empty")
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    test_shortlist()
    test_probe_gate()
    test_parse_ping()
    test_document_contract()
    test_atomic_publish()
    test_agent_verdicts_eject_servers_with_a_bounded_fail_open_cap()
    test_verdict_refresh_publish_failure_keeps_consumers_identical_and_retries()
    test_restart_restores_servers_when_verdict_expires_or_breaks()
    test_missing_or_corrupt_base_state_fails_open_without_guessing()
    test_base_state_requires_the_complete_ranking_row_contract()
    test_stale_ranking_keeps_its_timestamp()
    print("ALL SELF-CHECKS PASSED")
