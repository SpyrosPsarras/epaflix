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
          **overrides):
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
    assert agent.tun_device_ok is False, "a missing tunnel device was not noticed"
    assert agent.switches["boot_upgrade"] == 0, \
        "a switch that left qBittorrent with no listen socket was counted as a success"
    assert b"vpn_agent_tunnel_device_ok 0" in ag.render_metrics(agent), \
        "the missing device is invisible in metrics"
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
    test_boot_in_band_stays_put()
    test_boot_out_of_band_upgrades()
    test_boot_without_a_ranking_stays_on_quick()
    test_degradation_counting()
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
