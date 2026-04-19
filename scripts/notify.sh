#!/usr/bin/env bash
# Notificación portable. Uso: notify.sh "Título" "Mensaje"
#   - macOS: display notification via osascript.
#   - Linux: log a stderr (Telegram ya se usa como canal push cross-plataforma).
set -euo pipefail
title="${1:-PA}"
msg="${2:-}"
if command -v osascript >/dev/null 2>&1; then
  msg_escaped="${msg//\"/\\\"}"
  title_escaped="${title//\"/\\\"}"
  osascript -e "display notification \"${msg_escaped}\" with title \"${title_escaped}\""
else
  echo "[notify] $title — $msg" >&2
fi
