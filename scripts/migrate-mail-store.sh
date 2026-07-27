#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${CODEX_MAIL_HOME:-${HOME}/.codex-mail-workbench}"
SOURCE_STORE="${CODEX_MAIL_SOURCE_STORE:-}"
SOURCE_SYNC_STATE="${CODEX_MAIL_SOURCE_SYNC_STATE:-}"

usage() {
  cat <<'EOF'
Usage: migrate-mail-store --store <mail.sqlite> [--sync-state <dir>]

Environment:
  CODEX_MAIL_HOME               Target state directory.
  CODEX_MAIL_SOURCE_STORE       Source SQLite mail store path.
  CODEX_MAIL_SOURCE_SYNC_STATE  Optional source sync-state directory.
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
  echo "error: provide --store or CODEX_MAIL_SOURCE_STORE" >&2
  usage >&2
  exit 2
fi

if [[ ! -f "${SOURCE_STORE}" ]]; then
  echo "error: source store does not exist: ${SOURCE_STORE}" >&2
  exit 1
fi

mkdir -p "${STATE_DIR}"

install -m 600 "${ROOT_DIR}/config/accounts.example.toml" "${STATE_DIR}/accounts.toml"

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
