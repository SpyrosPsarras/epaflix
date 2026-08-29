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
import ipaddress
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

SCHEMA = 1

VERDICT_SCHEMA = 1
VERDICT_SOURCE = "vpn-agent"
VERDICT_TTL_SECONDS = 21600
MAX_PENDING_VERDICT_DESTINATIONS = 128
MAX_RANKING_BYTES = 1024 * 1024
MAX_RANKING_TTL_SECONDS = 2100
RANKING_FUTURE_SKEW_SECONDS = 300
RANKING_DOCUMENT_KEYS = frozenset((
    "schema", "generated_at", "ttl_seconds", "servers",
))
RANKING_SERVER_KEYS = frozenset((
    "name", "entry_ip", "loss_pct", "rtt_ms", "load", "bw_max", "headroom",
))
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")

MIN_THROUGHPUT_INTERVAL_SECONDS = 1.0

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
        self.ranking_path = os.environ.get(
            "VPN_AGENT_RANKING_PATH", "/media/.vpn-picker/ranking.json"
        )
        self.ranking_poll_seconds = _env_float(
            "VPN_AGENT_RANKING_POLL_SECONDS", 5.0
        )
        self.ranking_initial_wait_seconds = _env_float(
            "VPN_AGENT_RANKING_INITIAL_WAIT_SECONDS", 1.0
        )
        self.cache_path = os.environ.get(
            "VPN_AGENT_CACHE_PATH", "/var/cache/vpn-agent/ranking.json"
        )
        self.budget_path = os.environ.get(
            "VPN_AGENT_BUDGET_PATH", "/var/cache/vpn-agent/switches.json"
        )
        self.band = _env_int("VPN_AGENT_BAND", 5)

        self.probe_target = os.environ.get("VPN_AGENT_PROBE_TARGET", "10.128.0.1")
        self.probe_count = _env_int("VPN_AGENT_PROBE_COUNT", 50)
        self.probe_rate = _env_float("VPN_AGENT_PROBE_RATE", 5.0)
        self.probe_interval = _env_int("VPN_AGENT_PROBE_INTERVAL_SECONDS", 60)
        self.bad_loss_pct = _env_float("VPN_AGENT_BAD_LOSS_PCT", 5.0)
        self.bad_windows = _env_int("VPN_AGENT_BAD_WINDOWS", 3)
        self.cooldown_seconds = _env_int("VPN_AGENT_COOLDOWN_SECONDS", 21600)
        self.max_switches_per_day = _env_int("VPN_AGENT_MAX_SWITCHES_PER_DAY", 3)

        self.goldcrest_timeout = _env_int("VPN_AGENT_GOLDCREST_TIMEOUT", 25)
        self.max_output_bytes = _env_int("VPN_AGENT_MAX_OUTPUT_BYTES", 65536)
        self.verify_seconds = _env_int("VPN_AGENT_VERIFY_SECONDS", 30)
        self.verify_interval = _env_float("VPN_AGENT_VERIFY_INTERVAL", 2.0)
        self.verify_probe_count = _env_int("VPN_AGENT_VERIFY_PROBE_COUNT", 5)
        self.verify_max_loss_pct = _env_float("VPN_AGENT_VERIFY_MAX_LOSS_PCT", 50.0)
        self.failed_cooldown_seconds = _env_int("VPN_AGENT_FAILED_COOLDOWN_SECONDS", 300)
        self.attempts_per_candidate = _env_int("VPN_AGENT_ATTEMPTS", 2)
        self.retry_backoff = _env_float("VPN_AGENT_RETRY_BACKOFF", 5.0)
        self.recovery_budget = _env_int("VPN_AGENT_RECOVERY_BUDGET_SECONDS", 120)
        self.quick_reserve = _env_int("VPN_AGENT_QUICK_RESERVE_SECONDS", 35)
        self.jitter_seconds = _env_float("VPN_AGENT_JITTER_SECONDS", 30.0)

        self.boot_wait_seconds = _env_int("VPN_AGENT_BOOT_WAIT_SECONDS", 300)
        self.air_key = os.environ.get("VPN_AGENT_AIR_KEY", "Default")
        self.tun_device = os.environ.get("VPN_AGENT_TUN_DEVICE", "tun0")

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
        self.dry_run = _env_bool("VPN_AGENT_DRY_RUN", False)



_SENT_RECV = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
_RTT = re.compile(r"(?:rtt|round-trip) min/avg/max(?:/mdev)? = [\d.]+/([\d.]+)/")

_CONNECTED = re.compile(r"Connected to AirVPN server (\S+)")

_RC_MARKER = re.compile(r"___rc=(\d+)")

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


def _ranking_number(value, field, minimum=None, maximum=None):
    """A finite JSON number in the schema's range; bool is not numeric here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("ranking %s is not numeric" % field)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("ranking %s is not finite" % field)
    if minimum is not None and number < minimum:
        raise ValueError("ranking %s is below %s" % (field, minimum))
    if maximum is not None and number > maximum:
        raise ValueError("ranking %s is above %s" % (field, maximum))
    return number


def _validate_ranking_row(row):
    """Validate one complete scorer schema-1 row without normalizing it.

    The scorer can republish an already verdict-filtered document. Accepting a
    partial row is therefore not merely a display problem: boot's in_band() or
    degradation's candidate_names() will raise in the control path, after the
    last-good cache has already been replaced. Keep this contract byte-for-
    schema with vpn-picker's restart validator (#792 review).
    """
    if not isinstance(row, dict) or set(row) != RANKING_SERVER_KEYS:
        raise ValueError("ranking server row has wrong fields")
    name = row["name"]
    if not isinstance(name, str) or not _IDENTITY.fullmatch(name):
        raise ValueError("ranking server name is invalid")
    entry_ip = row["entry_ip"]
    if not isinstance(entry_ip, str):
        raise ValueError("ranking entry_ip is not a string")
    try:
        if ipaddress.ip_address(entry_ip).version != 4:
            raise ValueError("ranking entry_ip is not IPv4")
    except ValueError as exc:
        raise ValueError("ranking entry_ip is invalid") from exc
    _ranking_number(row["loss_pct"], "loss_pct", 0.0, 100.0)
    _ranking_number(row["rtt_ms"], "rtt_ms", 0.0)
    if type(row["load"]) is not int or not 0 <= row["load"] <= 100:
        raise ValueError("ranking load is not an integer percent")
    if type(row["bw_max"]) is not int or row["bw_max"] <= 0:
        raise ValueError("ranking bw_max is not a positive integer")
    if (type(row["headroom"]) is not int or row["headroom"] < 0
            or row["headroom"] > row["bw_max"]):
        raise ValueError("ranking headroom is outside 0..bw_max")


def _validate_ranking_rows(servers):
    if not isinstance(servers, list) or not servers:
        raise ValueError("ranking has no servers")
    seen = set()
    for row in servers:
        _validate_ranking_row(row)
        key = row["name"].lower()
        if key in seen:
            raise ValueError("ranking contains duplicate server %s" % row["name"])
        seen.add(key)
    return list(servers)


def parse_ranking(payload, now_epoch):
    """Return (servers, age_seconds) from a strict schema-1 ranking.

    Every caller - daemon snapshot publication and both local-cache paths -
    comes through here before adopting bytes. Past its TTL the ranking is
    ABSENT, not merely old; the age is still returned so a valid stale snapshot
    can report how far gone it is. Invalid schema/time fails open instead.
    """
    doc = json.loads(payload)
    if not isinstance(doc, dict) or set(doc) != RANKING_DOCUMENT_KEYS:
        raise ValueError("ranking document has wrong fields")
    if type(doc.get("schema")) is not int or doc["schema"] != SCHEMA:
        raise ValueError("unknown ranking schema %r" % (doc.get("schema"),))

    generated_at = doc.get("generated_at")
    if not isinstance(generated_at, str):
        raise ValueError("ranking generated_at is not a string")
    stamp = datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    age = now_epoch - stamp.timestamp()
    if age < -RANKING_FUTURE_SKEW_SECONDS:
        raise ValueError("ranking timestamp is %.0fs in the future" % -age)

    ttl = doc.get("ttl_seconds")
    if (type(ttl) is not int or ttl <= 0
            or ttl > MAX_RANKING_TTL_SECONDS):
        raise ValueError("invalid ranking ttl %r" % (ttl,))
    servers = _validate_ranking_rows(doc.get("servers"))
    if age > ttl:
        return [], age
    return servers, age


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


class RankingSnapshot:
    """Immutable in-memory result of one valid ranking-file read."""

    __slots__ = ("payload",)

    def __init__(self, payload):
        object.__setattr__(self, "payload", bytes(payload))

    def __setattr__(self, name, value):
        raise AttributeError("RankingSnapshot is immutable")


class RankingReader:
    """Exactly one daemon owns all access to the hard-NFS ranking path.

    An NFS `open()` or `read()` may sleep in the kernel forever. No timeout or
    try/except can bound that syscall, so control and HTTP-handler threads only
    read the immutable snapshot assigned here. The worker polls by opening the
    atomic publication path directly (no synchronous stat), then sleeps on a
    Condition. There is no work queue, no thread per read, and a blocked worker
    is never replaced.

    A valid fresh document is copied to the local emptyDir cache before it is
    published as the current snapshot. Missing/malformed input publishes an
    absent snapshot and leaves the last good cache alone. A valid stale document
    remains a snapshot so the age metric can report it, but never refreshes the
    cache. Consumers re-run generated_at/TTL validation against their current
    wall clock, so a worker wedged after one good read cannot preserve that
    ranking past its original lease.
    """

    def __init__(self, path, wallclock, on_fresh, poll_seconds,
                 max_bytes=MAX_RANKING_BYTES):
        if poll_seconds <= 0:
            raise ValueError("ranking poll interval must be positive")
        if max_bytes < 1:
            raise ValueError("ranking max_bytes must be positive")
        self.path = path
        self.wallclock = wallclock
        self.on_fresh = on_fresh
        self.poll_seconds = poll_seconds
        self.max_bytes = max_bytes
        self._condition = threading.Condition()
        self._snapshot = None
        self._attempted = False
        self._attempts = 0
        self._stopping = False
        self._thread = None
        self._last_problem = None

    def start(self):
        """Start once. A wedged or unexpectedly dead worker is never replaced."""
        with self._condition:
            if self._thread is not None or self._stopping:
                return self._thread
            self._thread = threading.Thread(
                target=self._run,
                name="vpn-ranking-reader-%x" % id(self),
                daemon=True,
            )
            try:
                self._thread.start()
            except RuntimeError as exc:
                self._thread = None
                self._attempted = True
                self._condition.notify_all()
                log.error("cannot start ranking reader: %s", exc)
            return self._thread

    def snapshot(self):
        """Current immutable snapshot, or None. Never waits for NFS."""
        self.start()
        with self._condition:
            return self._snapshot

    def wait_initial(self, timeout):
        """Bounded boot-only wait for the first attempt, never for completion."""
        self.start()
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while not self._attempted and not self._stopping:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._condition.wait(left)
            return self._attempted

    def _problem(self, key, message, *args):
        if key != self._last_problem:
            log.warning(message, *args)
        self._last_problem = key

    def _run(self):
        while True:
            snapshot = None
            servers = []
            valid = False
            try:
                with open(self.path, "rb") as fh:
                    payload = fh.read(self.max_bytes + 1)
                if len(payload) > self.max_bytes:
                    raise ValueError("ranking exceeds %d bytes" % self.max_bytes)
                servers, age = parse_ranking(payload, self.wallclock())
                snapshot = RankingSnapshot(payload)
                valid = True
            except FileNotFoundError:
                self._problem("missing", "no ranking at %s", self.path)
            except Exception as exc:
                self._problem(
                    ("unusable", type(exc).__name__, str(exc)),
                    "ranking at %s is unusable: %s", self.path, exc,
                )
            else:
                if servers:
                    self._last_problem = None
                else:
                    self._problem(
                        "stale",
                        "ranking is %.0fs old, past its TTL - treating it as absent",
                        age,
                    )

            if valid and servers:
                with self._condition:
                    if self._stopping:
                        return
                try:
                    self.on_fresh(snapshot.payload, servers)
                except Exception as exc:
                    log.warning("cannot adopt fresh ranking into local cache: %s", exc)

            with self._condition:
                if self._stopping:
                    return
                self._snapshot = snapshot
                self._attempted = True
                self._attempts += 1
                self._condition.notify_all()
                self._condition.wait(self.poll_seconds)
                if self._stopping:
                    return

    def shutdown(self):
        """Stop and wake a healthy reader; never join a kernel-wedged one."""
        with self._condition:
            self._stopping = True
            self._condition.notify_all()


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
                error = exc

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




class Agent:
    def __init__(self, cfg, bluetit=None, sleep=time.sleep, clock=time.monotonic,
                 wallclock=time.time):
        self.cfg = cfg
        self.bluetit = bluetit or Bluetit(cfg, sleep=sleep, clock=clock)
        self.sleep = sleep
        self.clock = clock
        self.wallclock = wallclock
        self.budget = SwitchBudget(cfg.cooldown_seconds, cfg.max_switches_per_day,
                                   clock=wallclock, path=cfg.budget_path)
        self.lock = threading.Lock()
        self.cache_lock = threading.Lock()
        self.verdict_publisher = VerdictPublisher(self._write_bad_server)
        self.current_server = None
        self.current_loss_pct = None
        self.throughput_bps = None
        self.consecutive_bad = 0
        self.consecutive_bad_server = None
        self.consecutive_bad_generation = None
        self.switching = False
        self.switches = {"degradation": 0, "boot_upgrade": 0, "fallback": 0}
        self.cached = []
        self.cached_payload = None
        self.ranking_reader = RankingReader(
            cfg.ranking_path,
            wallclock=self.wallclock,
            on_fresh=self._adopt_fresh_ranking,
            poll_seconds=cfg.ranking_poll_seconds,
        )


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
        if not isinstance(producer, str) or not _IDENTITY.fullmatch(producer):
            log.error("cannot publish bad-server verdict: invalid producer identity")
            return False
        if not _IDENTITY.fullmatch(server):
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


    def _adopt_fresh_ranking(self, payload, servers):
        """RankingReader callback: atomically adopt memory + local emptyDir."""
        payload = bytes(payload)
        with self.cache_lock:
            self._write_cache_locked(payload)
            self.cached_payload = payload
            self.cached = tuple(servers)

    def read_ranking(self):
        """Fresh servers from the daemon's immutable snapshot, never from NFS.

        The reader validates before publishing a snapshot. Revalidate here with
        the CURRENT wall clock because a worker may be asleep forever in the
        next hard-NFS open: its last good bytes are usable only through their
        original generated_at/TTL.
        """
        snapshot = self.ranking_reader.snapshot()
        if snapshot is None:
            return []
        try:
            servers, age = parse_ranking(snapshot.payload, self.wallclock())
        except Exception:
            return []
        if not servers:
            return []
        return servers

    def ranking_age_now(self):
        """Age of the published ranking snapshot AS OF THIS CALL, with no I/O.

        time. The follow-up keeps that property without opening hard NFS in the
        HTTP handler: the daemon supplies immutable bytes, and each scrape parses
        their original generated_at against the current clock. If the daemon is
        wedged after a good read, age continues increasing; malformed/missing
        input observed by a healthy reader is an honest absent.
        """
        snapshot = self.ranking_reader.snapshot()
        if snapshot is None:
            return None
        try:
            return parse_ranking(snapshot.payload, self.wallclock())[1]
        except Exception:
            return None

    def _write_cache_locked(self, payload):
        """Write exact cache bytes while cache_lock owns the transaction."""
        path = self.cfg.cache_path
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("cannot cache the ranking at %s: %s", path, exc)

    def _write_cache(self, payload):
        """Test/setup helper; production adoption uses the same cache lock."""
        with self.cache_lock:
            self._write_cache_locked(payload)

    def _discard_cache_locked(self):
        """Discard memory + disk while cache_lock owns the transaction."""
        self.cached = []
        self.cached_payload = None
        try:
            os.unlink(self.cfg.cache_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("cannot remove unusable ranking cache at %s: %s",
                        self.cfg.cache_path, exc)

    def _discard_cache(self):
        with self.cache_lock:
            self._discard_cache_locked()

    def _load_cache_locked(self):
        """Load and validate disk while cache_lock owns the transaction."""
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
            self._discard_cache_locked()
            return []
        if not servers:
            log.warning(
                "cached ranking is %.0fs old, past its original TTL - failing "
                "open to `quick` because no measured base can restore hidden rows",
                age,
            )
            self._discard_cache_locked()
            return []
        self.cached = tuple(servers)
        self.cached_payload = bytes(payload)
        log.info("adopted cached ranking servers=%s",
                 [s.get("name") for s in self.cached])
        return servers

    def load_cache(self):
        """Adopt a still-fresh cache left by an earlier agent process.

        The cache is the original ranking document, not a new lease. Revalidate
        generated_at and ttl_seconds against the current wall clock every time
        it becomes the fallback. This matters when the scorer is down: the cache
        may already be verdict-filtered, and using its bare server list forever
        turns temporary ejection into a permanent ban. Once it expires, the only
        honest fail-open is no ranking (`quick`); hidden rows cannot be invented.

        The complete read/parse/adopt-or-discard sequence is serialized with the
        reader callback. Atomic file replacement alone cannot prevent an old
        parser result from clearing a newer in-memory and on-disk adoption.
        """
        with self.cache_lock:
            return self._load_cache_locked()

    def cached_ranking(self):
        """Return the cache only while its original ranking lease is valid."""
        with self.cache_lock:
            if self.cached_payload is None:
                return self._load_cache_locked()
            try:
                servers, age = parse_ranking(
                    self.cached_payload, self.wallclock()
                )
            except Exception as exc:
                log.warning("in-memory ranking cache is unusable: %s", exc)
                self._discard_cache_locked()
                return []
            if not servers:
                log.warning(
                    "cached ranking is %.0fs old, past its original TTL - failing "
                    "open to `quick` because no measured base can restore hidden rows",
                    age,
                )
                self._discard_cache_locked()
                return []
            self.cached = tuple(servers)
            return servers

    def candidates(self, exclude=None):
        """Fresh snapshot > finite local cache > nothing (`quick`), with no NFS."""
        servers = self.read_ranking()
        source = "fresh"
        if not servers:
            servers = self.cached_ranking()
            source = "cache"
        if not servers:
            log.warning("no ranking and no cache - only `quick` is left")
            return [], "none"
        return candidate_names(servers, self.cfg.band, exclude), source


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
            self.budget.record(self._earned_cooldown(reason))
            return None

        delay = random.uniform(0, self.cfg.jitter_seconds)
        log.info("switching reason=%s from=%s candidates=%s jitter=%.1fs mandatory=%s",
                 reason, self.current_server, names or ["quick"], delay, mandatory)
        self.sleep(delay)

        started = self.clock()
        hard_deadline = started + self.cfg.recovery_budget
        walk_deadline = hard_deadline - self.cfg.quick_reserve

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
                        if not self.bluetit.tun_ok():
                            log.error("connected to %s but the tunnel device is wrong - "
                                      "the switch FAILED", connected)
                            return self._finish(connected, reason, None,
                                                cooldown=self.cfg.cooldown_seconds)
                        if self.verify_tunnel(connected):
                            log.info("connected server=%s reason=%s attempt=%d elapsed=%.1fs",
                                     connected, reason, attempt, self.clock() - started)
                            return self._finish(connected, reason, reason)
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
        armed = self._earned_cooldown(reason) if cooldown is None else cooldown
        self.budget.record(armed)
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
        """Post-boot upgrade - bounded snapshot, then finite cache, never NFS."""
        self.ranking_reader.start()
        current = self.wait_for_tunnel()
        if not current:
            return
        self.ranking_reader.wait_initial(self.cfg.ranking_initial_wait_seconds)
        servers = self.read_ranking()
        source = "fresh"
        if not servers:
            servers = self.cached_ranking()
            source = "cache"
        if not servers:
            log.info("no fresh ranking or valid cache at boot - staying on quick's pick %s",
                     current)
            return
        if in_band(current, servers, self.cfg.band):
            log.info("quick picked %s, already inside the top %d - staying put",
                     current, self.cfg.band)
            return
        names = candidate_names(servers, self.cfg.band, exclude=current)
        log.info("quick picked %s, outside the top %d %s from %s - upgrading",
                 current, self.cfg.band,
                 [s.get("name") for s in servers[: self.cfg.band]], source)
        self.switch(names, "boot_upgrade", mandatory=False)


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
        server_before = self.bluetit.status()
        generation_before = self.bluetit.tun_generation()

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
            self.throughput_bps = throughput if same_session else None
            self.current_loss_pct = loss if same_session and loss is not None else None
            if not same_session:
                self.consecutive_bad = 0
                self.consecutive_bad_server = None
                self.consecutive_bad_generation = None

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

        if not current:
            log.warning("loss=%.2f%% but Bluetit is not connected - the liveness probe "
                        "owns this, not the agent", loss)
            return False

        with self.lock:
            server_key = current.lower()
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
        self.ranking_reader.start()
        self.boot_check()
        while True:
            self.sleep(self.cfg.probe_interval)
            try:
                self.watch_once()
            except Exception:
                log.exception("watch cycle crashed, staying up for the next one")

    def close(self):
        """Signal both storage daemons without ever waiting on hard NFS."""
        self.ranking_reader.shutdown()
        self.verdict_publisher.shutdown()




def render_metrics(agent):
    with agent.lock:
        server = agent.current_server
        loss = agent.current_loss_pct
        bad = agent.consecutive_bad
        switching = agent.switching
        switches = dict(agent.switches)
        throughput = agent.throughput_bps
    age = agent.ranking_age_now()
    device_ok = agent.bluetit.tun_present()
    tun_bytes = agent.bluetit.tun_bytes()
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
    if throughput is not None:
        lines += [
            "# HELP vpn_agent_tunnel_throughput_bytes_per_sec rx+tx bytes/sec over the last probe window, i.e. the same interval vpn_agent_current_loss_pct describes. Diagnostic only - it decides nothing (#771). Absent when the window produced no honest number (device gone, tun0 rebuilt mid-window so the counters reset, or the window was shorter than MIN_THROUGHPUT_INTERVAL_SECONDS and too short to measure - #768).",
            "# TYPE vpn_agent_tunnel_throughput_bytes_per_sec gauge",
            "vpn_agent_tunnel_throughput_bytes_per_sec %.0f" % throughput,
        ]
    if loss is not None:
        lines += [
            "# HELP vpn_agent_current_loss_pct Last measured ICMP loss to the in-tunnel gateway.",
            "# TYPE vpn_agent_current_loss_pct gauge",
            "vpn_agent_current_loss_pct %s" % loss,
        ]
    if age is not None:
        lines += [
            "# HELP vpn_agent_ranking_age_seconds Age of the daemon reader's latest usable ranking snapshot, measured against wall time at scrape time. Absent when no usable snapshot has been observed.",
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
        agent.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
