#!/usr/bin/env python3
"""Mark / move / archive / snooze messages.

Usage:
  mail_flag.py UID --action seen      [--folder INBOX]
  mail_flag.py UID --action unseen
  mail_flag.py UID --action archive   # moves to "Archive" (created if missing)
  mail_flag.py UID --action trash
  mail_flag.py UID --action spam
  mail_flag.py UID --action delete    # flags as deleted and expunges
  mail_flag.py UID --action move --target "Some/Folder"
  mail_flag.py UID --action snooze --days N         # or --until YYYY-MM-DD
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from _mail import MailConfig, fetch_message, imap_connect, select_folder

REPO_DIR = Path(__file__).resolve().parent.parent
SNOOZE_STATE = REPO_DIR / ".pa_snooze.json"
SNOOZE_FOLDER = "Snoozed"


def ensure_folder(conn, name: str) -> None:
    conn.create(name)  # IMAP returns NO if it exists; ignore.


def _load_snooze_state() -> dict:
    if SNOOZE_STATE.exists():
        try:
            return json.loads(SNOOZE_STATE.read_text())
        except json.JSONDecodeError:
            pass
    return {"entries": []}


def _save_snooze_state(state: dict) -> None:
    # Atomic write.
    tmp = SNOOZE_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(SNOOZE_STATE)


def snooze(conn, *, account: str, uid: str, resurface_on: dt.date) -> None:
    """Record message-id, move to SNOOZE_FOLDER, persist state.

    Uses Message-ID (not UID) as the durable handle because UIDs are
    per-folder; after COPY the message has a new UID in Snoozed.
    """
    msg = fetch_message(conn, int(uid))
    message_id = str(msg.get("Message-ID", "")).strip()
    subject = str(msg.get("Subject", ""))
    from_header = str(msg.get("From", ""))
    if not message_id:
        raise RuntimeError("message has no Message-ID; cannot snooze safely")

    ensure_folder(conn, SNOOZE_FOLDER)
    conn.uid("COPY", uid, SNOOZE_FOLDER)
    conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
    conn.expunge()

    state = _load_snooze_state()
    state["entries"].append({
        "account": account,
        "message_id": message_id,
        "resurface_on": resurface_on.isoformat(),
        "subject": subject[:200],
        "from": from_header[:140],
        "snoozed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    })
    _save_snooze_state(state)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("uid", type=int)
    ap.add_argument("--account", default="example")
    ap.add_argument("--action", required=True,
                    choices=["seen", "unseen", "archive", "trash", "spam",
                             "delete", "move", "snooze"])
    ap.add_argument("--folder", default="INBOX")
    ap.add_argument("--target", help="Target folder for --action move")
    ap.add_argument("--days", type=int,
                    help="Snooze for N days (with --action snooze)")
    ap.add_argument("--until", help="Snooze until YYYY-MM-DD")
    args = ap.parse_args()

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
        elif args.action == "snooze":
            if args.until:
                try:
                    resurface_on = dt.date.fromisoformat(args.until)
                except ValueError:
                    print("--until must be YYYY-MM-DD", file=sys.stderr)
                    return 2
            elif args.days is not None:
                resurface_on = dt.date.today() + dt.timedelta(days=args.days)
            else:
                print("--days N or --until YYYY-MM-DD required for --action snooze",
                      file=sys.stderr)
                return 2
            snooze(conn, account=args.account, uid=uid, resurface_on=resurface_on)
            print(f"OK snooze uid={uid} until={resurface_on.isoformat()}")
            return 0
        print(f"OK {args.action} uid={uid}")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
