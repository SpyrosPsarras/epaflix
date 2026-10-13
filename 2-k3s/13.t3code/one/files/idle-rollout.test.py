#!/usr/bin/env python3
import os
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP_DIR = tempfile.TemporaryDirectory()
TMP = Path(TMP_DIR.name)

FAKE_KUBECTL = r"""#!/usr/bin/env bash
args="$*"
case "$args" in
  "get statefulset"*) printf %s "$FAKE_WANT" ;;
  *controller-revision-hash*) printf %s "$FAKE_HAVE" ;;
  *waiting.reason*) printf %s "$FAKE_WAITING" ;;
  *Ready*) printf %s "$FAKE_READY" ;;
  *metadata.uid*)
    uid=pod-1; deleting=
    if [[ -e $FAKE_LOG.exec ]]; then
      case "$FAKE_POD_AFTER_EXEC" in
        gone) [[ $args == *--ignore-not-found* ]] && exit 0; echo 'Error from server (NotFound): pods "t3code-0" not found' >&2; exit 1 ;;
        apierror) echo 'Error from server (Forbidden)' >&2; exit 1 ;;
        terminating) deleting=2026-10-09T12:00:00Z ;;
        replaced) uid=pod-2 ;;
      esac
    fi
    printf %s "$uid"
    [[ $args == *deletionTimestamp* ]] && printf ' %s' "$deleting"
    exit 0 ;;
  exec*) cat >/dev/null
    c=$(sed -E 's/.* -c ([a-z0-9]+) .*/\1/' <<<"$args")
    v=FAKE_IDLE_$c; [[ ${!v} == fail ]] && touch "$FAKE_LOG.exec" && exit "$FAKE_EXEC_CODE"; printf '%s\n' "${!v}" ;;
  delete*) echo "$args" >>"$FAKE_LOG" ;;
  *) echo "unexpected kubectl $args" >&2; exit 2 ;;
esac
"""


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_db(path, message=None, item=None, requested=None, completed=None):
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE orchestration_v2_projection_messages (updated_at TEXT)")
        c.execute("CREATE TABLE orchestration_v2_projection_turn_items (updated_at TEXT)")
        c.execute("CREATE TABLE orchestration_v2_projection_runs (requested_at TEXT, completed_at TEXT)")
        if message:
            c.execute("INSERT INTO orchestration_v2_projection_messages VALUES (?)", (iso(message),))
        if item:
            c.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES (?)", (iso(item),))
        if requested:
            c.execute("INSERT INTO orchestration_v2_projection_runs VALUES (?, ?)",
                      (iso(requested), iso(completed) if completed else None))


def idle(db, ttys=()):
    tty_dir = Path(tempfile.mkdtemp(dir=TMP))
    for i, atime in enumerate(ttys):
        p = tty_dir / str(i)
        p.touch()
        os.utime(p, (atime, time.time()))
    env = {**os.environ, "T3_STATE_DB": str(db), "T3_IDLE_TTYS": f"{tty_dir}/[0-9]*"}
    return subprocess.run(["python3", HERE / "t3-idle.py"], env=env, capture_output=True, text=True)


def near(out, expected):
    assert out.returncode == 0, out.stderr
    assert abs(int(out.stdout) - expected) <= 2, (out.stdout, expected)


now = time.time()
db = TMP / "state.sqlite"
make_db(db, message=now - 4000, item=now - 600, requested=now - 9000, completed=now - 5000)
near(idle(db), 600)
near(idle(db, ttys=[now - 9000, now - 120]), 120)

db2 = TMP / "state2.sqlite"
make_db(db2, requested=now - 3000, completed=now - 2500)
near(idle(db2, ttys=[now - 9000]), 2500)

db3 = TMP / "empty.sqlite"
make_db(db3)
out = idle(db3)
assert out.returncode == 0 and int(out.stdout) > 10**9, out

assert idle(TMP / "missing.sqlite").returncode != 0

bin_dir = TMP / "bin"
bin_dir.mkdir()
(bin_dir / "kubectl").write_text(FAKE_KUBECTL)
(bin_dir / "kubectl").chmod(0o755)


def rollout(**fake):
    log = TMP / f"delete-{time.monotonic_ns()}.log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LOG": str(log),
           "FAKE_WANT": "rev2", "FAKE_HAVE": "rev1", "FAKE_WAITING": "", "FAKE_READY": "True",
           "FAKE_IDLE_t3": "2000", "FAKE_IDLE_sshd": "2000",
           "FAKE_POD_AFTER_EXEC": "same", "FAKE_EXEC_CODE": "1"}
    env.update(fake)
    out = subprocess.run(["bash", HERE / "idle-rollout.sh"], env=env, capture_output=True, text=True)
    return out, log.exists()


cases = [
    ("up to date", {"FAKE_HAVE": "rev2"}, 0, False),
    ("no update revision yet", {"FAKE_WANT": ""}, 0, False),
    ("pending and idle", {}, 0, True),
    ("exactly 30 minutes", {"FAKE_IDLE_t3": "1800", "FAKE_IDLE_sshd": "1800"}, 0, True),
    ("agent or app active", {"FAKE_IDLE_t3": "100"}, 0, False),
    ("ssh shell active", {"FAKE_IDLE_sshd": "1799"}, 0, False),
    ("garbage idle output", {"FAKE_IDLE_t3": "soon"}, 0, False),
    ("starting up", {"FAKE_READY": "False"}, 0, False),
    ("crash looping", {"FAKE_READY": "False", "FAKE_WAITING": "CrashLoopBackOff",
                       "FAKE_IDLE_t3": "fail", "FAKE_IDLE_sshd": "fail"}, 0, True),
    ("exec fails", {"FAKE_IDLE_sshd": "fail"}, 1, False),
]
for name, fake, code, deleted in cases:
    out, did_delete = rollout(**fake)
    assert out.returncode == code, (name, out.returncode, out.stdout, out.stderr)
    assert did_delete == deleted, (name, out.stdout, out.stderr)

for container in ("t3", "sshd"):
    for state in ("terminating", "gone", "replaced"):
        out, did_delete = rollout(**{f"FAKE_IDLE_{container}": "fail", "FAKE_POD_AFTER_EXEC": state,
                                    "FAKE_EXEC_CODE": "137"})
        assert out.returncode == 0 and not did_delete and "went away" in out.stdout, (container, state, out.stdout, out.stderr)
    out, did_delete = rollout(**{f"FAKE_IDLE_{container}": "fail", "FAKE_POD_AFTER_EXEC": "apierror",
                                "FAKE_EXEC_CODE": "137"})
    assert out.returncode == 137 and not did_delete and "went away" not in out.stdout, (container, "apierror", out.returncode, out.stdout, out.stderr)

print("idle-rollout tests passed")
