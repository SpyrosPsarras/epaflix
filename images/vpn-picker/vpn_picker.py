#!/usr/bin/env python3
"""vpn-picker scorer - Components 1-3 of the vpn-picker design.

Spec: docs/superpowers/specs/2026-08-01-vpn-picker-design.md

Every cycle: read the AirVPN status API, shortlist by hard filters, probe the
shortlist by ICMP from outside any VPN netns, publish the ranked survivors.

Stdlib only on purpose. The whole job is one HTTPS GET, five `ping` runs and a
JSON file, and adding a dependency would mean a pip layer and a supply chain to
watch for something `urllib` already does.
"""

import json
import logging
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

# Contract version. A breaking change ships as a new filename (ranking.v2.json)
# so the non-GitOps consumer on Nick's VM can lag safely - see Component 3.
SCHEMA = 1


def _env_int(name, default):
    return int(os.environ.get(name, default))


def _env_float(name, default):
    return float(os.environ.get(name, default))


class Config:
    """Every knob the spec names, no magic numbers buried in the logic."""

    def __init__(self):
        self.api_url = os.environ.get("VPN_PICKER_API_URL", "https://airvpn.org/api/")
        self.api_key = os.environ.get("AIRVPN_API_KEY", "")
        # Must match `airwhitecountrylist` in bluetit-config.yaml. A winner
        # outside the pool is refused on connect, not silently ignored.
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
        self.listen_port = _env_int("VPN_PICKER_LISTEN_PORT", 8080)
        self.api_timeout = _env_int("VPN_PICKER_API_TIMEOUT", 30)


# --------------------------------------------------------------------------
# Component 2 - the scoring rule. Pure functions, so the fixture test in
# selftest.py exercises the real ranking code and not a copy of it.
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# API + probe
# --------------------------------------------------------------------------


def fetch_servers(cfg):
    """GET the AirVPN status API. Raises on anything that is not usable JSON."""
    query = urllib.parse.urlencode({"key": cfg.api_key, "service": "status", "format": "json"})
    with urllib.request.urlopen(f"{cfg.api_url}?{query}", timeout=cfg.api_timeout) as resp:
        payload = json.load(resp)
    servers = payload.get("servers")
    if not servers:
        raise ValueError("status API returned no servers")
    return servers


# iputils prints exact counts; the "N% packet loss" field it also prints is
# rounded to a whole percent, which cannot express the 1% gate. Parse the
# counts and do the division ourselves.
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
    # 100% loss prints no rtt line at all. Such a candidate is rejected by the
    # gate anyway, so a sentinel RTT is enough to keep it sortable.
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
    # A `ping -i` below 0.2 s needs root or CAP_NET_RAW. The default rate of
    # 5/s sits exactly on that line; raising VPN_PICKER_PROBE_RATE past 5
    # only works because the container runs as uid 0.
    expected = cfg.probe_count * interval
    result = subprocess.run(
        ["ping", "-n", "-q", "-c", str(cfg.probe_count), "-i", f"{interval:g}", "-W", "2", ip],
        capture_output=True,
        text=True,
        timeout=expected + 60,
    )
    # `ping` exits 1 on total loss and still prints the summary, so the return
    # code alone is not a failure signal. Only an unparseable body is.
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


# --------------------------------------------------------------------------
# Component 3 - the published ranking, and the atomic publish
# --------------------------------------------------------------------------


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
        # A failed publish must not leave a stray temp file behind for the next
        # reader to trip over.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# State + HTTP
# --------------------------------------------------------------------------


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

    def snapshot(self):
        with self.lock:
            return (
                self.payload,
                self.generated_epoch,
                list(self.candidates),
                self.passing,
                self.last_cycle_ok,
            )


def load_existing(state, cfg):
    """Adopt a ranking left on disk by a previous pod.

    Without this, a restart during an API outage serves nothing at all until
    the API recovers, even though a perfectly good in-TTL ranking is sitting on
    the PVC. Its original `generated_at` is kept, so a stale file stays stale.
    """
    try:
        with open(cfg.output_path, "rb") as fh:
            payload = fh.read()
        doc = json.loads(payload)
        stamp = datetime.strptime(doc["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except FileNotFoundError:
        return
    except Exception as exc:
        log.warning("existing ranking at %s is unusable: %s", cfg.output_path, exc)
        return
    with state.lock:
        state.payload = payload
        state.generated_epoch = stamp.timestamp()
        state.passing = len(doc.get("servers", []))
    log.info(
        "adopted existing ranking generated_at=%s servers=%s",
        doc["generated_at"], state.passing,
    )


def render_metrics(state):
    _, generated_epoch, candidates, passing, ok = state.snapshot()
    lines = [
        "# HELP vpn_picker_scrape_success Whether the last scoring cycle completed.",
        "# TYPE vpn_picker_scrape_success gauge",
        f"vpn_picker_scrape_success {1 if ok else 0}",
        "# HELP vpn_picker_ranking_generated_timestamp_seconds Unix time the served ranking was generated.",
        "# TYPE vpn_picker_ranking_generated_timestamp_seconds gauge",
        f"vpn_picker_ranking_generated_timestamp_seconds {generated_epoch:.0f}",
        "# HELP vpn_picker_candidates_passing_gate Shortlisted candidates that passed the loss gate.",
        "# TYPE vpn_picker_candidates_passing_gate gauge",
        f"vpn_picker_candidates_passing_gate {passing}",
    ]
    # Every probed candidate, not only the survivors - the rejected one at 22%
    # loss is the interesting series.
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


# --------------------------------------------------------------------------
# Cycle
# --------------------------------------------------------------------------


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

    survivors = rank_survivors(probed, cfg.max_loss_pct)
    for c in probed:
        if c["loss_pct"] > cfg.max_loss_pct:
            log.warning(
                "rejected server=%s reason=loss_gate loss_pct=%s ceiling=%s",
                c["name"], c["loss_pct"], cfg.max_loss_pct,
            )
    if not survivors:
        log.error(
            "every candidate failed the %s%% loss gate, keeping the last good ranking",
            cfg.max_loss_pct,
        )
        with state.lock:
            state.candidates = probed
            state.passing = 0
            state.last_cycle_ok = False
        return False

    log.info(
        "winner server=%s loss_pct=%s rtt_ms=%s headroom=%s runners_up=%s",
        survivors[0]["name"], survivors[0]["loss_pct"], survivors[0]["rtt_ms"],
        survivors[0]["headroom"], [s["name"] for s in survivors[1:]],
    )

    payload = build_document(survivors, cfg)
    try:
        publish(payload, cfg.output_path)
    except Exception as exc:
        # The HTTP copy is still worth serving even when the PVC is unwritable,
        # so this is logged and survived, not fatal.
        log.error("publish to %s failed: %s", cfg.output_path, exc)

    with state.lock:
        state.payload = payload
        state.generated_epoch = time.time()
        state.candidates = probed
        state.passing = len(survivors)
        state.last_cycle_ok = True
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
    while True:
        started = time.monotonic()
        try:
            run_cycle(state, cfg)
        except Exception:
            log.exception("scoring cycle crashed, staying up for the next one")
        time.sleep(max(0.0, cfg.interval_seconds - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
