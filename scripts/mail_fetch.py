#!/usr/bin/env python3
"""List recent / unread mail as JSON.

Usage:
  mail_fetch.py [--folder INBOX] [--unread] [--since 24h|7d|"01-Apr-2026"] [--limit 20]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys

from _mail import (
    fetch_envelope,
    imap_connect,
    imap_date,
    search_uids,
    select_folder,
    snippet,
    text_body,
    fetch_message,
)


def parse_since(value: str) -> str:
    """Convert '24h' / '7d' / '365d' / 'YYYY-MM-DD' to IMAP date 'DD-Mon-YYYY'."""
    m = re.fullmatch(r"(\d+)([hd])", value)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = dt.timedelta(hours=n) if unit == "h" else dt.timedelta(days=n)
        date = dt.datetime.now() - delta
    else:
        try:
            date = dt.datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return value  # assume already IMAP-formatted
    return imap_date(date)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="example",
                    help="Account id (as configured in Keychain / .env). Default: example")
    ap.add_argument("--folder", default="INBOX")
    ap.add_argument("--unread", action="store_true")
    ap.add_argument("--since", default=None, help='e.g. 24h, 7d, 2026-04-15')
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--with-body", action="store_true",
                    help="Include short body snippet (slower; one fetch per UID)")
    args = ap.parse_args()

    from _mail import MailConfig
    conn = imap_connect(MailConfig.load(args.account))
    try:
        select_folder(conn, args.folder)
        uids = search_uids(
            conn,
            unread=args.unread,
            since=parse_since(args.since) if args.since else None,
        )
        uids = uids[-args.limit:][::-1]  # most recent first
        envs = fetch_envelope(conn, uids)
        out = []
        for uid in uids:
            msg = envs.get(uid)
            if not msg:
                continue
            item = {
                "account": args.account,
                "uid": uid,
                "from": str(msg.get("From", "")),
                "to": str(msg.get("To", "")),
                "subject": str(msg.get("Subject", "")),
                "date": str(msg.get("Date", "")),
                "message_id": str(msg.get("Message-ID", "")),
            }
            if args.with_body:
                full = fetch_message(conn, uid)
                item["snippet"] = snippet(text_body(full), 200)
            out.append(item)
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
