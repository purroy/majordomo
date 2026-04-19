#!/usr/bin/env python3
"""Fetch full body + metadata for a single UID, as JSON.

Usage: mail_read.py UID [--folder INBOX]
"""
from __future__ import annotations

import argparse
import json
import sys

from _mail import (
    fetch_message,
    imap_connect,
    list_attachments,
    select_folder,
    text_body,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("uid", type=int)
    ap.add_argument("--account", default="example")
    ap.add_argument("--folder", default="INBOX")
    args = ap.parse_args()

    from _mail import MailConfig
    conn = imap_connect(MailConfig.load(args.account))
    try:
        select_folder(conn, args.folder)
        msg = fetch_message(conn, args.uid)
        out = {
            "account": args.account,
            "uid": args.uid,
            "from": str(msg.get("From", "")),
            "to": str(msg.get("To", "")),
            "cc": str(msg.get("Cc", "")),
            "subject": str(msg.get("Subject", "")),
            "date": str(msg.get("Date", "")),
            "message_id": str(msg.get("Message-ID", "")).strip(),
            "in_reply_to": str(msg.get("In-Reply-To", "")).strip(),
            "references": str(msg.get("References", "")).strip(),
            "body": text_body(msg),
            "attachments": list_attachments(msg),
        }
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
