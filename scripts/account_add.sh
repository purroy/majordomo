#!/usr/bin/env bash
# Add a mail account to the PA. Stores credentials in macOS Keychain and
# appends the id to the PA-mail-accounts index.
#
# Usage:
#   bash scripts/account_add.sh <id> <user> <imap-host> <imap-port> <smtp-host> <smtp-port> [from-name]
# Example:
#   bash scripts/account_add.sh example you@example.com mail.example.com 993 mail.example.com 587 "Your Name"
#
# Password is read from stdin (never from argv).
set -euo pipefail

if [ "$#" -lt 6 ]; then
  cat <<EOF
Usage: $0 <id> <user> <imap-host> <imap-port> <smtp-host> <smtp-port> [from-name]

Examples:
  $0 example  you@example.com       mail.example.com 993  mail.example.com 587  "Your Name"
  $0 work     alice@acme.com        mail.acme.com    993  smtp.acme.com    587  "Alice"
  $0 side     bob@bob-consulting.io mail.example.net 993  mail.example.net 587  "Bob"
EOF
  exit 2
fi

ID="$1" USER_EMAIL="$2" IMAP_HOST="$3" IMAP_PORT="$4" SMTP_HOST="$5" SMTP_PORT="$6"
FROM_NAME="${7:-}"

read -rsp "Password for ${USER_EMAIL}: " PASSWORD; echo
[ -z "$PASSWORD" ] && { echo "Empty password. Aborting." >&2; exit 1; }

PFX="PA-mail-${ID}"
security add-generic-password -U -a "$USER" -s "${PFX}-user"      -w "$USER_EMAIL"
security add-generic-password -U -a "$USER" -s "${PFX}-pass"      -w "$PASSWORD"
security add-generic-password -U -a "$USER" -s "${PFX}-imap-host" -w "$IMAP_HOST"
security add-generic-password -U -a "$USER" -s "${PFX}-imap-port" -w "$IMAP_PORT"
security add-generic-password -U -a "$USER" -s "${PFX}-smtp-host" -w "$SMTP_HOST"
security add-generic-password -U -a "$USER" -s "${PFX}-smtp-port" -w "$SMTP_PORT"
[ -n "$FROM_NAME" ] && security add-generic-password -U -a "$USER" -s "${PFX}-from-name" -w "$FROM_NAME"

# Update the account index (PA-mail-accounts)
CUR=$(security find-generic-password -a "$USER" -s "PA-mail-accounts" -w 2>/dev/null || echo "")
NEW=$(printf '%s,%s\n' "$CUR" "$ID" | tr ',' '\n' | awk 'NF && !seen[$0]++' | paste -sd ',' -)
security add-generic-password -U -a "$USER" -s "PA-mail-accounts" -w "$NEW"

echo
echo "OK account '${ID}' stored (${USER_EMAIL} @ ${IMAP_HOST})"
echo "OK PA-mail-accounts = $NEW"
echo
echo "Testing IMAP connection..."
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from _mail import imap_connect, MailConfig
cfg = MailConfig.load('${ID}')
c = imap_connect(cfg)
typ, data = c.list()
print(f'  OK IMAP ({len(data)} folders)')
c.logout()
" || { echo "  FAIL IMAP — check credentials / host / port"; exit 1; }
