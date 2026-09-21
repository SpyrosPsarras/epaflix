#!/usr/bin/env python3
"""Run on spylinux. Emit JSONL evidence and restore Wi-Fi after ten cycles."""

import concurrent.futures
import datetime
import ipaddress
import json
import re
import signal
import subprocess
import sys
import time


def adb(command):
    args = ["docker", "exec", "adbbox", "adb", "-s", "38111FDJH009XY", "shell", command]
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "ADB probe timed out")


def emit(**fields):
    print(json.dumps({"time": datetime.datetime.now(datetime.timezone.utc).isoformat(), **fields}), flush=True)


def address(output):
    match = re.search(r"^PING\s+\S+\s+\(([^)]+)\)", output)
    return match.group(1) if match else None


def snapshot(cycle, wifi, started):
    nonce = f"ts-roam-{time.time_ns()}.epaflix.com"
    commands = {
        "internet": "ping -c1 -W2 1.1.1.1",
        "public_dns": f"ping -c1 -W1 {nonce}",
        "blocked_dns": "ping -c1 -W1 doubleclick.net",
        "private_dns": "ping -c1 -W1 pihole.epaflix.com",
        "private_http": "printf 'GET /api/info/version HTTP/1.0\\r\\nHost: pihole.epaflix.com\\r\\n\\r\\n' | nc -w 3 192.168.10.30 80",
        "vpn": "dumpsys connectivity",
        "wifi": "cmd wifi status",
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(commands)) as pool:
        futures = {name: pool.submit(adb, command) for name, command in commands.items()}
        results = {name: future.result() for name, future in futures.items()}
    resolved = address(results["public_dns"].stdout)
    blocked = address(results["blocked_dns"].stdout)
    private = address(results["private_dns"].stdout)
    networks = [line for line in results["vpn"].stdout.splitlines()
                if line.lstrip().startswith("NetworkAgentInfo{")]
    vpn = next((line for line in networks if "VPN CONNECTED extra: VPN:com.tailscale.ipn" in line), "")
    underlay = "WIFI" if wifi else "CELLULAR"
    checks = {
        "internet": results["internet"].returncode == 0,
        "public_dns": bool(resolved and ipaddress.ip_address(resolved).is_global),
        "blocked_dns": blocked in {"0.0.0.0", "127.0.0.1"},
        "private_dns": private == "192.168.10.30",
        "private_http": '"version"' in results["private_http"].stdout,
        "tailscale_vpn": bool(vpn),
        "all_apps": "Uids: <{0-99999}>" in vpn,
        "underlay": f"Transports: {underlay}|VPN" in vpn and "&VALIDATED&" in vpn,
        "wifi_state": f"Wifi is {'enabled' if wifi else 'disabled'}" in results["wifi"].stdout,
    }
    emit(cycle=cycle, wifi=wifi, elapsed=round(time.monotonic() - started, 2),
         probe_domain=nonce, public_address=resolved, blocked_address=blocked,
         private_address=private, underlay=underlay, checks=checks)
    return all(checks.values())


def recover(cycle, wifi):
    started = time.monotonic()
    while time.monotonic() - started < 30:
        probe_started = time.monotonic()
        if snapshot(cycle, wifi, started):
            return time.monotonic() - started <= 30
        time.sleep(max(0, 5 - (time.monotonic() - probe_started)))
    return False


def interrupted(signum, frame):
    raise InterruptedError(f"signal {signum}")


def main():
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    initial = adb("cmd wifi status")
    if initial.returncode or "Wifi is enabled" not in initial.stdout:
        raise RuntimeError("Start with Wi-Fi enabled; the test restores that state.")
    if not recover(0, True):
        raise RuntimeError("Tailscale baseline failed; Wi-Fi was not changed.")
    try:
        for cycle in range(1, 11):
            for wifi in (False, True):
                result = adb(f"svc wifi {'enable' if wifi else 'disable'}")
                if result.returncode:
                    raise RuntimeError(f"Wi-Fi toggle failed: {result.stderr.strip()}")
                if not recover(cycle, wifi):
                    raise RuntimeError(f"cycle {cycle}, wifi={wifi}: recovery exceeded 30 seconds")
                # Verify the connection remains usable after its first success.
                time.sleep(5)
                if not snapshot(cycle, wifi, time.monotonic()):
                    raise RuntimeError(f"cycle {cycle}, wifi={wifi}: connection did not stay usable")
    finally:
        restored = adb("svc wifi enable")
        emit(event="wifi_restore", command_succeeded=restored.returncode == 0)
        if restored.returncode:
            raise RuntimeError("Could not restore Wi-Fi")
    if not recover(11, True):
        raise RuntimeError("Final Wi-Fi recovery failed")
    emit(result="PASS", cycles=10, transitions=20)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, InterruptedError, subprocess.TimeoutExpired) as error:
        emit(result="FAIL", error=str(error))
        sys.exit(1)
