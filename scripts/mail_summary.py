#!/usr/bin/env python3
"""Drain pending IMPORTANT mail items and push a 4h digest to Telegram.

Before pushing, each item is re-checked against IMAP and dropped if the
owner already handled it: replied (\\Answered), deleted, or no longer in
INBOX (archived). Only mail still waiting for an answer makes the digest.

Queue protocol (shared with mail_watcher.py):
  - watcher appends triage lines to .mail_pending_important.txt (O_APPEND)
  - this script atomically renames it to .draining, filters, pushes, and
    deletes .draining on success. On failure .draining is left in place and
    merged back on the next run, so items are never lost.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _mail import MailConfig, fetch_flags, imap_connect, select_folder
from mail_watcher import PENDING, TRIAGE_LINE_RE, format_triage_lines_html
from watcher_base import Logger, push_to_telegram

REPO_DIR = Path(__file__).resolve().parent.parent
DRAINING = PENDING.with_suffix(PENDING.suffix + ".draining")
log = Logger(REPO_DIR / "briefings" / "mail_watcher.log")

# A message with any of these flags no longer needs attention.
HANDLED_FLAGS = {"\\Answered", "\\Deleted"}


def drain_queue() -> list[str]:
    """Take ownership of the pending queue atomically.

    Leftovers from a run that failed after draining come first (they are
    older), then the current queue. The merged result is persisted back to
    DRAINING so a failed push never loses items.
    """
    leftover = DRAINING.read_text(encoding="utf-8") if DRAINING.exists() else ""
    current = ""
    if PENDING.exists():
        # Atomic take: watcher appends from now on land in a fresh PENDING
        # file and wait for the next cycle.
        os.replace(PENDING, DRAINING)
        current = DRAINING.read_text(encoding="utf-8")
    elif not leftover:
        return []
    combined = leftover + current
    tmp = DRAINING.with_suffix(DRAINING.suffix + ".tmp")
    tmp.write_text(combined, encoding="utf-8")
    os.replace(tmp, DRAINING)
    return [l for l in combined.splitlines() if l.strip()]


def parse_slot(line: str) -> tuple[str, int] | None:
    """Extract (account, uid) from a triage line, or None if unparseable."""
    m = TRIAGE_LINE_RE.match(line.strip())
    if not m:
        return None
    account, _, uid = m.group(2).rpartition("/")
    if not account or not uid.isdigit():
        return None
    return account, int(uid)


def filter_handled(lines: list[str]) -> tuple[list[str], int]:
    """Drop lines whose mail the owner already handled; return (kept, dropped).

    Dedupes by (account, uid) keeping the latest summary. Fails open: if
    IMAP cannot be checked for an account, its items are kept.
    """
    # Dedupe preserving first-seen order, latest text wins.
    parsed: list[tuple[tuple[str, int] | None, str]] = []
    index: dict[tuple[str, int], int] = {}
    for line in lines:
        key = parse_slot(line)
        if key is not None and key in index:
            parsed[index[key]] = (key, line)
            continue
        if key is not None:
            index[key] = len(parsed)
        parsed.append((key, line))

    by_account: dict[str, list[int]] = defaultdict(list)
    for key, _ in parsed:
        if key:
            by_account[key[0]].append(key[1])

    flags_by_key: dict[tuple[str, int], set[str] | None] = {}
    checked: set[str] = set()
    for account, uids in by_account.items():
        try:
            conn = imap_connect(MailConfig.load(account))
            try:
                select_folder(conn, "INBOX")
                flags = fetch_flags(conn, uids)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            log(f"summary 4h: {account}: flag check failed ({e}); keeping its items")
            continue
        checked.add(account)
        for uid in uids:
            # Missing from the FETCH result = gone from INBOX (archived).
            flags_by_key[(account, uid)] = flags.get(uid)

    kept: list[str] = []
    dropped = 0
    for key, line in parsed:
        if key is None or key[0] not in checked:
            kept.append(line)
            continue
        flags = flags_by_key.get(key)
        if flags is None or flags & HANDLED_FLAGS:
            dropped += 1
            continue
        kept.append(line)
    return kept, dropped


def main() -> int:
    pending = drain_queue()
    if not pending:
        log("summary 4h: nothing pending")
        return 0

    kept, dropped = filter_handled(pending)
    if dropped:
        log(f"summary 4h: dropped {dropped} already-handled of {len(pending)} queued")
    if not kept:
        log("summary 4h: all pending items already handled; nothing to push")
        DRAINING.unlink()
        return 0

    log(f"summary 4h: pushing {len(kept)} items")
    rc = push_to_telegram(format_triage_lines_html(kept, "Mail — resumen 4h"), log=log)
    if rc != 0:
        log("summary 4h: push failed; queue preserved for next run")
        return 1
    DRAINING.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
