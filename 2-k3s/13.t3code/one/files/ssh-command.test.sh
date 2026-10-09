#!/usr/bin/env bash
set -euo pipefail

wrapper=$(cd "$(dirname "$0")" && pwd)/ssh-command.sh
sandbox=$(mktemp -d)
trap 'rm -rf "$sandbox"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
run() { HOME="$sandbox/home" SHELL=/bin/bash SSH_ORIGINAL_COMMAND="$1" "$wrapper"; }
check_launch_home() {
  [[ $1 == "$sandbox/home/.t3-ssh-launch" && -d $1 ]] || fail 'launch HOME is not stable and isolated'
  local mode
  mode=$(stat -c %a "$1")
  [[ $mode == 700 ]] || fail 'launch HOME is not private'
  [[ -d $1/.t3/userdata ]] || fail 'userdata is missing'
  [[ -L $1/.t3/runtime ]] || fail 'runtime is not a symlink'
  [[ $(readlink "$1/.t3/runtime") == "$sandbox/home/.t3/runtime" ]] || fail 'runtime points to the wrong HOME'
}

mkdir -p "$sandbox/home/.t3/userdata" "$sandbox/home/.t3/runtime"
printf '%s\n' '{"pid":123,"port":3773,"origin":"http://127.0.0.1:3773"}' >"$sandbox/home/.t3/userdata/server-runtime.json"
printf '%s\n' live-data >"$sandbox/home/.t3/userdata/database"
chmod 2777 "$sandbox/home"
cp -a "$sandbox/home/.t3" "$sandbox/before"

launch_home=$(run 'sh -l -s -- 0123456789abcdef' <<'PROBE'
printf '%s\n' "$HOME"
[ "$1" = 0123456789abcdef ] || exit 1
PROBE
)
check_launch_home "$launch_home"
[[ -f $launch_home/.t3/userdata/server-runtime.json && ! -L $launch_home/.t3/userdata/server-runtime.json ]] || fail 'runtime state is not a copy'
cmp "$sandbox/home/.t3/userdata/server-runtime.json" "$launch_home/.t3/userdata/server-runtime.json"
[[ ! -e $launch_home/.t3/userdata/database ]] || fail 'live data leaked into launch HOME'
printf '%s\n' fallback-state >"$launch_home/.t3/userdata/server-runtime.json"
printf '%s\n' fallback-data >"$launch_home/.t3/userdata/database"
diff -r "$sandbox/before" "$sandbox/home/.t3"
echo 'PASS: launch gets private HOME, copied runtime state and shared runtime; live .t3 stays untouched'

first_launch_home=$launch_home
chmod 0777 "$launch_home"
rm "$launch_home/.t3/runtime"
ln -s "$sandbox/wrong-runtime" "$launch_home/.t3/runtime"
launch_home=$(run 'sh -l -s -- abcdef0123456789' <<'PROBE'
printf '%s\n' "$HOME"
PROBE
)
check_launch_home "$launch_home"
[[ $launch_home == "$first_launch_home" ]] || fail 'launch HOME changed between launches'
cmp "$sandbox/home/.t3/userdata/server-runtime.json" "$launch_home/.t3/userdata/server-runtime.json"
diff -r "$sandbox/before" "$sandbox/home/.t3"
echo 'PASS: repeated launch reuses HOME, replaces stale runtime state and fixes permissions and runtime symlink'

rm "$sandbox/home/.t3/userdata/server-runtime.json"
rm "$sandbox/before/userdata/server-runtime.json"
launch_home=$(run 'sh -l -s -- abcdef0123456789' <<'PROBE'
printf '%s\n' "$HOME"
PROBE
)
check_launch_home "$launch_home"
[[ ! -e $launch_home/.t3/userdata/server-runtime.json ]] || fail 'missing runtime state was copied'
diff -r "$sandbox/before" "$sandbox/home/.t3"
echo 'PASS: missing real runtime state removes the stale launch copy'

mv "$launch_home" "$sandbox/saved-launch-home"
ln -s "$sandbox/home" "$launch_home"
status=0
run 'sh -l -s -- 0123456789abcdef' <<<'exit 0' || status=$?
[[ $status != 0 ]] || fail 'symlinked launch HOME was accepted'
[[ $(stat -c %a "$sandbox/home") == 2777 ]] || fail 'symlink target permissions changed'
diff -r "$sandbox/before" "$sandbox/home/.t3"
rm "$launch_home"
mv "$sandbox/saved-launch-home" "$launch_home"
echo 'PASS: symlinked launch HOME is refused without changing real HOME'

[[ $(run 'sh -s' <<'PROBE'
printf '%s\n' "$HOME"
PROBE
) == "$sandbox/home" ]] || fail 'sh -s changed HOME'
[[ $(run "printf '%s\\n' \"\$HOME\"") == "$sandbox/home" ]] || fail 'ordinary command changed HOME'
[[ $(run 'cat' <<<stdin-probe) == stdin-probe ]] || fail 'ordinary command lost stdin'
[[ $(run '' <<'PROBE'
printf '%s\n' "$HOME"
case "$0" in -*) ;; *) exit 1 ;; esac
PROBE
) == "$sandbox/home" ]] || fail 'interactive session is not a login shell with real HOME'
status=0
run 'sh -s' <<<'exit 37' || status=$?
[[ $status == 37 ]] || fail 'sh -s lost exit status'
status=0
run 'exit 42' || status=$?
[[ $status == 42 ]] || fail 'ordinary command lost exit status'
status=0
launch_home=$(run 'sh -l -s -- 0123456789abcdef' <<'PROBE'
printf '%s\n' "$HOME"
exit 23
PROBE
) || status=$?
[[ $status == 23 ]] || fail 'launch lost exit status'
check_launch_home "$launch_home"
echo 'PASS: login shell, ordinary commands and sh -s preserve HOME, stdin and exit status'

for command in \
  'sh -l -s -- abc; printf "near-miss\\n"' \
  'sh -l -s -- 0123456789abcdeg' \
  'sh -l -s -- 0123456789ABCDEf' \
  'sh -l -s -- 0123456789abcde' \
  'sh -l -s -- 0123456789abcdef0' \
  'sh -l -s -- 0123456789abcdef extra' \
  'sh  -l -s -- 0123456789abcdef'; do
  output=$(run "$command" <<'PROBE'
printf '%s\n' "$HOME"
PROBE
)
  [[ ${output%%$'\n'*} == "$sandbox/home" ]] || fail "near-miss launch was isolated: $command"
done
diff -r "$sandbox/before" "$sandbox/home/.t3"
echo 'PASS: near-miss commands keep real HOME; live .t3 remains byte-identical'
