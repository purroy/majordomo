#!/usr/bin/env bash
# Interactive PA Telegram bot setup.
#   1) Prompt for the @BotFather token and store it in Keychain.
#   2) Ask you to message the bot, capture your chat_id.
#   3) Send a test message.
#   4) Load the launchd agent (bot auto-starts on login).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "== PA Telegram setup =="
echo

# 1. Token
if security find-generic-password -a "$USER" -s "PA-telegram-bot-token" -w >/dev/null 2>&1; then
  read -r -p "A token is already stored. Replace it? [y/N] " yn
  [[ "$yn" =~ ^[Yy]$ ]] && set_token=1 || set_token=0
else
  set_token=1
fi
if [ "$set_token" = 1 ]; then
  echo "1) Open Telegram -> chat with @BotFather -> /newbot and follow the steps."
  echo "   You will receive a token like 123456:ABC-XYZ_..."
  read -rsp "   Paste the token (hidden): " TOKEN; echo
  [ -z "$TOKEN" ] && { echo "Empty token. Aborting." >&2; exit 1; }
  security add-generic-password -U -a "$USER" -s "PA-telegram-bot-token" -w "$TOKEN"
  echo "   OK token stored (PA-telegram-bot-token)"
else
  TOKEN=$(security find-generic-password -a "$USER" -s "PA-telegram-bot-token" -w)
fi

# 2. Chat ID — poll getUpdates until we see the first message
echo
echo "2) Open the chat with YOUR bot on Telegram and send it ANY message (e.g. 'hi')."
echo "   Waiting for a message..."
attempts=0
CHAT_ID=""
while [ "$attempts" -lt 60 ] && [ -z "$CHAT_ID" ]; do
  resp=$(curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates?limit=1&offset=-1" || true)
  CHAT_ID=$(printf '%s' "$resp" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    res = d.get("result", [])
    if res:
        m = res[-1].get("message") or res[-1].get("edited_message") or {}
        c = m.get("chat", {}).get("id")
        if c is not None: print(c)
except Exception: pass
')
  [ -n "$CHAT_ID" ] && break
  sleep 2
  attempts=$((attempts + 1))
  printf '.'
done
echo
[ -z "$CHAT_ID" ] && { echo "No message received in 2 minutes. Retry." >&2; exit 1; }
echo "   OK chat_id detected: $CHAT_ID"
security add-generic-password -U -a "$USER" -s "PA-telegram-chat-id" -w "$CHAT_ID"
echo "   OK chat_id stored (PA-telegram-chat-id)"

# 3. Test message
echo
echo "3) Sending test message..."
bash "$REPO_DIR/scripts/telegram_send.sh" "PA is online. You will receive briefings here and can talk to me. Try: \`/ping\`."
echo "   OK test message sent"

# 4. launchd
echo
echo "4) Loading launchd agent..."
bash "$REPO_DIR/scripts/launchd/install.sh" telegram
echo
echo "Setup complete. Send /ping to the bot to verify the bridge."
