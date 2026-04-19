#!/usr/bin/env bash
# Read/write PA secrets in the macOS Keychain.
# Service prefix: PA-<name>  (e.g. PA-mail-<account>-user, PA-telegram-bot-token)
set -euo pipefail

pa_kc_get() {
  local name="$1"
  security find-generic-password -a "$USER" -s "PA-${name}" -w 2>/dev/null
}

pa_kc_set() {
  local name="$1" value="$2"
  security add-generic-password -U -a "$USER" -s "PA-${name}" -w "${value}"
}

pa_kc_has() {
  local name="$1"
  security find-generic-password -a "$USER" -s "PA-${name}" >/dev/null 2>&1
}

# CLI usage: scripts/keychain.sh {get|set|has} <name> [value]  (e.g. mail-example-user)
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  cmd="${1:-}"; name="${2:-}"; value="${3:-}"
  case "$cmd" in
    get)  pa_kc_get "$name" ;;
    set)  pa_kc_set "$name" "$value" ;;
    has)  pa_kc_has "$name" && echo "yes" || echo "no" ;;
    *)    echo "usage: $0 {get|set|has} <name> [value]" >&2; exit 2 ;;
  esac
fi
