#!/usr/bin/env bash
# Wrapper cron / scheduled triggers call to generate a briefing.
# Usage: run_briefing.sh {morning|evening}
#
# Runs Claude Code non-interactively (-p), executes the slash command,
# writes the output to briefings/YYYY-MM-DD-{kind}.md, and fires a macOS
# notification (and optionally a Telegram push).
set -euo pipefail

KIND="${1:-morning}"
case "$KIND" in
  morning|evening) ;;
  *) echo "usage: $0 {morning|evening}" >&2; exit 2 ;;
esac

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATE="$(date +%F)"
OUT_DIR="$REPO_DIR/briefings"
OUT_FILE="$OUT_DIR/${DATE}-${KIND}.md"
mkdir -p "$OUT_DIR"

# Load repo .env if present (Linux/systemd). Pre-exported env vars still win.
if [ -f "$REPO_DIR/.env" ]; then
  set -a; . "$REPO_DIR/.env"; set +a
fi

cd "$REPO_DIR"

if ! command -v claude >/dev/null 2>&1; then
  echo "ERROR: claude CLI not on PATH" >&2
  bash "$REPO_DIR/scripts/notify.sh" "PA error" "claude CLI not found" || true
  exit 1
fi

# Before the morning briefing, sweep benign DMARC / Aggregate reports across
# all configured accounts so they don't clutter the triage.
if [ "$KIND" = "morning" ]; then
  python3 "$REPO_DIR/scripts/mail_clean_reports.py" --days 14 --limit 50 \
    > "$OUT_DIR/janitor-${DATE}.json" 2>&1 || \
    echo "WARN: janitor failed, continuing" >&2
fi

# -p: non-interactive print mode
# --setting-sources project,user: load CLAUDE.md and .claude/settings.json
# --output-format text: plain output
# bypassPermissions: the briefing needs to run repo scripts (mail_fetch,
# mail_read, mail_clean_reports...). We trust the contained environment;
# the mail_watcher uses the same policy.
if ! claude -p "/$KIND" \
    --setting-sources project,user \
    --output-format text \
    --permission-mode bypassPermissions \
    > "$OUT_FILE" 2> "$OUT_FILE.err"; then
  rc=$?
  echo "ERROR generating briefing $KIND (rc=$rc). Log: $OUT_FILE.err" >&2
  bash "$REPO_DIR/scripts/notify.sh" "PA error" "Briefing $KIND failed (rc=$rc)" || true
  exit "$rc"
fi
rm -f "$OUT_FILE.err"

TITLE="$([ "$KIND" = morning ] && echo "Morning briefing ready" || echo "Day summary ready")"
bash "$REPO_DIR/scripts/notify.sh" "PA $DATE" "$TITLE" || true

# Push to Telegram if configured (env var or Keychain).
_has_tg=0
if [ -n "${PA_TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${PA_TELEGRAM_CHAT_ID:-}" ]; then
  _has_tg=1
elif command -v security >/dev/null 2>&1 \
     && security find-generic-password -a "$USER" -s "PA-telegram-bot-token" >/dev/null 2>&1 \
     && security find-generic-password -a "$USER" -s "PA-telegram-chat-id"  >/dev/null 2>&1; then
  _has_tg=1
fi
if [ "$_has_tg" = 1 ]; then
  HEADER="$TITLE — $DATE"
  BODY_PLAIN="$(python3 "$REPO_DIR/scripts/md_to_telegram.py" < "$OUT_FILE")"
  printf '%s\n\n%s\n' "$HEADER" "$BODY_PLAIN" \
    | bash "$REPO_DIR/scripts/telegram_send.sh" - "" \
    || echo "WARN: telegram_send.sh failed" >&2
fi

echo "OK $OUT_FILE"
