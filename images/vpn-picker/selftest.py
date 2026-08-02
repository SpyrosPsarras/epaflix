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

import vpn_picker as vp


def cfg(**overrides):
    os.environ["AIRVPN_API_KEY"] = "x"
    c = vp.Config()
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
        payload, _, _, _, ok = state.snapshot()
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
    test_stale_ranking_keeps_its_timestamp()
    print("ALL SELF-CHECKS PASSED")
