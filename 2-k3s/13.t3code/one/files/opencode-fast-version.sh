#!/bin/sh
# T3 runs `opencode --version` with a 4 s limit on every provider refresh. The
# real binary needs about 3 s to boot its runtime before printing, which tips
# over under any load and marks OpenCode unavailable. Answer from package.json
# and forward everything else to the real binary, which the package's
# postinstall copied into bin/opencode.exe for this CPU.
if [ "$#" = 1 ] && [ "$1" = "--version" ]; then
  grep -o '"version": *"[^"]*"' /tools/node_modules/@opencode/cli/package.json | head -1 | cut -d'"' -f4
  exit 0
fi
exec /tools/node_modules/@opencode/cli/bin/opencode.exe "$@"
