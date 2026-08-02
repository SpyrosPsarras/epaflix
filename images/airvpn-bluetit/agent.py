#!/usr/bin/env python3
"""vpn-picker agent - Components 4-7 of the vpn-picker design.

Spec: docs/superpowers/specs/2026-08-01-vpn-picker-design.md (#608, map #498)

Runs as a second container in the `qbittorrent` pod, next to `airvpn`, sharing
`/run/dbus`. It reads the ranking the scorer publishes, watches the current
tunnel, and drives Bluetit over D-Bus when it has to switch.

Second entrypoint of the airvpn-bluetit image rather than an image of its own:
`goldcrest` and the D-Bus client libraries are already here, and a second image
to hold one binary buys nothing.

Stdlib only, same as the scorer. The whole job is a JSON file, some `ping` runs
and a handful of `goldcrest` calls.
"""

import json
import logging
import os
import random
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("vpn-agent")

# Contract version the scorer publishes. A breaking change ships as a new
# filename (ranking.v2.json), so anything else here is a document we cannot read.
SCHEMA = 1


def _env_int(name, default):
    return int(os.environ.get(name, default))


def _env_float(name, default):
    return float(os.environ.get(name, default))


def _env_bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Config:
    """Every knob the spec names, no magic numbers buried in the logic."""

    def __init__(self):
        # The FILE, never the scorer's HTTP endpoint. The pod cannot reach
        # RFC1918 outbound: Bluetit's split routes exclude private ranges and
        # then a blunt 0.0.0.0/1 + 128.0.0.0/1 pair re-swallows them into tun0,
        # where AirVPN drops them. Measured - every LAN and ClusterIP address
        # times out from inside this netns.
        self.ranking_path = os.environ.get(
            "VPN_AGENT_RANKING_PATH", "/media/.vpn-picker/ranking.json"
        )
        # emptyDir is enough. Boot always starts on `quick`, so the cache only
        # has to survive within one pod lifetime.
        self.cache_path = os.environ.get(
            "VPN_AGENT_CACHE_PATH", "/var/cache/vpn-agent/ranking.json"
        )
        # Next to the cache, on the same emptyDir. Not a PVC: a pod restart
        # always re-runs `quick` and re-derives everything, so the budget only
        # has to outlive an agent.py restart inside one pod.
        self.budget_path = os.environ.get(
            "VPN_AGENT_BUDGET_PATH", "/var/cache/vpn-agent/switches.json"
        )
        # Sticky top-N band. Inside it we stay put, so ordinary pod restarts
        # cause no churn.
        self.band = _env_int("VPN_AGENT_BAND", 5)

        # In-tunnel gateway, reachable because `allowping yes` is set. Pinging
        # anything outside the tunnel measures the wrong path.
        self.probe_target = os.environ.get("VPN_AGENT_PROBE_TARGET", "10.128.0.1")
        self.probe_count = _env_int("VPN_AGENT_PROBE_COUNT", 50)
        self.probe_rate = _env_float("VPN_AGENT_PROBE_RATE", 5.0)
        self.probe_interval = _env_int("VPN_AGENT_PROBE_INTERVAL_SECONDS", 60)
        # The 2026-07-31 outage profile was 6.7-9% sustained.
        self.bad_loss_pct = _env_float("VPN_AGENT_BAD_LOSS_PCT", 5.0)
        # Same semantics as a probe's failureThreshold: 3.
        self.bad_windows = _env_int("VPN_AGENT_BAD_WINDOWS", 3)
        # Long on purpose. A switch changes the exit IP, which drops every peer
        # and re-announces to private trackers that have hit-and-run rules.
        self.cooldown_seconds = _env_int("VPN_AGENT_COOLDOWN_SECONDS", 21600)
        self.max_switches_per_day = _env_int("VPN_AGENT_MAX_SWITCHES_PER_DAY", 3)

        self.goldcrest_timeout = _env_int("VPN_AGENT_GOLDCREST_TIMEOUT", 25)
        # Bound by BYTES, never lines - see goldcrest() for the 673 MB reason.
        self.max_output_bytes = _env_int("VPN_AGENT_MAX_OUTPUT_BYTES", 65536)
        self.verify_seconds = _env_int("VPN_AGENT_VERIFY_SECONDS", 30)
        self.verify_interval = _env_float("VPN_AGENT_VERIFY_INTERVAL", 2.0)
        self.attempts_per_candidate = _env_int("VPN_AGENT_ATTEMPTS", 2)
        self.retry_backoff = _env_float("VPN_AGENT_RETRY_BACKOFF", 5.0)
        # The liveness budget is ~180 s (periodSeconds 60, failureThreshold 3)
        # and the restart it ends in is a SIGTERM, which drops the network lock
        # (#535). The agent must resolve a failed connect itself, inside this.
        self.recovery_budget = _env_int("VPN_AGENT_RECOVERY_BUDGET_SECONDS", 120)
        # Held back from the candidate walk so the terminal `quick` fallback
        # always has a verify window left. Without it the walk eats the whole
        # budget and the pod ends tunnel-less, which is the one outcome this
        # design must never produce.
        self.quick_reserve = _env_int("VPN_AGENT_QUICK_RESERVE_SECONDS", 35)
        # Both qBittorrent instances read the same ranking. Jitter so this box
        # and Nick's do not stampede the same winner.
        self.jitter_seconds = _env_float("VPN_AGENT_JITTER_SECONDS", 30.0)

        # Bluetit connects on its own at boot via `airconnectatboot quick`.
        # This is only how long we wait for that before giving up on the
        # post-boot band check.
        self.boot_wait_seconds = _env_int("VPN_AGENT_BOOT_WAIT_SECONDS", 300)
        # MUST match `airkey` in bluetit.conf, and must be passed explicitly.
        # A credentialed goldcrest call pushes a fresh option set into Bluetit
        # first, and the key in that set is the built-in default - the `airkey`
        # directive in bluetit.rc is NOT re-read on this path. Measured on a
        # scratch pod configured `airkey test`: the agent's own switch logged
        # `Selected user key: Default` and dialled AirVPN on the live cluster's
        # key. The country white/black lists ARE re-applied from the rc on the
        # same call; the key is the one thing that is not.
        self.air_key = os.environ.get("VPN_AGENT_AIR_KEY", "Default")
        self.tun_device = os.environ.get("VPN_AGENT_TUN_DEVICE", "tun0")
        self.listen_port = _env_int("VPN_AGENT_LISTEN_PORT", 8081)
        # DEFAULT OFF - the agent acts. The flag exists because the spec's
        # rollback is "flip the agent back to dry-run", one env var, never a
        # manifest revert.
        self.dry_run = _env_bool("VPN_AGENT_DRY_RUN", False)


# --------------------------------------------------------------------------
# Parsing. Pure functions, so the fixture tests in agent_selftest.py exercise
# the real decision code and not a copy of it.
# --------------------------------------------------------------------------

# iputils prints exact counts; its own "N% packet loss" field is rounded to a
# whole percent. Same parser as the scorer's - the two images cannot share code,
# so keep them in step by hand.
_SENT_RECV = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
_RTT = re.compile(r"(?:rtt|round-trip) min/avg/max(?:/mdev)? = [\d.]+/([\d.]+)/")

# "Connected to AirVPN server Aspidiske (Alblasserdam, Netherlands)". It does
# NOT say "Bluetit is connected" - that string never appears. The disconnected
# form is "Bluetit is not connected", so never grep for a bare "connected".
_CONNECTED = re.compile(r"Connected to AirVPN server (\S+)")

# Carries goldcrest's own exit code out of the pipeline - see goldcrest().
_RC_MARKER = re.compile(r"___rc=(\d+)")

# goldcrest prints a refusal as a bare `ERROR: ...` line.
_ERROR = re.compile(r"ERROR: .*")


def parse_ping(text):
    """Return (loss_pct, rtt_ms) from a `ping` summary block."""
    m = _SENT_RECV.search(text)
    if not m:
        raise ValueError("no ping summary line")
    sent, received = int(m.group(1)), int(m.group(2))
    if sent == 0:
        raise ValueError("ping transmitted 0 packets")
    loss_pct = round(100.0 * (sent - received) / sent, 2)
    rtt = _RTT.search(text)
    rtt_ms = round(float(rtt.group(1)), 2) if rtt else float("inf")
    return loss_pct, rtt_ms


def parse_status(text):
    """Return the connected server name, or None when Bluetit is not connected."""
    m = _CONNECTED.search(text or "")
    return m.group(1) if m else None


def parse_ranking(payload, now_epoch):
    """Return (servers, age_seconds) from a published ranking document.

    Past its TTL the ranking is ABSENT, not merely old - one rule, no second
    staleness state. The age is still returned so the metric can show how far
    gone it is.
    """
    doc = json.loads(payload)
    if doc.get("schema") != SCHEMA:
        raise ValueError("unknown schema %r" % (doc.get("schema"),))
    stamp = datetime.strptime(doc["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    age = now_epoch - stamp.timestamp()
    ttl = int(doc.get("ttl_seconds", 0))
    if age > ttl:
        return [], age
    return list(doc.get("servers", [])), age


def in_band(current, servers, band):
    """Is the server we are on inside the scorer's top N?

    Server names are exact and case-insensitive on the connect path
    (`getServerByName`), so compare them the same way here.
    """
    if not current:
        return False
    names = [str(s.get("name", "")).lower() for s in servers[:band]]
    return current.lower() in names


def candidate_names(servers, band, exclude=None):
    """The ranked names to walk, minus the one we are already on.

    Excluding the current server matters on a degradation switch: a cached
    ranking will usually still list it first, and reconnecting to the box that
    is dropping our packets is not a fix.
    """
    skip = (exclude or "").lower()
    return [
        str(s["name"])
        for s in servers[:band]
        if str(s.get("name", "")).lower() != skip
    ]


class SwitchBudget:
    """Cooldown plus a daily cap, the two flap dampers from Component 5.

    A failed recovery that ends on `quick` records a switch here too - that is
    the Component 7 circuit breaker. A bad pool must not cause thrash.

    Kept on disk, and on WALL time so it survives a restart. The launcher
    restarts a crashed agent.py, and an in-memory budget would start empty every
    time - a crash loop would then switch every restart and walk straight
    through the 6 h cooldown this exists to enforce.
    """

    def __init__(self, cooldown_seconds, max_per_day, clock=time.time, path=None):
        self.cooldown = cooldown_seconds
        self.max_per_day = max_per_day
        self.clock = clock
        self.path = path
        self.history = []
        self._load()

    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path) as fh:
                self.history = [float(t) for t in json.load(fh)]
        except FileNotFoundError:
            return
        except Exception as exc:
            log.warning("switch history at %s is unusable: %s", self.path, exc)
            return
        self._prune(self.clock())
        if self.history:
            log.info("adopted switch history, %d in the last day, last %.0fs ago",
                     len(self.history), self.clock() - self.history[-1])

    def _save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.history, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("cannot persist the switch history: %s", exc)

    def _prune(self, now):
        self.history = [t for t in self.history if now - t < 86400]

    def allowed(self):
        """Return (allowed, reason). The reason is for the log line."""
        now = self.clock()
        self._prune(now)
        if self.history and now - self.history[-1] < self.cooldown:
            left = int(self.cooldown - (now - self.history[-1]))
            return False, "cooldown, %ds left" % left
        if len(self.history) >= self.max_per_day:
            return False, "daily cap %d reached" % self.max_per_day
        return True, ""

    def record(self):
        now = self.clock()
        self._prune(now)
        self.history.append(now)
        self._save()


# --------------------------------------------------------------------------
# Talking to Bluetit
# --------------------------------------------------------------------------


def goldcrest(args, cfg):
    """Run one goldcrest call. Returns (rc, output). rc is None on a flood.

    Every invocation is `timeout N ... < /dev/null | head -c BYTES`, and each
    of those three parts is load-bearing:

    - With no credentials goldcrest prompt-loops `AirVPN Username: ` forever and
      emits 673 MB in 20 s. The prompt has NO newline, so `head -n` never fires.
      Bound by BYTES.
    - `< /dev/null` does not stop the loop either. On EOF getline fails and
      leaves the string empty, so the loop never exits. The `timeout` is the
      only thing that ends it.
    - `sh` reports head's exit status, not goldcrest's, so the exit code is
      carried out in a `___rc=` marker. If the flood ever fires, `head` closes
      the pipe before the marker is written and the call reports rc=None, which
      is a failure - never a reason to retry unbounded.
    """
    quoted = " ".join(shlex.quote(a) for a in args)
    script = (
        "{ timeout %d goldcrest %s </dev/null 2>&1; echo \"___rc=$?\"; } | head -c %d"
        % (cfg.goldcrest_timeout, quoted, cfg.max_output_bytes)
    )
    try:
        proc = subprocess.run(
            ["sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=cfg.goldcrest_timeout + 15,
        )
    except subprocess.TimeoutExpired:
        log.error("goldcrest %s did not return even past its own timeout", quoted)
        return None, ""
    out = proc.stdout
    m = _RC_MARKER.search(out)
    rc = int(m.group(1)) if m else None
    if rc is None:
        log.error("goldcrest %s produced no exit marker - output was cut at the byte cap", quoted)
    elif rc != 0:
        log.warning("goldcrest %s exited %d", quoted, rc)
    return rc, out


class Bluetit:
    """The only three calls the agent is allowed to make.

    NEVER a signal. About 1.4 s after SIGTERM the OUTPUT chain is empty and the
    policy is back to ACCEPT (#535), so anything that kills Bluetit unprotects
    qBittorrent. `--bluetit-status`, `--disconnect`, `--air-connect --async`,
    nothing else. Listing calls are out too: one against the live daemon on
    2026-07-31 was immediately followed by a full tunnel outage.
    """

    def __init__(self, cfg, runner=goldcrest, sleep=time.sleep, clock=time.monotonic):
        self.cfg = cfg
        self.runner = runner
        self.sleep = sleep
        self.clock = clock

    def status(self):
        """Current server name, or None. Never trust anything else for this."""
        _, out = self.runner(["--bluetit-status"], self.cfg)
        return parse_status(out)

    def disconnect(self):
        """Fire and re-read.

        `--disconnect` reads an uninitialized VPNClient::Status in its busy
        check, so its return value is undefined behaviour. It may report
        "Bluetit is currently busy" on a perfectly idle daemon. Treat it as
        retryable and believe only `--bluetit-status`.
        """
        self.runner(["--disconnect"], self.cfg)
        return self.status()

    def connect(self, name, deadline=None):
        """Issue a connect and VERIFY it. Returns the connected name or None.

        `--async` is MANDATORY. Without it the tunnel is bound to the goldcrest
        process: the `timeout` wrapper kills goldcrest, its signal handler sends
        stop_connection, and the tunnel comes down - so a switch would leave the
        pod tunnel-less every single time.

        `--async` returns in about 1.9 s, before the tunnel is up, so its exit
        status proves nothing. Poll the status instead. Names are exact-match on
        the connect path, so a different name coming back is a failure too.
        """
        args = ["--air-connect", "--async", "--air-key", self.cfg.air_key]
        if name:
            args += ["--air-server", name]
        _, out = self.runner(args, self.cfg)
        # A refusal is loud and instant: `ERROR: AirVPN Server "X" does not
        # exist.` for a name a stale manifest cannot resolve, or `is not allowed
        # by <list> policy` for one outside the pool. Polling 30 s for a connect
        # that was already rejected spends the recovery budget on nothing, and
        # the budget is what has to cover the `quick` fallback.
        refused = _ERROR.search(out)

        limit = self.clock() + self.cfg.verify_seconds
        if deadline is not None:
            limit = min(limit, deadline)
        while True:
            current = self.status()
            if current and (name is None or current.lower() == name.lower()):
                return current
            if refused:
                log.warning("connect to %s refused: %s", name, refused.group(0).strip())
                return None
            if current and name is not None:
                # Connected, but not where we asked. Bluetit refuses names
                # outside its own pool and a stale manifest can reject a valid
                # one, so this is a real outcome, not a transient.
                log.warning("asked for %s, Bluetit reports %s", name, current)
            if self.clock() >= limit:
                return None
            self.sleep(self.cfg.verify_interval)

    def tun_ok(self):
        """Is the tunnel device still named tun0?

        `tunpersist` is inert under WireGuard - wireguardclient.cpp never reads
        it, and WireGuardClient::stop() calls wg_del_device() on every
        disconnect. The device is destroyed and rebuilt on every switch, and it
        only comes back as tun0 because slot 0 happens to be free. If a
        disconnect ever fails to delete it, the next connect lands on tun1 and
        qBittorrent - bound to tun0 in its config - opens no listen socket at
        all while the WebUI, both probes and ArgoCD stay green.
        """
        try:
            devices = sorted(os.listdir("/sys/class/net"))
        except OSError as exc:
            log.error("cannot read /sys/class/net: %s", exc)
            return False
        if self.cfg.tun_device in devices:
            return True
        log.error(
            "device %s is MISSING after a connect, tun devices present: %s - "
            "qBittorrent is bound to %s and has no listen socket. Recover by "
            "restarting the pod, or `goldcrest --remove-wireguard-device` from "
            "the airvpn container followed by a reconnect.",
            self.cfg.tun_device,
            [d for d in devices if d.startswith("tun")] or "none",
            self.cfg.tun_device,
        )
        return False


# --------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------


class Agent:
    def __init__(self, cfg, bluetit=None, sleep=time.sleep, clock=time.monotonic,
                 wallclock=time.time):
        self.cfg = cfg
        self.bluetit = bluetit or Bluetit(cfg, sleep=sleep, clock=clock)
        self.sleep = sleep
        self.clock = clock
        self.wallclock = wallclock
        # Wall time, not the monotonic clock: the history outlives this
        # process, and a monotonic stamp means nothing to the next one.
        self.budget = SwitchBudget(cfg.cooldown_seconds, cfg.max_switches_per_day,
                                   clock=wallclock, path=cfg.budget_path)
        self.lock = threading.Lock()
        self.current_server = None
        self.current_loss_pct = None
        self.consecutive_bad = 0
        self.ranking_age = None
        self.tun_device_ok = True
        self.switches = {"degradation": 0, "boot_upgrade": 0, "fallback": 0}
        self.cached = []

    # -- ranking ----------------------------------------------------------

    def read_ranking(self):
        """Fresh ranking from the shared file, or [] when there is not one.

        Only a fresh read updates the cache. The cache is "the last good ranking
        the agent fetched", so a stale file must not refresh it.
        """
        try:
            with open(self.cfg.ranking_path, "rb") as fh:
                payload = fh.read()
            servers, age = parse_ranking(payload, self.wallclock())
        except FileNotFoundError:
            log.warning("no ranking at %s", self.cfg.ranking_path)
            with self.lock:
                self.ranking_age = None
            return []
        except Exception as exc:
            log.warning("ranking at %s is unusable: %s", self.cfg.ranking_path, exc)
            with self.lock:
                self.ranking_age = None
            return []

        with self.lock:
            self.ranking_age = age
        if not servers:
            log.warning("ranking is %.0fs old, past its TTL - treating it as absent", age)
            return []
        self.cached = servers
        self._write_cache(payload)
        return servers

    def _write_cache(self, payload):
        path = self.cfg.cache_path
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("cannot cache the ranking at %s: %s", path, exc)

    def load_cache(self):
        """Adopt a cache left by a previous run of this process in this pod."""
        try:
            with open(self.cfg.cache_path, "rb") as fh:
                doc = json.loads(fh.read())
        except FileNotFoundError:
            return
        except Exception as exc:
            log.warning("cached ranking is unusable: %s", exc)
            return
        self.cached = list(doc.get("servers", []))
        log.info("adopted cached ranking servers=%s",
                 [s.get("name") for s in self.cached])

    def candidates(self, exclude=None):
        """fresh ranking > cached last-good > nothing (which means `quick`)."""
        servers = self.read_ranking()
        source = "fresh"
        if not servers:
            servers = self.cached
            source = "cache"
        if not servers:
            log.warning("no ranking and no cache - only `quick` is left")
            return [], "none"
        return candidate_names(servers, self.cfg.band, exclude), source

    # -- switching --------------------------------------------------------

    def switch(self, names, reason, mandatory):
        """Apply a switch: disconnect, then walk the candidates, then `quick`.

        `mandatory` is the boot/mid-session asymmetry. At boot we are already
        connected via `quick`, so the switch is optional and ending back on
        `quick` is merely a no-op. Mid-session the tunnel is already down before
        this returns, so recovery has to happen.
        """
        if self.cfg.dry_run:
            log.info("DRY RUN reason=%s would switch from=%s to=%s",
                     reason, self.current_server, names[0] if names else "quick")
            # Recorded anyway, so a dry-run soak shows the real switch rate
            # instead of one trip every three minutes forever.
            self.budget.record()
            return None

        # Jitter before we touch anything. Both instances read the same file and
        # would otherwise pile onto the same winner the moment it publishes.
        delay = random.uniform(0, self.cfg.jitter_seconds)
        log.info("switching reason=%s from=%s candidates=%s jitter=%.1fs mandatory=%s",
                 reason, self.current_server, names or ["quick"], delay, mandatory)
        self.sleep(delay)

        started = self.clock()
        hard_deadline = started + self.cfg.recovery_budget
        walk_deadline = hard_deadline - self.cfg.quick_reserve

        self.bluetit.disconnect()

        for name in names:
            for attempt in range(1, self.cfg.attempts_per_candidate + 1):
                if self.clock() >= walk_deadline:
                    log.warning("recovery budget spent on the candidate walk, "
                                "falling back to quick")
                    return self._finish(self._quick(hard_deadline), reason, "fallback")
                connected = self.bluetit.connect(name, deadline=walk_deadline)
                if connected:
                    self.tun_device_ok = self.bluetit.tun_ok()
                    if self.tun_device_ok:
                        log.info("connected server=%s reason=%s attempt=%d elapsed=%.1fs",
                                 connected, reason, attempt, self.clock() - started)
                        return self._finish(connected, reason, reason)
                    # Connected, wrong device. The switch is NOT successful -
                    # qBittorrent is bound to tun0 and has no listen socket, so
                    # counting this as a win would hide a client that is dead
                    # while the WebUI, both probes and ArgoCD stay green.
                    # Reconnecting cannot free the taken slot either, so stop
                    # rather than walk the pool creating tun2, tun3, ...
                    log.error("connected to %s but the tunnel device is wrong - "
                              "the switch FAILED", connected)
                    return self._finish(connected, reason, None)
                if attempt < self.cfg.attempts_per_candidate:
                    self.sleep(random.uniform(0.5, 1.5) * self.cfg.retry_backoff)
            log.warning("candidate %s failed %d attempts, moving on",
                        name, self.cfg.attempts_per_candidate)

        return self._finish(self._quick(hard_deadline), reason, "fallback")

    def _quick(self, deadline):
        """Terminal fallback. Connected to a mediocre server beats tunnel-less.

        No `--air-server`, which is the quick-connect path: Bluetit picks a
        recommended server from its own pool.
        """
        log.warning("falling back to quick")
        connected = self.bluetit.connect(None, deadline=deadline)
        if connected:
            self.tun_device_ok = self.bluetit.tun_ok()
        return connected

    def _finish(self, connected, reason, counted_as):
        """counted_as is None when the switch is not to be called a success."""
        with self.lock:
            self.current_server = connected
            self.consecutive_bad = 0
            if connected and counted_as:
                self.switches[counted_as] = self.switches.get(counted_as, 0) + 1
        # Every attempt arms the cooldown, successful or not. Ending on `quick`
        # rather than a ranked candidate is the Component 7 circuit breaker -
        # a bad pool must not cause thrash, and every switch costs peer
        # connections and a private-tracker re-announce.
        self.budget.record()
        if not connected:
            log.error("switch reason=%s ended with NO tunnel - the network lock is "
                      "still armed, so nothing leaks, but qBittorrent has no egress",
                      reason)
        elif counted_as == "fallback":
            log.warning("switch reason=%s ended on quick (%s) - treating it as failed "
                        "and holding the full cooldown", reason, connected)
        return connected

    # -- boot -------------------------------------------------------------

    def wait_for_tunnel(self):
        """Bluetit connects on its own via `airconnectatboot quick`.

        `airconnectatboot off` was tested and is wrong: both probes grep for
        "Connected to AirVPN server", so an agent that fails to connect would
        have the kubelet kill the airvpn container every ~3 min forever with
        qBittorrent alive and tunnel-less.
        """
        limit = self.clock() + self.cfg.boot_wait_seconds
        while True:
            current = self.bluetit.status()
            if current:
                with self.lock:
                    self.current_server = current
                return current
            if self.clock() >= limit:
                log.error("Bluetit is still not connected after %ds - leaving the boot "
                          "check alone, the startup probe owns this",
                          self.cfg.boot_wait_seconds)
                return None
            self.sleep(5)

    def boot_check(self):
        """Post-boot upgrade - the sticky top-N band test."""
        current = self.wait_for_tunnel()
        if not current:
            return
        servers = self.read_ranking()
        if not servers:
            log.info("no fresh ranking at boot - staying on quick's pick %s", current)
            return
        if in_band(current, servers, self.cfg.band):
            log.info("quick picked %s, already inside the top %d - staying put",
                     current, self.cfg.band)
            return
        names = candidate_names(servers, self.cfg.band, exclude=current)
        log.info("quick picked %s, outside the top %d %s - upgrading",
                 current, self.cfg.band,
                 [s.get("name") for s in servers[: self.cfg.band]])
        self.switch(names, "boot_upgrade", mandatory=False)

    # -- degradation ------------------------------------------------------

    def probe(self):
        """50 ICMP packets to the in-tunnel gateway. Returns loss percent."""
        interval = 1.0 / self.cfg.probe_rate
        try:
            result = subprocess.run(
                ["ping", "-n", "-q", "-c", str(self.cfg.probe_count),
                 "-i", "%g" % interval, "-W", "2", self.cfg.probe_target],
                capture_output=True, text=True,
                timeout=self.cfg.probe_count * interval + 60,
            )
            # `ping` exits 1 on total loss and still prints the summary, so the
            # return code alone is not a failure signal.
            loss, _ = parse_ping(result.stdout + result.stderr)
        except Exception as exc:
            log.warning("probe failed: %s", exc)
            return None
        return loss

    def watch_once(self):
        """One degradation window. Returns True if it triggered a switch."""
        loss = self.probe()
        if loss is None:
            return False
        with self.lock:
            self.current_loss_pct = loss

        if loss < self.cfg.bad_loss_pct:
            with self.lock:
                self.consecutive_bad = 0
            return False

        # A fully dead tunnel is EXEMPT. The liveness probe plus `quick` already
        # own that path, and a switch cannot fix a Bluetit that is not connected.
        current = self.bluetit.status()
        with self.lock:
            self.current_server = current
        if not current:
            log.warning("loss=%.2f%% but Bluetit is not connected - the liveness probe "
                        "owns this, not the agent", loss)
            return False

        with self.lock:
            self.consecutive_bad += 1
            bad = self.consecutive_bad
        log.warning("bad window loss=%.2f%% server=%s %d/%d",
                    loss, current, bad, self.cfg.bad_windows)
        if bad < self.cfg.bad_windows:
            return False

        with self.lock:
            self.consecutive_bad = 0
        allowed, why = self.budget.allowed()
        if not allowed:
            log.warning("degradation switch suppressed: %s", why)
            return False

        names, source = self.candidates(exclude=current)
        log.info("degradation trip on %s, candidates from the %s ranking: %s",
                 current, source, names or ["quick"])
        self.switch(names, "degradation", mandatory=True)
        return True

    def run(self):
        self.load_cache()
        self.boot_check()
        while True:
            self.sleep(self.cfg.probe_interval)
            try:
                self.watch_once()
            except Exception:
                # Nothing in the loop may kill this process. A crash-looping
                # agent container makes the POD not-ready, which pulls
                # qBittorrent out of its Service endpoints and takes the WebUI
                # and every *arr download client down with it.
                log.exception("watch cycle crashed, staying up for the next one")


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def render_metrics(agent):
    with agent.lock:
        server = agent.current_server
        loss = agent.current_loss_pct
        bad = agent.consecutive_bad
        age = agent.ranking_age
        device_ok = agent.tun_device_ok
        switches = dict(agent.switches)
    lines = [
        "# HELP vpn_agent_dry_run Whether the agent is logging switches instead of applying them.",
        "# TYPE vpn_agent_dry_run gauge",
        "vpn_agent_dry_run %d" % (1 if agent.cfg.dry_run else 0),
        "# HELP vpn_agent_switches_total Switches applied, by what triggered them.",
        "# TYPE vpn_agent_switches_total counter",
    ]
    for reason in ("degradation", "boot_upgrade", "fallback"):
        lines.append('vpn_agent_switches_total{reason="%s"} %d'
                     % (reason, switches.get(reason, 0)))
    lines += [
        "# HELP vpn_agent_consecutive_bad_windows Consecutive probe windows over the loss threshold.",
        "# TYPE vpn_agent_consecutive_bad_windows gauge",
        "vpn_agent_consecutive_bad_windows %d" % bad,
        "# HELP vpn_agent_tunnel_device_ok Whether the tunnel came back as the device qBittorrent is bound to.",
        "# TYPE vpn_agent_tunnel_device_ok gauge",
        "vpn_agent_tunnel_device_ok %d" % (1 if device_ok else 0),
    ]
    if server:
        lines += [
            "# HELP vpn_agent_current_server The AirVPN server the tunnel is on.",
            "# TYPE vpn_agent_current_server gauge",
            'vpn_agent_current_server{server="%s"} 1' % server,
        ]
    if loss is not None:
        lines += [
            "# HELP vpn_agent_current_loss_pct Last measured ICMP loss to the in-tunnel gateway.",
            "# TYPE vpn_agent_current_loss_pct gauge",
            "vpn_agent_current_loss_pct %s" % loss,
        ]
    if age is not None:
        lines += [
            "# HELP vpn_agent_ranking_age_seconds Age of the published ranking the agent read.",
            "# TYPE vpn_agent_ranking_age_seconds gauge",
            "vpn_agent_ranking_age_seconds %.0f" % age,
        ]
    return ("\n".join(lines) + "\n").encode()


class Handler(BaseHTTPRequestHandler):
    agent = None
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/metrics":
            self._send(200, render_metrics(self.agent), "text/plain; version=0.0.4")
        elif path == "/healthz":
            self._send(200, b"ok\n", "text/plain")
        else:
            self._send(404, b"not found\n", "text/plain")

    def log_message(self, fmt, *args):
        log.debug("http %s", fmt % args)


def serve(agent):
    Handler.agent = agent
    server = ThreadingHTTPServer(("", agent.cfg.listen_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("serving metrics on :%s", agent.cfg.listen_port)


def main():
    logging.basicConfig(
        level=os.environ.get("VPN_AGENT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    cfg = Config()
    log.info(
        "vpn-picker agent starting dry_run=%s band=%d probe=%d packets to %s every %ds "
        "bad>=%.1f%% trip=%d cooldown=%ds cap=%d/day",
        cfg.dry_run, cfg.band, cfg.probe_count, cfg.probe_target, cfg.probe_interval,
        cfg.bad_loss_pct, cfg.bad_windows, cfg.cooldown_seconds, cfg.max_switches_per_day,
    )
    agent = Agent(cfg)
    serve(agent)
    agent.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
