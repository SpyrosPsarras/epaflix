#!/usr/bin/env python3
"""Self-checks for the agent's decision logic.

Run inside the built image by test.sh. Plain asserts, no framework - the point
is that a wrong decision fails the build.

The fake Bluetit below is the real seam: `Agent` drives it through the same
`Bluetit` class the live agent uses, so these exercise the actual call sequence
(`--disconnect` then `--air-connect --async`) and not a description of it.

The ranking fixture is the real 2026-08-02 document the live scorer published.
"""

import builtins
import glob
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
import time

import agent as ag


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

_TEST_AGENTS = []


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
          throughput=0.0, start_ranking_reader=True, **overrides):
    """An Agent wired to a fake daemon, a fake clock and a real ranking file.

    `dead` names servers with the 2026-08-02 shape: they connect, they answer
    the status grep with their own name, and no packet ever comes back.
    """
    os.environ["VPN_AGENT_RANKING_PATH"] = os.path.join(tmp, "ranking.json")
    os.environ["VPN_AGENT_CACHE_PATH"] = os.path.join(tmp, "cache", "ranking.json")
    os.environ["VPN_AGENT_BUDGET_PATH"] = os.path.join(tmp, "cache", "switches.json")
    cfg = ag.Config()
    cfg.jitter_seconds = 0.0
    cfg.ranking_poll_seconds = 0.02
    cfg.ranking_initial_wait_seconds = 0.05
    cfg.tun_device = "lo"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    if ranking is not None:
        with open(cfg.ranking_path, "w") as fh:
            json.dump(ranking, fh)

    clock = FakeClock()
    fake = FakeBluetit(clock, server=server, refused=refused, silent=silent)
    bluetit = ag.Bluetit(cfg, runner=fake, sleep=clock.sleep, clock=clock)
    agent = ag.Agent(cfg, bluetit=bluetit, sleep=clock.sleep, clock=clock,
                     wallclock=lambda: GENERATED_EPOCH + 60 + (clock() - 1000.0))
    _TEST_AGENTS.append(agent)

    dead_names = {n.lower() for n in dead}
    agent.probe = lambda count=None: (
        100.0 if (fake.server or "").lower() in dead_names else 0.0
    )
    agent.throughput_since = lambda before, started: throughput
    if start_ranking_reader:
        agent.ranking_reader.start()
        assert agent.ranking_reader.wait_initial(1.0), \
            "the healthy ranking reader did not complete its first attempt"
    return agent, fake, clock


def reader_attempts(agent):
    with agent.ranking_reader._condition:
        return agent.ranking_reader._attempts


def wait_for_reader_attempt(agent, after, timeout=1.0):
    """Wait for a NEW completed poll; test-only, never a production dependency."""
    deadline = time.monotonic() + timeout
    with agent.ranking_reader._condition:
        while agent.ranking_reader._attempts <= after:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            agent.ranking_reader._condition.wait(left)
    return True


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
        first_agent = len(_TEST_AGENTS)
        try:
            fn(tmp)
        finally:
            agents = _TEST_AGENTS[first_agent:]
            for agent in agents:
                publisher = getattr(agent, "verdict_publisher", None)
                if publisher is not None:
                    publisher.wait_idle(2.0)
                close = getattr(agent, "close", None)
                if close is not None:
                    close()
                reader = getattr(agent, "ranking_reader", None)
                worker = getattr(reader, "_thread", None)
                if worker is not None and worker is not threading.current_thread():
                    worker.join(2.0)
            del _TEST_AGENTS[first_agent:]
            shutil.rmtree(tmp)
    wrapper.__name__ = fn.__name__
    return wrapper




@with_tmp
def test_stale_ranking_is_absent(tmp):
    """Past its TTL, stale = absent. One rule, no second staleness state."""
    payload = json.dumps(RANKING).encode()

    fresh, age = ag.parse_ranking(payload, GENERATED_EPOCH + 60)
    assert len(fresh) == 4 and age == 60, (fresh, age)

    stale, age = ag.parse_ranking(payload, GENERATED_EPOCH + 2101)
    assert stale == [], "a ranking past its TTL must read as absent, got %s" % stale
    assert age == 2101, age

    edge, _ = ag.parse_ranking(payload, GENERATED_EPOCH + 2100)
    assert len(edge) == 4, "the ranking must survive right up to its TTL"

    agent, _, _ = build(tmp, start_ranking_reader=False)
    agent.wallclock = lambda: GENERATED_EPOCH + 9999
    agent.ranking_reader.wallclock = agent.wallclock
    agent.ranking_reader.start()
    assert agent.ranking_reader.wait_initial(1.0)
    assert agent.read_ranking() == []
    assert agent.cached == [], "a stale read updated the cache"
    assert not os.path.exists(agent.cfg.cache_path), "a stale read wrote the cache file"
    print("ok  stale ranking: absent past TTL, good at the TTL, never cached")


def _ranking_schema_rejections(now_epoch):
    """Valid JSON documents that are not valid schema-1 rankings.

    Keep the fixtures centralized: the parser, daemon snapshot path and finite
    emptyDir cache must reject the same bytes. A shape accepted by only one path
    can still crash boot/candidate selection or preserve an ejected ranking
    beyond its lease (#792 reader review).
    """
    row = dict(RANKING["servers"][0])

    def document(**changes):
        value = dict(RANKING)
        value.update(changes)
        return value

    def with_row(changes=None, remove=(), extra=None):
        value = dict(row)
        for key in remove:
            value.pop(key, None)
        if changes:
            value.update(changes)
        if extra:
            value.update(extra)
        return document(servers=[value])

    just_beyond_skew = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_epoch + 301)
    )
    far_future = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_epoch + 10 * 365 * 86400)
    )
    return [
        ("servers is not a list", document(servers={"name": "Dalim"})),
        ("servers is empty", document(servers=[])),
        ("servers is a string row", document(servers=["Dalim"])),
        ("servers is a null row", document(servers=[None])),
        ("server row is missing name", with_row(remove=("name",))),
        ("server row has an extra field", with_row(extra={"score": 1})),
        ("server name is not a string", with_row({"name": 7})),
        ("server name is not a safe identity", with_row({"name": "../Dalim"})),
        ("entry_ip has the wrong type", with_row({"entry_ip": 7})),
        ("entry_ip is not IPv4", with_row({"entry_ip": "2001:db8::1"})),
        ("loss_pct has the wrong type", with_row({"loss_pct": "0.0"})),
        ("loss_pct is outside 0..100", with_row({"loss_pct": 101.0})),
        ("rtt_ms is negative", with_row({"rtt_ms": -0.1})),
        ("load is not an integer percent", with_row({"load": 20.5})),
        ("bw_max is not positive", with_row({"bw_max": 0})),
        ("headroom exceeds bw_max", with_row({"headroom": row["bw_max"] + 1})),
        ("server names are duplicated", document(servers=[row, dict(row)])),
        ("document has an extra field", dict(document(), score_version=1)),
        ("ttl_seconds is a string", document(ttl_seconds="2100")),
        ("ttl_seconds is zero", document(ttl_seconds=0)),
        ("ttl_seconds is negative", document(ttl_seconds=-1)),
        ("ttl_seconds exceeds the contract", document(ttl_seconds=2101)),
        ("ttl_seconds is huge", document(ttl_seconds=10 * 365 * 86400)),
        ("generated_at exceeds allowed skew",
         document(generated_at=just_beyond_skew)),
        ("generated_at is ten years in the future",
         document(generated_at=far_future, ttl_seconds=1)),
    ]


def test_ranking_schema_and_time_bounds():
    """Only a complete, finite schema-1 document can become a ranking.

    These are syntactically valid JSON. Before the review fix, string/null/name-
    less rows survived parse_ranking() and raised later in in_band() or
    candidate_names(); ttl_seconds was coerced with int(); and a timestamp ten
    years ahead made a one-second lease usable for roughly ten years. Reject at
    the one parser every snapshot/cache path shares instead.
    """
    now = GENERATED_EPOCH + 60
    payload = json.dumps(RANKING).encode()
    servers, age = ag.parse_ranking(payload, now)
    assert servers and age == 60

    for label, document in _ranking_schema_rejections(now):
        try:
            ag.parse_ranking(json.dumps(document).encode(), now)
        except ValueError:
            pass
        else:
            raise AssertionError(
                "%s was accepted as a ranking; malformed schema/time can crash "
                "boot or preserve a verdict-filtered ranking indefinitely (#792)"
                % label
            )

    assert ag.MAX_RANKING_TTL_SECONDS == 2100
    assert ag.RANKING_FUTURE_SKEW_SECONDS == 300
    near_future = dict(RANKING)
    near_future["generated_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 300)
    )
    accepted, future_age = ag.parse_ranking(json.dumps(near_future).encode(), now)
    assert accepted and future_age == -300, future_age
    print("ok  #792 ranking validation: complete rows, bounded TTL and future skew")


@with_tmp
def test_invalid_shared_and_cached_rankings_fail_open(tmp):
    """Every invalid-but-JSON document is absent before snapshot/cache adoption.

    Shared storage must leave the last good finite cache untouched. The same
    bytes found in the local cache must be discarded and fall open to `quick`.
    This pins the two independently dangerous paths from review rather than only
    unit-testing parse_ranking().
    """
    shared = os.path.join(tmp, "shared")
    cache_only = os.path.join(tmp, "cache-only")
    os.makedirs(shared)
    os.makedirs(cache_only)

    agent, _, _ = build(shared, server="Aspidiske")
    with open(agent.cfg.cache_path, "rb") as fh:
        last_good_cache = fh.read()
    assert last_good_cache

    for label, document in _ranking_schema_rejections(agent.wallclock()):
        payload = json.dumps(document, sort_keys=True).encode()
        replacement = agent.cfg.ranking_path + ".replacement"
        with open(replacement, "wb") as fh:
            fh.write(payload)
        before = reader_attempts(agent)
        os.replace(replacement, agent.cfg.ranking_path)
        assert wait_for_reader_attempt(agent, before), \
            "reader did not inspect invalid shared ranking: %s" % label
        assert agent.ranking_reader.snapshot() is None, \
            "invalid shared ranking became a snapshot: %s" % label
        names, source = agent.candidates(exclude="Aspidiske")
        assert source == "cache" and names[0] == "Dalim", (label, source, names)
        with open(agent.cfg.cache_path, "rb") as fh:
            assert fh.read() == last_good_cache, \
                "invalid shared ranking replaced the last good cache: %s" % label

    cached, _, _ = build(
        cache_only, ranking=None, server="Aspidiske", start_ranking_reader=False
    )
    for label, document in _ranking_schema_rejections(cached.wallclock()):
        payload = json.dumps(document, sort_keys=True).encode()
        cached._write_cache(payload)
        with cached.cache_lock:
            cached.cached = []
            cached.cached_payload = None
        assert cached.load_cache() == [], \
            "invalid local ranking cache was adopted: %s" % label
        assert not os.path.exists(cached.cfg.cache_path), \
            "invalid local ranking cache was retained: %s" % label
    assert cached.candidates() == ([], "none"), \
        "invalid cache did not fail open to quick"
    print("ok  #792 ranking validation: invalid shared/cache documents fail open")


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

    clock.sleep(2000)
    assert metric(agent, "vpn_agent_ranking_age_seconds") == 2660.0

    attempt = reader_attempts(agent)
    with open(agent.cfg.ranking_path, "w") as fh:
        fh.write('{"schema": 1, "generated_a')
    assert wait_for_reader_attempt(agent, attempt), \
        "the reader did not observe the truncated atomic replacement"
    assert metric(agent, "vpn_agent_ranking_age_seconds") is None, \
        "a truncated ranking must not report an age"
    attempt = reader_attempts(agent)
    os.unlink(agent.cfg.ranking_path)
    assert wait_for_reader_attempt(agent, attempt), \
        "the reader did not observe the missing ranking"
    assert metric(agent, "vpn_agent_ranking_age_seconds") is None, \
        "a missing ranking must not report an age"
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

    assert not hasattr(agent, "tun_device_ok"), \
        "self.tun_device_ok is back - a cached device verdict is what #690 removed"

    with CaptureLog() as caught:
        for _ in range(5):
            ag.render_metrics(agent)
        assert caught.records == [], (
            "%d log records from scraping a missing device - at 60 s scrapes that "
            "is a flood: %s" % (len(caught.records), caught.messages()))

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

    def status_calls():
        return sum(1 for c in fake.calls if c[0] == "--bluetit-status")

    before = status_calls()
    for _ in range(3):
        agent.watch_once()
    assert status_calls() - before == 6, \
        "3 clean windows made %d status calls, expected 6 (before+after each probe)" \
        % (status_calls() - before)

    before = status_calls()
    for _ in range(5):
        ag.render_metrics(agent)
    assert status_calls() == before, "a /metrics scrape called goldcrest"

    fake.server = None
    agent.watch_once()
    assert agent.current_server is None, agent.current_server
    assert b"vpn_agent_current_server" not in ag.render_metrics(agent), \
        "kept naming a server while Bluetit reports not connected"
    print("ok  current server: bracketed around every window, none per scrape")


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

    assert ag.in_band("menkent", RANKING["servers"], 5)
    assert ag.in_band("DALIM", RANKING["servers"], 5)
    assert not ag.in_band("Aspidiske", RANKING["servers"], 5)
    assert not ag.in_band(None, RANKING["servers"], 5)
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
    def sub(name):
        path = os.path.join(tmp, name)
        os.makedirs(path)
        return path

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


    blind, fake3, _ = build(sub("blind"), server="Aspidiske")
    blind.throughput_since = lambda before, started: None
    blind.probe = scripted_probe(15.0)
    for _ in range(blind.cfg.bad_windows - 1):
        assert blind.watch_once() is False, "tripped too early"
    assert blind.watch_once() is True, \
        "3 windows at 15% loss did not trip while tun0 throughput was " \
        "unmeasurable - an absent metric is vetoing the decision (#771)"
    assert fake3.connects() == ["Dalim"], fake3.connects()

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

    agent.cfg.tun_device = "tun-nope-0"
    assert agent.bluetit.tun_bytes() is None, \
        "an unreadable counter read as a number - a missing device is not an idle one"
    assert agent.throughput_since(0, clock() - 10) is None
    agent.cfg.tun_device = "lo"

    with CaptureLog() as caught:
        agent.cfg.tun_device = "tun-nope-0"
        for _ in range(5):
            agent.bluetit.tun_bytes()
            ag.render_metrics(agent)
        assert caught.records == [], \
            "%d log records from reading a missing counter: %s" % (
                len(caught.records), caught.messages())
    agent.cfg.tun_device = "lo"

    agent.bluetit.tun_bytes = lambda: 24576
    assert agent.throughput_since(50 * 1024 * 1024, clock() - 10) is None, \
        "a negative delta produced a number - tun0 is rebuilt on every switch, " \
        "so this is the normal post-switch window, and reading it as 0 bytes/s " \
        "would count the window as bad on loss alone"
    assert agent.throughput_since(0, clock()) is None
    assert agent.throughput_since(4096, clock() - 10) == 2048.0
    print("ok  throughput: real /sys read, silent, resets and gaps are no-number not zero")


@with_tmp
def test_a_window_too_short_to_measure_is_no_measurement(tmp):
    """#768: a millisecond interval is not a rate, in either direction.

    Drives the REAL `throughput_since()`, stub deleted. Every other test that
    drives a window goes through build()'s stub - the fake clock does not advance
    during a scripted probe - so the arithmetic's resolution was masked everywhere.

    Measured on the live pod 2026-08-04 with the probe stubbed to return
    instantly: the same tunnel, in the same second, moving several MB/s, read
    `0.0 B/s` in two windows and `11.5 MB/s` in a third. `0.0 B/s` is the worse of
    the two - it reads as a measured idle tunnel and it never measured one
    (#686/#690).
    """
    agent, fake, clock = build(tmp)
    del agent.throughput_since                  # drop the stub, use the real method
    agent.bluetit.tun_bytes = lambda: 1500000

    assert agent.throughput_since(1500000 - 1500, clock() - 0.001) is None, \
        "a 1 ms interval produced %r - one 1500-byte packet divided by a " \
        "millisecond is 1.5 MB/s, which is the #768 defect" \
        % (agent.throughput_since(1500000 - 1500, clock() - 0.001),)
    assert agent.throughput_since(1500000, clock() - 0.001) is None, \
        "a 1 ms interval with no bytes produced %r - that publishes an idle " \
        "tunnel nobody measured (#768)" \
        % (agent.throughput_since(1500000, clock() - 0.001),)
    assert agent.throughput_since(1500000 - 1500, clock() - 0.999) is None
    assert agent.throughput_since(1500000 - 1500, clock() - 1.0) == 1500.0
    assert agent.throughput_since(500000, clock() - 10.0) == 100000.0

    reads = iter([1500000, 1500000 + 1500])
    agent.bluetit.tun_bytes = lambda: next(reads, 1500000 + 1500)
    agent.probe = lambda count=None: (clock.sleep(0.001), 0.0)[1]
    agent.watch_once()
    assert agent.throughput_bps is None, \
        "a window whose probe returned in 1 ms published throughput_bps=%r " \
        "(#768)" % (agent.throughput_bps,)
    assert metric(agent, "vpn_agent_tunnel_throughput_bytes_per_sec") is None, \
        "vpn_agent_tunnel_throughput_bytes_per_sec is published as %r off a 1 ms " \
        "window - a number that reads as measured and is not (#768)" \
        % (metric(agent, "vpn_agent_tunnel_throughput_bytes_per_sec"),)
    assert metric(agent, "vpn_agent_tunnel_bytes_total") == 1501500.0, \
        "the raw counter went missing with the rate - it is measured at scrape " \
        "time and stays publishable when the rate is not"

    assert ag.MIN_THROUGHPUT_INTERVAL_SECONDS >= 1.0, \
        "the floor is %r - under a second one 1500-byte packet still swings the " \
        "quotient by MB/s" % (ag.MIN_THROUGHPUT_INTERVAL_SECONDS,)
    assert ag.MIN_THROUGHPUT_INTERVAL_SECONDS < 10.0, \
        "the floor is %r, which a real ~10 s probe window cannot reliably clear - " \
        "that would delete the metric in normal operation" \
        % (ag.MIN_THROUGHPUT_INTERVAL_SECONDS,)
    print("ok  #768: a window too short to measure publishes no rate, not a zero")


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
    agent.candidates = lambda exclude=None: (
        ag.candidate_names(RANKING["servers"], agent.cfg.band, exclude), "fixture"
    )

    def trip():
        for _ in range(agent.cfg.bad_windows):
            got = agent.watch_once()
        return got

    assert trip() is True
    assert agent.switches["degradation"] == 1

    assert trip() is False, "switched again inside the 6 h cooldown"
    assert agent.switches["degradation"] == 1

    clock.sleep(agent.cfg.cooldown_seconds - 10)
    assert trip() is False, "cooldown expired early"

    clock.sleep(20)
    assert trip() is True, "cooldown never expired"
    assert agent.switches["degradation"] == 2

    clock.sleep(agent.cfg.cooldown_seconds + 1)
    assert trip() is True
    assert agent.switches["degradation"] == 3

    clock.sleep(agent.cfg.cooldown_seconds + 1)
    assert trip() is False, "went over the 3-a-day cap"
    assert agent.switches["degradation"] == 3

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

    allowed, why = agent.budget.allowed()
    assert allowed is False, "a failed recovery did not arm the cooldown"
    assert "cooldown" in why, why
    assert agent.budget.history[-1][1] == agent.cfg.failed_cooldown_seconds, \
        "a boot_upgrade that ended on quick armed %ds - quick's pick is the least " \
        "considered destination there is, and 6 h of it blocks the degradation " \
        "watch (#789)" % agent.budget.history[-1][1]
    print("ok  total failure: ends on quick inside the budget, then holds the cooldown")


@with_tmp
def test_recovery_never_spends_the_quick_reserve(tmp):
    """A long walk must still leave a verify window for the fallback.

    This is the sharp part of Component 7: a kubelet restart is a SIGTERM and
    SIGTERM drops the network lock, so the supervisor must never become the
    recovery path.
    """
    ranking = dict(RANKING, servers=[
        {"name": "S%d" % i, "entry_ip": "198.51.100.%d" % (i + 1),
         "loss_pct": 0.0, "rtt_ms": 20.0 + i, "load": 20,
         "bw_max": 20000, "headroom": 16000}
        for i in range(5)
    ])
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

    assert fake.connects()[0] == "Dalim", fake.connects()
    assert fake.server == "Piautos", \
        "the agent settled on %s - a status string with no traffic behind it " \
        "was accepted as a working tunnel" % fake.server
    assert agent.current_server == "Piautos", agent.current_server
    assert agent.switches["boot_upgrade"] == 1, agent.switches
    assert agent.switches["fallback"] == 0, "a verified candidate is not a fallback"

    allowed, why = agent.budget.allowed()
    assert allowed is False and "cooldown" in why, why
    assert agent.budget.history[-1][1] == agent.cfg.failed_cooldown_seconds, \
        "a boot_upgrade armed %ds after walking onto a server it knows nothing " \
        "about - see test_a_boot_upgrade_does_not_lock_out_the_degradation_watch " \
        "(#789)" % agent.budget.history[-1][1]
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
    assert all(c[0] in ("--bluetit-status", "--disconnect", "--air-connect")
               for c in fake.calls), fake.calls
    print("ok  recovery: fresh disconnect + connect for every attempt, no signal")


def test_no_signal_path_survives_in_the_source():
    """#611. The `kill -USR2` degradation watcher must not come back.

    A grep, because this is the one regression that reads as harmless in a
    diff. `kill -0` is fine and stays - it sends no signal, it only asks
    whether Bluetit is still alive.
    """
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

    for _ in range(2):
        agent.switch(["Dalim"], "degradation", mandatory=True)
        clock.sleep(agent.cfg.failed_cooldown_seconds + 1)
    allowed, why = agent.budget.allowed()
    assert allowed is False and "daily cap" in why, \
        "failed switches do not count against the daily cap: %s" % why

    print("ok  cooldown: a failed switch arms the short one, still costs a daily slot")


@with_tmp
def test_a_boot_upgrade_does_not_lock_out_the_degradation_watch(tmp):
    """#789. The 2026-08-04 lockout - #627's principle, one costume over.

    that lands on a working but LOSSY server still armed the full 21600 s, and
    then blocked the only instrument that can see the fault. Measured live: the
    PR #786 recreate ran boot_upgrade onto Dalim, verified it at loss=0.0%, and
    nine minutes later the degradation watch counted 3/3 at 6-8% loss and was
    refused with `degradation switch suppressed: cooldown, 21034s left`. Dalim
    read 6.0% at 1.39 MB/s where Dedalus read 0.0% at 3.49 MB/s, so load is ruled
    out and the detector was right (#775).

    The scorer cannot arbitrate this: it probes each server's ENTRY IP from a LAN
    node and read Dalim clean the same day, which is why #767 was closed as
    disproved. In-tunnel monitoring is the only instrument that sees it, so it
    must not be the thing that gets suppressed.

    Asserts on the cooldown ACTUALLY armed (the budget entry the switch wrote)
    and on a real trip afterwards, never on a log string. The clock is advanced
    by hand between windows because the fake one does not move during a scripted
    probe (#768) - which is also what makes the timing arithmetic real here.
    """
    agent, fake, clock = build(tmp, server="Aspidiske")
    agent.probe = scripted_probe(0.0)         # a clean boot pick, like Dalim's
    agent.boot_check()
    agent.candidates = lambda exclude=None: (
        ag.candidate_names(RANKING["servers"], agent.cfg.band, exclude), "fixture"
    )

    assert fake.connects() == ["Dalim"], fake.connects()
    assert agent.switches["boot_upgrade"] == 1, agent.switches
    armed_at, armed = agent.budget.history[-1]
    assert armed == agent.cfg.failed_cooldown_seconds, (
        "a boot_upgrade armed a %ds cooldown. It is an unverified PLACEMENT - "
        "nothing was measured about the server it left, and the ranking that "
        "chose the new one cannot see in-tunnel loss - so it has not earned more "
        "than the short %ds one, and 6 h of it blocks the degradation watch that "
        "is the only thing able to correct it (#789)"
        % (armed, agent.cfg.failed_cooldown_seconds))

    agent.probe = scripted_probe(8.0, verify=0.0)
    window = agent.cfg.probe_interval + 10    # 60 s of sleep + ~10 s of probing
    windows = 0
    switched = False
    while windows < 12 and not switched:      # 12 windows is ~14 min, not 6 h
        clock.sleep(window)
        windows += 1
        switched = agent.watch_once()
    assert switched, (
        "12 bad windows at 8%% loss (~%d s) after a verified boot_upgrade "
        "produced NO degradation switch - the boot pick armed the full %ds "
        "cooldown and locked out the only mechanism that can correct it (#789)"
        % (12 * window, agent.cfg.cooldown_seconds))
    assert agent.switches["degradation"] == 1, agent.switches
    assert fake.server == "Piautos", \
        "corrected onto %s - the lossy server must be excluded" % fake.server

    assert windows == 2 * agent.cfg.bad_windows, (
        "the correction took %d windows, expected exactly %d: one trip refused "
        "inside the %ds cooldown, the next one through"
        % (windows, 2 * agent.cfg.bad_windows, agent.cfg.failed_cooldown_seconds))
    elapsed = agent.budget.clock() - armed_at
    assert agent.cfg.failed_cooldown_seconds < elapsed < 600, (
        "corrected %.0fs after the boot pick - expected between the %ds cooldown "
        "and ~10 min" % (elapsed, agent.cfg.failed_cooldown_seconds))

    clock.sleep(agent.cfg.cooldown_seconds + 1)
    while not agent.watch_once():
        clock.sleep(window)
    assert agent.switches["degradation"] == 2, agent.switches
    clock.sleep(agent.cfg.cooldown_seconds + 1)
    for _ in range(agent.cfg.bad_windows):
        assert agent.watch_once() is False, "went over the %d-a-day cap" \
            % agent.cfg.max_switches_per_day
    allowed, why = agent.budget.allowed()
    assert allowed is False and "daily cap" in why, why
    print("ok  #789: a boot_upgrade arms the short cooldown (corrected in %.0fs, "
          "%d windows), cap still 1 placement + 2 corrections" % (elapsed, windows))


@with_tmp
def test_a_blocked_ranking_open_cannot_delay_live_degradation_switch(tmp):
    """#792 follow-up: the candidate read is not allowed back onto hard NFS.

    PR #810 moved verdict writes off the watch loop, but after the third bad
    window watch_once() still called candidates() -> read_ranking() -> open() on
    the same hard-mounted NFS volume. A wedged read therefore stopped the
    independently budgeted switch before the local cache fallback could run.

    Seed only the local emptyDir cache, wedge the ranking open, then drive the
    exact 3/3 degradation path. The switch must finish inside a small wall-clock
    bound while the one ranking reader remains asleep in the simulated kernel.
    """
    agent, fake, _ = build(tmp, server="Aspidiske", start_ranking_reader=False)
    with open(agent.cfg.ranking_path, "rb") as fh:
        payload = fh.read()
    agent._write_cache(payload)
    agent.load_cache()
    agent.probe = scripted_probe(9.0)
    for _ in range(agent.cfg.bad_windows - 1):
        assert agent.watch_once() is False

    real_open = builtins.open
    reader_entered = threading.Event()
    release_reader = threading.Event()
    call_done = threading.Event()
    outcome = {}

    def blocked_open(path, mode="r", *args, **kwargs):
        try:
            candidate = os.path.abspath(os.fspath(path))
        except TypeError:
            candidate = ""
        if (candidate == os.path.abspath(agent.cfg.ranking_path)
                and "r" in mode and not any(flag in mode for flag in ("w", "a", "x"))):
            reader_entered.set()
            release_reader.wait()
        return real_open(path, mode, *args, **kwargs)

    def third_window():
        started = time.monotonic()
        try:
            outcome["result"] = agent.watch_once()
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            outcome["elapsed"] = time.monotonic() - started
            call_done.set()

    builtins.open = blocked_open
    caller = threading.Thread(target=third_window, name="blocked-ranking-watch-once")
    try:
        ranking_reader = getattr(agent, "ranking_reader", None)
        if ranking_reader is not None:
            ranking_reader.start()
            assert reader_entered.wait(1.0), \
                "the daemon reader never entered ranking open"
        caller.start()
        assert reader_entered.wait(1.0), "the ranking open was never attempted"
        assert call_done.wait(0.25), (
            "watch_once() was still blocked after 250 ms by candidates() opening "
            "ranking.json. On the live hard-mounted NFS PVC that syscall has no "
            "upper bound, so switch() is never reached (#792 follow-up)")
        assert "error" not in outcome, "watch_once raised while the ranking reader was wedged"
        assert outcome["result"] is True, outcome
        assert outcome["elapsed"] < 0.25, outcome["elapsed"]
        assert fake.connects() == ["Dalim"], (
            "the fresh snapshot/cache fallback did not feed the independently "
            "budgeted switch: %s" % fake.connects())
        if ranking_reader is not None:
            worker = ranking_reader._thread
            assert worker is not None and worker.daemon and worker.is_alive(), \
                "hard NFS must be confined to one live daemon reader"
    finally:
        release_reader.set()
        if caller.is_alive():
            caller.join(2.0)
        builtins.open = real_open
    print("ok  #792 ranking NFS: a wedged open cannot delay the 3/3 degradation switch")


@with_tmp
def test_boot_metrics_and_shutdown_do_not_wait_for_blocked_ranking_nfs(tmp):
    """Every non-reader path remains finite when the first NFS open wedges.

    Boot is the only consumer allowed a wait at all, and that wait is a bounded
    Condition wait so a healthy first read preserves the existing workflow. A
    valid local emptyDir cache must still drive the band upgrade while the NFS
    worker sleeps. Metrics/candidates start no replacement readers, and close()
    never joins the wedged daemon.
    """
    agent, fake, _ = build(
        tmp, server="Aspidiske", start_ranking_reader=False,
        ranking_initial_wait_seconds=0.05)
    with open(agent.cfg.ranking_path, "rb") as fh:
        payload = fh.read()
    agent._write_cache(payload)
    agent.load_cache()

    real_open = builtins.open
    reader_entered = threading.Event()
    release_reader = threading.Event()

    def blocked_open(path, mode="r", *args, **kwargs):
        try:
            candidate = os.path.abspath(os.fspath(path))
        except TypeError:
            candidate = ""
        if (candidate == os.path.abspath(agent.cfg.ranking_path)
                and "r" in mode and not any(flag in mode for flag in ("w", "a", "x"))):
            reader_entered.set()
            release_reader.wait()
        return real_open(path, mode, *args, **kwargs)

    builtins.open = blocked_open
    worker = None
    try:
        started = time.monotonic()
        agent.boot_check()
        elapsed = time.monotonic() - started
        assert reader_entered.is_set(), "boot never started the ranking daemon"
        assert elapsed < 0.25, (
            "boot waited %.3fs for hard NFS instead of its bounded snapshot wait"
            % elapsed)
        assert fake.connects() == ["Dalim"], (
            "boot did not fail open to the still-fresh local cache: %s"
            % fake.connects())

        worker = agent.ranking_reader._thread
        assert worker is not None and worker.daemon and worker.is_alive()
        started = time.monotonic()
        assert agent.read_ranking() == []
        names, source = agent.candidates(exclude="Aspidiske")
        assert source == "cache" and names[0] == "Dalim", (source, names)
        assert metric(agent, "vpn_agent_ranking_age_seconds") is None
        assert metric(agent, "vpn_agent_dry_run") == 0
        assert time.monotonic() - started < 0.1, \
            "a control/metrics path waited for the ranking reader"
        for _ in range(20):
            agent.ranking_reader.start()
            agent.read_ranking()
            ag.render_metrics(agent)
        assert agent.ranking_reader._thread is worker
        assert sum(1 for t in threading.enumerate() if t.name == worker.name) == 1, \
            "a wedged ranking read spawned a replacement worker"

        started = time.monotonic()
        agent.close()
        assert time.monotonic() - started < 0.1, \
            "shutdown joined the daemon blocked in hard NFS"
        assert worker.is_alive(), "the fixture did not leave the reader wedged"
    finally:
        release_reader.set()
        builtins.open = real_open
    if worker is not None:
        worker.join(2.0)
        assert not worker.is_alive(), "the released reader ignored shutdown"
    print("ok  #792 ranking boot: cache fail-open, metrics and shutdown stay non-blocking, one daemon")


@with_tmp
def test_ranking_reader_refreshes_atomic_replacements_without_busy_looping(tmp):
    """A healthy daemon notices scorer os.replace() promptly and stays bounded."""
    agent, _, _ = build(tmp, server="Aspidiske", ranking_poll_seconds=0.02)
    reader = agent.ranking_reader
    worker = reader._thread
    original = reader.snapshot()
    assert original is not None and agent.read_ranking()[0]["name"] == "Dalim"
    try:
        original.payload = b"changed"
        raise AssertionError("RankingSnapshot allowed mutation")
    except AttributeError:
        pass

    replacement = dict(RANKING)
    replacement["servers"] = [
        RANKING["servers"][2], RANKING["servers"][0],
        RANKING["servers"][1], RANKING["servers"][3],
    ]
    payload = json.dumps(replacement, sort_keys=True).encode()
    tmp_path = agent.cfg.ranking_path + ".replacement"
    with open(tmp_path, "wb") as fh:
        fh.write(payload)
    before = reader_attempts(agent)
    started = time.monotonic()
    os.replace(tmp_path, agent.cfg.ranking_path)
    assert wait_for_reader_attempt(agent, before), \
        "the daemon did not notice an atomic ranking replacement"
    assert time.monotonic() - started < 0.5, \
        "the 5 s production poll cannot support prompt verdict reconciliation"
    assert agent.read_ranking()[0]["name"] == "Menkent"
    with open(agent.cfg.cache_path, "rb") as fh:
        assert fh.read() == payload, "the local cache did not receive exact replacement bytes"

    before = reader_attempts(agent)
    time.sleep(0.12)
    attempts = reader_attempts(agent) - before
    assert 2 <= attempts <= 10, \
        "ranking reader polling is stalled or busy-looping: %d attempts/120ms" % attempts
    for _ in range(50):
        reader.start()
        agent.candidates()
    assert reader._thread is worker
    assert sum(1 for t in threading.enumerate() if t.name == worker.name) == 1
    print("ok  #792 ranking refresh: atomic replacement observed promptly by one sleeping daemon")


@with_tmp
def test_fresh_cache_adoption_cannot_be_erased_by_stale_discard(tmp):
    """A stale consumer cannot unlink a concurrently adopted fresh cache.

    Reproduce the exact lost-update interleaving from review: cached_ranking()
    parses the old payload as expired and pauses before _discard_cache(); the
    reader callback then tries to adopt a fresh replacement. Without one cache
    critical section, adoption completes first and the resumed stale consumer
    clears the fresh in-memory bytes and unlinks the newly written file.
    """
    agent, _, _ = build(tmp, start_ranking_reader=False)
    old_doc = dict(RANKING)
    old_doc["ttl_seconds"] = 1
    old_payload = json.dumps(old_doc, sort_keys=True).encode()
    fresh_payload = json.dumps(RANKING, sort_keys=True).encode()
    fresh_servers, _ = ag.parse_ranking(fresh_payload, agent.wallclock())
    assert fresh_servers and old_payload != fresh_payload

    agent._write_cache(old_payload)
    agent.cached_payload = old_payload
    agent.cached = tuple(old_doc["servers"])

    real_parse = ag.parse_ranking
    stale_parsed = threading.Event()
    allow_stale_discard = threading.Event()
    adoption_started = threading.Event()
    adoption_done = threading.Event()
    stale_done = threading.Event()
    errors = []
    stale_result = []

    def paused_parse(payload, now_epoch):
        result = real_parse(payload, now_epoch)
        if bytes(payload) == old_payload:
            assert result[0] == [], "the one-second old lease was not expired"
            stale_parsed.set()
            if not allow_stale_discard.wait(2.0):
                raise AssertionError("test never released the stale cache discard")
        return result

    def consume_stale():
        try:
            stale_result.append(agent.cached_ranking())
        except BaseException as exc:
            errors.append(exc)
        finally:
            stale_done.set()

    def adopt_fresh():
        adoption_started.set()
        try:
            agent._adopt_fresh_ranking(fresh_payload, fresh_servers)
        except BaseException as exc:
            errors.append(exc)
        finally:
            adoption_done.set()

    ag.parse_ranking = paused_parse
    consumer = threading.Thread(target=consume_stale, name="stale-cache-consumer")
    adopter = threading.Thread(target=adopt_fresh, name="fresh-cache-adopter")
    try:
        consumer.start()
        assert stale_parsed.wait(1.0), \
            "cached_ranking did not pause after parsing the old expired lease"
        adopter.start()
        assert adoption_started.wait(1.0)
        adoption_finished_before_discard = adoption_done.wait(0.1)
        allow_stale_discard.set()
        assert stale_done.wait(1.0), "stale cache consumer did not finish"
        assert adoption_done.wait(1.0), "fresh cache adoption did not finish"
    finally:
        allow_stale_discard.set()
        consumer.join(2.0)
        adopter.join(2.0)
        ag.parse_ranking = real_parse

    assert not errors, errors
    assert stale_result == [[]], stale_result
    assert not adoption_finished_before_discard, (
        "fresh adoption completed inside an expired cache's parse/discard "
        "transaction - the lost-update interleaving is still possible")
    assert agent.cached_payload == fresh_payload, \
        "the resumed stale consumer cleared the fresh in-memory payload"
    assert tuple(fresh_servers) == agent.cached
    with open(agent.cfg.cache_path, "rb") as fh:
        assert fh.read() == fresh_payload, \
            "the resumed stale consumer unlinked or replaced the fresh disk cache"

    with agent.cache_lock:
        agent.cached_payload = None
        agent.cached = ()
    assert agent.load_cache()[0]["name"] == "Dalim"
    assert agent.cached_payload == fresh_payload
    print("ok  #792 cache race: stale discard cannot erase a fresh reader adoption")


@with_tmp
def test_malformed_snapshot_fails_open_to_cache_then_quick(tmp):
    """Malformed NFS input never replaces the last good finite local cache."""
    agent, _, clock = build(tmp, server="Aspidiske")
    assert agent.read_ranking() and os.path.exists(agent.cfg.cache_path)

    replacement = agent.cfg.ranking_path + ".replacement"
    with open(replacement, "wb") as fh:
        fh.write(b'{"schema": 1, "generated_at":')
    before = reader_attempts(agent)
    os.replace(replacement, agent.cfg.ranking_path)
    assert wait_for_reader_attempt(agent, before), \
        "the daemon did not observe malformed ranking bytes"
    assert agent.ranking_reader.snapshot() is None, \
        "malformed input remained a usable in-memory snapshot"
    assert metric(agent, "vpn_agent_ranking_age_seconds") is None
    names, source = agent.candidates(exclude="Aspidiske")
    assert source == "cache" and names[0] == "Dalim", (source, names)

    clock.sleep(RANKING["ttl_seconds"] + 1)
    names, source = agent.candidates()
    assert (names, source) == ([], "none"), (names, source)
    assert not os.path.exists(agent.cfg.cache_path), \
        "expired cache survived malformed shared storage"
    print("ok  #792 malformed ranking: finite cache fallback, then quick at document TTL")


@with_tmp
def test_wedged_reader_snapshot_and_cache_expire_on_original_ttl(tmp):
    """A daemon blocked after one good read cannot make that lease permanent."""
    agent, _, clock = build(tmp, server="Aspidiske")
    assert agent.read_ranking() and os.path.exists(agent.cfg.cache_path)

    real_open = builtins.open
    reader_entered = threading.Event()
    release_reader = threading.Event()

    def blocked_open(path, mode="r", *args, **kwargs):
        try:
            candidate = os.path.abspath(os.fspath(path))
        except TypeError:
            candidate = ""
        if (candidate == os.path.abspath(agent.cfg.ranking_path)
                and "r" in mode and not any(flag in mode for flag in ("w", "a", "x"))):
            reader_entered.set()
            release_reader.wait()
        return real_open(path, mode, *args, **kwargs)

    builtins.open = blocked_open
    try:
        assert reader_entered.wait(1.0), "reader did not enter its next poll"
        clock.sleep(RANKING["ttl_seconds"] + 1)
        started = time.monotonic()
        names, source = agent.candidates()
        assert time.monotonic() - started < 0.1
        assert (names, source) == ([], "none"), (
            "a stale in-memory snapshot/cache survived its original ranking TTL: %s"
            % ((names, source),))
        assert metric(agent, "vpn_agent_ranking_age_seconds") == 2161.0
        assert not os.path.exists(agent.cfg.cache_path)
    finally:
        release_reader.set()
        builtins.open = real_open
    print("ok  #792 ranking lease: wedged snapshot and cache expire to quick on original TTL")


@with_tmp
def test_a_blocked_verdict_writer_cannot_delay_switching(tmp):
    """#792 hardening: a hard-mounted NFS write may never return.

    The live servarr-media PVC is NFSv4.2 mounted `hard`. mkdir/open/write/fsync/
    replace therefore need not raise when storage disappears; any one of them can
    sleep in the kernel indefinitely. The third bad window must still reach the
    independently budgeted switch decision promptly, with exactly one daemon
    writer and one latest-useful pending publication behind a wedged write.
    """
    verdict_dir = os.path.join(tmp, "hard-nfs", "verdicts")
    agent, fake, clock = build(
        tmp, server="Aspidiske", verdict_dir=verdict_dir,
        verdict_producer_id="cluster-default", verdict_ttl_seconds=21600)
    agent.probe = scripted_probe(9.0)
    for _ in range(agent.cfg.bad_windows - 1):
        assert agent.watch_once() is False

    real_open = builtins.open
    writer_entered = threading.Event()
    release_writer = threading.Event()
    call_done = threading.Event()
    outcome = {}
    publisher = None

    def blocked_open(path, mode="r", *args, **kwargs):
        try:
            candidate = os.path.abspath(os.fspath(path))
        except TypeError:
            candidate = ""
        if (candidate.startswith(os.path.abspath(verdict_dir) + os.sep)
                and any(flag in mode for flag in ("w", "a", "x"))):
            writer_entered.set()
            release_writer.wait()
        return real_open(path, mode, *args, **kwargs)

    def third_window():
        started = time.monotonic()
        try:
            outcome["result"] = agent.watch_once()
        except BaseException as exc:  # fixed-string reporting below; never hide it
            outcome["error"] = exc
        finally:
            outcome["elapsed"] = time.monotonic() - started
            call_done.set()

    builtins.open = blocked_open
    caller = threading.Thread(target=third_window, name="blocked-nfs-watch-once")
    caller.start()
    try:
        assert call_done.wait(0.25), (
            "watch_once() was still blocked after 250 ms by the verdict writer. "
            "On the live hard-mounted NFS PVC that wait has no upper bound, so "
            "SwitchBudget.allowed() and switch() are never reached (#792 review)")
        assert "error" not in outcome, "watch_once raised while publication was blocked"
        assert outcome["result"] is True, outcome
        assert outcome["elapsed"] < 0.25, outcome["elapsed"]
        assert fake.connects() == ["Dalim"], (
            "the degradation switch did not proceed independently of publication: %s"
            % fake.connects())
        assert writer_entered.wait(1.0), "the asynchronous writer never attempted the verdict"

        publisher = agent.verdict_publisher
        worker = publisher._thread
        assert worker is not None and worker.daemon and worker.is_alive(), \
            "the one NFS worker must be a live daemon so it cannot hold process exit"

        agent.probe = scripted_probe(9.0)
        for _ in range(agent.cfg.bad_windows):
            assert agent.watch_once() is False
        assert fake.connects() == ["Dalim"], "switched again inside the cooldown"
        allowed, why = agent.budget.allowed()
        assert allowed is False and "cooldown" in why, why

        clock.sleep(agent.cfg.cooldown_seconds + 1)
        agent.budget.record(0)
        agent.budget.record(0)
        allowed, why = agent.budget.allowed()
        assert allowed is False and "daily cap" in why, why
        for _ in range(agent.cfg.bad_windows):
            assert agent.watch_once() is False
        assert fake.connects() == ["Dalim"], "went over the daily cap"

        submitted = time.monotonic()
        for server, loss in (("Dalim", 7.0), ("Piautos", 8.0), ("Menkent", 9.0),
                             ("Dalim", 10.0)):
            assert agent.publish_bad_server(server, loss) is True
        assert time.monotonic() - submitted < 0.25, \
            "submitting repeated verdicts waited on the blocked writer"
        assert publisher._thread is worker, "a blocked write spawned another worker"
        assert sum(1 for t in threading.enumerate() if t.name == worker.name) == 1, \
            "more than one verdict worker exists"

        release_writer.set()
        assert publisher.wait_idle(2.0), "the writer did not drain after storage recovered"
        docs = []
        for path in glob.glob(os.path.join(verdict_dir, "*.json")):
            with real_open(path) as fh:
                docs.append(json.load(fh))
        by_server = {d["server"]: d for d in docs}
        assert sorted(by_server) == ["Aspidiske", "Dalim", "Menkent", "Piautos"], (
            "the blocked publisher erased distinct per-server evidence; after "
            "recovery every observed server must be durable, got %s (#792 review)"
            % sorted(by_server))
        assert by_server["Dalim"]["loss_pct"] == 10.0, \
            "same-server coalescing did not retain Dalim's latest observation"
    finally:
        release_writer.set()
        caller.join(2.0)
        if publisher is not None:
            publisher.wait_idle(2.0)
        close = getattr(agent, "close", None)
        if close is not None:
            close()
        builtins.open = real_open
    print("ok  #792 NFS: blocked writer is bounded, per-server evidence preserved, switch budget independent")


@with_tmp
def test_verdict_pending_set_has_a_hard_bound(tmp):
    """A wedged worker retains per-server evidence without unbounded growth."""
    writer_entered = threading.Event()
    release_writer = threading.Event()
    written = []

    def writer(publication):
        if not writer_entered.is_set():
            writer_entered.set()
            release_writer.wait()
        written.append((publication.server, publication.loss_pct))

    publisher = ag.VerdictPublisher(writer, max_pending=2)

    def publication(server, loss):
        return ag.VerdictPublication(
            path=os.path.join(tmp, server + ".json"), payload=b"{}\n",
            server=server, loss_pct=loss, producer="cluster-default",
            ttl_seconds=21600,
        )

    worker = None
    try:
        with CaptureLog():
            assert publisher.submit(publication("Aspidiske", 9.0)) is True
            assert writer_entered.wait(1.0), "the bounded-set worker did not start"
            assert publisher.submit(publication("Dalim", 7.0)) is True
            assert publisher.submit(publication("Piautos", 8.0)) is True
            assert publisher.submit(publication("Dalim", 10.0)) is True
            assert publisher.submit(publication("Menkent", 9.0)) is False
            with publisher._condition:
                assert len(publisher._pending) == 2, publisher._pending
                assert sorted(p.server for p in publisher._pending.values()) == \
                    ["Dalim", "Piautos"]
            release_writer.set()
            assert publisher.wait_idle(2.0), "the bounded pending set did not drain"
        assert written == [
            ("Aspidiske", 9.0), ("Dalim", 10.0), ("Piautos", 8.0)
        ], written
        worker = publisher._thread
    finally:
        release_writer.set()
        publisher.shutdown()
    if worker is not None:
        worker.join(2.0)
        assert not worker.is_alive(), "the bounded-set worker did not stop"
    print("ok  #792 queue bound: reject a new destination, never erase accepted evidence")


@with_tmp
def test_a_wedged_verdict_worker_cannot_hold_process_shutdown(tmp):
    """A kernel-wedged NFS thread is abandoned safely when the agent exits."""
    verdict_dir = os.path.join(tmp, "shutdown", "verdicts")
    agent, _, _ = build(
        tmp, verdict_dir=verdict_dir, verdict_producer_id="cluster-default")
    real_open = builtins.open
    writer_entered = threading.Event()
    release_writer = threading.Event()

    def blocked_open(path, mode="r", *args, **kwargs):
        try:
            candidate = os.path.abspath(os.fspath(path))
        except TypeError:
            candidate = ""
        if (candidate.startswith(os.path.abspath(verdict_dir) + os.sep)
                and any(flag in mode for flag in ("w", "a", "x"))):
            writer_entered.set()
            release_writer.wait()
        return real_open(path, mode, *args, **kwargs)

    builtins.open = blocked_open
    try:
        started = time.monotonic()
        assert agent.publish_bad_server("Dalim", 9.0) is True
        assert time.monotonic() - started < 0.25
        assert writer_entered.wait(1.0), "the worker never entered the blocked write"
        worker = agent.verdict_publisher._thread
        assert worker.daemon, "a wedged non-daemon worker would keep Python alive"
        assert agent.publish_bad_server("Piautos", 8.0) is True

        started = time.monotonic()
        agent.close()
        assert time.monotonic() - started < 0.1, \
            "shutdown joined a writer that hard NFS may never wake"
        assert worker.is_alive(), \
            "the fixture did not leave the daemon wedged for the shutdown assertion"
        with agent.verdict_publisher._condition:
            assert not agent.verdict_publisher._pending, \
                "shutdown kept queued work that can no longer be completed"
    finally:
        release_writer.set()
        builtins.open = real_open
    worker.join(2.0)
    assert not worker.is_alive(), "the released daemon did not observe shutdown"
    assert not os.path.exists(agent.verdict_path("Piautos")), \
        "shutdown processed a pending verdict after it was told to stop"
    print("ok  #792 shutdown: a wedged daemon is never joined and queued work is discarded")


@with_tmp
def test_shutdown_claims_stop_before_waiting_for_completion_log(tmp):
    """Shutdown must discard queued storage work before waiting on a log.

    A completion log is allowed to finish so shutdown cannot return while the
    daemon still touches stdout. But `_logging` is outside the Condition while
    the handler runs. If shutdown waits for it BEFORE setting `_stopping` and
    clearing `_pending`, the worker can finish the log, reacquire the Condition
    first, pop a second destination, and enter a new hard-NFS syscall while
    teardown is trying to stop it. The race reproduced 200/200 times in review.

    Block the first completion log, queue a second destination behind it, and
    instrument shutdown's Condition.wait(). At the instant shutdown starts
    waiting, acceptance must already be stopped and pending storage work gone.
    """
    written = []
    second_write_started = threading.Event()

    def writer(publication):
        written.append(publication.server)
        if publication.server == "Piautos":
            second_write_started.set()

    publisher = ag.VerdictPublisher(writer)

    def publication(server):
        return ag.VerdictPublication(
            path=os.path.join(tmp, server + ".json"), payload=b"{}\n",
            server=server, loss_pct=9.0, producer="cluster-default",
            ttl_seconds=21600,
        )

    log_entered = threading.Event()
    release_log = threading.Event()
    shutdown_waiting = threading.Event()
    shutdown_done = threading.Event()
    stopping_seen_by_log = []

    class BlockingCompletionLog(logging.Handler):
        def emit(self, record):
            if "published in-tunnel bad-server verdict" not in record.getMessage():
                return
            log_entered.set()
            release_log.wait()
            stopping_seen_by_log.append(publisher._stopping)

    handler = BlockingCompletionLog()
    ag.log.addHandler(handler)
    original_wait = publisher._condition.wait
    shutdown_thread = None

    def observed_wait(timeout=None):
        if threading.current_thread() is shutdown_thread:
            shutdown_waiting.set()
        return original_wait(timeout)

    publisher._condition.wait = observed_wait
    stop_claimed_before_wait = False
    pending_cleared_before_wait = False
    shutdown_returned_during_log = None
    try:
        assert publisher.submit(publication("Dalim")) is True
        assert log_entered.wait(1.0), "the first completion log was never reached"
        assert publisher.submit(publication("Piautos")) is True
        with publisher._condition:
            assert [p.server for p in publisher._pending.values()] == ["Piautos"], \
                "the second destination was not pending behind completion logging"

        def stop():
            publisher.shutdown()
            shutdown_done.set()

        shutdown_thread = threading.Thread(
            target=stop, name="verdict-shutdown-order-race")
        shutdown_thread.start()
        assert shutdown_waiting.wait(1.0), \
            "shutdown never reached its wait for the blocked completion log"
        shutdown_returned_during_log = shutdown_done.is_set()
        with publisher._condition:
            stop_claimed_before_wait = publisher._stopping
            pending_cleared_before_wait = not publisher._pending
    finally:
        release_log.set()
        if shutdown_thread is not None:
            shutdown_thread.join(2.0)
        publisher._condition.wait = original_wait
        ag.log.removeHandler(handler)
        publisher.shutdown()

    worker = publisher._thread
    if worker is not None:
        worker.join(2.0)
        assert not worker.is_alive(), "the shutdown-order worker did not stop"
    assert shutdown_returned_during_log is False, \
        "shutdown returned while the already-started completion log was blocked"
    assert stop_claimed_before_wait is True, (
        "shutdown waited for completion logging before setting _stopping. The "
        "worker can win the Condition after the log and start another hard-NFS "
        "write during teardown (#792 final review)")
    assert pending_cleared_before_wait is True, (
        "shutdown waited for completion logging before clearing the second "
        "destination; teardown can initiate a new blocking NFS syscall (#792 "
        "final review)")
    assert stopping_seen_by_log == [True], (
        "the already-started completion log did not observe shutdown's stop "
        "claim: %s" % stopping_seen_by_log)
    assert second_write_started.is_set() is False and written == ["Dalim"], (
        "shutdown let the queued destination enter storage after teardown "
        "started: %s" % written)
    print("ok  #792 shutdown order: stop and discard pending before waiting for completion log")


@with_tmp
def test_verdict_completion_and_shutdown_cannot_race_logging(tmp):
    """A completed write cannot log after shutdown starts finalization.

    The worker used to mark itself idle and snapshot `_stopping` before the log
    call. shutdown() could then set `_stopping=True` and return while the daemon
    was about to touch stdout. This fixture blocks inside the completion handler
    to make that completion-versus-shutdown interleaving deterministic. Shutdown
    now claims stopping before it waits, but still must not return until this
    already-started non-storage phase finishes.
    """
    verdict_dir = os.path.join(tmp, "completion-race", "verdicts")
    agent, _, _ = build(
        tmp, verdict_dir=verdict_dir, verdict_producer_id="cluster-default")
    publisher = agent.verdict_publisher
    log_entered = threading.Event()
    release_log = threading.Event()
    shutdown_started = threading.Event()
    shutdown_done = threading.Event()
    stopping_seen_by_log = []

    class BlockingCompletionLog(logging.Handler):
        def emit(self, record):
            if "published in-tunnel bad-server verdict" not in record.getMessage():
                return
            log_entered.set()
            release_log.wait()
            stopping_seen_by_log.append(publisher._stopping)

    handler = BlockingCompletionLog()
    ag.log.addHandler(handler)
    shutdown_thread = None
    try:
        assert agent.publish_bad_server("Dalim", 9.0) is True
        assert log_entered.wait(1.0), "the completion log was never reached"

        idle_before_log_finished = publisher.wait_idle(0.05)

        def stop():
            shutdown_started.set()
            agent.close()
            shutdown_done.set()

        shutdown_thread = threading.Thread(target=stop, name="verdict-shutdown-race")
        shutdown_thread.start()
        assert shutdown_started.wait(1.0), "the shutdown contender never ran"
        shutdown_returned_during_log = shutdown_done.wait(0.05)
    finally:
        release_log.set()
        if shutdown_thread is not None:
            shutdown_thread.join(2.0)
        ag.log.removeHandler(handler)

    assert shutdown_done.is_set(), "shutdown did not finish after completion logging"
    assert idle_before_log_finished is False, (
        "wait_idle() returned before verdict completion logging finished - test "
        "teardown can remove stdout while the daemon still uses it (#792 review)")
    assert shutdown_returned_during_log is False, (
        "shutdown returned while the verdict daemon was still logging - interpreter "
        "finalization can race that stdout write (#792 review)")
    assert stopping_seen_by_log == [True], (
        "shutdown did not claim stopping before waiting for the completion "
        "warning: %s (#792 final review)" % stopping_seen_by_log)
    worker = publisher._thread
    worker.join(2.0)
    assert not worker.is_alive(), "the completion-race worker did not stop cleanly"
    print("ok  #792 completion/shutdown: logging finishes before idle or shutdown returns")


@with_tmp
def test_degradation_publishes_a_durable_shared_verdict(tmp):
    """#792: the in-tunnel verdict must outlive this pod and this producer.

    The scorer's entry-IP probe read Dalim clean while this exact path measured
    6-24% loss through the tunnel. The agent is therefore the authority for the
    verdict. Publish it as soon as bad_windows is reached, even when the switch
    budget refuses the switch: a recreate must not erase the only measurement
    that can stop the ranking putting us straight back on the server.
    """
    verdict_dir = os.path.join(tmp, "shared", "verdicts")
    agent, fake, _ = build(
        tmp, server="Aspidiske", verdict_dir=verdict_dir,
        verdict_producer_id="cluster-default", verdict_ttl_seconds=21600)
    agent.probe = scripted_probe(9.0)

    agent.budget.record(agent.cfg.failed_cooldown_seconds)
    for window in range(agent.cfg.bad_windows - 1):
        assert agent.watch_once() is False
        assert glob.glob(os.path.join(verdict_dir, "*.json")) == [], \
            "published before bad_windows was reached on window %d" % (window + 1)
    assert agent.watch_once() is False, "the fixture cooldown should suppress the switch"
    assert fake.connects() == [], "the fixture unexpectedly switched"
    assert agent.verdict_publisher.wait_idle(2.0), \
        "healthy local verdict publication did not complete"

    files = glob.glob(os.path.join(verdict_dir, "*.json"))
    assert len(files) == 1, (
        "bad_windows reached on Aspidiske at 9%% in-tunnel loss but the agent "
        "published %d verdict files - the scorer will keep ranking a server the "
        "only authoritative probe measured as lossy, and a pod recreate forgets "
        "the fault (#792)" % len(files))
    with open(files[0]) as fh:
        doc = json.load(fh)
    assert doc == {
        "schema": 1,
        "source": "vpn-agent",
        "producer": "cluster-default",
        "server": "Aspidiske",
        "observed_at": "2026-08-02T09:31:59Z",
        "ttl_seconds": 21600,
        "loss_pct": 9.0,
        "bad_windows": agent.cfg.bad_windows,
    }, doc

    assert agent.publish_bad_server("Dalim", 8.0) is True
    assert agent.verdict_publisher.wait_idle(2.0)
    nick_tmp = os.path.join(tmp, "nick")
    os.makedirs(nick_tmp)
    other, _, _ = build(
        nick_tmp, server="Piautos", verdict_dir=verdict_dir,
        verdict_producer_id="nick", verdict_ttl_seconds=21600)
    assert other.publish_bad_server("Piautos", 7.0) is True
    assert other.verdict_publisher.wait_idle(2.0)
    files = glob.glob(os.path.join(verdict_dir, "*.json"))
    assert len(files) == 3, "one producer erased another verdict: %s" % files

    path = agent.verdict_path("Dalim")
    partial = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                with open(path) as fh:
                    json.load(fh)
            except Exception as exc:
                partial.append(type(exc).__name__)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        with CaptureLog():
            for loss in range(200):
                assert agent.publish_bad_server("Dalim", float(loss % 101)) is True
                assert agent.verdict_publisher.wait_idle(2.0), \
                    "healthy atomic verdict write did not drain"
    finally:
        stop.set()
        thread.join()
    assert partial == [], "a reader saw partial verdict JSON: %s" % partial[:3]
    assert not glob.glob(os.path.join(verdict_dir, ".*.tmp.*")), \
        "atomic verdict writer left temp files behind"

    failure_tmp = os.path.join(tmp, "write-failure")
    os.makedirs(failure_tmp)
    blocked = os.path.join(failure_tmp, "not-a-directory")
    with open(blocked, "w") as fh:
        fh.write("fixture")
    failing, failing_fake, _ = build(
        failure_tmp, server="Aspidiske", verdict_dir=blocked,
        verdict_producer_id="cluster-default", verdict_ttl_seconds=21600)
    failing.probe = scripted_probe(9.0)
    for _ in range(failing.cfg.bad_windows - 1):
        assert failing.watch_once() is False
    assert failing.watch_once() is True, \
        "an unwritable verdict directory blocked the degradation switch"
    assert failing_fake.connects() == ["Dalim"], failing_fake.connects()
    assert failing.verdict_publisher.wait_idle(2.0), \
        "an immediate write error left the publisher active"

    recreated_tmp = os.path.join(tmp, "recreated")
    os.makedirs(recreated_tmp)
    recreated, _, _ = build(
        recreated_tmp, server="Menkent", verdict_dir=verdict_dir,
        verdict_producer_id="cluster-default", verdict_ttl_seconds=21600)
    assert os.path.exists(recreated.verdict_path("Aspidiske")), \
        "a pod recreation forgot the lossy-server verdict"
    print("ok  #792: degradation publishes atomic per-producer/server verdicts on shared storage")


@with_tmp
def test_bad_windows_are_bound_to_one_unchanged_session(tmp):
    """#792 reviews: never carry loss evidence across a tunnel session.

    The AirVPN sidecar can reconnect independently while this process stays up.
    Two bad Dalim windows followed by one Dedalus window must not become a 3/3
    verdict against Dedalus, and a reconnect during the probe makes that whole
    window unattributable. A six-hour shared ejection needs three windows from
    one unchanged server, not three windows from whatever names were observed
    after each probe.
    """
    def sub(name):
        path = os.path.join(tmp, name)
        os.makedirs(path)
        return path

    during = sub("during-probe")
    verdict_dir = os.path.join(during, "shared", "verdicts")
    agent, fake, _ = build(
        during, server="Dalim", verdict_dir=verdict_dir,
        verdict_producer_id="cluster-default")
    agent.probe = scripted_probe([9.0, 9.0])
    assert agent.watch_once() is False
    assert agent.watch_once() is False
    assert agent.consecutive_bad == 2

    def reconnecting_probe(count=None):
        if count is None:
            fake.server = "Dedalus"
            return 9.0
        return 0.0

    agent.probe = reconnecting_probe
    assert agent.watch_once() is False, (
        "a reconnect during the probe completed a 3/3 degradation trip - the "
        "window was attributed from the post-probe server name instead of "
        "being discarded (#792 review)")
    assert agent.current_server == "Dedalus", agent.current_server
    assert agent.consecutive_bad == 0, (
        "Dalim's two bad windows survived a reconnect to Dedalus: %s" %
        agent.consecutive_bad)
    assert glob.glob(os.path.join(verdict_dir, "*.json")) == [], (
        "a reconnect during the third window published a false verdict against "
        "Dedalus for both ranking consumers")

    same_name = sub("same-name-session-replacement")
    verdict_dir = os.path.join(same_name, "shared", "verdicts")
    agent, fake, _ = build(
        same_name, server="Dalim", verdict_dir=verdict_dir,
        verdict_producer_id="cluster-default")
    generation = [41]
    agent.bluetit.tun_generation = lambda: generation[0]
    agent.probe = scripted_probe([9.0, 9.0])
    agent.watch_once()
    agent.watch_once()
    assert agent.consecutive_bad == 2

    def same_name_replacement(count=None):
        if count is None:
            fake.server = "Dalim"
            generation[0] += 1
            return 9.0
        return 0.0

    agent.probe = same_name_replacement
    assert agent.watch_once() is False, (
        "a same-name tunnel replacement during the probe completed a 3/3 trip; "
        "matching pre/post server names are an ABA race and cannot authorize a "
        "six-hour shared verdict (#792 second review)")
    assert agent.consecutive_bad == 0, agent.consecutive_bad
    assert agent.current_loss_pct is None, agent.current_loss_pct
    assert glob.glob(os.path.join(verdict_dir, "*.json")) == [], (
        "teardown loss during a same-name reconnect published a verdict against "
        "a healthy Dalim session (#792 second review)")

    between_sessions = sub("same-server-replacement-between-windows")
    verdict_dir = os.path.join(between_sessions, "shared", "verdicts")
    agent, fake, _ = build(
        between_sessions, server="Dalim", verdict_dir=verdict_dir,
        verdict_producer_id="cluster-default")
    generation = [41]
    agent.bluetit.tun_generation = lambda: generation[0]
    agent.probe = scripted_probe([9.0, 9.0])
    agent.watch_once()
    agent.watch_once()
    assert agent.consecutive_bad == 2

    generation[0] = 42  # same-server reconnect completed between windows
    agent.probe = scripted_probe(9.0)
    assert agent.watch_once() is False, (
        "two 9% Dalim windows on ifindex 41 plus one on replacement ifindex 42 "
        "completed a 3/3 trip and durable six-hour verdict - the generation "
        "was not carried across probe windows (#792 fourth refinement)")
    assert agent.consecutive_bad == 1, agent.consecutive_bad
    assert agent.consecutive_bad_server == "dalim", agent.consecutive_bad_server
    assert agent.consecutive_bad_generation == 42, agent.consecutive_bad_generation
    assert glob.glob(os.path.join(verdict_dir, "*.json")) == [], (
        "a recovered same-server session inherited the old session's bad "
        "windows and published a shared Dalim verdict (#792 fourth refinement)")

    between = sub("between-windows")
    verdict_dir = os.path.join(between, "shared", "verdicts")
    agent, fake, _ = build(
        between, server="Dalim", verdict_dir=verdict_dir,
        verdict_producer_id="cluster-default")
    agent.probe = scripted_probe([9.0, 9.0])
    agent.watch_once()
    agent.watch_once()
    fake.server = "Dedalus"
    agent.probe = scripted_probe(9.0)
    assert agent.watch_once() is False, (
        "one stable Dedalus window inherited Dalim's two-window counter and "
        "tripped a false ejection (#792 review)")
    assert agent.consecutive_bad == 1, agent.consecutive_bad
    assert glob.glob(os.path.join(verdict_dir, "*.json")) == []
    print("ok  #792 race: bad windows belong to one unchanged tunnel session; reconnects discard or reset attribution")


@with_tmp
def test_cache_is_the_fallback_when_the_file_goes_away(tmp):
    """Fresh ranking > still-fresh cache > quick after the original TTL.

    The cache carries the same measured generation as ranking.json; it is not a
    new lease. If the scorer stays down after publishing an ejected ranking,
    keeping that filtered list forever makes a six-hour verdict permanent. Once
    restoration information is unavailable and the original ranking expires,
    fail open to `quick` instead of inventing a hidden pre-ejection base.
    """
    agent, fake, clock = build(tmp, server="Aspidiske")
    assert agent.read_ranking(), "the fresh read failed"
    assert os.path.exists(agent.cfg.cache_path)

    attempt = reader_attempts(agent)
    os.unlink(agent.cfg.ranking_path)
    assert wait_for_reader_attempt(agent, attempt), \
        "the daemon did not observe ranking.json disappearing"
    agent.cached = []
    agent.load_cache()
    names, source = agent.candidates(exclude="Aspidiske")
    assert source == "cache", source
    assert names == ["Dalim", "Piautos", "Menkent", "Ashlesha"], names

    names, _ = agent.candidates(exclude="Dalim")
    assert "Dalim" not in names, names

    clock.sleep(agent.cfg.verdict_ttl_seconds + 1)
    names, source = agent.candidates()
    assert (names, source) == ([], "none"), (
        "a stale cached, verdict-filtered ranking survived the scorer outage "
        "indefinitely; temporary ejection became a permanent ban (#792 second "
        "review): %s" % ((names, source),))

    shutil.rmtree(os.path.dirname(agent.cfg.cache_path))
    agent.cached = []
    agent.cached_payload = None
    names, source = agent.candidates()
    assert (names, source) == ([], "none"), (names, source)
    print("ok  #792 fail-open cache: fresh > finite cache > quick after scorer outage")


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
def test_switch_budget_is_exported_at_scrape_time(tmp):
    """#783. The alert has to read the number the agent ENFORCES.

    vpn_agent_switches_total cannot express the cap: _finish() calls
    budget.record() unconditionally but increments switches[] only `if connected
    and counted_as`, and the counter has no window while the budget prunes on a
    rolling 24 h. So the two cases here are the two the counter gets wrong - a
    switch that ends with NO tunnel, and entries ageing out - plus the cooldown,
    which is the other half of allowed() and the thing that made the 2026-08-02
    and 2026-08-04 lockouts invisible (#627, #789).

    Every number below comes out of a real ag.render_metrics() call against the
    real SwitchBudget - build() stubs probe() and throughput_since() only - and is
    cross-checked against allowed() and against the on-disk switches.json, which
    is the same cross-check the live verification does.
    """
    agent, fake, clock = build(tmp, server="Aspidiske",
                               refused=("Dalim", "Piautos", "Menkent", "Ashlesha"),
                               silent=("QuickPick",))
    for name in ("vpn_agent_switch_budget_used",
                 "vpn_agent_switch_budget_exhausted",
                 "vpn_agent_switch_cooldown_seconds_left"):
        assert metric(agent, name) == 0, (
            "%s reads %s on a fresh budget with no switch behind it. The cap the "
            "agent ENFORCES is len(SwitchBudget.history) >= max_per_day, and "
            "nothing exports it, so the daily-cap alert can only approximate the "
            "number and cannot tell 'capped' from 'one switch short' (#783)"
            % (name, metric(agent, name)))

    agent.boot_check()
    assert fake.server is None, "this case needs a switch that ends with NO tunnel"
    assert sum(agent.switches.values()) == 0, \
        "a switch with no tunnel counted somewhere: %s" % agent.switches
    assert len(agent.budget.history) == 1, agent.budget.history
    assert metric(agent, "vpn_agent_switch_budget_used") == 1, (
        "a switch that ended with NO tunnel spent a slot the agent enforces - "
        "allowed() sees it and so does switches.json - and the exported budget "
        "read %s. That is the blind spot vpn_agent_switches_total has, because "
        "_finish() records unconditionally and counts only `if connected and "
        "counted_as` (#783)" % metric(agent, "vpn_agent_switch_budget_used"))
    with open(agent.cfg.budget_path) as fh:
        assert metric(agent, "vpn_agent_switch_budget_used") == len(json.load(fh)), \
            "the exported count disagrees with the budget file the agent enforces"

    first = metric(agent, "vpn_agent_switch_cooldown_seconds_left")
    assert first == agent.cfg.failed_cooldown_seconds, (first, agent.budget.history)
    clock.sleep(100)
    second = metric(agent, "vpn_agent_switch_cooldown_seconds_left")
    assert second == first - 100, (
        "vpn_agent_switch_cooldown_seconds_left read %s and then %s after 100 s - "
        "a cooldown that does not count down is a value written once that reads as "
        "measured (#686/#690/#771)" % (first, second))
    clock.sleep(agent.cfg.failed_cooldown_seconds)
    assert metric(agent, "vpn_agent_switch_cooldown_seconds_left") == 0, \
        "an expired cooldown must read 0, not a negative number"
    assert agent.budget.allowed()[0] is True, "the short cooldown never expired"

    fake.server, fake.refused, fake.silent = "Aspidiske", set(), set()
    agent.probe = scripted_probe(9.0)
    for _ in range(agent.cfg.max_switches_per_day + 1):
        if agent.budget.snapshot()[1]:
            break
        clock.sleep(agent.cfg.cooldown_seconds + 1)
        for _ in range(agent.cfg.bad_windows):
            agent.watch_once()
        used = metric(agent, "vpn_agent_switch_budget_used")
        exhausted = metric(agent, "vpn_agent_switch_budget_exhausted")
        allowed = agent.budget.allowed()[0]
        assert used == len(agent.budget.history), (used, agent.budget.history)
        assert exhausted == (1 if used >= agent.cfg.max_switches_per_day else 0), \
            "exhausted read %s at %s/%s slots used - it must be the same " \
            "len(history) >= max_per_day test allowed() uses, or the alert and " \
            "the enforcement drift (#783)" \
            % (exhausted, used, agent.cfg.max_switches_per_day)
    assert metric(agent, "vpn_agent_switch_budget_used") == agent.cfg.max_switches_per_day
    clock.sleep(agent.cfg.cooldown_seconds + 1)          # past the cooldown, so
    allowed, why = agent.budget.allowed()                # only the cap can refuse
    assert allowed is False and "daily cap" in why, why
    assert metric(agent, "vpn_agent_switch_budget_exhausted") == 1, \
        "allowed() refuses with %r and the exported flag says not exhausted" % why

    clock.sleep(86401)
    assert metric(agent, "vpn_agent_switch_budget_used") == 0, (
        "%s slots still counted a day after the last switch - the exported budget "
        "must prune on the same rolling 86400 s allowed() prunes on, or the alert "
        "latches on forever (#783)"
        % metric(agent, "vpn_agent_switch_budget_used"))
    assert metric(agent, "vpn_agent_switch_budget_exhausted") == 0
    assert len(agent.budget.history) == agent.cfg.max_switches_per_day, \
        "render_metrics() mutated the budget - snapshot() must be read-only, it " \
        "runs in the HTTP handler thread with no lock (#783)"
    allowed, why = agent.budget.allowed()
    assert allowed is True, "the daily cap never rolls off: %s" % why
    assert agent.budget.history == [], "allowed() is the path that prunes"
    print("ok  #783: the budget is exported at scrape time - a no-tunnel switch "
          "counts, the cooldown counts down, exhausted tracks allowed()")


@with_tmp
def test_dry_run_takes_no_action(tmp):
    agent, fake, _ = build(tmp, server="Aspidiske", dry_run=True)
    agent.boot_check()
    assert fake.calls and all(c[0] == "--bluetit-status" for c in fake.calls), fake.calls
    assert fake.server == "Aspidiske"
    assert agent.switches["boot_upgrade"] == 0

    agent.probe = scripted_probe(9.0)
    for _ in range(agent.cfg.bad_windows):
        agent.watch_once()
    assert glob.glob(os.path.join(agent.cfg.verdict_dir, "*.json")) == [], \
        "dry-run published a verdict and changed the shared ranking"
    assert agent.verdict_publisher._thread is None, \
        "dry-run started a verdict writer even though it must publish nothing"
    print("ok  dry run: logs the switch and verdict, starts no writer, touches nothing")


def test_goldcrest_is_bounded_by_bytes():
    """The 673 MB prompt loop. `head -n` does not save you, the prompt has no
    newline, and `< /dev/null` does not stop it either."""
    tmp = tempfile.mkdtemp()
    try:
        fake = os.path.join(tmp, "goldcrest")
        with open(fake, "w") as fh:
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
        assert elapsed < cfg.goldcrest_timeout + 5, \
            "the goldcrest timeout did not end it, took %.1fs" % elapsed

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
    if sys.argv[1:] == ["--ranking-open-regression-only"]:
        test_a_blocked_ranking_open_cannot_delay_live_degradation_switch()
        raise SystemExit(0)
    if sys.argv[1:]:
        raise SystemExit("unknown arguments: %s" % " ".join(sys.argv[1:]))

    test_parse_status()
    test_parse_ping()
    test_goldcrest_is_bounded_by_bytes()
    test_stale_ranking_is_absent()
    test_ranking_schema_and_time_bounds()
    test_invalid_shared_and_cached_rankings_fail_open()
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
    test_a_window_too_short_to_measure_is_no_measurement()
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
    test_a_boot_upgrade_does_not_lock_out_the_degradation_watch()
    test_a_blocked_ranking_open_cannot_delay_live_degradation_switch()
    test_boot_metrics_and_shutdown_do_not_wait_for_blocked_ranking_nfs()
    test_ranking_reader_refreshes_atomic_replacements_without_busy_looping()
    test_fresh_cache_adoption_cannot_be_erased_by_stale_discard()
    test_malformed_snapshot_fails_open_to_cache_then_quick()
    test_wedged_reader_snapshot_and_cache_expire_on_original_ttl()
    test_a_blocked_verdict_writer_cannot_delay_switching()
    test_verdict_pending_set_has_a_hard_bound()
    test_a_wedged_verdict_worker_cannot_hold_process_shutdown()
    test_shutdown_claims_stop_before_waiting_for_completion_log()
    test_verdict_completion_and_shutdown_cannot_race_logging()
    test_degradation_publishes_a_durable_shared_verdict()
    test_bad_windows_are_bound_to_one_unchanged_session()
    test_cache_is_the_fallback_when_the_file_goes_away()
    test_budget_survives_a_restart()
    test_switch_budget_is_exported_at_scrape_time()
    test_dry_run_takes_no_action()
    print("ALL AGENT SELF-CHECKS PASSED")
