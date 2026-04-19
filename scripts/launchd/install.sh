#!/usr/bin/env bash
# Install one or more PA launchd agents on macOS by substituting the
# __REPO_DIR__ / __USER__ / __HOME__ placeholders and copying the resulting
# plist into ~/Library/LaunchAgents.
#
# Usage:
#   bash scripts/launchd/install.sh                 # install all PA agents
#   bash scripts/launchd/install.sh telegram        # install just one (matches *telegram*.plist)
#   bash scripts/launchd/install.sh briefing-morning briefing-evening
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SRC_DIR="$REPO_DIR/scripts/launchd"
DST_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$DST_DIR"

substitute() {
  local src="$1" dst="$2"
  sed -e "s|__REPO_DIR__|${REPO_DIR}|g" \
      -e "s|__USER__|${USER}|g" \
      -e "s|__HOME__|${HOME}|g" \
      "$src" > "$dst"
}

select_plists() {
  if [ "$#" -eq 0 ]; then
    find "$SRC_DIR" -maxdepth 1 -name 'com.example.pa-*.plist' -print
  else
    for tag in "$@"; do
      local hits
      hits=$(find "$SRC_DIR" -maxdepth 1 -name "com.example.pa-*${tag}*.plist" -print)
      [ -z "$hits" ] && { echo "No plist matches '*${tag}*'" >&2; exit 1; }
      echo "$hits"
    done
  fi
}

while read -r src; do
  [ -z "$src" ] && continue
  name=$(basename "$src")
  dst="$DST_DIR/$name"
  echo "-> $name"
  substitute "$src" "$dst"
  launchctl unload "$dst" 2>/dev/null || true
  launchctl load "$dst"
done < <(select_plists "$@")

echo "OK. Loaded agents in $DST_DIR"
