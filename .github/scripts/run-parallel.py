#!/usr/bin/env python3
"""Run each stdin line as a shell command, in parallel, and fail if any fails."""
import contextlib
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

TIMEOUT = int(os.environ.get("RUN_PARALLEL_TIMEOUT", 300))


def run(cmd):
    start = time.monotonic()
    with subprocess.Popen(cmd, shell=True, executable="/bin/bash", stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                          start_new_session=True) as proc:
        try:
            output, _ = proc.communicate(timeout=TIMEOUT)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            output = proc.communicate()[0] + f"\nkilled after {TIMEOUT}s"
            rc = "timeout"
    return cmd, rc, time.monotonic() - start, output.rstrip("\n")


cmds = [line.strip() for line in sys.stdin if line.strip() and not line.lstrip().startswith("#")]
if not cmds:
    sys.exit("::error::no commands to run")
failed = []
with ThreadPoolExecutor(os.cpu_count()) as pool:
    for future in as_completed([pool.submit(run, cmd) for cmd in cmds]):
        cmd, rc, seconds, output = future.result()
        if rc == 0:
            print(f"::group::ok {seconds:.1f}s {cmd}", output, "::endgroup::", sep="\n", flush=True)
        else:
            failed.append(cmd)
            print(f"::error::exit {rc} after {seconds:.1f}s: {cmd}", output, sep="\n", flush=True)
print(f"{len(cmds) - len(failed)}/{len(cmds)} passed")
sys.exit(1 if failed else 0)
