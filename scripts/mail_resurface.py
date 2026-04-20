#!/usr/bin/env python3
"""Move snoozed messages back to INBOX when their resurface date is reached.

Meant to be called once at the start of the morning briefing. Reads
.pa_snooze.json, and for each entry with resurface_on <= today, finds the
message in the Snoozed folder (by Message-ID) and moves it back to INBOX.

Resurfaced entries are removed from the state file. Entries that can't be
found in the Snoozed folder (user deleted them, server resorted, etc.) are
also removed with a warning - we don't want stale state blocking forever.

Prints a summary JSON to stdout:
  {"resurfaced": [{"account","subject","from"}], "still_snoozed": N, "lost": N}
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mail import MailConfig, imap_connect, select_folder

REPO_DIR = Path(__file__).resolve().parent.parent
SNOOZE_STATE = REPO_DIR / ".pa_snooze.json"
SNOOZE_FOLDER = "Snoozed"


def _load() -> dict:
    if SNOOZE_STATE.exists():
        try:
            return json.loads(SNOOZE_STATE.read_text())
        except json.JSONDecodeError:
            pass
    return {"entries": []}


def _save(state: dict) -> None:
    tmp = SNOOZE_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(SNOOZE_STATE)


def _find_uid_in_snoozed(conn, message_id: str) -> str | None:
    typ, data = conn.uid("SEARCH", None, "HEADER", "Message-ID", message_id)
    if typ != "OK" or not data or not data[0]:
        return None
    tokens = (data[0] or b"").decode().split()
    return tokens[0] if tokens else None


def resurface_one(account: str, entry: dict) -> str:
    """Move one entry's message back to INBOX. Returns 'ok', 'lost', or 'error:<msg>'."""
    try:
        conn = imap_connect(MailConfig.load(account))
    except Exception as e:
        return f"error:{e}"
    try:
        select_folder(conn, SNOOZE_FOLDER)
        uid = _find_uid_in_snoozed(conn, entry["message_id"])
        if not uid:
            return "lost"
        conn.uid("COPY", uid, "INBOX")
        conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
        conn.expunge()
        return "ok"
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def main() -> int:
    state = _load()
    today = dt.date.today()
    due: list[dict] = []
    kept: list[dict] = []
    for e in state.get("entries", []):
        try:
            rd = dt.date.fromisoformat(e["resurface_on"])
        except Exception:
            kept.append(e)
            continue
        if rd <= today:
            due.append(e)
        else:
            kept.append(e)

    resurfaced: list[dict] = []
    lost = 0
    errors: list[str] = []
    for e in due:
        status = resurface_one(e["account"], e)
        if status == "ok":
            resurfaced.append({
                "account": e["account"],
                "subject": e.get("subject", ""),
                "from": e.get("from", ""),
                "snoozed_at": e.get("snoozed_at", ""),
            })
        elif status == "lost":
            lost += 1
        else:
            # Network / IMAP error: keep entry for next attempt.
            errors.append(status)
            kept.append(e)

    state["entries"] = kept
    _save(state)

    summary = {
        "resurfaced": resurfaced,
        "still_snoozed": len(kept),
        "lost": lost,
        "errors": errors,
    }
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
