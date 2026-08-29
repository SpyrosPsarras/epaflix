#!/usr/bin/env python3
"""vpn-picker scorer - Components 1-3 of the vpn-picker design.

Spec: docs/superpowers/specs/2026-08-01-vpn-picker-design.md

Every scoring cycle: read the AirVPN status API, shortlist by hard filters,
probe the shortlist by ICMP from outside any VPN netns, apply active in-tunnel
agent verdicts, and publish the ranked survivors. Between cycles, reapply new or
expired verdicts to the last measured base ranking without restamping it (#792).

Stdlib only on purpose. The whole job is one HTTPS GET, five `ping` runs and a
JSON file, and adding a dependency would mean a pip layer and a supply chain to
watch for something `urllib` already does.
"""

import ipaddress
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("vpn-picker")

SCHEMA = 1

VERDICT_SCHEMA = 1
VERDICT_SOURCE = "vpn-agent"
VERDICT_MAX_BYTES = 65536
VERDICT_MAX_TTL_SECONDS = 21600
VERDICT_FUTURE_SKEW_SECONDS = 300
MAX_EJECTION_FRACTION = 0.5
VERDICT_RECHECK_SECONDS = 5
BASE_STATE_SCHEMA = 1
BASE_STATE_MAX_BYTES = 1048576
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
RANKING_DOCUMENT_KEYS = frozenset((
    "schema", "generated_at", "ttl_seconds", "servers",
))
RANKING_SERVER_KEYS = frozenset((
    "name", "entry_ip", "loss_pct", "rtt_ms", "load", "bw_max", "headroom",
))


def _env_int(name, default):
    return int(os.environ.get(name, default))


def _env_float(name, default):
    return float(os.environ.get(name, default))


class Config:
    """Every knob the spec names, no magic numbers buried in the logic."""

    def __init__(self):
        self.api_url = os.environ.get("VPN_PICKER_API_URL", "https://airvpn.org/api/")
        self.api_key = os.environ.get("AIRVPN_API_KEY", "")
        self.countries = [
            c.strip().lower()
            for c in os.environ.get("VPN_PICKER_COUNTRIES", "nl,de,se").split(",")
            if c.strip()
        ]
        self.max_load = _env_int("VPN_PICKER_MAX_LOAD", 75)
        self.shortlist_size = _env_int("VPN_PICKER_SHORTLIST", 5)
        self.probe_count = _env_int("VPN_PICKER_PROBE_COUNT", 300)
        self.probe_rate = _env_float("VPN_PICKER_PROBE_RATE", 5.0)
        self.max_loss_pct = _env_float("VPN_PICKER_MAX_LOSS_PCT", 1.0)
        self.interval_seconds = _env_int("VPN_PICKER_INTERVAL_SECONDS", 900)
        self.ttl_seconds = _env_int("VPN_PICKER_TTL_SECONDS", 2100)
        self.output_path = os.environ.get(
            "VPN_PICKER_OUTPUT", "/media/.vpn-picker/ranking.json"
        )
        self.base_state_path = os.environ.get(
            "VPN_PICKER_BASE_STATE",
            os.path.join(
                os.path.dirname(self.output_path) or ".", "ranking-base.json"
            ),
        )
        self.verdict_dir = os.environ.get(
            "VPN_PICKER_VERDICT_DIR",
            os.path.join(os.path.dirname(self.output_path) or ".", "verdicts"),
        )
        self.verdict_max_ttl_seconds = _env_int(
            "VPN_PICKER_VERDICT_MAX_TTL_SECONDS", VERDICT_MAX_TTL_SECONDS
        )
        self.verdict_future_skew_seconds = _env_int(
            "VPN_PICKER_VERDICT_FUTURE_SKEW_SECONDS", VERDICT_FUTURE_SKEW_SECONDS
        )
        self.max_ejection_fraction = _env_float(
            "VPN_PICKER_MAX_EJECTION_FRACTION", MAX_EJECTION_FRACTION
        )
        self.verdict_recheck_seconds = _env_int(
            "VPN_PICKER_VERDICT_RECHECK_SECONDS", VERDICT_RECHECK_SECONDS
        )
        self.listen_port = _env_int("VPN_PICKER_LISTEN_PORT", 8080)
        self.api_timeout = _env_int("VPN_PICKER_API_TIMEOUT", 30)




def headroom(server):
    """Absolute spare bandwidth in Mbit.

    Not bare load percent: a 2 Gbit box and a 20 Gbit box at the same load are
    not equals. `quick` picked a 2 Gbit box at 78% load twice while eleven
    20 Gbit boxes in the same pool sat at 20-33%.
    """
    return int(server.get("bw_max", 0)) - int(server.get("bw", 0))


def shortlist(servers, cfg):
    """Stage 1 - hard filters, then rank by headroom, then take the top N.

    `health == "ok"` is a precondition, never a quality signal - it told us
    `Anser` was fine at 22% measured loss. `health == "error"` means the server
    is CLOSED to new connections, so it is not merely deprioritised.
    """
    kept = [
        s
        for s in servers
        if s.get("health") == "ok"
        and str(s.get("country_code", "")).lower() in cfg.countries
        and int(s.get("currentload", 100)) <= cfg.max_load
    ]
    kept.sort(key=headroom, reverse=True)
    return kept[: cfg.shortlist_size]


def rank_survivors(probed, max_loss_pct):
    """Stage 2 - the probe gate is a gate, not a weight.

    Anything over the loss ceiling is dropped outright. Survivors sort by loss,
    then RTT, then load.
    """
    survivors = [c for c in probed if c["loss_pct"] <= max_loss_pct]
    survivors.sort(key=lambda c: (c["loss_pct"], c["rtt_ms"], c["load"]))
    return survivors




def fetch_servers(cfg):
    """GET the AirVPN status API. Raises on anything that is not usable JSON."""
    query = urllib.parse.urlencode({"key": cfg.api_key, "service": "status", "format": "json"})
    with urllib.request.urlopen(f"{cfg.api_url}?{query}", timeout=cfg.api_timeout) as resp:
        payload = json.load(resp)
    servers = payload.get("servers")
    if not servers:
        raise ValueError("status API returned no servers")
    return servers


_SENT_RECV = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
_RTT = re.compile(r"(?:rtt|round-trip) min/avg/max(?:/mdev)? = [\d.]+/([\d.]+)/")


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


def probe(ip, cfg):
    """300 ICMP packets at 5/s against one entry IP, about 60 s.

    Probe size is settled by measurement: ~100 packets separates clean from
    broken but cannot tell 4% from 9% apart, 300-500 settles the mid-range to
    about +/-2 points. Loss is not bursty here (longest drop run measured was 5
    packets), so a short dense probe is a fair sample.

    Runs the `ping` binary rather than a hand-rolled raw socket: with
    `capabilities.drop: ["ALL"]` iputils falls back to an unprivileged
    SOCK_DGRAM ICMP socket, which the pod netns permits (see the manifest
    comment on `net.ipv4.ping_group_range`).
    """
    interval = 1.0 / cfg.probe_rate
    expected = cfg.probe_count * interval
    result = subprocess.run(
        ["ping", "-n", "-q", "-c", str(cfg.probe_count), "-i", f"{interval:g}", "-W", "2", ip],
        capture_output=True,
        text=True,
        timeout=expected + 60,
    )
    return parse_ping(result.stdout + result.stderr)


def probe_all(candidates, cfg):
    """Probe the shortlist in parallel. One unreachable candidate is not fatal."""

    def one(server):
        name, ip = server["public_name"], server["ip_v4_in1"]
        try:
            loss_pct, rtt_ms = probe(ip, cfg)
        except Exception as exc:
            log.warning("probe failed server=%s ip=%s err=%s", name, ip, exc)
            return None
        row = {
            "name": name,
            "entry_ip": ip,
            "loss_pct": loss_pct,
            "rtt_ms": rtt_ms,
            "load": int(server.get("currentload", 0)),
            "bw_max": int(server.get("bw_max", 0)),
            "headroom": headroom(server),
        }
        log.info(
            "probed server=%s ip=%s loss_pct=%s rtt_ms=%s load=%s headroom=%s",
            name, ip, loss_pct, rtt_ms, row["load"], row["headroom"],
        )
        return row

    with ThreadPoolExecutor(max_workers=max(1, len(candidates))) as pool:
        return [r for r in pool.map(one, candidates) if r is not None]




def parse_verdict(payload, cfg, now_epoch=None):
    """Validate one agent verdict; return a normalized dict or None if stale.

    The scorer is deliberately NOT an arbiter here. A clean entry-IP probe does
    not veto the agent: #767 proved that would suppress a correct switch. This
    function only decides whether the agent document is structurally valid and
    still inside its finite lease. Any doubt fails open by making the verdict
    absent, never by dropping the otherwise-good ranking.
    """
    if len(payload) > VERDICT_MAX_BYTES:
        raise ValueError("verdict exceeds %d bytes" % VERDICT_MAX_BYTES)
    doc = json.loads(payload)
    if not isinstance(doc, dict):
        raise ValueError("verdict is not an object")
    if type(doc.get("schema")) is not int or doc["schema"] != VERDICT_SCHEMA:
        raise ValueError("unknown verdict schema %r" % (doc.get("schema"),))
    if doc.get("source") != VERDICT_SOURCE:
        raise ValueError("untrusted verdict source %r" % (doc.get("source"),))

    producer = doc.get("producer")
    server = doc.get("server")
    if not isinstance(producer, str) or not _IDENTITY.fullmatch(producer):
        raise ValueError("invalid producer identity")
    if not isinstance(server, str) or not _IDENTITY.fullmatch(server):
        raise ValueError("invalid server name")

    observed = doc.get("observed_at")
    if not isinstance(observed, str):
        raise ValueError("observed_at is not a string")
    stamp = datetime.strptime(observed, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    observed_epoch = stamp.timestamp()
    now = time.time() if now_epoch is None else now_epoch
    age = now - observed_epoch
    if age < -cfg.verdict_future_skew_seconds:
        raise ValueError("verdict timestamp is %.0fs in the future" % -age)

    ttl = doc.get("ttl_seconds")
    if type(ttl) is not int or ttl <= 0 or ttl > cfg.verdict_max_ttl_seconds:
        raise ValueError("invalid verdict ttl %r" % (ttl,))
    if age > ttl:
        return None

    loss = doc.get("loss_pct")
    if isinstance(loss, bool) or not isinstance(loss, (int, float)):
        raise ValueError("loss_pct is not numeric")
    loss = float(loss)
    if not math.isfinite(loss) or loss < 0.0 or loss > 100.0:
        raise ValueError("invalid loss_pct")
    bad_windows = doc.get("bad_windows")
    if type(bad_windows) is not int or not 1 <= bad_windows <= 1000:
        raise ValueError("invalid bad_windows")

    return {
        "schema": VERDICT_SCHEMA,
        "source": VERDICT_SOURCE,
        "producer": producer,
        "server": server,
        "observed_at": observed,
        "observed_epoch": observed_epoch,
        "ttl_seconds": ttl,
        "loss_pct": loss,
        "bad_windows": bad_windows,
    }


def load_active_verdicts(cfg, now_epoch=None, log_issues=True):
    """Merge active per-producer/server files without mutating shared storage.

    One producer writes one file per server, so observing a second bad server
    cannot erase the first and the two qBittorrent instances cannot erase each
    other. Duplicate files for the same producer+server are tolerated by taking
    the newest valid observation. Temp files never end in .json and are ignored.
    """
    try:
        with os.scandir(cfg.verdict_dir) as scan:
            entries = sorted(scan, key=lambda e: e.name)
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("verdict directory %s is unusable, failing open: %s",
                    cfg.verdict_dir, exc)
        return []

    merged = {}
    for entry in entries:
        try:
            if (not entry.is_file(follow_symlinks=False)
                    or entry.name.startswith(".") or not entry.name.endswith(".json")):
                continue
            with open(entry.path, "rb") as fh:
                payload = fh.read(VERDICT_MAX_BYTES + 1)
            verdict = parse_verdict(payload, cfg, now_epoch=now_epoch)
        except Exception as exc:
            emit = log.warning if log_issues else log.debug
            emit("ignoring unusable agent verdict %s: %s", entry.name, exc)
            continue
        if verdict is None:
            log.debug("ignoring stale agent verdict %s", entry.name)
            continue
        key = (verdict["producer"].lower(), verdict["server"].lower())
        previous = merged.get(key)
        if previous is None or verdict["observed_epoch"] > previous["observed_epoch"]:
            merged[key] = verdict
    return sorted(
        merged.values(),
        key=lambda v: (-v["observed_epoch"], v["producer"].lower(), v["server"].lower()),
    )


def apply_ejections(survivors, verdicts, max_fraction):
    """Remove the newest agent-rejected servers, bounded to keep a ranking.

    This is Envoy-style outlier ejection: a finite lease plus a maximum ejection
    percentage. At the default 50%, 5 survivors can lose at most 2, 2-3 can
    lose 1, and with only 1 survivor the limit is 0. `len(survivors) - 1` is the
    hard final cap regardless of configuration.

    Multiple producers rejecting the same server count once. Their newest
    observation wins only for deterministic priority when more servers are bad
    than the cap allows; it does not grant the scorer a veto over the verdict.
    """
    if not survivors:
        return [], [], []
    by_server = {}
    available = {str(s.get("name", "")).lower() for s in survivors}
    for verdict in verdicts:
        key = verdict["server"].lower()
        if key not in available:
            continue
        previous = by_server.get(key)
        if previous is None or verdict["observed_epoch"] > previous["observed_epoch"]:
            by_server[key] = verdict
    ordered = sorted(
        by_server.values(),
        key=lambda v: (-v["observed_epoch"], v["server"].lower()),
    )

    fraction = float(max_fraction)
    if not math.isfinite(fraction):
        fraction = 0.0                 # invalid control input fails open
    fraction = max(0.0, min(1.0, fraction))
    limit = int(len(survivors) * fraction)
    limit = min(limit, len(survivors) - 1)
    ejected = ordered[:limit]
    capped = ordered[limit:]
    rejected = {v["server"].lower() for v in ejected}
    kept = [s for s in survivors if str(s.get("name", "")).lower() not in rejected]
    return kept, ejected, capped




def _ranking_number(value, field, minimum=None, maximum=None):
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
    """Validate one complete schema-1 server row without normalizing it.

    Base-journal rows are later republished byte-for-schema into ranking.json.
    A name-only hidden row is therefore not harmless recovery metadata; it is a
    malformed future public row. Keep one validator for both paths so the
    journal can never accept less than the consumer contract (#792 review).
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


def build_document(survivors, cfg, generated_at=None):
    stamp = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = {
        "schema": SCHEMA,
        "generated_at": stamp,
        "ttl_seconds": cfg.ttl_seconds,
        "servers": survivors,
    }
    return json.dumps(doc, indent=2).encode() + b"\n"


def publish(payload, path):
    """Write temp in the SAME directory, fsync, then os.replace.

    Never write in place. A consumer polling the file would otherwise read a
    half-written document, and a JSON parse failure on a partial read is
    indistinguishable from a corrupt publish. `os.replace` over NFS is a
    rename, verified atomic on this NFSv4.2 mount - the temp file has to be on
    the same filesystem for that, hence the same directory.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.tmp.{os.getpid()}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parse_ranking_payload(payload):
    """Validate the public ranking fields needed for restart recovery."""
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
    ttl = doc.get("ttl_seconds")
    if type(ttl) is not int or ttl <= 0:
        raise ValueError("ranking ttl_seconds is not a positive integer")
    servers = _validate_ranking_rows(doc.get("servers"))
    return doc, stamp, servers


def build_base_state(candidate_servers, candidate_generated_at,
                     served_servers=None, served_generated_at=None):
    """Journal the candidate base plus the base behind the served ranking.

    The candidate is written before ranking.json. If that second atomic replace
    fails, the old public bytes still match the `served` entry. Repeated failed
    score cycles keep carrying that served entry forward instead of rotating it
    out, so a restart can always reconstruct the base behind the file consumers
    actually have.
    """
    bases = [{
        "generated_at": candidate_generated_at,
        "servers": list(candidate_servers),
    }]
    if (served_servers and served_generated_at
            and (served_generated_at != candidate_generated_at
                 or list(served_servers) != list(candidate_servers))):
        bases.append({
            "generated_at": served_generated_at,
            "servers": list(served_servers),
        })
    return json.dumps({
        "schema": BASE_STATE_SCHEMA,
        "bases": bases,
    }, indent=2).encode() + b"\n"


def parse_base_state(payload):
    """Return the at-most-two persisted measured bases, strictly validated."""
    if len(payload) > BASE_STATE_MAX_BYTES:
        raise ValueError("base state exceeds %d bytes" % BASE_STATE_MAX_BYTES)
    doc = json.loads(payload)
    if not isinstance(doc, dict) or doc.get("schema") != BASE_STATE_SCHEMA:
        raise ValueError("unknown base-state schema")
    bases = doc.get("bases")
    if not isinstance(bases, list) or not 1 <= len(bases) <= 2:
        raise ValueError("base state must carry one or two bases")
    out = []
    for entry in bases:
        if not isinstance(entry, dict):
            raise ValueError("base entry is not an object")
        generated_at = entry.get("generated_at")
        if not isinstance(generated_at, str):
            raise ValueError("base generated_at is not a string")
        datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ")
        servers = _validate_ranking_rows(entry.get("servers"))
        out.append((servers, generated_at))
    return out


def _is_ranked_subset(public_servers, base_servers):
    """Could public_servers be base_servers with only ejections removed?"""
    positions = {}
    for index, row in enumerate(base_servers):
        key = row["name"].lower()
        if key in positions:
            return False
        positions[key] = (index, row)
    seen = -1
    for row in public_servers:
        match = positions.get(row["name"].lower())
        if match is None or match[0] <= seen or match[1] != row:
            return False
        seen = match[0]
    return True


def load_persisted_base(cfg, public_servers, generated_at):
    """Find the measured base that produced the persisted public ranking."""
    try:
        with open(cfg.base_state_path, "rb") as fh:
            payload = fh.read(BASE_STATE_MAX_BYTES + 1)
        bases = parse_base_state(payload)
    except FileNotFoundError:
        log.warning(
            "base state at %s is missing; preserving the public ranking only "
            "until its original TTL and disabling verdict reconciliation until "
            "a fresh score succeeds (hidden rows are unavailable)",
            cfg.base_state_path,
        )
        return [], None
    except Exception as exc:
        log.warning(
            "base state at %s is unusable: %s; preserving the public ranking "
            "only until its original TTL and disabling verdict reconciliation "
            "until a fresh score succeeds (hidden rows are unavailable)",
            cfg.base_state_path, exc,
        )
        return [], None

    for servers, base_generated_at in bases:
        if (base_generated_at == generated_at
                and _is_ranked_subset(public_servers, servers)):
            return servers, base_generated_at
    log.warning(
        "no persisted measured base matches ranking generated_at=%s; "
        "keeping the public ranking but disabling verdict reconciliation until "
        "a fresh score succeeds",
        generated_at,
    )
    return [], None




class State:
    """The last good ranking, plus the last cycle's raw probe rows for metrics.

    On an AirVPN API outage the scorer keeps serving the last good ranking with
    its ORIGINAL `generated_at`, so the consumer-side TTL expires it naturally.
    Never a fresh timestamp on stale data, never an empty list.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.payload = None
        self.generated_epoch = 0.0
        self.candidates = []
        self.passing = 0
        self.last_cycle_ok = False
        self.active_verdicts = 0
        self.ejected = 0
        self.base_servers = []
        self.base_generated_at = None

    def snapshot(self):
        with self.lock:
            return (
                self.payload,
                self.generated_epoch,
                list(self.candidates),
                self.passing,
                self.last_cycle_ok,
                self.active_verdicts,
                self.ejected,
            )

    def base_snapshot(self):
        with self.lock:
            return list(self.base_servers), self.base_generated_at


def load_existing(state, cfg):
    """Adopt public bytes and their separately persisted measured base.

    ranking.json may already have agent-ejected servers removed. Treating those
    public bytes as the pre-ejection base makes an expired/malformed verdict
    permanent after a scorer restart during an API outage. The base journal is
    written before every public ranking and carries both the candidate and the
    currently served generation, so one entry still matches across a crash or a
    transient publication failure (#792 review).
    """
    try:
        with open(cfg.output_path, "rb") as fh:
            payload = fh.read()
        doc, stamp, public_servers = _parse_ranking_payload(payload)
    except FileNotFoundError:
        return
    except Exception as exc:
        log.warning("existing ranking at %s is unusable: %s", cfg.output_path, exc)
        return

    base_servers, base_generated_at = load_persisted_base(
        cfg, public_servers, doc["generated_at"]
    )
    passing = len(base_servers) if base_servers else len(public_servers)
    ejected = max(0, len(base_servers) - len(public_servers))
    with state.lock:
        state.payload = payload
        state.generated_epoch = stamp.timestamp()
        state.passing = passing
        state.ejected = ejected
        state.base_servers = list(base_servers)
        state.base_generated_at = base_generated_at
    log.info(
        "adopted existing ranking generated_at=%s servers=%s base_servers=%s",
        doc["generated_at"], len(public_servers), len(base_servers),
    )


def render_metrics(state):
    _, generated_epoch, candidates, passing, ok, active_verdicts, ejected = state.snapshot()
    lines = [
        "# HELP vpn_picker_scrape_success Whether the last scoring cycle completed.",
        "# TYPE vpn_picker_scrape_success gauge",
        f"vpn_picker_scrape_success {1 if ok else 0}",
        "# HELP vpn_picker_ranking_generated_timestamp_seconds Unix time the served ranking was generated.",
        "# TYPE vpn_picker_ranking_generated_timestamp_seconds gauge",
        f"vpn_picker_ranking_generated_timestamp_seconds {generated_epoch:.0f}",
        "# HELP vpn_picker_candidates_passing_gate Shortlisted candidates that passed the loss gate, before temporary agent verdict ejection.",
        "# TYPE vpn_picker_candidates_passing_gate gauge",
        f"vpn_picker_candidates_passing_gate {passing}",
        "# HELP vpn_picker_active_agent_verdicts Valid non-stale per-producer/server in-tunnel verdict documents read this cycle.",
        "# TYPE vpn_picker_active_agent_verdicts gauge",
        f"vpn_picker_active_agent_verdicts {active_verdicts}",
        "# HELP vpn_picker_servers_ejected Servers temporarily removed from the published ranking by agent verdicts, after the max-ejection cap.",
        "# TYPE vpn_picker_servers_ejected gauge",
        f"vpn_picker_servers_ejected {ejected}",
    ]
    for label, help_text in (
        ("loss_pct", "Measured ICMP packet loss percent."),
        ("rtt_ms", "Measured average ICMP round-trip time in ms."),
        ("load", "AirVPN reported load percent."),
        ("headroom", "Absolute spare bandwidth in Mbit (bw_max - bw)."),
    ):
        lines.append(f"# HELP vpn_picker_{label} {help_text}")
        lines.append(f"# TYPE vpn_picker_{label} gauge")
        for c in candidates:
            value = c[label]
            if value == float("inf"):
                value = "+Inf"
            lines.append(f'vpn_picker_{label}{{server="{c["name"]}"}} {value}')
    return ("\n".join(lines) + "\n").encode()


class Handler(BaseHTTPRequestHandler):
    state = None
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
            self._send(200, render_metrics(self.state), "text/plain; version=0.0.4")
            return
        if path in ("/", "/ranking.json"):
            payload = self.state.snapshot()[0]
            if payload is None:
                self._send(503, b"no ranking yet\n", "text/plain")
            else:
                self._send(200, payload, "application/json")
            return
        if path == "/healthz":
            self._send(200, b"ok\n", "text/plain")
            return
        self._send(404, b"not found\n", "text/plain")

    def log_message(self, fmt, *args):
        log.debug("http %s", fmt % args)


def serve(state, cfg):
    Handler.state = state
    server = ThreadingHTTPServer(("", cfg.listen_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("serving ranking and metrics on :%s", cfg.listen_port)




def _log_ejections(base_survivors, ejected, capped, cfg):
    for verdict in ejected:
        log.warning(
            "ejected server=%s reason=agent_in_tunnel_loss producer=%s "
            "loss_pct=%s observed_at=%s ttl=%s",
            verdict["server"], verdict["producer"], verdict["loss_pct"],
            verdict["observed_at"], verdict["ttl_seconds"],
        )
    if capped:
        log.warning(
            "max-ejection cap retained agent-rejected servers=%s survivors=%s "
            "fraction=%.2f; ranking stays non-empty",
            [v["server"] for v in capped], len(base_survivors),
            cfg.max_ejection_fraction,
        )


def refresh_ejections(state, cfg):
    """Reconcile new/expired verdicts against the last measured base ranking.

    No API call and no probe. The ranking's original generated_at is preserved,
    so frequent reconciliation cannot keep stale measurements alive. This closes
    the 15-minute race: a verdict published immediately before a pod recreation
    reaches both the shared file consumer and Nick's HTTP consumer in at most the
    short recheck interval, rather than waiting for the next scoring cycle.
    """
    base_survivors, generated_at = state.base_snapshot()
    if not base_survivors or not generated_at:
        return False
    verdicts = load_active_verdicts(cfg, log_issues=False)
    survivors, ejected, capped = apply_ejections(
        base_survivors, verdicts, cfg.max_ejection_fraction
    )
    payload = build_document(survivors, cfg, generated_at=generated_at)
    previous = state.snapshot()[0]
    changed = payload != previous
    if changed:
        try:
            publish(payload, cfg.output_path)
        except Exception as exc:
            log.error("verdict refresh publish to %s failed: %s", cfg.output_path, exc)
            return False
        _log_ejections(base_survivors, ejected, capped, cfg)
    with state.lock:
        state.payload = payload
        state.active_verdicts = len(verdicts)
        state.ejected = len(ejected)
    return changed


def run_cycle(state, cfg):
    """One scoring cycle. Returns True if it published a fresh ranking.

    Nothing in here may raise past the caller: the pod has to stay up and keep
    serving the last good ranking through an API outage, a probe failure or an
    unwritable path.
    """
    try:
        servers = fetch_servers(cfg)
    except Exception as exc:
        log.error(
            "AirVPN API unreachable, keeping the last good ranking and its original "
            "generated_at so the consumer TTL expires it: %s", exc,
        )
        return False

    candidates = shortlist(servers, cfg)
    log.info(
        "shortlisted %s of %s servers filters=health=ok,country=%s,load<=%s picks=%s",
        len(candidates), len(servers), ",".join(cfg.countries), cfg.max_load,
        [f'{s["public_name"]}(headroom={headroom(s)},load={s["currentload"]})' for s in candidates],
    )
    if not candidates:
        log.error("no server passed the API filters, keeping the last good ranking")
        return False

    probed = probe_all(candidates, cfg)
    if not probed:
        log.error("every probe failed, keeping the last good ranking")
        return False

    base_survivors = rank_survivors(probed, cfg.max_loss_pct)
    for c in probed:
        if c["loss_pct"] > cfg.max_loss_pct:
            log.warning(
                "rejected server=%s reason=loss_gate loss_pct=%s ceiling=%s",
                c["name"], c["loss_pct"], cfg.max_loss_pct,
            )
    if not base_survivors:
        log.error(
            "every candidate failed the %s%% loss gate, keeping the last good ranking",
            cfg.max_loss_pct,
        )
        with state.lock:
            state.candidates = probed
            state.passing = 0
            state.last_cycle_ok = False
            state.active_verdicts = 0
            state.ejected = 0
        return False

    verdicts = load_active_verdicts(cfg)
    survivors, ejected, capped = apply_ejections(
        base_survivors, verdicts, cfg.max_ejection_fraction
    )

    log.info(
        "winner server=%s loss_pct=%s rtt_ms=%s headroom=%s runners_up=%s",
        survivors[0]["name"], survivors[0]["loss_pct"], survivors[0]["rtt_ms"],
        survivors[0]["headroom"], [s["name"] for s in survivors[1:]],
    )

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = build_document(survivors, cfg, generated_at=generated_at)

    served_base, served_generated_at = state.base_snapshot()
    base_state = build_base_state(
        base_survivors, generated_at, served_base, served_generated_at
    )
    try:
        publish(base_state, cfg.base_state_path)
    except Exception as exc:
        log.error(
            "cannot persist measured base to %s; keeping both consumers on the "
            "last good ranking: %s", cfg.base_state_path, exc,
        )
        with state.lock:
            state.candidates = probed
            state.passing = len(base_survivors)
            state.last_cycle_ok = False
        return False

    try:
        publish(payload, cfg.output_path)
    except Exception as exc:
        log.error("publish to %s failed; keeping the last good ranking: %s",
                  cfg.output_path, exc)
        with state.lock:
            state.candidates = probed
            state.passing = len(base_survivors)
            state.last_cycle_ok = False
        return False

    _log_ejections(base_survivors, ejected, capped, cfg)
    with state.lock:
        state.payload = payload
        state.generated_epoch = datetime.strptime(
            generated_at, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc).timestamp()
        state.candidates = probed
        state.passing = len(base_survivors)
        state.last_cycle_ok = True
        state.active_verdicts = len(verdicts)
        state.ejected = len(ejected)
        state.base_servers = list(base_survivors)
        state.base_generated_at = generated_at
    return True


def main():
    logging.basicConfig(
        level=os.environ.get("VPN_PICKER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    cfg = Config()
    if not cfg.api_key:
        log.error("AIRVPN_API_KEY is empty, nothing to do")
        return 1

    state = State()
    load_existing(state, cfg)

    if "--once" in sys.argv:
        return 0 if run_cycle(state, cfg) else 1

    serve(state, cfg)
    next_cycle = 0.0
    while True:
        now = time.monotonic()
        try:
            if now >= next_cycle:
                started = now
                next_cycle = started + cfg.interval_seconds
                run_cycle(state, cfg)
            else:
                refresh_ejections(state, cfg)
        except Exception:
            log.exception("scoring/ejection cycle crashed, staying up for the next one")
        until_cycle = max(0.0, next_cycle - time.monotonic())
        time.sleep(min(max(1, cfg.verdict_recheck_seconds), until_cycle))


if __name__ == "__main__":
    sys.exit(main())
