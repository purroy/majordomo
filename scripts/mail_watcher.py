#!/usr/bin/env python3
"""Mail watcher — every N minutes, poll INBOX for new mail across all accounts.

Goal: keep Claude calls as cheap as possible.
  1. For each account, do a cheap IMAP search for UIDs > last_uid (state file).
  2. Nothing new anywhere → log and exit (zero-cost tick).
  3. Something new → call `claude --print` ONCE with the new headers and
     classify according to memory/triage_rules.md.
  4. If Claude flags any FIRE or IMPORTANT items → push to Telegram.
  5. Advance last_uid per account.

Never marks messages as read. Uses BODY.PEEK throughout.

Special modes:
  python3 scripts/mail_watcher.py --baseline
    → only sets last_uid = current max per account and exits (use after a
       long offline window to avoid flooding Telegram with backlog).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from _mail import (
    MailConfig,
    fetch_envelope,
    imap_connect,
    list_accounts,
    search_uids,
    select_folder,
)
from mail_clean_reports import clean_account as clean_reports_for

REPO_DIR = Path(__file__).resolve().parent.parent
STATE = REPO_DIR / ".mail_watch_state.json"
LOG = REPO_DIR / "briefings" / "mail_watcher.log"
CLAUDE_TIMEOUT_S = 240
MAX_NEW_PER_RUN = 30  # safety cap per account


def log(msg: str) -> None:
    LOG.parent.mkdir(exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    sys.stderr.write(line)
    sys.stderr.flush()


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except json.JSONDecodeError:
            pass
    return {"accounts": {}}


def save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=2))


def collect_new(account: str, last_uid: int) -> tuple[int, list[dict]]:
    """Return (current_max_uid, [headers of the new messages])."""
    cfg = MailConfig.load(account)
    conn = imap_connect(cfg)
    try:
        select_folder(conn, "INBOX")
        all_uids = search_uids(conn)
        if not all_uids:
            return 0, []
        max_uid = all_uids[-1]
        # Solo los que sean > last_uid, hasta el tope
        new_uids = [u for u in all_uids if u > last_uid][:MAX_NEW_PER_RUN]
        if not new_uids:
            return max_uid, []
        envs = fetch_envelope(conn, new_uids)
        items = []
        for uid in new_uids:
            msg = envs.get(uid)
            if not msg:
                continue
            items.append({
                "account": account,
                "uid": uid,
                "from": str(msg.get("From", ""))[:140],
                "subject": str(msg.get("Subject", ""))[:200],
                "date": str(msg.get("Date", "")),
            })
        return max_uid, items
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def build_prompt(items: list[dict]) -> str:
    listing = "\n".join(
        f"- [{it['account']}/{it['uid']}] {it['from']} → {it['subject']}"
        for it in items
    )
    return f"""Autonomous PA task: triage NEW mail received in the last poll
window. DO NOT mark anything as read. Use `mail_read.py UID --account ID`
(BODY.PEEK) when you need the body.

New headers:
{listing}

Procedure:
1. For each item, decide whether you need to open the body. If From/Subject
   already make it clearly NOISE (newsletter, no-reply, platform notice
   with no action), do not open it.
2. Otherwise, read with `python3 scripts/mail_read.py <UID> --account <ID>`.
3. Classify according to memory/triage_rules.md:
   FIRE       — production down, angry customer, <24h deadline, hosting or
                bank blocked, security incident.
   IMPORTANT  — customer with a concrete question, pre-sales, blocked
                employee, invoice / contract.
   SUSPICIOUS — phishing, fake invoices, malicious attachments, brand
                impersonation, broken-grammar mass outbound.
   (Routine and benign noise are NOT reported here — only in briefings.)

Output. Exactly one of:
  a) If nothing qualifies: the literal word `NONE`.
  b) Otherwise, one line per item:
     `<tag> [<account>/<UID>] <short sender> · <one-line summary>`
     For SUSPICIOUS, always append `· mark as spam?`.
     Example:
     `FIRE [example/12345] Hosting ACME · server down since 09:14`
     `IMPORTANT [example/56720] Alice (ACME) · quote needed before Monday`
     `SUSPICIOUS [example/164940] Fake Bank <noreply@bank-secure.tk> · phishing for credentials · mark as spam?`

Rules:
- No Markdown.
- No greetings or extra explanation.
- If a body cannot be read (timeout, error), skip that item.
"""


def run_claude(prompt: str) -> tuple[int, str, str]:
    cmd = [
        "claude", "--print",
        "--model", "sonnet",
        "--session-id", str(uuid.uuid4()),
        "--no-session-persistence",
        "--setting-sources", "project,user",
        "--output-format", "text",
        "--permission-mode", "bypassPermissions",
        prompt,
    ]
    try:
        out = subprocess.run(
            cmd, cwd=REPO_DIR, capture_output=True, text=True,
            timeout=CLAUDE_TIMEOUT_S,
        )
        return out.returncode, out.stdout or "", out.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "claude CLI not in PATH"


def push_to_telegram(text: str) -> None:
    res = subprocess.run(
        ["bash", str(REPO_DIR / "scripts" / "telegram_send.sh"), "-", ""],
        input=text, text=True, capture_output=True,
    )
    if res.returncode != 0:
        log(f"telegram_send rc={res.returncode}: {res.stderr.strip()[:200]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true",
                    help="Only set last_uid to current max per account and exit.")
    args = ap.parse_args()

    state = load_state()
    accounts = list_accounts()

    # Step 0 (skipped in baseline): sweep benign DMARC/Aggregate reports
    # BEFORE listing new mail, so they don't reach triage.
    if not args.baseline:
        for acc in accounts:
            try:
                res = clean_reports_for(acc, days=2, limit=30, dry_run=False)
                if res.get("trashed"):
                    log(f"{acc}: janitor -> {len(res['trashed'])} reports to Trash")
                if res.get("error"):
                    log(f"{acc}: janitor error -> {res['error']}")
            except Exception as e:
                log(f"{acc}: janitor exception -> {e}")

    all_new: list[dict] = []
    new_uids_per_account: dict[str, int] = {}

    for acc in accounts:
        last = state["accounts"].get(acc, {}).get("last_uid", 0)
        try:
            max_uid, items = collect_new(acc, last)
        except Exception as e:
            log(f"{acc}: error {e}")
            continue
        new_uids_per_account[acc] = max_uid
        if last == 0 or args.baseline:
            state["accounts"].setdefault(acc, {})["last_uid"] = max_uid
            log(f"{acc}: baseline last_uid={max_uid}")
            continue
        if items:
            log(f"{acc}: {len(items)} new (UIDs {[i['uid'] for i in items]})")
            all_new.extend(items)
        else:
            state["accounts"].setdefault(acc, {})["last_uid"] = max_uid

    if args.baseline:
        save_state(state)
        return 0

    if not all_new:
        save_state(state)
        log("nothing new across accounts")
        return 0

    rc, stdout, stderr = run_claude(build_prompt(all_new))
    output = stdout.strip()

    if rc != 0:
        log(f"claude rc={rc}, stderr={stderr.strip()[:300]}")
        save_state(state)
        return rc

    for acc, max_uid in new_uids_per_account.items():
        state["accounts"].setdefault(acc, {})["last_uid"] = max_uid
    save_state(state)

    if not output or output == "NONE":
        log(f"triaged {len(all_new)} new mails: nothing important")
        return 0

    log(f"sending to telegram ({len(output)} chars, {output.count(chr(10))+1} lines)")
    push_to_telegram(f"Mail — something for you\n\n{output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
