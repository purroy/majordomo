#!/usr/bin/env python3
"""Send a mail (or save as draft).

Usage:
  mail_send.py --to a@b.com[,c@d.com] --subject "..." --body-file path
               [--cc ...] [--bcc ...] [--in-reply-to UID] [--folder INBOX]
               [--attach FILE]... [--yes]

Without --yes: writes a .eml to drafts/ and prints its path. Nothing is sent.
With    --yes: sends via SMTP+STARTTLS.
"""
from __future__ import annotations

import argparse
import datetime as dt
import mimetypes
import os
import re
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from _mail import MailConfig, fetch_message, imap_connect, select_folder, smtp_connect

DRAFTS = Path(__file__).resolve().parent.parent / "drafts"


def split_addrs(value: str | None) -> list[str]:
    if not value:
        return []
    return [a.strip() for a in value.split(",") if a.strip()]


def build_message(args, cfg: MailConfig) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.from_header
    msg["To"] = ", ".join(split_addrs(args.to))
    if args.cc:
        msg["Cc"] = ", ".join(split_addrs(args.cc))
    msg["Subject"] = args.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.user.split("@", 1)[-1])

    if args.in_reply_to:
        conn = imap_connect(cfg)
        try:
            select_folder(conn, args.folder)
            original = fetch_message(conn, int(args.in_reply_to))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        orig_id = (original.get("Message-ID") or "").strip()
        if orig_id:
            msg["In-Reply-To"] = orig_id
            refs = (original.get("References") or "").strip()
            msg["References"] = (refs + " " + orig_id).strip() if refs else orig_id
        if not args.subject_explicit:
            subj = original.get("Subject", "") or ""
            if not re.match(r"^\s*re:", subj, re.I):
                subj = f"Re: {subj}"
            msg.replace_header("Subject", subj)

    body = Path(args.body_file).read_text(encoding="utf-8")
    msg.set_content(body)

    for path in args.attach or []:
        p = Path(path)
        ctype, _ = mimetypes.guess_type(p.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(
            p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name
        )
    return msg


def write_draft(msg: EmailMessage) -> Path:
    DRAFTS.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_subj = re.sub(r"[^A-Za-z0-9._-]+", "-", (msg["Subject"] or "no-subject"))[:60]
    out = DRAFTS / f"{stamp}-{safe_subj}.eml"
    out.write_bytes(bytes(msg))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="example",
                    help="Account id to send from (as configured in Keychain / .env)")
    ap.add_argument("--to", required=True)
    ap.add_argument("--cc")
    ap.add_argument("--bcc")
    ap.add_argument("--subject", default="")
    ap.add_argument("--body-file", required=True)
    ap.add_argument("--in-reply-to", help="UID to reply to (sets headers + Re: prefix)")
    ap.add_argument("--folder", default="INBOX",
                    help="Folder containing the original (used with --in-reply-to)")
    ap.add_argument("--attach", action="append", help="File to attach (repeatable)")
    ap.add_argument("--yes", action="store_true",
                    help="Actually send. Without this, only writes a draft.")
    args = ap.parse_args()
    args.subject_explicit = bool(args.subject)

    cfg = MailConfig.load(args.account)
    msg = build_message(args, cfg)

    if not args.yes:
        path = write_draft(msg)
        print(f"DRAFT {path}")
        return 0

    rcpts = split_addrs(args.to) + split_addrs(args.cc) + split_addrs(args.bcc)
    smtp = smtp_connect(cfg)
    try:
        smtp.send_message(msg, from_addr=cfg.user, to_addrs=rcpts)
    finally:
        smtp.quit()
    print(f"SENT {msg['Message-ID']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
