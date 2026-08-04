#!/usr/bin/env python3
"""Self-checks for the agent's decision logic.

Run inside the built image by test.sh. Plain asserts, no framework - the point
is that a wrong decision fails the build.

The fake Bluetit below is the real seam: `Agent` drives it through the same
`Bluetit` class the live agent uses, so these exercise the actual call sequence
(`--disconnect` then `--air-connect --async`) and not a description of it.

The ranking fixture is the real 2026-08-02 document the live scorer published.
"""

import json
import logging
import os
import shutil
import stat
import tempfile
import time

import agent as ag


# The live ranking, 2026-08-02T09:30:59Z. Aspidiske - what `quick` actually
# picked on the live pod, a 2 Gbit box at 69% load - is deliberately absent.
RANKING = {
    "schema": 1,
    "generated_at": "2026-08-02T09:30:59Z",
    "ttl_seconds": 2100,
    "servers": [
        {"name": "Dalim", "entry_ip": "134.19.179.210", "loss_pct": 0.0,
         "rtt_ms": 26.7, "load": 24, "bw_max": 20000, "headroom": 15041},
        {"name": "Piautos", "entry_ip": "134.19.178.166", "loss_pct": 0.0,
         "rtt_ms": 26.87, "load": 22, "bw_max": 20000, "headroom": 15565},
        {"name": "Menkent", "entry_ip": "213.152.176.134", "loss_pct": 0.33,
         "rtt_ms": 27.07, "load": 20, "bw_max": 20000, "headroom": 15862},
        {"name": "Ashlesha", "entry_ip": "37.46.199.50", "loss_pct": 1.0,
         "rtt_ms": 30.37, "load": 25, "bw_max": 20000, "headroom": 14910},
    ],
}
GENERATED_EPOCH = 1785663059.0  # 2026-08-02T09:30:59Z


class FakeClock:
    """Time only moves when something sleeps, so a 6 h cooldown costs nothing."""

    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeBluetit:
    """Scripted goldcrest. Records every call so the walk can be asserted on.

    Two failure modes, because they cost very different amounts of the recovery
    budget: `refused` is the loud instant one Bluetit actually produces, and
    `silent` is a connect that is accepted and simply never lands.

    A connect takes `latency` seconds to land, matching the ~3-5 s measured on a
    real switch. Modelling that matters: an instant fake makes the recovery
    budget look infinite and hides a walk that has eaten the whole of it.
    """

    def __init__(self, clock, server="Aspidiske", refused=(), silent=(), latency=4.0):
        self.clock = clock
        self.server = server
        self.refused = {n.lower() for n in refused}
        self.silent = {n.lower() for n in silent}
        self.latency = latency
        self.pending = None
        self.ready_at = 0.0
        self.calls = []

    def _settle(self):
        if self.pending and self.clock() >= self.ready_at:
            self.server, self.pending = self.pending, None

    def __call__(self, args, cfg):
        self.calls.append(list(args))
        self._settle()
        if args[0] == "--bluetit-status":
            if self.server:
                return 0, ("Connected to AirVPN server %s (Somewhere, Netherlands)\n"
                           "___rc=0\n" % self.server)
            return 0, "Bluetit is not connected\n___rc=0\n"
        if args[0] == "--disconnect":
            self.server = self.pending = None
            return 0, "___rc=0\n"
        if args[0] == "--air-connect":
            assert "--async" in args, "--air-connect without --async tears the tunnel down"
            # Without an explicit --air-key the connect silently uses the
            # built-in `Default` key, not the one in bluetit.conf. Proved on a
            # scratch pod configured `airkey test`.
            assert "--air-key" in args, "--air-connect without --air-key uses the wrong device key"
            assert args[args.index("--air-key") + 1] == cfg.air_key, args
            name = args[args.index("--air-server") + 1] if "--air-server" in args else None
            if name is None:
                name = "QuickPick"                 # the quick-connect path
            elif name.lower() in self.refused:
                return 0, 'ERROR: AirVPN Server "%s" does not exist.\n___rc=1\n' % name
            if name.lower() not in self.silent:
                self.pending = name
                self.ready_at = self.clock() + self.latency
            return 0, "___rc=0\n"
        raise AssertionError("the agent may only call status/disconnect/air-connect, got %s"
                             % args)

    def connects(self):
        return [c[c.index("--air-server") + 1] if "--air-server" in c else "quick"
                for c in self.calls if c[0] == "--air-connect"]


def build(tmp, ranking=RANKING, server="Aspidiske", refused=(), silent=(), dead=(),
          throughput=0.0, **overrides):
    """An Agent wired to a fake daemon, a fake clock and a real ranking file.

    `dead` names servers with the 2026-08-02 shape: they connect, they answer
    the status grep with their own name, and no packet ever comes back.
    """
    os.environ["VPN_AGENT_RANKING_PATH"] = os.path.join(tmp, "ranking.json")
    os.environ["VPN_AGENT_CACHE_PATH"] = os.path.join(tmp, "cache", "ranking.json")
    os.environ["VPN_AGENT_BUDGET_PATH"] = os.path.join(tmp, "cache", "switches.json")
    cfg = ag.Config()
    cfg.jitter_seconds = 0.0
    # `lo` always exists, so tun_ok() runs its real /sys/class/net read and
    # passes. Point it at a device that does not exist to test the failure.
    cfg.tun_device = "lo"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    if ranking is not None:
        with open(cfg.ranking_path, "w") as fh:
            json.dump(ranking, fh)

    clock = FakeClock()
    fake = FakeBluetit(clock, server=server, refused=refused, silent=silent)
    bluetit = ag.Bluetit(cfg, runner=fake, sleep=clock.sleep, clock=clock)
    # The wall clock moves with the fake one, offset so the ranking starts 60 s
    # old. The switch budget is on wall time because it is persisted.
    agent = ag.Agent(cfg, bluetit=bluetit, sleep=clock.sleep, clock=clock,
                     wallclock=lambda: GENERATED_EPOCH + 60 + (clock() - 1000.0))

    # The probe is the agent's only view of real traffic, so model it off the
    # fake daemon's CURRENT server: a `dead` one reads as 100% loss exactly the
    # way the live tunnel did on 2026-08-02 while --bluetit-status kept naming
    # it. This also keeps every test off a real ping - 10.128.0.1 is
    # unreachable from a build container, so an unstubbed post-connect
    # verification would be a slow false red on every check in this file.
    dead_names = {n.lower() for n in dead}
    agent.probe = lambda count=None: (
        100.0 if (fake.server or "").lower() in dead_names else 0.0
    )
    # The throughput METRIC's input, stubbed for the same reason the probe is: the
    # fixture has no tun0, and the fake clock does not move during a scripted
    # probe, so the real /sys read would divide by a zero interval. It decides
    # nothing since #771, so its value does not change any degradation test - it
    # only makes vpn_agent_tunnel_throughput_bytes_per_sec assertable. The real
    # read and the real delta arithmetic are covered by
    # test_throughput_is_measured_not_assumed().
    agent.throughput_since = lambda before, started: throughput
    return agent, fake, clock


def scripted_probe(watch, verify=0.0):
    """Split the agent's two probe callers apart.

    `watch_once()` calls `probe()` with no count; the post-connect verification
    calls it with an explicit one. A test that scripts degradation windows must
    not have the verification eat one of them.
    """
    take = (lambda: watch.pop(0)) if isinstance(watch, list) else (lambda: watch)
    return lambda count=None: verify if count is not None else take()


def with_tmp(fn):
    def wrapper():
        tmp = tempfile.mkdtemp()
        try:
            fn(tmp)
        finally:
            shutil.rmtree(tmp)
    wrapper.__name__ = fn.__name__
    return wrapper


# --------------------------------------------------------------------------


@with_tmp
def test_stale_ranking_is_absent(tmp):
    """Past its TTL, stale = absent. One rule, no second staleness state."""
    payload = json.dumps(RANKING).encode()

    fresh, age = ag.parse_ranking(payload, GENERATED_EPOCH + 60)
    assert len(fresh) == 4 and age == 60, (fresh, age)

    # 2100 s TTL. One second past it is already gone.
    stale, age = ag.parse_ranking(payload, GENERATED_EPOCH + 2101)
    assert stale == [], "a ranking past its TTL must read as absent, got %s" % stale
    assert age == 2101, age

    # And the boundary is not off by one - exactly at the TTL it is still good.
    edge, _ = ag.parse_ranking(payload, GENERATED_EPOCH + 2100)
    assert len(edge) == 4, "the ranking must survive right up to its TTL"

    # A stale file must not refresh the cache, or the fallback chain silently
    # promotes an expired ranking to "last good".
    agent, _, _ = build(tmp)
    agent.wallclock = lambda: GENERATED_EPOCH + 9999
    assert agent.read_ranking() == []
    assert agent.cached == [], "a stale read updated the cache"
    assert not os.path.exists(agent.cfg.cache_path), "a stale read wrote the cache file"
    print("ok  stale ranking: absent past TTL, good at the TTL, never cached")


def metric(agent, name):
    """One metric value out of a real /metrics render, or None when absent."""
    for line in ag.render_metrics(agent).decode().splitlines():
        if line.startswith(name + " "):
            return float(line.split()[1])
    return None


@with_tmp
def test_ranking_age_is_measured_at_scrape_time(tmp):
    """#686: the age must not be pinned to whatever some earlier read left.

    `read_ranking()` runs at boot and when a switch is being decided. A healthy
    pod does neither again, so an age cached by that path never moves - the live
    pod reported 266 s for its entire life while the real ranking aged past
    11 minutes. Read the ranking once, then move ONLY the wall clock: no probe,
    no trip, no switch, which is exactly the steady state that froze it. The
    number has to follow.
    """
    agent, _, clock = build(tmp)

    assert len(agent.read_ranking()) == 4      # the one read the old code had
    assert metric(agent, "vpn_agent_ranking_age_seconds") == 60.0, "build() starts it 60 s old"

    clock.sleep(600)
    aged = metric(agent, "vpn_agent_ranking_age_seconds")
    assert aged == 660.0, (
        "the ranking aged 600 s and the metric says %s - it is reporting a value "
        "written by some earlier code path instead of measuring (#686)" % aged)

    # Past the 2100 s TTL the ranking is absent for DECISIONS, but the metric
    # exists to say how far gone it is, so it must keep counting.
    clock.sleep(2000)
    assert metric(agent, "vpn_agent_ranking_age_seconds") == 2660.0

    # A truncated or missing file is an honest absent - never a stale number,
    # and never an exception out of the /metrics handler.
    with open(agent.cfg.ranking_path, "w") as fh:
        fh.write('{"schema": 1, "generated_a')
    assert metric(agent, "vpn_agent_ranking_age_seconds") is None, \
        "a truncated ranking must not report an age"
    os.unlink(agent.cfg.ranking_path)
    assert metric(agent, "vpn_agent_ranking_age_seconds") is None, \
        "a missing ranking must not report an age"
    # And the rest of the render still works with no ranking at all.
    assert metric(agent, "vpn_agent_dry_run") == 0.0
    print("ok  ranking age: measured at scrape time, absent when the file is not usable")


class CaptureLog(logging.Handler):
    """The agent's own log records, so "must not log" can be asserted on."""

    def __init__(self):
        logging.Handler.__init__(self)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def __enter__(self):
        ag.log.addHandler(self)
        return self

    def __exit__(self, *exc):
        ag.log.removeHandler(self)
        return False

    def messages(self):
        return [r.getMessage() for r in self.records]


@with_tmp
def test_tunnel_device_is_measured_at_scrape_time(tmp):
    """#690: a `1` nothing ever measured is worse than a stale measurement.

    `vpn_agent_tunnel_device_ok` was a field initialised True and written only on
    the switch path, so a pod that never switched published a green it had never
    looked at. Here the device goes away with NO switch, NO probe and NO trip -
    exactly the steady state that hid it - and the metric has to notice.
    """
    agent, _, _ = build(tmp)                  # build() points tun_device at `lo`
    assert metric(agent, "vpn_agent_tunnel_device_ok") == 1.0, \
        "`lo` exists, so a real read must pass"

    agent.cfg.tun_device = "tun-nope-0"
    got = metric(agent, "vpn_agent_tunnel_device_ok")
    assert got == 0.0, (
        "the tunnel device is gone and the metric says %s - it is publishing a "
        "value nothing measured. qBittorrent would be bound to a device that "
        "does not exist with every signal green (#690)" % got)

    # No field left for the metric to be wired back to. An unread cache is what
    # invites exactly that, which is why #686 deleted its one too.
    assert not hasattr(agent, "tun_device_ok"), \
        "self.tun_device_ok is back - a cached device verdict is what #690 removed"

    # Scraping must be SILENT. /metrics is scraped every 60 s and tun_ok()'s log
    # line is four lines of recovery instructions, so the loud version in here
    # would bury the one occurrence that matters under a thousand copies.
    with CaptureLog() as caught:
        for _ in range(5):
            ag.render_metrics(agent)
        assert caught.records == [], (
            "%d log records from scraping a missing device - at 60 s scrapes that "
            "is a flood: %s" % (len(caught.records), caught.messages()))

        # The switch path stays loud, or a genuinely taken tun0 slot goes unsaid.
        assert agent.bluetit.tun_ok() is False
        assert any("MISSING" in m for m in caught.messages()), \
            "tun_ok() stopped saying the device is missing: %s" % caught.messages()
    print("ok  tun device: measured at scrape time, silent there, still loud on a switch")


@with_tmp
def test_switch_in_progress_marks_the_device_rebuild(tmp):
    """The mid-switch transient, made readable instead of fudged.

    WireGuardClient::stop() calls wg_del_device() on every disconnect, so a
    scrape landing inside a switch honestly sees no tunnel device. Without a
    second series a reader cannot tell that true 0 from a taken tun0 slot - which
    is the silent failure vpn_agent_tunnel_device_ok exists to catch. Suppressing
    the device metric during a switch was the alternative and is worse: absent is
    the one state that cannot be told apart from "the agent stopped answering".
    """
    agent, _, _ = build(tmp, server="Aspidiske")
    inner = agent.probe
    during = []

    def probe(count=None):
        if count is not None:      # the post-connect verification, mid-switch
            during.append(metric(agent, "vpn_agent_switch_in_progress"))
        return inner(count)

    agent.probe = probe

    idle = metric(agent, "vpn_agent_switch_in_progress")
    assert idle == 0.0, (
        "vpn_agent_switch_in_progress reads %s on an idle agent - with no such "
        "series a 0 on vpn_agent_tunnel_device_ok cannot be told apart from the "
        "device rebuild every switch performs (#690)" % idle)
    agent.boot_check()
    assert during == [1.0], (
        "a scrape inside the switch reported switch_in_progress=%s, so a 0 on "
        "vpn_agent_tunnel_device_ok cannot be told apart from a taken tun0 slot"
        % during)
    assert metric(agent, "vpn_agent_switch_in_progress") == 0.0, \
        "the flag never cleared - a stuck 1 excuses every later device failure"

    # And it clears through a crash. run() wraps watch_once() in a bare `except`
    # to keep the container up, so a flag left set would stay set for the life of
    # the pod - the same permanently-wrong signal this change removes.
    boom = RuntimeError("connect blew up")

    def explode(*args, **kwargs):
        raise boom

    agent.bluetit.disconnect = explode
    try:
        agent.switch(["Dalim"], "degradation", mandatory=True)
    except RuntimeError as exc:
        assert exc is boom, exc
    else:
        raise AssertionError("the fixture did not raise")
    assert metric(agent, "vpn_agent_switch_in_progress") == 0.0, \
        "a crash inside the switch left switch_in_progress stuck at 1"
    print("ok  switch window: flagged while the device is rebuilt, cleared even on a crash")


@with_tmp
def test_current_server_is_refreshed_every_window(tmp):
    """#690: the label must not stay on whatever boot found.

    `--bluetit-status` used to be called only AFTER the loss threshold tripped,
    below the clean-window early return, so a healthy pod refreshed it exactly
    never. The tunnel does move under the agent: the `airvpn` sidecar restarts on
    its own liveness probe and `airconnectatboot quick` picks again, while this
    process keeps running - so the metric names a server we are not on.

    The window below is entirely clean: no loss, no trip, no switch.
    """
    agent, fake, _ = build(tmp, server="Menkent")     # in band, so boot stays put
    agent.probe = scripted_probe(0.0)

    agent.boot_check()
    assert agent.current_server == "Menkent", agent.current_server

    fake.server = "Ashlesha"                          # the sidecar picked again
    agent.watch_once()

    assert agent.current_server == "Ashlesha", (
        "the agent still reports %s after the tunnel moved under it - the label "
        "is whatever boot found, for the life of the pod (#690)"
        % agent.current_server)
    assert b'vpn_agent_current_server{server="Ashlesha"} 1' in ag.render_metrics(agent)

    # Exactly one status call per window. This is the cost the fix pays, so it is
    # asserted: not one per scrape, and not several per window either.
    def status_calls():
        return sum(1 for c in fake.calls if c[0] == "--bluetit-status")

    before = status_calls()
    for _ in range(3):
        agent.watch_once()
    assert status_calls() - before == 3, \
        "3 clean windows made %d status calls, expected 3" % (status_calls() - before)

    # And a scrape makes NONE. render_metrics() runs in the HTTP handler thread
    # and a goldcrest call can take 25 s, which would blow the Prometheus scrape
    # timeout and take the target down - losing every metric, honest ones too.
    before = status_calls()
    for _ in range(5):
        ag.render_metrics(agent)
    assert status_calls() == before, "a /metrics scrape called goldcrest"

    # Bluetit going down is an honest absent, never a stale name.
    fake.server = None
    agent.watch_once()
    assert agent.current_server is None, agent.current_server
    assert b"vpn_agent_current_server" not in ag.render_metrics(agent), \
        "kept naming a server while Bluetit reports not connected"
    print("ok  current server: re-read every window, one call each, none per scrape")


@with_tmp
def test_boot_in_band_stays_put(tmp):
    """Inside the band, do nothing. Ordinary pod restarts must not churn."""
    agent, fake, _ = build(tmp, server="Menkent")
    agent.boot_check()

    assert fake.connects() == [], "switched while already inside the top 5"
    assert not any(c[0] == "--disconnect" for c in fake.calls), \
        "tore down a perfectly good tunnel"
    assert fake.server == "Menkent"
    assert agent.switches["boot_upgrade"] == 0

    # Case-insensitive, because the connect path is.
    assert ag.in_band("menkent", RANKING["servers"], 5)
    assert ag.in_band("DALIM", RANKING["servers"], 5)
    assert not ag.in_band("Aspidiske", RANKING["servers"], 5)
    assert not ag.in_band(None, RANKING["servers"], 5)
    # The band is a real cut, not the whole list.
    assert not ag.in_band("Ashlesha", RANKING["servers"], 2)
    print("ok  boot: in-band stays put, band test is case-insensitive and bounded")


@with_tmp
def test_boot_out_of_band_upgrades(tmp):
    """quick's 2 Gbit pick is not in the top 5, so upgrade to the winner."""
    agent, fake, _ = build(tmp, server="Aspidiske")
    agent.boot_check()

    assert fake.connects() == ["Dalim"], fake.connects()
    assert fake.server == "Dalim"
    assert agent.switches["boot_upgrade"] == 1
    # A live switch is REFUSED while connected, so the disconnect must come
    # first and there must be exactly one of it.
    order = [c[0] for c in fake.calls if c[0] in ("--disconnect", "--air-connect")]
    assert order == ["--disconnect", "--air-connect"], order
    print("ok  boot: out-of-band upgrades, disconnect strictly before connect")


@with_tmp
def test_boot_without_a_ranking_stays_on_quick(tmp):
    agent, fake, _ = build(tmp, ranking=None, server="Aspidiske")
    agent.boot_check()
    assert fake.connects() == [], "switched with no ranking to switch to"
    assert fake.server == "Aspidiske"
    print("ok  boot: no ranking means stay on quick's pick")


@with_tmp
def test_degradation_counting(tmp):
    """Three consecutive bad windows, and a good one resets the count."""
    agent, fake, _ = build(tmp, server="Aspidiske")
    losses = []
    agent.probe = scripted_probe(losses)

    losses[:] = [0.0, 6.7, 8.0, 0.5, 9.0, 7.0]
    for _ in range(6):
        assert agent.watch_once() is False, "tripped too early"
    assert agent.consecutive_bad == 2, agent.consecutive_bad

    losses[:] = [6.0]
    assert agent.watch_once() is True, "did not trip on the third consecutive bad window"
    assert fake.connects() == ["Dalim"], fake.connects()
    assert agent.switches["degradation"] == 1
    assert agent.consecutive_bad == 0, "the counter must reset after a switch"

    # 4.99% is not a bad window. The bar is >= 5%.
    agent2, fake2, _ = build(tmp, server="Aspidiske")
    agent2.probe = scripted_probe(4.99)
    for _ in range(5):
        agent2.watch_once()
    assert agent2.consecutive_bad == 0, "counted a window under the threshold"
    assert fake2.connects() == []
    print("ok  degradation: 3 consecutive trips, a good window resets, 4.99% is clean")


@with_tmp
def test_high_loss_counts_on_a_busy_tunnel_too(tmp):
    """#771: throughput does not excuse loss. It is a metric, not a veto.

    PR #764 gated the verdict on tun0 being busy, on the theory that loaded
    seeding puts a 15-20% ICMP floor under every window. Measured 2026-08-04,
    same probe and same code, that is not so: Dedalus carried 13.66 MB/s at 0.0%
    loss and 3.49 MB/s at 0.0%, while Dalim had read 15% at ~4.2 MB/s the day
    before. Load is not the variable, so a busy tunnel at 15% is a real signal -
    and the gate suppressed it, on a server that was delivering 3.8 MiB/s while
    an alternative delivered 13.7.

    Worse than a wrong verdict, it was an unreachable one: a seeding box is
    almost never under 64 KiB/s, so the loss path was effectively never
    consulted while the detector reported itself armed.
    """
    # One tmp per case: the switch budget file lives in it, so two trips sharing a
    # directory means the second is suppressed by the first one's cooldown.
    def sub(name):
        path = os.path.join(tmp, name)
        os.makedirs(path)
        return path

    # The 2026-08-03 Dalim window: 15% loss with 3.8 MiB/s + 292 KiB/s moving.
    busy, fake, _ = build(sub("busy"), server="Aspidiske",
                          throughput=(3825 + 292) * 1024.0)
    busy.probe = scripted_probe(15.0)
    for i in range(busy.cfg.bad_windows - 1):
        assert busy.watch_once() is False, "tripped too early"
        assert busy.consecutive_bad == i + 1, (
            "window %d at 15%% loss on a tunnel moving 4 MiB/s left "
            "consecutive_bad at %d - throughput is suppressing the loss "
            "verdict, which is the #771 gate" % (i + 1, busy.consecutive_bad))
    assert busy.watch_once() is True, \
        "3 windows at 15% loss did not trip because tun0 was busy - that is the " \
        "PR #764 throughput gate, removed in #771: it suppressed a correct " \
        "detection on a lossy-but-still-delivering server"
    assert fake.connects() == ["Dalim"], fake.connects()

    # The idle-tunnel half needs no case of its own: build()'s default throughput is
    # 0.0, so test_degradation_counting() already trips on loss alone with tun0 idle.

    # An UNMEASURABLE window is a missing metric, not a veto. The gate returned
    # early here, so a torn-down tun0 (every switch rebuilds it) silently held the
    # verdict back. Loss is the whole decision now, so it counts.
    blind, fake3, _ = build(sub("blind"), server="Aspidiske")
    blind.throughput_since = lambda before, started: None
    blind.probe = scripted_probe(15.0)
    for _ in range(blind.cfg.bad_windows - 1):
        assert blind.watch_once() is False, "tripped too early"
    assert blind.watch_once() is True, \
        "3 windows at 15% loss did not trip while tun0 throughput was " \
        "unmeasurable - an absent metric is vetoing the decision (#771)"
    assert fake3.connects() == ["Dalim"], fake3.connects()

    # And the counter this all used to feed is GONE, not parked at 0 (#771).
    assert not hasattr(busy, "healthy_by_throughput"), \
        "healthy_by_throughput is still a field - with the gate removed nothing " \
        "can write it, and a counter no code path writes is the #629/#686/#690 " \
        "shape this subsystem keeps producing"
    assert metric(busy, "vpn_agent_healthy_by_throughput_windows_total") is None, \
        "vpn_agent_healthy_by_throughput_windows_total is still published; " \
        "nothing increments it, so it can only ever read 0 (#691/#729 precedent: " \
        "delete it, do not merely stop reading it)"
    print("ok  #771: loss decides on a busy tunnel too, no gate, no dead counter")


@with_tmp
def test_throughput_is_measured_not_assumed(tmp):
    """The real /sys read and the real delta arithmetic, no fixture in the way.

    build() models `throughput_since()`, so without this test the gate would be
    tested against a stub of its own input. Here the counters are real files
    (`lo`, which always exists) and a fake device, and the arithmetic runs for
    real - including the reset case, which is NOT an edge: tun0 is deleted and
    rebuilt on every switch (wg_del_device) so its counters restart at 0.
    """
    agent, fake, clock = build(tmp)             # build() points tun_device at `lo`
    del agent.throughput_since                  # drop the stub, use the real method

    live = agent.bluetit.tun_bytes()
    assert isinstance(live, int) and live >= 0, \
        "the real rx+tx read of `lo` returned %r" % (live,)

    # A missing device is None - never 0. Zero would mean "idle tunnel", which
    # hands the verdict straight back to the loss threshold (#732).
    agent.cfg.tun_device = "tun-nope-0"
    assert agent.bluetit.tun_bytes() is None, \
        "an unreadable counter read as a number - a missing device is not an idle one"
    assert agent.throughput_since(0, clock() - 10) is None
    agent.cfg.tun_device = "lo"

    # And it is SILENT, on both paths. This runs once per window AND once per
    # scrape, so a log line here is 2 lines a minute forever (#690's flood).
    with CaptureLog() as caught:
        agent.cfg.tun_device = "tun-nope-0"
        for _ in range(5):
            agent.bluetit.tun_bytes()
            ag.render_metrics(agent)
        assert caught.records == [], \
            "%d log records from reading a missing counter: %s" % (
                len(caught.records), caught.messages())
    agent.cfg.tun_device = "lo"

    # The counter reset. `before` is the old device's total, the read after it is
    # the new device starting from ~0, so the delta is negative.
    agent.bluetit.tun_bytes = lambda: 24576
    assert agent.throughput_since(50 * 1024 * 1024, clock() - 10) is None, \
        "a negative delta produced a number - tun0 is rebuilt on every switch, " \
        "so this is the normal post-switch window, and reading it as 0 bytes/s " \
        "would count the window as bad on loss alone"
    # Nothing to divide by is no number either.
    assert agent.throughput_since(0, clock()) is None
    # A plain positive delta is the ordinary case, and the arithmetic is bytes/sec.
    assert agent.throughput_since(4096, clock() - 10) == 2048.0
    print("ok  throughput: real /sys read, silent, resets and gaps are no-number not zero")


@with_tmp
def test_throughput_metrics_are_measurements(tmp):
    """#686/#690 again: nothing published that nothing refreshes.

    `vpn_agent_tunnel_bytes_total` is read from /sys at scrape time, so it has to
    disappear when the device does. `vpn_agent_tunnel_throughput_bytes_per_sec` is
    last window's number, so it has to disappear when a window cannot produce one -
    stale here would be the exact #686 defect.
    """
    agent, _, _ = build(tmp, throughput=1024.0 * 1024.0)
    agent.probe = scripted_probe(15.0)

    assert metric(agent, "vpn_agent_tunnel_bytes_total") is not None, \
        "`lo` exists, so the scrape-time counter read must produce a value"
    assert metric(agent, "vpn_agent_tunnel_throughput_bytes_per_sec") is None, \
        "a rate was published before any window measured one"

    agent.watch_once()
    assert metric(agent, "vpn_agent_tunnel_throughput_bytes_per_sec") == 1048576.0
    # Loss is published because it is the decision (#771). The throughput series
    # next to it is context, not a reason to discount it.
    assert metric(agent, "vpn_agent_current_loss_pct") == 15.0

    agent.throughput_since = lambda before, started: None
    agent.watch_once()
    assert metric(agent, "vpn_agent_tunnel_throughput_bytes_per_sec") is None, \
        "the rate survived a window that produced no measurement - a stale rate " \
        "is what #686 was"

    agent.cfg.tun_device = "tun-nope-0"
    assert metric(agent, "vpn_agent_tunnel_bytes_total") is None, \
        "the byte counter kept publishing with no device - it is not being read " \
        "at scrape time (#690)"
    print("ok  throughput metrics: measured at scrape time, absent instead of stale")


@with_tmp
def test_dead_tunnel_is_the_liveness_probes_job(tmp):
    """100% loss with Bluetit down is exempt - a switch cannot fix that."""
    agent, fake, _ = build(tmp, server=None)
    agent.probe = scripted_probe(100.0)
    for _ in range(5):
        assert agent.watch_once() is False
    assert agent.consecutive_bad == 0, "counted windows against a disconnected daemon"
    assert fake.connects() == [], "tried to switch a tunnel that is not up"
    print("ok  dead tunnel: exempt, never counted, never switched")


@with_tmp
def test_cooldown_and_daily_cap(tmp):
    agent, fake, clock = build(tmp, server="Aspidiske")
    agent.probe = scripted_probe(9.0)

    def trip():
        for _ in range(agent.cfg.bad_windows):
            got = agent.watch_once()
        return got

    assert trip() is True
    assert agent.switches["degradation"] == 1

    # Straight back into a bad window - the cooldown must hold.
    assert trip() is False, "switched again inside the 6 h cooldown"
    assert agent.switches["degradation"] == 1

    # Just short of 6 h is still inside it.
    clock.sleep(agent.cfg.cooldown_seconds - 10)
    assert trip() is False, "cooldown expired early"

    clock.sleep(20)
    assert trip() is True, "cooldown never expired"
    assert agent.switches["degradation"] == 2

    clock.sleep(agent.cfg.cooldown_seconds + 1)
    assert trip() is True
    assert agent.switches["degradation"] == 3

    # Three in a day is the cap, and it outlives the cooldown.
    clock.sleep(agent.cfg.cooldown_seconds + 1)
    assert trip() is False, "went over the 3-a-day cap"
    assert agent.switches["degradation"] == 3

    # A full day later the window has rolled.
    clock.sleep(86401)
    assert trip() is True, "the daily cap never rolls off"
    print("ok  cooldown: holds for the full 6 h, daily cap of 3 holds past it and rolls")


@with_tmp
def test_failed_connect_walks_the_pool(tmp):
    """2 attempts per candidate, then the next one in the top 5."""
    agent, fake, clock = build(tmp, server="Aspidiske",
                               refused=("Dalim", "Piautos"))
    started = clock()
    agent.boot_check()

    assert fake.connects() == ["Dalim", "Dalim", "Piautos", "Piautos", "Menkent"], \
        fake.connects()
    assert fake.server == "Menkent"
    assert agent.switches["boot_upgrade"] == 1
    assert agent.switches["fallback"] == 0
    assert clock() - started <= agent.cfg.recovery_budget, \
        "the walk ran past the recovery budget"
    print("ok  failed connect: 2 attempts each, walks to the next candidate, in budget")


@with_tmp
def test_every_candidate_fails_ends_on_quick(tmp):
    """Connected to a mediocre server beats tunnel-less. Then the breaker."""
    agent, fake, clock = build(tmp, server="Aspidiske",
                               refused=("Dalim", "Piautos", "Menkent", "Ashlesha"))
    started = clock()
    agent.boot_check()

    assert fake.server == "QuickPick", fake.server
    assert fake.connects()[-1] == "quick", fake.connects()
    assert agent.switches["fallback"] == 1
    assert agent.switches["boot_upgrade"] == 0, "a fallback is not a successful upgrade"
    assert clock() - started <= agent.cfg.recovery_budget, \
        "recovery ran past the budget - the liveness restart would beat us to it"

    # The circuit breaker: ending on quick arms the full cooldown.
    allowed, why = agent.budget.allowed()
    assert allowed is False, "a failed recovery did not arm the cooldown"
    assert "cooldown" in why, why
    print("ok  total failure: ends on quick inside the budget, then holds the cooldown")


@with_tmp
def test_recovery_never_spends_the_quick_reserve(tmp):
    """A long walk must still leave a verify window for the fallback.

    This is the sharp part of Component 7: a kubelet restart is a SIGTERM and
    SIGTERM drops the network lock, so the supervisor must never become the
    recovery path.
    """
    ranking = dict(RANKING, servers=[{"name": "S%d" % i, "loss_pct": 0.0}
                                     for i in range(5)])
    agent, fake, clock = build(tmp, ranking=ranking, server="Aspidiske",
                               silent=["S%d" % i for i in range(5)],
                               recovery_budget=60, quick_reserve=35)
    started = clock()
    agent.boot_check()

    assert fake.server == "QuickPick", \
        "the walk spent the reserve, so the quick fallback never landed and the pod " \
        "is tunnel-less waiting for a SIGTERM that drops the network lock"
    assert agent.switches["fallback"] == 1
    assert clock() - started <= 60 + agent.cfg.verify_seconds, clock() - started
    print("ok  budget: the walk is cut short so the quick fallback always runs")


@with_tmp
def test_wrong_tunnel_device_fails_the_switch(tmp):
    """tun0 is destroyed and rebuilt on every switch. If it comes back as
    anything else, qBittorrent has no listen socket at all."""
    agent, fake, _ = build(tmp, server="Aspidiske", tun_device="tun-nope-0")
    agent.boot_check()

    assert fake.connects() == ["Dalim"], "kept walking after a device failure"
    assert agent.switches["boot_upgrade"] == 0, \
        "a switch that left qBittorrent with no listen socket was counted as a success"
    assert b"vpn_agent_tunnel_device_ok 0" in ag.render_metrics(agent), \
        "the missing device is invisible in metrics"
    # The switch is over, so the 0 above is the real thing and not the teardown.
    assert b"vpn_agent_switch_in_progress 0" in ag.render_metrics(agent)
    print("ok  tun device: not tun0 means the switch failed, and it says so in metrics")


@with_tmp
def test_verification_rejects_a_lying_status_string(tmp):
    """#627 fix 1. `Connected to AirVPN server X` is PROVEN to lie.

    On 2026-08-02 it said exactly that for 13 minutes over a WireGuard
    interface that never completed a handshake - 0 B transferred, 100% ICMP
    loss, no AirVPN session for the device key - and it was reproduced
    deliberately by blackholing only the handshake port. The string was the
    agent's only success criterion, so the agent declared the switch a success
    and went to sleep. A switch is only successful when a packet comes back.
    """
    agent, fake, _ = build(tmp, server="Aspidiske", dead=("Dalim",))
    agent.boot_check()

    # Dalim answered the status grep with its own name, exactly as the live
    # daemon did. Traffic said otherwise, so the agent walked on to Piautos.
    assert fake.connects()[0] == "Dalim", fake.connects()
    assert fake.server == "Piautos", \
        "the agent settled on %s - a status string with no traffic behind it " \
        "was accepted as a working tunnel" % fake.server
    assert agent.current_server == "Piautos", agent.current_server
    assert agent.switches["boot_upgrade"] == 1, agent.switches
    assert agent.switches["fallback"] == 0, "a verified candidate is not a fallback"

    # And the verified switch arms the FULL cooldown, unchanged.
    allowed, why = agent.budget.allowed()
    assert allowed is False and "cooldown" in why, why
    print("ok  verification: a connected-but-silent tunnel fails the switch")


@with_tmp
def test_a_failed_candidate_is_dropped_not_redialled(tmp):
    """#627 fix 3. Never re-dial a name that failed verification.

    Re-dialling the same dead peer is precisely what Bluetit's internal
    reconnect already does, and that was measured doing it four times over a
    verified-clean path with zero handshakes. A refused connect still gets its
    two attempts (that is a different failure); a silent tunnel gets one.
    """
    agent, fake, _ = build(tmp, server="Aspidiske", dead=("Dalim", "Piautos"))
    agent.boot_check()

    assert fake.connects() == ["Dalim", "Piautos", "Menkent"], fake.connects()
    assert fake.connects().count("Dalim") == 1, \
        "re-dialled a candidate that had already proven it passes no traffic"
    assert fake.server == "Menkent", fake.server
    print("ok  walk: a candidate that fails verification is dropped, never re-dialled")


@with_tmp
def test_recovery_is_disconnect_plus_connect_never_a_signal(tmp):
    """#627 fix 4. Every attempt is a FRESH --disconnect + --air-connect.

    Only a fresh connect re-authenticates with AirVPN ("Logging in AirVPN user
    ... successfully logged in ... Selected user key"). Bluetit's internal
    reconnect - what SIGUSR2 triggers - rebuilds tun0 from the cached profile
    with no login at all, so it can never get a dropped peer back. Bluetit also
    refuses a connect while it believes it is connected, so the disconnect
    after a failed verification is load-bearing, not tidiness.
    """
    agent, fake, _ = build(tmp, server="Aspidiske", dead=("Dalim",))
    agent.boot_check()

    pairs = [c[0] for c in fake.calls if c[0] in ("--disconnect", "--air-connect")]
    assert pairs == ["--disconnect", "--air-connect",   # Dalim, connects and lies
                     "--disconnect", "--air-connect"], pairs
    # FakeBluetit raises on anything that is not status/disconnect/air-connect,
    # so a signal or a listing call cannot pass this line silently.
    assert all(c[0] in ("--bluetit-status", "--disconnect", "--air-connect")
               for c in fake.calls), fake.calls
    print("ok  recovery: fresh disconnect + connect for every attempt, no signal")


def test_no_signal_path_survives_in_the_source():
    """#611. The `kill -USR2` degradation watcher must not come back.

    A grep, because this is the one regression that reads as harmless in a
    diff. `kill -0` is fine and stays - it sends no signal, it only asks
    whether Bluetit is still alive.
    """
    # Ways either file could actually deliver a signal. Prose about SIGUSR2 is
    # wanted, a call that sends one is not - so match the call shapes, not the
    # word. agent.py does not import `signal` at all and must not start.
    forbidden = ("kill -USR2", "kill -SIG", "os.kill", "pkill",
                 "import signal", "signal.SIG")
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("agent.py", "entrypoint.sh"):
        with open(os.path.join(here, name)) as fh:
            lines = fh.read().splitlines()
        for line in lines:
            if line.lstrip().startswith("#"):
                continue        # a comment explaining the ban is the point
            for bad in forbidden:
                assert bad not in line, \
                    "%s signals Bluetit again (%r): %s" % (name, bad, line.strip())
            assert "kill -" not in line or "kill -0" in line, \
                "%s sends a signal to Bluetit: %s" % (name, line.strip())
        assert any("USR2" in l for l in lines), \
            "%s lost the comment saying WHY there is no USR2 path - the next " \
            "person will re-add it" % name
    print("ok  #611: no USR2 watcher in agent.py or entrypoint.sh, and both say why")


@with_tmp
def test_a_failed_switch_does_not_arm_the_full_cooldown(tmp):
    """#627 fix 2. The 2026-08-02 lockout, in one test.

    `_finish()` used to record unconditionally, so the boot upgrade onto a
    tunnel that never handshook armed the full 21600 s. Three minutes later the
    agent's own degradation watch reached bad_windows=3, called
    budget.allowed(), was told "cooldown, ~21400s left", logged `degradation
    switch suppressed` and did nothing for six hours. A switch that produced no
    working tunnel must never lock out recovery from itself.
    """
    agent, fake, clock = build(
        tmp, server="Aspidiske",
        dead=("Dalim", "Piautos", "Menkent", "Ashlesha", "QuickPick"))
    agent.boot_check()

    # Everything lied, including the terminal `quick` fallback.
    assert fake.connects() == ["Dalim", "Piautos", "Menkent", "Ashlesha", "quick"], \
        fake.connects()

    allowed, why = agent.budget.allowed()
    assert allowed is False, "a failed switch must still hold a short cooldown"

    clock.sleep(agent.cfg.failed_cooldown_seconds + 1)
    allowed, why = agent.budget.allowed()
    assert allowed is True, \
        "a switch that produced NO working tunnel armed a cooldown longer than " \
        "%ds and locked the agent out of its own recovery: %s" \
        % (agent.cfg.failed_cooldown_seconds, why)
    assert agent.cfg.failed_cooldown_seconds < agent.cfg.cooldown_seconds

    # It still costs one of the day's three, so a broken pool cannot thrash
    # instead. Two more failures and the cap holds for the rest of the day.
    for _ in range(2):
        agent.switch(["Dalim"], "degradation", mandatory=True)
        clock.sleep(agent.cfg.failed_cooldown_seconds + 1)
    allowed, why = agent.budget.allowed()
    assert allowed is False and "daily cap" in why, \
        "failed switches do not count against the daily cap: %s" % why

    # A switch that WORKS still arms the full 6 h - the Component 7 circuit
    # breaker is untouched, and that is checked by
    # test_every_candidate_fails_ends_on_quick and test_cooldown_and_daily_cap.
    print("ok  cooldown: a failed switch arms the short one, still costs a daily slot")


@with_tmp
def test_cache_is_the_fallback_when_the_file_goes_away(tmp):
    """fresh ranking > cached last-good > quick."""
    agent, fake, _ = build(tmp, server="Aspidiske")
    assert agent.read_ranking(), "the fresh read failed"
    assert os.path.exists(agent.cfg.cache_path)

    os.unlink(agent.cfg.ranking_path)
    agent.cached = []
    agent.load_cache()
    names, source = agent.candidates(exclude="Aspidiske")
    assert source == "cache", source
    assert names == ["Dalim", "Piautos", "Menkent", "Ashlesha"], names

    # The server we are already on is never a candidate - a cached ranking will
    # usually still list the box that is dropping our packets.
    names, _ = agent.candidates(exclude="Dalim")
    assert "Dalim" not in names, names

    shutil.rmtree(os.path.dirname(agent.cfg.cache_path))
    agent.cached = []
    names, source = agent.candidates()
    assert (names, source) == ([], "none"), (names, source)
    print("ok  fallback chain: fresh > cache > quick, current server excluded")


@with_tmp
def test_budget_survives_a_restart(tmp):
    """The launcher restarts a crashed agent.py. An in-memory budget would let a
    crash loop switch every restart, straight through the 6 h cooldown."""
    agent, _, clock = build(tmp, server="Aspidiske")
    agent.probe = scripted_probe(9.0)
    for _ in range(agent.cfg.bad_windows):
        agent.watch_once()
    assert agent.switches["degradation"] == 1
    assert os.path.exists(agent.cfg.budget_path), "the switch history was not persisted"

    # A brand new Agent over the same emptyDir, as after a supervisor restart.
    cfg = agent.cfg
    bluetit = ag.Bluetit(cfg, runner=FakeBluetit(clock, server="Dalim"),
                         sleep=clock.sleep, clock=clock)
    fresh = ag.Agent(cfg, bluetit=bluetit, sleep=clock.sleep, clock=clock,
                     wallclock=agent.wallclock)
    allowed, why = fresh.budget.allowed()
    assert allowed is False, "a restart wiped the cooldown"
    assert "cooldown" in why, why

    clock.sleep(cfg.cooldown_seconds + 1)
    allowed, _ = fresh.budget.allowed()
    assert allowed is True, "the restored cooldown never expires"
    print("ok  budget: the cooldown and the daily cap survive an agent restart")


@with_tmp
def test_dry_run_takes_no_action(tmp):
    agent, fake, _ = build(tmp, server="Aspidiske", dry_run=True)
    agent.boot_check()
    assert fake.calls and all(c[0] == "--bluetit-status" for c in fake.calls), fake.calls
    assert fake.server == "Aspidiske"
    assert agent.switches["boot_upgrade"] == 0
    print("ok  dry run: logs the switch, touches nothing")


def test_goldcrest_is_bounded_by_bytes():
    """The 673 MB prompt loop. `head -n` does not save you, the prompt has no
    newline, and `< /dev/null` does not stop it either."""
    tmp = tempfile.mkdtemp()
    try:
        fake = os.path.join(tmp, "goldcrest")
        with open(fake, "w") as fh:
            # `trap '' PIPE` models the real binary: goldcrest survives SIGPIPE,
            # so `head -c` closing the pipe does NOT end it. Only `timeout` does.
            fh.write("#!/bin/sh\ntrap '' PIPE\n"
                     "while true; do printf 'AirVPN Username: '; done\n")
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        os.environ["PATH"] = tmp + ":" + os.environ["PATH"]

        cfg = ag.Config()
        cfg.goldcrest_timeout = 3
        cfg.max_output_bytes = 4096
        started = time.monotonic()
        rc, out = ag.goldcrest(["--air-connect"], cfg)
        elapsed = time.monotonic() - started

        assert 0 < len(out) <= cfg.max_output_bytes, \
            "output was not bounded by bytes, got %d" % len(out)
        assert rc is None, "a flood must report as a failure, got rc=%s" % rc
        # goldcrest's own `timeout` has to be what ends this. Our outer
        # subprocess timeout is a backstop, and hitting it instead means the
        # call ran 15 s longer than the budget allows for.
        assert elapsed < cfg.goldcrest_timeout + 5, \
            "the goldcrest timeout did not end it, took %.1fs" % elapsed

        # And a well-behaved call carries its real exit code out of the pipeline.
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\necho 'Bluetit is not connected'\nexit 3\n")
        rc, out = ag.goldcrest(["--bluetit-status"], cfg)
        assert rc == 3, "goldcrest's exit code was lost, got %s" % rc
        assert ag.parse_status(out) is None
        print("ok  goldcrest: bounded by bytes in %.1fs, real exit code recovered" % elapsed)
    finally:
        shutil.rmtree(tmp)


def test_parse_status():
    connected = ("2026-08-02 09:42:59 Connected to AirVPN server Aspidiske "
                 "(Alblasserdam, Netherlands)\n"
                 "2026-08-02 09:42:59 Users: 138 - Load: 69%\n")
    assert ag.parse_status(connected) == "Aspidiske"
    # The disconnected form contains the word "connected" - never grep for that.
    assert ag.parse_status("Bluetit is not connected") is None
    assert ag.parse_status("") is None
    print("ok  parse_status: exact name out, 'is not connected' is not a match")


def test_parse_ping():
    text = """--- 10.128.0.1 ping statistics ---
50 packets transmitted, 47 received, 6% packet loss, time 9800ms
rtt min/avg/max/mdev = 26.0/27.0/28.0/1.0 ms
"""
    loss, rtt = ag.parse_ping(text)
    assert loss == 6.0 and rtt == 27.0, (loss, rtt)
    dead = "50 packets transmitted, 0 received, 100% packet loss, time 9800ms\n"
    loss, rtt = ag.parse_ping(dead)
    assert loss == 100.0 and rtt == float("inf"), (loss, rtt)
    print("ok  parse_ping: exact loss from the counts")


if __name__ == "__main__":
    test_parse_status()
    test_parse_ping()
    test_goldcrest_is_bounded_by_bytes()
    test_stale_ranking_is_absent()
    test_ranking_age_is_measured_at_scrape_time()
    test_tunnel_device_is_measured_at_scrape_time()
    test_switch_in_progress_marks_the_device_rebuild()
    test_current_server_is_refreshed_every_window()
    test_boot_in_band_stays_put()
    test_boot_out_of_band_upgrades()
    test_boot_without_a_ranking_stays_on_quick()
    test_degradation_counting()
    test_high_loss_counts_on_a_busy_tunnel_too()
    test_throughput_is_measured_not_assumed()
    test_throughput_metrics_are_measurements()
    test_dead_tunnel_is_the_liveness_probes_job()
    test_cooldown_and_daily_cap()
    test_failed_connect_walks_the_pool()
    test_every_candidate_fails_ends_on_quick()
    test_recovery_never_spends_the_quick_reserve()
    test_wrong_tunnel_device_fails_the_switch()
    test_verification_rejects_a_lying_status_string()
    test_a_failed_candidate_is_dropped_not_redialled()
    test_recovery_is_disconnect_plus_connect_never_a_signal()
    test_no_signal_path_survives_in_the_source()
    test_a_failed_switch_does_not_arm_the_full_cooldown()
    test_cache_is_the_fallback_when_the_file_goes_away()
    test_budget_survives_a_restart()
    test_dry_run_takes_no_action()
    print("ALL AGENT SELF-CHECKS PASSED")
