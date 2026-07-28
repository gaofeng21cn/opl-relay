#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${OPL_PROFILE_WORKSPACE:-}" ]]; then
  STATE_DIR="${OPL_PROFILE_WORKSPACE}/data/relay"
else
  STATE_DIR="${HOME}/OPL/profiles/${USER}/data/relay"
fi
SOURCE_STORE="${OPL_RELAY_SOURCE_STORE:-}"
SOURCE_SYNC_STATE="${OPL_RELAY_SOURCE_SYNC_STATE:-}"

usage() {
  cat <<'EOF'
Usage: migrate-mail-store --store <mail.sqlite> [--sync-state <dir>]

Environment:
  OPL_PROFILE_WORKSPACE         Shared Profile Workspace; target is data/relay.
  OPL_RELAY_SOURCE_STORE        Source SQLite mail store path.
  OPL_RELAY_SOURCE_SYNC_STATE   Optional source sync-state directory.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --store)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "error: --store requires a path" >&2
        usage >&2
        exit 2
      fi
      SOURCE_STORE="$2"
      shift 2
      ;;
    --sync-state)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "error: --sync-state requires a directory" >&2
        usage >&2
        exit 2
      fi
      SOURCE_SYNC_STATE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "${SOURCE_STORE}" ]]; then
  echo "error: provide --store or OPL_RELAY_SOURCE_STORE" >&2
  usage >&2
  exit 2
fi

if [[ ! -f "${SOURCE_STORE}" ]]; then
  echo "error: source store does not exist: ${SOURCE_STORE}" >&2
  exit 1
fi

mkdir -p "${STATE_DIR}"

if [[ ! -e "${STATE_DIR}/accounts.toml" ]]; then
  install -m 600 "${ROOT_DIR}/config/accounts.example.toml" "${STATE_DIR}/accounts.toml"
fi

install -m 600 "${SOURCE_STORE}" "${STATE_DIR}/mail.sqlite"
for suffix in -wal -shm; do
  if [[ -f "${SOURCE_STORE}${suffix}" ]]; then
    install -m 600 "${SOURCE_STORE}${suffix}" "${STATE_DIR}/mail.sqlite${suffix}"
  fi
done

if [[ -n "${SOURCE_SYNC_STATE}" ]]; then
  if [[ ! -d "${SOURCE_SYNC_STATE}" ]]; then
    echo "error: source sync-state directory does not exist: ${SOURCE_SYNC_STATE}" >&2
    exit 1
  fi
  mkdir -p "${STATE_DIR}/sync-state"
  shopt -s nullglob
  sync_state_files=("${SOURCE_SYNC_STATE}"/*.json)
  if [[ ${#sync_state_files[@]} -gt 0 ]]; then
    cp -p "${sync_state_files[@]}" "${STATE_DIR}/sync-state/"
  fi
fi

echo "[OK] migrated mail config/store to ${STATE_DIR}"
