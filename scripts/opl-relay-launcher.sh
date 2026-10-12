#!/usr/bin/env bash
# Stable host entry point for an already-installed Codex Plugin carrier.
# This resolves installed bytes; it never installs or updates a package.
set -euo pipefail

base="${OPL_RELAY_CARRIER_ROOT:-${CODEX_HOME:-$HOME/.codex}/plugins/cache/opl-relay/opl-relay}"
relay_python="${OPL_RELAY_PYTHON:-$HOME/.local/bin/python3}"

if [ ! -d "$base" ]; then
  echo "opl-relay: installed carrier directory is unavailable" >&2
  exit 127
fi

carrier="$(
  ls -1 "$base" \
    | sed -n '/^[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*$/p' \
    | sort -t. -k1,1n -k2,2n -k3,3n \
    | tail -n1
)"

if [ -z "${carrier:-}" ] || [ ! -x "$base/$carrier/bin/opl-relay" ]; then
  echo "opl-relay: no installed carrier executable found" >&2
  exit 127
fi

# Pin the supported, Keychain-authorized interpreter across terminal and launchd.
# A minimal daemon PATH must not select an older system Python implicitly.
if [ ! -x "$relay_python" ]; then
  echo "opl-relay: configured Python runtime is unavailable" >&2
  exit 127
fi
exec "$relay_python" "$base/$carrier/bin/opl-relay" "$@"
