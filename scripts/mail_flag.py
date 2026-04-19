#!/usr/bin/env python3
"""Mark / move / archive messages.

Usage:
  mail_flag.py UID --action seen      [--folder INBOX]
  mail_flag.py UID --action unseen
  mail_flag.py UID --action archive   # moves to "Archive" (created if missing)
  mail_flag.py UID --action delete    # moves to Trash and expunges
  mail_flag.py UID --action move --target "Some/Folder"
"""
from __future__ import annotations

import argparse
import sys

from _mail import imap_connect, select_folder


def ensure_folder(conn, name: str) -> None:
    typ, _ = conn.create(name)
    # If already exists IMAP returns NO; ignore.


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("uid", type=int)
    ap.add_argument("--account", default="example")
    ap.add_argument("--action", required=True,
                    choices=["seen", "unseen", "archive", "trash", "spam",
                             "delete", "move"])
    ap.add_argument("--folder", default="INBOX")
    ap.add_argument("--target", help="Target folder for --action move")
    args = ap.parse_args()

    from _mail import MailConfig
    conn = imap_connect(MailConfig.load(args.account))
    try:
        select_folder(conn, args.folder)
        uid = str(args.uid)

        if args.action == "seen":
            conn.uid("STORE", uid, "+FLAGS", r"(\Seen)")
        elif args.action == "unseen":
            conn.uid("STORE", uid, "-FLAGS", r"(\Seen)")
        elif args.action == "archive":
            ensure_folder(conn, "Archive")
            conn.uid("COPY", uid, "Archive")
            conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            conn.expunge()
        elif args.action == "trash":
            ensure_folder(conn, "Trash")
            conn.uid("COPY", uid, "Trash")
            conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            conn.expunge()
        elif args.action == "spam":
            ensure_folder(conn, "Junk")
            conn.uid("COPY", uid, "Junk")
            conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            conn.expunge()
        elif args.action == "delete":
            conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            conn.expunge()
        elif args.action == "move":
            if not args.target:
                print("--target required for --action move", file=sys.stderr)
                return 2
            ensure_folder(conn, args.target)
            conn.uid("COPY", uid, args.target)
            conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            conn.expunge()
        print(f"OK {args.action} uid={uid}")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
