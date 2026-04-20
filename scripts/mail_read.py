#!/usr/bin/env python3
"""Fetch full body + metadata for a single UID, as JSON.

Usage: mail_read.py UID [--account NAME] [--folder INBOX] [--with-thread]

--with-thread fetches siblings that share the same conversation chain
(matched by References/In-Reply-To/Message-ID). Returns them in the
`thread` array, oldest first, with body and metadata. Useful for reply
drafts so Claude sees the full conversation.
"""
from __future__ import annotations

import argparse
import json
import sys

from _mail import (
    MailConfig,
    fetch_message,
    imap_connect,
    list_attachments,
    search_uids,
    select_folder,
    text_body,
)


def _msg_to_dict(msg, account: str, uid: int, include_body: bool = True) -> dict:
    out = {
        "account": account,
        "uid": uid,
        "from": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "cc": str(msg.get("Cc", "")),
        "subject": str(msg.get("Subject", "")),
        "date": str(msg.get("Date", "")),
        "message_id": str(msg.get("Message-ID", "")).strip(),
        "in_reply_to": str(msg.get("In-Reply-To", "")).strip(),
        "references": str(msg.get("References", "")).strip(),
    }
    if include_body:
        out["body"] = text_body(msg)
        out["attachments"] = list_attachments(msg)
    return out


def _collect_thread_ids(primary: dict) -> list[str]:
    """All Message-IDs in this conversation: References + In-Reply-To + self."""
    ids: list[str] = []
    for raw in (primary["references"], primary["in_reply_to"], primary["message_id"]):
        for token in raw.split():
            token = token.strip()
            if token and token not in ids:
                ids.append(token)
    return ids


def fetch_thread(conn, account: str, folder: str, primary: dict) -> list[dict]:
    """Find sibling messages sharing Message-IDs with `primary`.

    Uses IMAP `HEADER Message-ID` searches. Returns siblings sorted by Date,
    excluding `primary` itself.
    """
    ids = _collect_thread_ids(primary)
    if not ids:
        return []
    seen_uids: set[int] = {primary["uid"]}
    siblings: list[dict] = []
    for mid in ids:
        # Skip the empty/invalid entries
        if not mid.startswith("<") or not mid.endswith(">"):
            continue
        try:
            typ, data = conn.uid("SEARCH", None, "HEADER", "Message-ID", mid)
        except Exception:
            continue
        if typ != "OK" or not data or not data[0]:
            # Also try References search to catch replies
            try:
                typ2, data2 = conn.uid("SEARCH", None, "HEADER", "References", mid)
            except Exception:
                continue
            if typ2 != "OK":
                continue
            data = data2
        for raw in (data[0] or b"").decode().split():
            uid = int(raw)
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
            try:
                msg = fetch_message(conn, uid)
            except Exception:
                continue
            siblings.append(_msg_to_dict(msg, account, uid))
    siblings.sort(key=lambda d: d.get("date", ""))
    return siblings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("uid", type=int)
    ap.add_argument("--account", default="example")
    ap.add_argument("--folder", default="INBOX")
    ap.add_argument("--with-thread", action="store_true",
                    help="Also fetch sibling messages in the same thread.")
    args = ap.parse_args()

    conn = imap_connect(MailConfig.load(args.account))
    try:
        select_folder(conn, args.folder)
        msg = fetch_message(conn, args.uid)
        out = _msg_to_dict(msg, args.account, args.uid)
        if args.with_thread:
            out["thread"] = fetch_thread(conn, args.account, args.folder, out)
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
