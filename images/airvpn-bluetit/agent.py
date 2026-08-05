#!/usr/bin/env python3
"""vpn-picker agent - Components 4-7 of the vpn-picker design.

Spec: docs/superpowers/specs/2026-08-01-vpn-picker-design.md (#608, map #498)

Runs as a second container in the `qbittorrent` pod, next to `airvpn`, sharing
`/run/dbus`. It reads the ranking the scorer publishes, watches the current
tunnel, publishes its sustained in-tunnel bad-server verdicts on the shared
/media channel, and drives Bluetit over D-Bus when it has to switch.

Second entrypoint of the airvpn-bluetit image rather than an image of its own:
`goldcrest` and the D-Bus client libraries are already here, and a second image
to hold one binary buys nothing.

Stdlib only, same as the scorer. The whole job is a JSON file, some `ping` runs
and a handful of `goldcrest` calls.
"""

import hashlib
import json
import logging
import math
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

# Separate contract for the agent's in-tunnel bad-server verdict. The scorer's
# ranking schema stays at 1 and carries no producer state; verdicts are separate
# files so two qBittorrent instances sharing /media can merge without either
# writer replacing the other's evidence (#792).
VERDICT_SCHEMA = 1
VERDICT_SOURCE = "vpn-agent"
VERDICT_TTL_SECONDS = 21600
# Hard memory bound for evidence waiting behind one kernel-wedged NFS write.
# Coalescing happens per destination, so observations for different servers do
# not erase each other. If every slot is occupied, a new destination is rejected
# rather than destroying earlier evidence; same-destination refreshes still fit.
MAX_PENDING_VERDICT_DESTINATIONS = 128
_VERDICT_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")

# Shortest interval throughput_since() will divide by. Below this the quotient is
# an artefact of packet arrival timing, not a rate, so there is NO measurement -
# not a zero (#768).
#
# The arithmetic, at the tun0 MTU of 1500 bytes:
#   1 ms  -> one packet reads as 1.5 MB/s, and zero packets reads as 0.0 B/s
#   10 ms -> one packet reads as 150 KB/s
#   1 s   -> one packet reads as 1.5 KB/s, and 1 MB/s needs ~700 packets
# One second is where a single packet stops being able to move the number by more
# than its own share of it, i.e. where the quotient starts describing the traffic
# instead of the sampling. Measured on the live pod 2026-08-04: the same tunnel,
# in the same second, moving several MB/s, read 0.0 B/s in two windows and
# 11.5 MB/s in a third, because the probe was stubbed and returned instantly.
#
# Deliberately NOT justified by the old 64 KiB/s throughput gate (PR #764/#766) -
# that gate is gone (#771) and this number must not depend on it coming back. The
# only reason is metric resolution.
#
# It cannot suppress the real metric: a production window is one `ping` of
# probe_count (50) packets at probe_rate (5/s), so ~10 s of wall time - four
# orders of magnitude above this floor.
MIN_THROUGHPUT_INTERVAL_SECONDS = 1.0

# Which switch reasons have EARNED the full cooldown, and it is only the one that
# corrected something MEASURED. A `degradation` switch left a server the agent
# had just watched fail bad_windows consecutive loss windows, so spacing the next
# one 6 h out is the flap damper working as designed.
#
# `boot_upgrade` measured nothing: quick's pick was merely outside the ranking's
# top band, and the ranking is scored from a LAN node against each server's ENTRY
# IP, which cannot see an in-tunnel fault (#775, #767). So it is an unverified
# PLACEMENT, and arming 6 h on it blocks the only instrument that CAN see the
# fault. Measured live 2026-08-04: a boot_upgrade landed on Dalim, verified it at
# loss=0.0%, and nine minutes later the degradation watch reached 3/3 at 6-8%
# loss and was refused with "cooldown, 21034s left" (#789). Same principle as
# #627 fix 2 - a switch must never lock out recovery from itself - for a switch
# that produced a working but LOSSY tunnel rather than no tunnel at all.
#
# `fallback` is not here either: it ends on whatever Bluetit's own quick-connect
# picked after every ranked candidate failed, which is the least considered
# destination of the three. A degradation switch that ends on `quick` still gets
# the full cooldown, because the reason is what is scored here, not the
# destination - it did correct a measured fault.
FULL_COOLDOWN_REASONS = ("degradation",)


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
        # The 2026-07-31 outage profile was 6.7-9% sustained. Loss decides a
        # window on its own, which is what #771 restored.
        #
        # What is MEASURED - same pod, same 50-packet probe, same code, and the
        # load comparable across rows:
        #
        #   2026-08-03  Dalim     ~4.2 MB/s through tun0   15% loss
        #   2026-08-04  Dedalus    13.66 MB/s              0.0%
        #   2026-08-04  Dedalus     4.40 MB/s              0.0%
        #   2026-08-04  Dedalus     3.49 MB/s              0.0%
        #
        # What that RULES OUT: the earlier claim that loaded seeding puts a
        # 15-20% ICMP floor under every window. Dedalus at 3.3x the Dalim load
        # reads 0.0%, so load is not the variable.
        #
        # What it does NOT establish: that the loss was Dalim's fault. That
        # explanation fits better and is unproven. Confounders never recorded:
        # time of day, torrent set, peer mix, AirVPN-side load on Dalim. The
        # discriminating test - put comparable load on Dalim and re-measure loss
        # to the in-tunnel gateway - has NOT been run, because it needs a
        # deliberate switch onto a suspect server and that costs a peer drop and
        # a private-tracker re-announce (#498). Do not write down a confident
        # cause here until someone runs it (#517 is what happens if you do).
        #
        # Do NOT re-add a throughput gate (PR #764/#766, removed in #771). Two
        # faults: on a lossy-but-still-delivering server it returns "healthy" and
        # suppresses a correct detection; and because a seeding box is almost
        # never under 64 KiB/s it made the detector LOOK armed while being
        # effectively unreachable - vpn_agent_healthy_by_throughput_windows_total
        # read 0 for its entire life. Same shape as #629, #686 and #690.
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
        # Post-connect traffic check (#627 fix 1). A short burst on purpose:
        # 5 packets at probe_rate is about 1 s of sending plus the -W 2 wait,
        # and the failed-connect walk deadline has ~85 s spare, so it fits
        # without touching the recovery budget.
        self.verify_probe_count = _env_int("VPN_AGENT_VERIFY_PROBE_COUNT", 5)
        # The shape this has to catch is 100% loss on a tunnel that never
        # handshook. Deliberately NOT bad_loss_pct: over 5 packets a 5% bar
        # means one dropped packet fails a perfectly good candidate. Anything
        # between "traffic moves" and "traffic moves well" is the degradation
        # watch's job, not this one's.
        self.verify_max_loss_pct = _env_float("VPN_AGENT_VERIFY_MAX_LOSS_PCT", 50.0)
        # The SHORT cooldown - what an attempt that has not earned the full one
        # arms. Two kinds of attempt use it: one that produced NO working tunnel
        # (#627 fix 2) and one that is an unverified placement rather than a
        # measured correction, i.e. any reason outside FULL_COOLDOWN_REASONS
        # (#789). Short because the agent must be able to recover from its own
        # bad switch; longer than one degradation trip (3 x ~70 s windows) so a
        # failure cannot immediately chain into the next attempt - which means
        # the earliest trip after a boot_upgrade is refused once and the next one
        # (~7 min in) goes through. That is deliberate, see _earned_cooldown().
        # The daily cap is what actually bounds thrash - both count against it
        # exactly like a successful degradation switch.
        self.failed_cooldown_seconds = _env_int("VPN_AGENT_FAILED_COOLDOWN_SECONDS", 300)
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

        # Shared, durable verdict channel for #792. Default next to ranking.json
        # so the existing /media transport carries both. A later deployment PR
        # makes the agent's mount writable; this image-only PR changes no pod.
        #
        # The device key is the default producer identity because it is stable
        # across pod/container recreation and unique for the two AirVPN sessions
        # (`Default` and `nick`). VPN_AGENT_PRODUCER_ID is the explicit override.
        # Files are per producer+server, not one mutable producer document: one
        # writer observing a second bad server must not erase its first verdict,
        # and neither qBittorrent instance may erase the other's evidence.
        ranking_dir = os.path.dirname(self.ranking_path) or "."
        self.verdict_dir = os.environ.get(
            "VPN_AGENT_VERDICT_DIR", os.path.join(ranking_dir, "verdicts")
        )
        self.verdict_producer_id = (
            os.environ.get("VPN_AGENT_PRODUCER_ID", self.air_key).strip()
        )
        self.verdict_ttl_seconds = _env_int(
            "VPN_AGENT_VERDICT_TTL_SECONDS", VERDICT_TTL_SECONDS
        )

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


class VerdictPublication:
    """One immutable-enough write request prepared before touching storage."""

    __slots__ = ("path", "payload", "server", "loss_pct", "producer", "ttl_seconds")

    def __init__(self, path, payload, server, loss_pct, producer, ttl_seconds):
        self.path = path
        self.payload = payload
        self.server = server
        self.loss_pct = loss_pct
        self.producer = producer
        self.ttl_seconds = ttl_seconds


class VerdictPublisher:
    """One daemon writer plus a bounded per-destination pending set.

    `/media` is a hard-mounted NFSv4.2 PVC. A storage outage can leave a thread
    asleep forever inside mkdir/open/write/fsync/replace; try/except cannot put a
    time bound on that. The control loop therefore never performs those calls.
    submit() only updates bounded in-memory state under a Condition and returns.

    There is exactly one worker for the lifetime of this publisher. Behind its
    in-flight write, one latest observation is retained PER destination path.
    Another observation of the same producer/server replaces only that entry;
    evidence for a different server remains queued. The number of distinct
    destinations is capped. At the cap, a new destination is rejected instead
    of erasing existing evidence, while a refresh of an existing one is still
    accepted. This gives latest-useful-work semantics without an unbounded queue
    or a worker per verdict.

    The worker is a daemon and shutdown() never joins or waits for NFS. Pending
    work is discarded at shutdown; the active storage call is abandoned with
    the process. A completion log is different: shutdown serializes with that
    short non-storage step so it cannot return and let interpreter finalization
    remove stdout while the daemon is still using it.
    """

    def __init__(self, writer, max_pending=MAX_PENDING_VERDICT_DESTINATIONS):
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self._writer = writer
        self._max_pending = max_pending
        self._condition = threading.Condition()
        # Plain dict insertion order is the drain order. Assigning an existing
        # path refreshes its payload without moving it behind noisier servers.
        self._pending = {}
        self._active = False
        self._logging = False
        self._stopping = False
        self._thread = None

    def submit(self, publication):
        """Accept bounded work without waiting for storage.

        Latest pending work wins for this destination only. False means either
        shutdown has started or the distinct-destination bound is full.
        """
        with self._condition:
            if self._stopping:
                return False
            path = publication.path
            if path not in self._pending and len(self._pending) >= self._max_pending:
                return False
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="vpn-verdict-writer-%x" % id(self),
                    daemon=True,
                )
                try:
                    self._thread.start()
                except RuntimeError as exc:
                    self._thread = None
                    log.error("cannot start bad-server verdict writer: %s", exc)
                    return False
            self._pending[path] = publication
            self._condition.notify()
        return True

    def _run(self):
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    self._pending.clear()
                    self._active = False
                    self._condition.notify_all()
                    return
                path = next(iter(self._pending))
                publication = self._pending.pop(path)
                self._active = True

            error = None
            try:
                self._writer(publication)
            except Exception as exc:
                # Storage exceptions are expected here. The important property
                # is that they are confined to this daemon and never re-enter
                # the switch path.
                error = exc

            # Claim the completion-log phase while holding the same Condition
            # shutdown() uses. There are only two possible orderings:
            # shutdown wins and this daemon never logs, or completion wins and
            # shutdown marks stopping + discards pending work before waiting for
            # that already-started log. Claiming the stop FIRST is load-bearing:
            # after the log this worker must not pop another destination and
            # enter a new hard-NFS syscall while teardown is waiting (#792).
            with self._condition:
                if self._stopping:
                    self._active = False
                    self._condition.notify_all()
                    return
                self._logging = True

            try:
                if error is not None:
                    log.error(
                        "cannot publish bad-server verdict for %s: %s",
                        publication.server, error,
                    )
                else:
                    log.warning(
                        "published in-tunnel bad-server verdict server=%s "
                        "loss=%.2f%% producer=%s ttl=%ds",
                        publication.server, publication.loss_pct,
                        publication.producer, publication.ttl_seconds,
                    )
            finally:
                # Completion includes logging. wait_idle() and shutdown() must
                # not report it finished while the daemon still touches stdout.
                with self._condition:
                    self._logging = False
                    self._active = False
                    self._condition.notify_all()

    def wait_idle(self, timeout):
        """Test/diagnostic helper. Production switching never calls this."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._active or self._logging or self._pending:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._condition.wait(left)
            return True

    def shutdown(self):
        """Stop acceptance without waiting for a possibly wedged NFS call.

        Claim stopping and discard pending storage work BEFORE waiting for an
        already-started completion log. Otherwise that log can finish, the
        worker can win the Condition, pop another destination, and begin a new
        blocking NFS syscall before shutdown marks stopping (#792 final review).

        Waiting for `_logging` is safe because that phase starts only after its
        storage call returned. This is not a worker join: an active mkdir/open/
        write/fsync/replace has `_logging == False`, so hard NFS still cannot
        delay shutdown.
        """
        with self._condition:
            self._stopping = True
            self._pending.clear()
            self._condition.notify_all()
            while self._logging:
                self._condition.wait()


class SwitchBudget:
    """Cooldown plus a daily cap, the two flap dampers from Component 5.

    A failed recovery that ends on `quick` records a switch here too - that is
    the Component 7 circuit breaker. A bad pool must not cause thrash.

    Kept on disk, and on WALL time so it survives a restart. The launcher
    restarts a crashed agent.py, and an in-memory budget would start empty every
    time - a crash loop would then switch every restart and walk straight
    through the 6 h cooldown this exists to enforce.

    Each entry is `[when, cooldown]`, not a bare timestamp, because the outcomes
    have to cost different amounts of time (#627 fix 2, #789). A switch that
    corrected a measured fault arms the full cooldown; one that produced nothing,
    and one that is only an unverified placement, arm a short retry window so the
    agent can still recover from its own bad switch. See FULL_COOLDOWN_REASONS
    and Agent._earned_cooldown(). All of them count against the daily cap.

    The old bare-timestamp file cannot be read by this code and does not need to
    be: the budget lives on an emptyDir that only survives inside one pod, and
    the pod is recreated by the same rollout that ships this. An unreadable file
    already fails safe in _load() - a warning and an empty history.
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
                self.history = [[float(when), float(cd)] for when, cd in json.load(fh)]
        except FileNotFoundError:
            return
        except Exception as exc:
            log.warning("switch history at %s is unusable: %s", self.path, exc)
            return
        self._prune(self.clock())
        if self.history:
            log.info("adopted switch history, %d in the last day, last %.0fs ago",
                     len(self.history), self.clock() - self.history[-1][0])

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

    def _fresh(self, now):
        """The entries still inside the rolling 24 h. Pure - see snapshot()."""
        return [e for e in self.history if now - e[0] < 86400]

    def _prune(self, now):
        self.history = self._fresh(now)

    def snapshot(self):
        """(used, exhausted, cooldown_left) for the metrics. Read-only, no lock.

        This is called from the HTTP handler thread, so it MUST NOT mutate.
        _prune() rebinds self.history and record() appends to the list it just
        pruned, so a scrape that pruned concurrently would drop the switch the
        agent thread was in the middle of recording - a slot that never counted
        against the cap. Nothing protects the budget today: agent.lock does not
        cover it (_finish() releases the lock before budget.record()), and adding
        a lock the scrape path has to take would let a scrape block the switch
        path. Reading the list reference is atomic under the GIL and _fresh()
        copies it - at most a handful of entries - so this needs no lock at all
        and cannot block anything.

        The same two predicates allowed() enforces, through the same _fresh(), so
        the metric and the enforcement cannot drift apart. Measured here, at
        scrape time, off the in-memory history, which IS the authority: _load()
        reads the file once at construction and allowed() never re-reads it, so
        there is no file I/O in the scrape path either (#686/#690/#771/#783).
        """
        now = self.clock()
        history = self._fresh(now)
        left = 0.0
        if history:
            when, cooldown = history[-1]
            left = max(0.0, cooldown - (now - when))
        return len(history), len(history) >= self.max_per_day, left

    def allowed(self):
        """Return (allowed, reason). The reason is for the log line."""
        now = self.clock()
        self._prune(now)
        if self.history:
            when, cooldown = self.history[-1]
            if now - when < cooldown:
                return False, "cooldown, %ds left" % int(cooldown - (now - when))
        if len(self.history) >= self.max_per_day:
            return False, "daily cap %d reached" % self.max_per_day
        return True, ""

    def record(self, cooldown=None):
        """Arm a cooldown. None means the full one - see the class docstring."""
        now = self.clock()
        self._prune(now)
        self.history.append([now, self.cooldown if cooldown is None else cooldown])
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

    SIGUSR2 is out for a second, independent reason (#627 fix 4, #611). It runs
    Bluetit's own internal reconnect, which rebuilds tun0 from the CACHED
    PROFILE without re-logging in to AirVPN, so once the peer is gone
    server-side it retries the same dead endpoint forever - measured 2026-08-02,
    four reconnect cycles over a verified-clean network path, zero handshakes,
    five minutes after the fault was removed. Only a fresh `--disconnect` +
    `--air-connect` re-authenticates ("Logging in AirVPN user ... successfully
    logged in ... Selected user key"), and that recovered in under one second.
    Recovery here is always that pair, never a signal and never waiting for
    Bluetit to sort itself out.
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

    def tun_present(self):
        """Does the tunnel device exist RIGHT NOW? Silent, no subprocess.

        Split out of `tun_ok()` so `/metrics` can measure the real device tree
        on every scrape (#690). The metric used to be a field written only on
        the switch path, so a pod that never switched reported `1` for its whole
        life without ever having looked - green by default, not by measurement.

        Silence is the whole point of the split: `/metrics` is scraped every 60 s
        and `tun_ok()`'s log line is four lines of recovery instructions, so
        scraping the loud version on a genuinely broken pod would bury the one
        occurrence that matters under a thousand copies.

        A `/sys` read that fails is not a present device, so it reads False -
        `tun_ok()` is the caller that says so out loud.
        """
        try:
            return self.cfg.tun_device in os.listdir("/sys/class/net")
        except OSError:
            return False

    def tun_generation(self):
        """Current tunnel netdevice ifindex, or None when it cannot be read.

        The interface NAME is not a session identity. WireGuard reconnects
        delete and recreate tun0, and Bluetit may return to the same AirVPN
        server (or move A -> B -> A) during one ping window. Pre/post server
        names then match even though teardown loss crossed sessions. Linux gives
        each recreated netdevice a new ifindex, so bracketing the probe with this
        cheap /sys read closes that ABA race (#792 second review).

        None is deliberately not a wildcard match. If generation cannot be
        measured, the loss window is unattributable and must not authorize a
        shared six-hour verdict.
        """
        try:
            with open("/sys/class/net/%s/ifindex" % self.cfg.tun_device) as fh:
                value = int(fh.read())
            return value if value > 0 else None
        except (OSError, ValueError):
            return None

    def tun_bytes(self):
        """rx+tx bytes carried by the tunnel device, or None when unreadable.

        Same cheap mechanism as `tun_present()` - two /sys reads, no subprocess -
        because this runs once per probe window AND once per scrape, and anything
        needing a goldcrest call cannot live on either path.

        rx+tx summed, not rx alone: a heavy-seeding pod is upload-saturated, and a
        tunnel moving 4 MiB/s of uploads is no less alive than one downloading.

        SILENT on failure, like `tun_present()`. A missing device here would
        otherwise print a line every window and every scrape (#690's flood
        argument). None means "no number", never zero - the caller must not read
        an unreadable counter as an idle tunnel.
        """
        total = 0
        for counter in ("rx_bytes", "tx_bytes"):
            try:
                with open("/sys/class/net/%s/statistics/%s"
                          % (self.cfg.tun_device, counter)) as fh:
                    total += int(fh.read())
            except (OSError, ValueError):
                return None
        return total

    def tun_ok(self):
        """Is the tunnel device still named tun0? The LOUD switch-path verdict.

        `tunpersist` is inert under WireGuard - wireguardclient.cpp never reads
        it, and WireGuardClient::stop() calls wg_del_device() on every
        disconnect. The device is destroyed and rebuilt on every switch, and it
        only comes back as tun0 because slot 0 happens to be free. If a
        disconnect ever fails to delete it, the next connect lands on tun1 and
        qBittorrent - bound to tun0 in its config - opens no listen socket at
        all while the WebUI, both probes and ArgoCD stay green.

        Call this where a missing device has to change what the agent does and
        be shouted about once. Anything that only wants to REPORT the state -
        the metric - uses `tun_present()`.
        """
        if self.tun_present():
            return True
        try:
            devices = sorted(os.listdir("/sys/class/net"))
        except OSError as exc:
            log.error("cannot read /sys/class/net: %s", exc)
            return False
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
        # Lazy: no thread exists until the first authoritative verdict. Once
        # started, this is the only code allowed to touch the hard NFS verdict
        # path. One wedged call cannot freeze watch_once() or create more workers.
        self.verdict_publisher = VerdictPublisher(self._write_bad_server)
        self.current_server = None
        self.current_loss_pct = None
        # Last window's tunnel throughput, or None when the window produced no
        # honest number (see throughput_since()). Written on EVERY window,
        # including back to None - a stale rate is the #686 defect, and absent is
        # readable here because render_metrics() also publishes the raw byte
        # counter read at scrape time, which a reader can rate() itself.
        self.throughput_bps = None
        # There is deliberately NO self.healthy_by_throughput. It counted windows
        # the throughput gate cleared, and #771 removed the gate - so nothing
        # could ever have incremented it again. A counter parked at 0 that no code
        # path writes is the #629/#686/#690 shape, so the field went with the gate
        # rather than being left to read as coverage. Do not re-add it.
        self.consecutive_bad = 0
        # The counter belongs to one server AND one tun0 generation, not merely
        # to this process. The airvpn sidecar can reconnect independently while
        # the agent stays up. Server identity prevents two windows on Dalim plus
        # one on Dedalus becoming a false 3/3 verdict (#792 first review).
        # Generation identity also covers a same-server reconnect that completes
        # BETWEEN windows: both brackets around the next probe then see Dalim and
        # the new ifindex, so only carrying the server name would let ifindex 41's
        # two windows become ifindex 42's third (#792 fourth refinement).
        self.consecutive_bad_server = None
        self.consecutive_bad_generation = None
        # There is deliberately NO self.tun_device_ok. It existed, was
        # initialised True and written only on the switch path, so a pod that
        # never switched published `1` without ever having looked (#690). The
        # metric reads /sys/class/net at scrape time instead - see
        # render_metrics(). Do not re-add a field for it.
        #
        # `switching` is what makes that scrape-time read usable: True from the
        # disconnect until the switch resolves, published as
        # vpn_agent_switch_in_progress. The device is DELETED and rebuilt inside
        # that window (wg_del_device on every disconnect), so a scrape landing
        # mid-switch honestly sees no device. That is the teardown, not a taken
        # slot, and this flag is how a reader tells the two apart without the
        # device metric having to lie about what it sees.
        self.switching = False
        self.switches = {"degradation": 0, "boot_upgrade": 0, "fallback": 0}
        self.cached = []
        # The full bytes retain generated_at + ttl_seconds. A cached server list
        # alone has no finite lease and can preserve an already-ejected ranking
        # forever while the scorer is down (#792 second review).
        self.cached_payload = None

    # -- ranking + shared verdicts ----------------------------------------

    def verdict_path(self, server):
        """Stable file for one producer+server, safe for arbitrary identities.

        The identity remains inside the validated JSON; only its SHA-256 digest
        enters the pathname. This prevents slashes or traversal in a producer
        supplied by an environment variable, and keeps filenames bounded.
        """
        identity = "%s\0%s" % (self.cfg.verdict_producer_id, str(server).lower())
        name = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24] + ".json"
        return os.path.join(self.cfg.verdict_dir, name)

    def publish_bad_server(self, server, loss_pct):
        """Queue this agent's authoritative verdict without touching storage.

        Validation and JSON construction are bounded in-memory work. The
        observed_at timestamp is captured here, when the evidence was observed,
        rather than whenever a delayed worker happens to wake. All hard-NFS I/O
        lives in the one daemon VerdictPublisher worker.

        True means accepted for best-effort publication, not durably written.
        A later write failure is logged by the worker and cannot suppress or
        delay the independently budgeted degradation switch.
        """
        producer = self.cfg.verdict_producer_id
        server = str(server or "")
        if not isinstance(producer, str) or not _VERDICT_IDENTITY.fullmatch(producer):
            log.error("cannot publish bad-server verdict: invalid producer identity")
            return False
        if not _VERDICT_IDENTITY.fullmatch(server):
            log.error("cannot publish bad-server verdict: invalid server name")
            return False
        if not 1 <= self.cfg.verdict_ttl_seconds <= VERDICT_TTL_SECONDS:
            log.error("cannot publish bad-server verdict: ttl must be in 1..%d",
                      VERDICT_TTL_SECONDS)
            return False
        try:
            loss_pct = float(loss_pct)
        except (TypeError, ValueError):
            log.error("cannot publish bad-server verdict: loss is not numeric")
            return False
        if not math.isfinite(loss_pct) or not 0.0 <= loss_pct <= 100.0:
            log.error("cannot publish bad-server verdict: loss is outside 0..100")
            return False

        observed_at = datetime.fromtimestamp(
            self.wallclock(), timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc = {
            "schema": VERDICT_SCHEMA,
            "source": VERDICT_SOURCE,
            "producer": producer,
            "server": server,
            "observed_at": observed_at,
            "ttl_seconds": self.cfg.verdict_ttl_seconds,
            "loss_pct": loss_pct,
            "bad_windows": self.cfg.bad_windows,
        }
        publication = VerdictPublication(
            path=self.verdict_path(server),
            payload=json.dumps(doc, indent=2, sort_keys=True).encode() + b"\n",
            server=server,
            loss_pct=loss_pct,
            producer=producer,
            ttl_seconds=self.cfg.verdict_ttl_seconds,
        )
        if not self.verdict_publisher.submit(publication):
            log.error(
                "cannot queue bad-server verdict for %s: publisher is stopping "
                "or its bounded destination set is full", server,
            )
            return False
        return True

    def _write_bad_server(self, publication):
        """Atomic storage half of publish_bad_server(), daemon-worker only."""
        path = publication.path
        directory = os.path.dirname(path) or "."
        tmp = os.path.join(
            directory,
            ".%s.tmp.%d.%d" % (
                os.path.basename(path), os.getpid(), threading.get_ident()
            ),
        )
        try:
            os.makedirs(directory, exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(publication.payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


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
            return []
        except Exception as exc:
            log.warning("ranking at %s is unusable: %s", self.cfg.ranking_path, exc)
            return []

        if not servers:
            log.warning("ranking is %.0fs old, past its TTL - treating it as absent", age)
            return []
        self.cached = servers
        self.cached_payload = payload
        self._write_cache(payload)
        return servers

    def ranking_age_now(self):
        """Age of the published ranking AS OF THIS CALL, read from the file.

        Deliberately not a field the decision path keeps up to date. It used to
        be one (`self.ranking_age`, written only inside `read_ranking()`), and
        `read_ranking()` has exactly two callers: `boot_check()` and
        `candidates()` - boot, and the moment a switch is being decided. The
        60 s `watch_once()` loop never calls it, so on a healthy pod that never
        trips, the age was measured once and never again: 18 samples 60 s apart
        all read `266`, and the same pod still read `266` three hours later
        (#686). The one metric #670 named to answer "is the scorer publishing
        reliably" could not answer it.

        Read at scrape time it cannot go stale by construction - there is no
        cached age left to forget to refresh. Cost is one read of a small local
        file per scrape.

        Returns None when the ranking is missing, truncated or otherwise
        unusable. Honest absent, never a stale number, and never an exception
        out of the /metrics handler. Silent on purpose: /metrics is scraped every
        30 s and `read_ranking()` already logs the same condition on the path
        where it changes a decision.
        """
        try:
            with open(self.cfg.ranking_path, "rb") as fh:
                payload = fh.read()
            return parse_ranking(payload, self.wallclock())[1]
        except Exception:
            return None

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

    def _discard_cache(self):
        self.cached = []
        self.cached_payload = None
        try:
            os.unlink(self.cfg.cache_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("cannot remove unusable ranking cache at %s: %s",
                        self.cfg.cache_path, exc)

    def load_cache(self):
        """Adopt a still-fresh cache left by an earlier agent process.

        The cache is the original ranking document, not a new lease. Revalidate
        generated_at and ttl_seconds against the current wall clock every time
        it becomes the fallback. This matters when the scorer is down: the cache
        may already be verdict-filtered, and using its bare server list forever
        turns temporary ejection into a permanent ban. Once it expires, the only
        honest fail-open is no ranking (`quick`); hidden rows cannot be invented.
        """
        try:
            with open(self.cfg.cache_path, "rb") as fh:
                payload = fh.read()
            servers, age = parse_ranking(payload, self.wallclock())
        except FileNotFoundError:
            self.cached = []
            self.cached_payload = None
            return []
        except Exception as exc:
            log.warning("cached ranking is unusable: %s", exc)
            self._discard_cache()
            return []
        if not servers:
            log.warning(
                "cached ranking is %.0fs old, past its original TTL - failing "
                "open to `quick` because no measured base can restore hidden rows",
                age,
            )
            self._discard_cache()
            return []
        self.cached = servers
        self.cached_payload = payload
        log.info("adopted cached ranking servers=%s",
                 [s.get("name") for s in self.cached])
        return servers

    def cached_ranking(self):
        """Return the cache only while its original ranking lease is valid."""
        if self.cached_payload is None:
            return self.load_cache()
        try:
            servers, age = parse_ranking(self.cached_payload, self.wallclock())
        except Exception as exc:
            log.warning("in-memory ranking cache is unusable: %s", exc)
            self._discard_cache()
            return []
        if not servers:
            log.warning(
                "cached ranking is %.0fs old, past its original TTL - failing "
                "open to `quick` because no measured base can restore hidden rows",
                age,
            )
            self._discard_cache()
            return []
        self.cached = servers
        return servers

    def candidates(self, exclude=None):
        """Fresh ranking > still-fresh cache > nothing (which means `quick`)."""
        servers = self.read_ranking()
        source = "fresh"
        if not servers:
            servers = self.cached_ranking()
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
            # instead of one trip every three minutes forever. Same cooldown the
            # real path would arm, or the soak measures a rate nothing produces.
            self.budget.record(self._earned_cooldown(reason))
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

        # Everything from here to the end of the walk runs with no tunnel device
        # for part of the time, so say so for the whole of it. try/finally, not a
        # clear-on-the-way-out: `watch_once()` is wrapped in a bare `except` by
        # run(), so a crash anywhere below would otherwise leave the flag stuck
        # at 1 forever - which is the same class of permanently-wrong signal this
        # change exists to remove.
        with self.lock:
            self.switching = True
        try:
            self.bluetit.disconnect()

            for name in names:
                for attempt in range(1, self.cfg.attempts_per_candidate + 1):
                    if self.clock() >= walk_deadline:
                        log.warning("recovery budget spent on the candidate walk, "
                                    "falling back to quick")
                        return self._fall_back(hard_deadline, reason)
                    connected = self.bluetit.connect(name, deadline=walk_deadline)
                    if connected:
                        # A local, not a field. `tun_ok()` is the loud verdict
                        # that fails this switch; what the METRIC reports is a
                        # fresh read at scrape time (#690).
                        if not self.bluetit.tun_ok():
                            # Connected, wrong device. The switch is NOT successful -
                            # qBittorrent is bound to tun0 and has no listen socket, so
                            # counting this as a win would hide a client that is dead
                            # while the WebUI, both probes and ArgoCD stay green.
                            # Reconnecting cannot free the taken slot either, so stop
                            # rather than walk the pool creating tun2, tun3, ...
                            # The FULL cooldown is right here, unlike a verification
                            # failure below: another switch cannot fix a taken slot,
                            # it can only take one more. Passed by VALUE, because
                            # `None` means "whatever this reason earned" and this is
                            # the one case that wants the full one regardless (#789).
                            log.error("connected to %s but the tunnel device is wrong - "
                                      "the switch FAILED", connected)
                            return self._finish(connected, reason, None,
                                                cooldown=self.cfg.cooldown_seconds)
                        if self.verify_tunnel(connected):
                            log.info("connected server=%s reason=%s attempt=%d elapsed=%.1fs",
                                     connected, reason, attempt, self.clock() - started)
                            return self._finish(connected, reason, reason)
                        # The name matched and no packet came back (#627 fix 3).
                        # Drop the candidate rather than re-dial it: re-dialling the
                        # same dead peer is exactly what Bluetit's internal reconnect
                        # already does, and that was measured doing it four times
                        # over a clean path with zero handshakes. The disconnect is
                        # not optional - Bluetit refuses a connect while it believes
                        # it is connected, and only a fresh connect re-logs in to
                        # AirVPN (#627 fix 4).
                        log.warning("dropping candidate %s - it reported connected but "
                                    "passed no traffic, walking to the next one", name)
                        self.bluetit.disconnect()
                        break
                    if attempt < self.cfg.attempts_per_candidate:
                        self.sleep(random.uniform(0.5, 1.5) * self.cfg.retry_backoff)
                else:
                    log.warning("candidate %s failed %d attempts, moving on",
                                name, self.cfg.attempts_per_candidate)

            return self._fall_back(hard_deadline, reason)
        finally:
            with self.lock:
                self.switching = False

    def _quick(self, deadline):
        """Terminal fallback. Connected to a mediocre server beats tunnel-less.

        No `--air-server`, which is the quick-connect path: Bluetit picks a
        recommended server from its own pool.
        """
        log.warning("falling back to quick")
        connected = self.bluetit.connect(None, deadline=deadline)
        if connected:
            # For the log line only, and the return value is deliberately
            # dropped: `quick` is the last thing between the pod and no tunnel at
            # all, so a wrong device here is not a reason to refuse it - but it
            # has to be SAID. The metric reads the device itself (#690).
            self.bluetit.tun_ok()
        return connected

    def _fall_back(self, hard_deadline, reason):
        """`quick`, then the bookkeeping. Verified like any other connect.

        `quick` is the last thing standing between the pod and no tunnel at all,
        so the one thing it must not do is report a success it cannot back with
        traffic.
        """
        connected = self._quick(hard_deadline)
        verified = bool(connected) and self.verify_tunnel(connected)
        return self._finish(
            connected, reason, "fallback",
            cooldown=None if verified else self.cfg.failed_cooldown_seconds)

    def verify_tunnel(self, name):
        """Did the tunnel actually pass a packet? The only signal that cannot lie.

        `goldcrest --bluetit-status` printing `Connected to AirVPN server X` is
        PROVEN false for a WireGuard interface that never completed a handshake:
        on 2026-08-02 it said exactly that for 13 minutes over a tunnel with
        0 B transferred, 100% ICMP loss and no AirVPN session for the device key,
        and it was reproduced deliberately by blackholing only the handshake port
        (#627 rootcause). The string was the agent's ONLY success criterion, so
        the agent logged `connected server=Dalim` and went to sleep on a dead
        tunnel.
        """
        loss = self.probe(self.cfg.verify_probe_count)
        if loss is None:
            log.error("cannot verify %s - the probe did not run. Treating the "
                      "switch as FAILED, because an unverified switch is exactly "
                      "what the 2026-08-02 incident was", name)
            return False
        if loss > self.cfg.verify_max_loss_pct:
            log.error("connected to %s but the tunnel passes no traffic "
                      "(loss=%.1f%% over %d packets to %s) - the status string "
                      "lied, the switch FAILED",
                      name, loss, self.cfg.verify_probe_count, self.cfg.probe_target)
            return False
        log.info("verified %s by traffic, loss=%.1f%%", name, loss)
        return True

    def _earned_cooldown(self, reason):
        """How long `reason` gets to protect the server it just put us on.

        The full cooldown only for a reason that corrected something measured;
        the short one for an unverified placement. FULL_COOLDOWN_REASONS carries
        the why, and the timing is the point, so here is the arithmetic for the
        short one at its default 300 s:

        - a degradation trip needs bad_windows (3) consecutive windows, and one
          window is probe_interval (60 s) plus the ~10 s the 50-packet probe
          takes, so the earliest trip is ~210 s after the agent starts watching;
        - 210 s < 300 s, so the FIRST trip after a boot_upgrade is refused. The
          bad-window counter resets, three more windows run, and the second trip
          lands at ~420 s and goes through. So a bad boot pick is corrected about
          7 minutes in - not 6 hours (#789), and not instantly either, which is
          what keeps a flapping pool from chaining switches back to back;
        - the observed 2026-08-04 sequence was slower than that floor (boot
          verified 13:43:17, 3/3 at 13:52:42 = 565 s) and would have switched on
          its FIRST trip with no refusal at all.

        The daily cap is untouched and still bounds churn: worst case is one boot
        placement plus two degradation corrections (the second 6 h after the
        first), then max_switches_per_day (3) stops the agent for the day.
        """
        return (self.cfg.cooldown_seconds if reason in FULL_COOLDOWN_REASONS
                else self.cfg.failed_cooldown_seconds)

    def _finish(self, connected, reason, counted_as, cooldown=None):
        """counted_as is None when the switch is not to be called a success.

        `cooldown` is what this attempt arms; None means "whatever `reason` has
        earned" - see _earned_cooldown().
        """
        with self.lock:
            self.current_server = connected
            self.consecutive_bad = 0
            self.consecutive_bad_server = None
            self.consecutive_bad_generation = None
            if connected and counted_as:
                self.switches[counted_as] = self.switches.get(counted_as, 0) + 1
        # Every attempt arms a cooldown, successful or not. Ending on `quick`
        # rather than a ranked candidate is the Component 7 circuit breaker -
        # a bad pool must not cause thrash, and every switch costs peer
        # connections and a private-tracker re-announce.
        #
        # But an attempt that has not earned the full cooldown arms the SHORT one,
        # and there are two of those. A switch that produced no working tunnel
        # (#627 fix 2, passed in explicitly): the 2026-08-02 boot upgrade onto a
        # tunnel that never handshook armed the full 21600 s, and three minutes
        # later the degradation watch reached bad_windows=3, asked
        # budget.allowed(), was told "cooldown, ~21400s left", logged
        # `degradation switch suppressed` and did nothing for six hours. And a
        # switch whose REASON is only a placement rather than a measured
        # correction (#789, derived from `reason` when no cooldown is passed):
        # 2026-08-04 that same lockout happened again with a tunnel that DID
        # handshake and was merely lossy, so the verification carve-out alone was
        # not enough. A switch must never lock out recovery from itself, whichever
        # way it went wrong. The daily cap still counts every one of them, so a
        # broken pool cannot thrash instead.
        armed = self._earned_cooldown(reason) if cooldown is None else cooldown
        self.budget.record(armed)
        # Say which one was armed. The budget lives on an emptyDir, so after the
        # pod is gone this log line is the only record of whether a boot pick
        # locked the degradation watch out (#789), and "which cooldown did it
        # arm" was exactly the question that could not be answered live.
        log.info("armed a %ds cooldown for reason=%s, %d/%d switches used today",
                 armed, reason, len(self.budget.history), self.cfg.max_switches_per_day)
        if not connected:
            log.error("switch reason=%s ended with NO tunnel - the network lock is "
                      "still armed, so nothing leaks, but qBittorrent has no egress",
                      reason)
        elif counted_as == "fallback":
            log.warning("switch reason=%s ended on quick (%s) - not counted as a "
                        "successful %s", reason, connected, reason)
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

    def probe(self, count=None):
        """50 ICMP packets to the in-tunnel gateway. Returns loss percent.

        `count` overrides the degradation window's packet count. The
        post-connect verification (#627 fix 1) passes a short burst, because it
        runs inside the recovery budget and only has to answer "did anything
        come back", not "how good is this server".
        """
        count = self.cfg.probe_count if count is None else count
        interval = 1.0 / self.cfg.probe_rate
        try:
            result = subprocess.run(
                ["ping", "-n", "-q", "-c", str(count),
                 "-i", "%g" % interval, "-W", "2", self.cfg.probe_target],
                capture_output=True, text=True,
                timeout=count * interval + 60,
            )
            # `ping` exits 1 on total loss and still prints the summary, so the
            # return code alone is not a failure signal.
            loss, _ = parse_ping(result.stdout + result.stderr)
        except Exception as exc:
            log.warning("probe failed: %s", exc)
            return None
        return loss

    def throughput_since(self, before, started):
        """tun0 bytes/sec since (before, started), or None when there is no number.

        Feeds vpn_agent_tunnel_throughput_bytes_per_sec and NOTHING else - the
        gate that used to decide on it is gone (#771). None is still not zero,
        because the metric has to be absent rather than claim an idle tunnel it
        never measured (#686/#690). Three ways there is no number:

        - the device is missing or its counters are unreadable (`tun_bytes()` None);
        - the delta is negative, which is the counter RESET - tun0 is deleted and
          rebuilt on every switch (wg_del_device) and the `airvpn` sidecar can
          reconnect under us on its own liveness probe, so this is a normal event,
          not a corrupt read;
        - the interval is shorter than MIN_THROUGHPUT_INTERVAL_SECONDS, so the
          quotient would describe packet arrival timing rather than the traffic
          (#768). NOT `elapsed > 0`: at 1 ms a single MTU-sized packet reads as
          1.5 MB/s and an empty millisecond reads as 0.0 B/s, and 0.0 B/s is the
          worse one - it looks like a measured idle tunnel (#686/#690).
        """
        after = self.bluetit.tun_bytes()
        elapsed = self.clock() - started
        if before is None or after is None or elapsed < MIN_THROUGHPUT_INTERVAL_SECONDS:
            return None
        delta = after - before
        if delta < 0:
            return None
        return delta / elapsed

    def watch_once(self):
        """One degradation window. Returns True if it triggered a switch."""
        # A loss window is evidence about ONE unchanged tunnel SESSION. The
        # airvpn sidecar can reconnect independently while this process stays up.
        # Server names catch Dalim -> Dedalus, but not a same-server reconnect or
        # A -> B -> A during ping: both names read A while teardown loss crosses
        # sessions. Bracket both the server and tun0's ifindex generation. Any
        # changed/disconnected/unmeasurable endpoint discards the whole window.
        server_before = self.bluetit.status()
        generation_before = self.bluetit.tun_generation()

        # Bracket the probe's byte counters too, so throughput covers the SAME
        # interval the loss figure describes. Not the whole 60 s window and not
        # either status call - a goldcrest call can take 25 s, and averaging the
        # traffic over that would describe a different interval than the loss.
        before, started = self.bluetit.tun_bytes(), self.clock()
        loss = self.probe()
        throughput = self.throughput_since(before, started)
        current = self.bluetit.status()
        generation_after = self.bluetit.tun_generation()
        same_server = bool(
            server_before and current
            and server_before.lower() == current.lower()
        )
        same_session = bool(
            same_server
            and generation_before is not None
            and generation_before == generation_after
        )

        with self.lock:
            self.current_server = current
            # A reconnect-spanning rate/loss is not attributable to the current
            # server. Clear it instead of leaving a stale value next to the new
            # current_server label (#686/#690).
            self.throughput_bps = throughput if same_session else None
            self.current_loss_pct = loss if same_session and loss is not None else None
            if not same_session:
                self.consecutive_bad = 0
                self.consecutive_bad_server = None
                self.consecutive_bad_generation = None

        # TWO `--bluetit-status` calls and two cheap ifindex reads per window,
        # before and after the probe, and none per scrape. One status call fixed
        # the stale current_server metric in #690; the second plus the ifindex
        # generation bind this safety-critical verdict to one unchanged session.
        # Status stays in the loop, not render_metrics(): a call can take 25 s
        # plus a 15 s subprocess backstop, which would blow the Prometheus scrape
        # timeout and lose every metric.
        if not same_session:
            log.info(
                "discarding probe window because tunnel session changed during "
                "it: server=%s -> %s ifindex=%s -> %s",
                server_before or "disconnected", current or "disconnected",
                generation_before if generation_before is not None else "unreadable",
                generation_after if generation_after is not None else "unreadable",
            )
            return False
        if loss is None:
            return False

        if loss < self.cfg.bad_loss_pct:
            with self.lock:
                self.consecutive_bad = 0
                self.consecutive_bad_server = None
                self.consecutive_bad_generation = None
            return False

        # NOTHING about `throughput` may appear between here and the verdict.
        # PR #764 put a gate here that returned "healthy" whenever tun0 was busy,
        # and #771 removed it: a lossy-but-still-delivering server was declared
        # healthy, and since a seeding box is almost never under the floor the
        # detector was unreachable in practice while looking armed. `throughput`
        # is measured above for the METRICS only - it is not an input to this
        # decision, and an unmeasurable window is a missing metric, not a veto.
        #
        # A fully dead tunnel is EXEMPT. The liveness probe plus `quick` already
        # own that path, and a switch cannot fix a Bluetit that is not connected.
        if not current:
            log.warning("loss=%.2f%% but Bluetit is not connected - the liveness probe "
                        "owns this, not the agent", loss)
            return False

        with self.lock:
            server_key = current.lower()
            # same_session proves this ONE window did not cross a reconnect.
            # Carry the ifindex too so a reconnect completed BETWEEN windows
            # starts a new run even when it returns to the same server name.
            if (self.consecutive_bad_server != server_key
                    or self.consecutive_bad_generation != generation_after):
                self.consecutive_bad = 0
                self.consecutive_bad_server = server_key
                self.consecutive_bad_generation = generation_after
            self.consecutive_bad += 1
            bad = self.consecutive_bad
        log.warning("bad window loss=%.2f%% server=%s %d/%d",
                    loss, current, bad, self.cfg.bad_windows)
        if bad < self.cfg.bad_windows:
            return False

        with self.lock:
            self.consecutive_bad = 0
            self.consecutive_bad_server = None
            self.consecutive_bad_generation = None

        # Submit the MEASUREMENT before asking whether a switch is allowed.
        # Cooldown and the daily cap govern peer-disrupting actions; they do not
        # make three sustained in-tunnel loss windows untrue. The one daemon
        # writer persists it when storage is available, but hard NFS can never
        # hold this control loop before allowed()/switch() (#792 hardening).
        # Dry-run is the one exception: changing the shared ranking would be an
        # action, so rollback to dry-run remains behaviorally inert.
        if self.cfg.dry_run:
            log.info(
                "DRY RUN would publish in-tunnel bad-server verdict "
                "server=%s loss=%.2f%%", current, loss,
            )
        else:
            self.publish_bad_server(current, loss)

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

    def close(self):
        """Signal the verdict daemon without ever waiting on hard NFS."""
        self.verdict_publisher.shutdown()


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def render_metrics(agent):
    with agent.lock:
        server = agent.current_server
        loss = agent.current_loss_pct
        bad = agent.consecutive_bad
        switching = agent.switching
        switches = dict(agent.switches)
        throughput = agent.throughput_bps
    # Both of these are MEASURED HERE, outside the lock, not taken off a field on
    # the agent. A cached value is only ever as fresh as its last writer, and in
    # both cases the only writer was the switch path - which a healthy pod never
    # enters, so the age froze at its boot value (#686) and the device flag
    # published the `True` it was initialised with, without ever having looked
    # (#690). Do NOT "simplify" either of these back to an attribute lookup.
    #
    # Both are cheap enough to sit in the HTTP handler thread: one small local
    # file read and one os.listdir, no subprocess. That is the line - anything
    # needing a goldcrest call is refreshed in watch_once() instead, because a
    # 25 s D-Bus call in here would blow the scrape timeout and take the whole
    # target down.
    age = agent.ranking_age_now()
    device_ok = agent.bluetit.tun_present()
    # Same rule, same reason: MEASURED here, at scrape time, two /sys reads. This
    # is the raw counter, so a reader (or #736's alert) can rate() it without
    # trusting anything the agent cached. The windowed rate below is a different
    # thing - last probe window's average - and it is absent, not stale, when a
    # window could not produce one.
    tun_bytes = agent.bluetit.tun_bytes()
    # Same rule once more, and this one is the ENFORCEMENT's own state rather than
    # a count of what the switch path happened to increment. snapshot() is
    # read-only and lock-free on purpose - see its docstring - and costs one
    # clock() call plus a filter of at most max_switches_per_day entries, no file
    # I/O and no subprocess, so it belongs in the handler thread (#783).
    budget_used, budget_exhausted, cooldown_left = agent.budget.snapshot()
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
        # What vpn_agent_switches_total above cannot say, per reason it cannot:
        # _finish() calls budget.record() unconditionally but increments
        # switches[] only `if connected and counted_as`, so a switch that ends
        # with no tunnel (and a dry-run switch) spends a slot and moves no
        # counter; Agent.switches is in-memory while the history is on disk
        # exactly so it outlives an agent.py restart; and the counter is
        # monotonic with no window while the budget prunes on a rolling 24 h.
        # These three read the budget itself, so there is nothing left to infer.
        "# HELP vpn_agent_switch_budget_used Switch slots spent inside the enforced rolling 24 h window, counting every reason - len(SwitchBudget.history) after pruning, measured at scrape time. Counts switches vpn_agent_switches_total cannot see (no tunnel, dry run) and survives an agent.py restart.",
        "# TYPE vpn_agent_switch_budget_used gauge",
        "vpn_agent_switch_budget_used %d" % budget_used,
        "# HELP vpn_agent_switch_budget_exhausted Whether the agent has stopped switching for the day: the same len(history) >= VPN_AGENT_MAX_SWITCHES_PER_DAY test allowed() enforces, so this cannot drift from the enforcement.",
        "# TYPE vpn_agent_switch_budget_exhausted gauge",
        "vpn_agent_switch_budget_exhausted %d" % (1 if budget_exhausted else 0),
        "# HELP vpn_agent_switch_cooldown_seconds_left Seconds until the cooldown armed by the last switch expires - the other half of allowed(), and what made the 2026-08-02 and 2026-08-04 lockouts invisible. 0 when no cooldown is holding.",
        "# TYPE vpn_agent_switch_cooldown_seconds_left gauge",
        "vpn_agent_switch_cooldown_seconds_left %.0f" % cooldown_left,
        "# HELP vpn_agent_consecutive_bad_windows Consecutive probe windows with ICMP loss to the in-tunnel gateway at or over VPN_AGENT_BAD_LOSS_PCT. Resets on the first window under it.",
        "# TYPE vpn_agent_consecutive_bad_windows gauge",
        "vpn_agent_consecutive_bad_windows %d" % bad,
        "# HELP vpn_agent_tunnel_device_ok Whether the device qBittorrent is bound to exists, read from /sys/class/net at scrape time. Read it together with vpn_agent_switch_in_progress: 0 during a switch is the normal teardown, not a failure.",
        "# TYPE vpn_agent_tunnel_device_ok gauge",
        "vpn_agent_tunnel_device_ok %d" % (1 if device_ok else 0),
        # The companion the scrape-time read needs, rather than fudging the read
        # itself. The device is deleted and rebuilt on every switch, so a scrape
        # inside that window honestly sees no device - suppressing the metric
        # there would trade a true 0 for an absent series, and absent is the one
        # state that cannot be told apart from "the agent stopped answering".
        # Emitting both lets a reader (or an alert) say `device_ok == 0 unless
        # switch_in_progress` and be exactly right. The window is bounded by
        # recovery_budget (120 s) and capped at 3 switches a day.
        "# HELP vpn_agent_switch_in_progress Whether a switch is between its disconnect and its outcome, the window in which the tunnel device is torn down and rebuilt.",
        "# TYPE vpn_agent_switch_in_progress gauge",
        "vpn_agent_switch_in_progress %d" % (1 if switching else 0),
    ]
    if server:
        lines += [
            "# HELP vpn_agent_current_server The AirVPN server the tunnel is on, re-read from Bluetit once per probe window. Absent when Bluetit reports not connected.",
            "# TYPE vpn_agent_current_server gauge",
            'vpn_agent_current_server{server="%s"} 1' % server,
        ]
    if tun_bytes is not None:
        lines += [
            "# HELP vpn_agent_tunnel_bytes_total rx+tx bytes on the tunnel device, read from /sys/class/net at scrape time. Resets to 0 whenever tun0 is rebuilt, which is every switch.",
            "# TYPE vpn_agent_tunnel_bytes_total counter",
            "vpn_agent_tunnel_bytes_total %d" % tun_bytes,
        ]
    # Absent, never 0, when the window produced no number. `vpn_agent_tunnel_bytes_total`
    # above is published independently and is measured at scrape time, so a reader
    # keeps the raw counter and can rate() it over its own window even when this
    # series is gone - which is what makes omitting this one cheap. A placeholder
    # here would be a value that reads as measured and is not (#686/#690/#771).
    if throughput is not None:
        lines += [
            "# HELP vpn_agent_tunnel_throughput_bytes_per_sec rx+tx bytes/sec over the last probe window, i.e. the same interval vpn_agent_current_loss_pct describes. Diagnostic only - it decides nothing (#771). Absent when the window produced no honest number (device gone, tun0 rebuilt mid-window so the counters reset, or the window was shorter than MIN_THROUGHPUT_INTERVAL_SECONDS and too short to measure - #768).",
            "# TYPE vpn_agent_tunnel_throughput_bytes_per_sec gauge",
            "vpn_agent_tunnel_throughput_bytes_per_sec %.0f" % throughput,
        ]
    if loss is not None:
        lines += [
            # This is the whole degradation decision again (#771). Read it next to
            # vpn_agent_tunnel_throughput_bytes_per_sec, but read that as context
            # and not as an excuse: a busy tunnel at high loss is NOT known to be
            # fine - measured 2026-08-04, 13.66 MB/s came with 0.0% loss, so
            # throughput does not explain loss away. See Config.bad_loss_pct.
            "# HELP vpn_agent_current_loss_pct Last measured ICMP loss to the in-tunnel gateway.",
            "# TYPE vpn_agent_current_loss_pct gauge",
            "vpn_agent_current_loss_pct %s" % loss,
        ]
    if age is not None:
        lines += [
            "# HELP vpn_agent_ranking_age_seconds Age of the published ranking, measured at scrape time. Absent when there is no usable ranking file.",
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
        "bad>=%.1f%% trip=%d cooldown=%ds failed_cooldown=%ds cap=%d/day "
        "verify=%d packets at <=%.0f%% loss",
        cfg.dry_run, cfg.band, cfg.probe_count, cfg.probe_target, cfg.probe_interval,
        cfg.bad_loss_pct, cfg.bad_windows, cfg.cooldown_seconds,
        cfg.failed_cooldown_seconds, cfg.max_switches_per_day,
        cfg.verify_probe_count, cfg.verify_max_loss_pct,
    )
    agent = Agent(cfg)
    serve(agent)
    try:
        agent.run()
    finally:
        # The publisher is a daemon by design, and close() never joins it. A
        # thread asleep forever inside a hard-NFS syscall cannot delay process
        # or container shutdown.
        agent.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
