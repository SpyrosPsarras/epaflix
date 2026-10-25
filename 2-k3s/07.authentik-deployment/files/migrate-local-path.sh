#!/bin/sh
set -eu
old=${1:?usage: migrate-local-path.sh <source> <target>}
target=${2:?usage: migrate-local-path.sh <source> <target>}
marker="$target/.migrated-from-local-path"
if [ -e "$marker" ]; then
  echo "already migrated: $(cat "$marker")"
  exit 0
fi
lock="$target/.migrate-lock"
mkdir "$lock" 2>/dev/null || { echo "another migration holds $lock" >&2; exit 1; }
trap 'rmdir "$lock"' EXIT
if [ -e "$marker" ]; then
  echo "already migrated: $(cat "$marker")"
  exit 0
fi
existing=$(find "$target" -mindepth 1 -maxdepth 1 ! -name lost+found ! -name .migrate-lock) || exit 1
if [ -n "$existing" ]; then
  echo "refusing to migrate: $target has data but no $marker" >&2
  exit 1
fi
cp -a "$old/." "$target/"
chown "$(stat -c %u:%g "$old")" "$target"
chmod "$(stat -c %a "$old")" "$target"
manifest() {
  cd "$1" || exit 1
  other=$(find . ! -type f ! -path ./lost+found ! -path './lost+found/*' ! -path ./.migrate-lock ! -path './.migrate-lock/*' -exec stat -c '%n %F %u:%g %a' {} +) || exit 1
  files=$(find . -type f ! -path './lost+found/*' ! -path ./.migrate-lock ! -path './.migrate-lock/*' -exec stat -c '%n %F %u:%g %a %s' {} +) || exit 1
  printf '%s\n%s\n' "$other" "$files" | sort
}
old_manifest=$(manifest "$old") || exit 1
new_manifest=$(manifest "$target") || exit 1
count=$(printf '%s\n' "$old_manifest" | wc -l)
echo "entries old=$count new=$(printf '%s\n' "$new_manifest" | wc -l)"
if [ "$old_manifest" != "$new_manifest" ]; then
  echo "copy check failed; $target left without marker" >&2
  exit 1
fi
sync
date -u +%Y-%m-%dT%H:%M:%SZ > "$marker"
sync
echo "migrated $count entries from $old to $target"
