#!/usr/bin/env bash
# Two-way sync of the PA's persistent memory between this machine and the
# prod server, so both brains converge instead of drifting (the server
# also WRITES memories via the bot's distill hook — a one-way push would
# clobber them).
#
# Strategy:
#   1. Pull:  remote *.md newer than local  → local   (rsync -au)
#   2. Push:  local  *.md newer than remote → remote  (rsync -au)
#   3. MEMORY.md (the index) is merged by entry: union of both sides'
#      "- [Title](file.md) — hook" lines keyed by target file, local line
#      wins on conflict. Result written to both sides.
#
# Config (in the repo's .env, gitignored — never hardcode here):
#   PA_SYNC_SSH           ssh destination, e.g. root@server.example.com
#   PA_SYNC_MEMORY_DIR    remote memory dir, e.g. /home/pa/.claude/projects/<slug>/memory
#   PA_SYNC_MEMORY_OWNER  optional chown after push, e.g. pa:pa
#
# Usage: memory_sync.sh [--dry-run]
set -euo pipefail

DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="--dry-run"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$REPO_DIR/.env" ]; then
  set -a; . "$REPO_DIR/.env"; set +a
fi

: "${PA_SYNC_SSH:?PA_SYNC_SSH missing in .env (e.g. user@host)}"
: "${PA_SYNC_MEMORY_DIR:?PA_SYNC_MEMORY_DIR missing in .env}"
OWNER="${PA_SYNC_MEMORY_OWNER:-}"

# Local memory dir follows Claude Code's slug convention: the repo path
# with path separators (and dots) replaced by '-'.
SLUG="$(printf '%s' "$REPO_DIR" | sed 's#[/.]#-#g')"
LOCAL_MEM="$HOME/.claude/projects/$SLUG/memory"
[ -d "$LOCAL_MEM" ] || { echo "ERROR: local memory dir not found: $LOCAL_MEM" >&2; exit 1; }

echo "local : $LOCAL_MEM"
echo "remote: $PA_SYNC_SSH:$PA_SYNC_MEMORY_DIR"

ssh "$PA_SYNC_SSH" "mkdir -p '$PA_SYNC_MEMORY_DIR'"

# 1+2. Two-way file sync, newest wins, index excluded (merged below).
rsync -au $DRY --exclude 'MEMORY.md' \
  "$PA_SYNC_SSH:$PA_SYNC_MEMORY_DIR/" "$LOCAL_MEM/"
rsync -au $DRY --exclude 'MEMORY.md' \
  "$LOCAL_MEM/" "$PA_SYNC_SSH:$PA_SYNC_MEMORY_DIR/"

# 3. Merge MEMORY.md by entry (target filename is the key; local wins).
REMOTE_IDX="$(mktemp)"
trap 'rm -f "$REMOTE_IDX"' EXIT
ssh "$PA_SYNC_SSH" "cat '$PA_SYNC_MEMORY_DIR/MEMORY.md' 2>/dev/null" > "$REMOTE_IDX" || true

MERGED="$(LOCAL_IDX="$LOCAL_MEM/MEMORY.md" REMOTE_IDX="$REMOTE_IDX" python3 - <<'PY'
import os, re

def entries(path):
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8").read().splitlines():
        m = re.match(r"^- \[[^\]]*\]\(([^)]+)\)", line.strip())
        out.append((m.group(1) if m else None, line))
    return out

local = entries(os.environ["LOCAL_IDX"])
remote = entries(os.environ["REMOTE_IDX"])
seen = {key for key, _ in local if key}
lines = [line for _key, line in local]
for key, line in remote:
    if key and key not in seen:
        lines.append(line)
        seen.add(key)
print("\n".join(lines))
PY
)"

if [ -z "$DRY" ]; then
  printf '%s\n' "$MERGED" > "$LOCAL_MEM/MEMORY.md"
  printf '%s\n' "$MERGED" | ssh "$PA_SYNC_SSH" "cat > '$PA_SYNC_MEMORY_DIR/MEMORY.md'"
  if [ -n "$OWNER" ]; then
    ssh "$PA_SYNC_SSH" "chown -R '$OWNER' '$PA_SYNC_MEMORY_DIR'"
  fi
  echo "OK memory synced ($(printf '%s\n' "$MERGED" | grep -c '^- ' || true) index entries)"
else
  echo "[dry-run] merged index would have $(printf '%s\n' "$MERGED" | grep -c '^- ' || true) entries"
fi
