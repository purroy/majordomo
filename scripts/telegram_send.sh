#!/usr/bin/env bash
# Envía un mensaje al chat privado del PA en Telegram.
# Uso: telegram_send.sh "Texto" [parse_mode]
#   parse_mode: Markdown (default), MarkdownV2, HTML, "" (sin formato)
#
# Lee el texto del primer argumento, o de stdin si el argumento es "-".
set -euo pipefail

text="${1:-}"
mode="${2:-Markdown}"
[ -z "$text" ] && { echo "usage: $0 <text|-> [parse_mode]" >&2; exit 2; }
if [ "$text" = "-" ]; then
  text="$(cat)"
fi

# Carga .env del repo si existe (Linux/systemd); env vars ya exportadas ganan.
_REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$_REPO_DIR/.env" ]; then
  set -a; . "$_REPO_DIR/.env"; set +a
fi

token="${PA_TELEGRAM_BOT_TOKEN:-}"
chat="${PA_TELEGRAM_CHAT_ID:-}"
if [ -z "$token" ] && command -v security >/dev/null 2>&1; then
  token=$(security find-generic-password -a "$USER" -s "PA-telegram-bot-token" -w 2>/dev/null || true)
fi
if [ -z "$chat" ] && command -v security >/dev/null 2>&1; then
  chat=$(security find-generic-password -a "$USER" -s "PA-telegram-chat-id" -w 2>/dev/null || true)
fi
[ -n "$token" ] || { echo "ERROR: PA_TELEGRAM_BOT_TOKEN missing (.env o Keychain)" >&2; exit 1; }
[ -n "$chat" ]  || { echo "ERROR: PA_TELEGRAM_CHAT_ID missing (.env o Keychain)"  >&2; exit 1; }

PA_TG_TOKEN="$token" PA_TG_CHAT="$chat" PA_TG_MODE="$mode" PA_TG_TEXT="$text" \
  python3 - <<'PY'
import json, os, sys, urllib.parse, urllib.request
token = os.environ["PA_TG_TOKEN"]
chat  = os.environ["PA_TG_CHAT"]
mode  = os.environ["PA_TG_MODE"]
text  = os.environ["PA_TG_TEXT"].rstrip("\n")
CHUNK = 4000
chunks = [text[i:i+CHUNK] for i in range(0, len(text), CHUNK)] or [""]
for ch in chunks:
    data = {"chat_id": chat, "text": ch, "disable_web_page_preview": "true"}
    if mode:
        data["parse_mode"] = mode
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.load(r)
        if not resp.get("ok"):
            sys.stderr.write(f"telegram_send.sh: {resp}\n"); sys.exit(1)
    except Exception as e:
        sys.stderr.write(f"telegram_send.sh: {e}\n"); sys.exit(1)
PY
