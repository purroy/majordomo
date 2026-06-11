#!/usr/bin/env python3
"""Follow-up watcher — nudge about sent mail that never got a reply.

For each account, scans the Sent folder for messages the owner sent to a
small number of human recipients at least FOLLOWUP_DAYS ago, checks
whether any reply exists (INBOX, Archive, or a newer own follow-up in
Sent, matched via References/In-Reply-To), and pushes a Telegram nudge
with action buttons for the ones still hanging:

    ✍️ Follow-up   → the bot drafts a chase-up (f:fup:<acc>:<uid>)
    ✅ Resuelto     → stop tracking this thread (f:dis:...)
    ⏰ +3 días      → re-nudge later (f:lat:...)

Each thread is nudged ONCE (unless re-armed with ⏰). State lives in
.followup_state.json keyed by Message-ID.

Config (env / Keychain via get_secret):
    PA-followup-days            days without reply before nudging (default 4)
    PA-mail-<acc>-sent-folder   Sent folder name (default "Sent")

Run daily via cron. `--dry-run` logs what it would push.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import telegram_api as tg_api
from _mail import (
    MailConfig,
    fetch_envelope,
    get_secret,
    imap_connect,
    imap_date,
    list_accounts,
    search_uids,
    select_folder,
)
from watcher_base import Logger, atomic_write_json, html_escape, load_json

REPO_DIR = Path(__file__).resolve().parent.parent
STATE = REPO_DIR / ".followup_state.json"
log = Logger(REPO_DIR / "briefings" / "followup_watcher.log")

MAX_NUDGES_PER_RUN = 5
MAX_RECIPIENTS = 3      # more than this = broadcast, not a conversation
PRUNE_AFTER_DAYS = 60

# Recipients that never reply by definition.
ROBOT_RCPT_RE = re.compile(
    r"no-?reply|notifications?@|mailer-daemon|donotreply|@noreply",
    re.IGNORECASE,
)


def followup_days() -> int:
    try:
        return int(get_secret("followup-days", default="4"))
    except (RuntimeError, ValueError):
        return 4


def sent_folder(account: str) -> str:
    return get_secret(f"mail-{account}-sent-folder", default="Sent")


# --- state -------------------------------------------------------------------

def load_state() -> dict:
    return load_json(STATE, {"threads": {}})


def save_state(state: dict) -> None:
    atomic_write_json(STATE, state)


def prune_state(state: dict, today: dt.date) -> None:
    cutoff = (today - dt.timedelta(days=PRUNE_AFTER_DAYS)).isoformat()
    threads = state.get("threads", {})
    for msgid in [m for m, e in threads.items()
                  if e.get("sent_at", "9999") < cutoff]:
        del threads[msgid]


def mark_by_uid(account: str, uid: str | int, status: str, days: int = 3) -> None:
    """Bot callback hook: mark the tracked thread for (account, sent uid).

    `status`: "dismissed" (stop tracking) or "later" (re-nudge in `days`).
    """
    state = load_state()
    uid = int(uid)
    entry = None
    for e in state.get("threads", {}).values():
        if e.get("account") == account and e.get("sent_uid") == uid:
            entry = e
            break
    if entry is None:
        # Nudge predates the state schema or state was lost; track minimally.
        entry = {"account": account, "sent_uid": uid}
        state.setdefault("threads", {})[f"uid:{account}:{uid}"] = entry
    entry["status"] = status
    if status == "later":
        entry["remind_on"] = (dt.date.today() + dt.timedelta(days=days)).isoformat()
    save_state(state)


# --- Sent-folder scan ----------------------------------------------------------

def parse_recipients(msg) -> list[str]:
    pairs = email.utils.getaddresses(
        [str(msg.get("To", "")), str(msg.get("Cc", ""))]
    )
    return [addr.lower() for _name, addr in pairs if addr]


def collect_candidates(conn, account: str, cfg, lookback_days: int) -> list[dict]:
    """Return sent messages that could deserve a follow-up (age not yet checked)."""
    since = imap_date(dt.date.today() - dt.timedelta(days=lookback_days))
    uids = search_uids(conn, since=since)
    if not uids:
        return []
    envs = fetch_envelope(conn, uids)
    out = []
    own = cfg.user.lower()
    for uid, msg in envs.items():
        msgid = str(msg.get("Message-ID", "")).strip()
        if not msgid:
            continue
        rcpts = [r for r in parse_recipients(msg) if r != own]
        if not rcpts or len(rcpts) > MAX_RECIPIENTS:
            continue
        if all(ROBOT_RCPT_RE.search(r) for r in rcpts):
            continue
        sent_at = None
        try:
            sent_at = email.utils.parsedate_to_datetime(str(msg.get("Date", "")))
        except (TypeError, ValueError):
            pass
        if sent_at is None:
            continue
        out.append({
            "account": account,
            "uid": uid,
            "msgid": msgid,
            "in_reply_to": str(msg.get("In-Reply-To", "")).strip(),
            "to": ", ".join(rcpts),
            "subject": str(msg.get("Subject", ""))[:160],
            "sent_at": sent_at,
        })
    return out


def drop_superseded(candidates: list[dict]) -> list[dict]:
    """If the owner already replied again in the same thread, only the
    NEWEST sent message should be tracked — drop the ones an own later
    message points at via In-Reply-To."""
    by_msgid = {c["msgid"]: c for c in candidates}
    superseded = {c["in_reply_to"] for c in candidates if c["in_reply_to"] in by_msgid}
    return [c for c in candidates if c["msgid"] not in superseded]


def _search_header(conn, field: str, value: str) -> bool:
    typ, data = conn.uid("SEARCH", None, "HEADER", field, value)
    return typ == "OK" and bool((data[0] or b"").strip())


def has_reply(conn, account: str, msgid: str, sent_uid: int) -> bool:
    """True if any message references `msgid` (a reply, or an own newer
    follow-up). Checks INBOX, Archive, and the Sent folder itself."""
    folders = ["INBOX", "Archive", sent_folder(account)]
    for folder in folders:
        try:
            select_folder(conn, folder)
        except RuntimeError:
            continue  # folder may not exist (e.g. no Archive yet)
        for field in ("In-Reply-To", "References"):
            try:
                if _search_header(conn, field, msgid):
                    if folder == sent_folder(account):
                        # A hit in Sent could be the candidate itself when a
                        # server indexes References loosely; require another UID.
                        typ, data = conn.uid("SEARCH", None, "HEADER", field, msgid)
                        uids = {int(x) for x in (data[0] or b"").split()}
                        if uids - {sent_uid}:
                            return True
                    else:
                        return True
            except Exception:
                continue
    return False


# --- nudge -------------------------------------------------------------------

def build_nudge(c: dict, days: int) -> str:
    return "\n".join([
        f"<b>⏳ Sin respuesta tras {days} días</b>",
        f"<b>Para:</b> {html_escape(c['to'])}",
        f"<b>Asunto:</b> {html_escape(c['subject'])}",
        f"Enviado el {c['sent_at'].strftime('%d %b')} · "
        f"<code>{html_escape(c['account'])}/{c['uid']}</code> (Sent)",
    ])


def nudge_buttons(c: dict) -> list[list[tuple[str, str]]]:
    acc, uid = c["account"], c["uid"]
    return [[
        ("✍️ Follow-up", f"f:fup:{acc}:{uid}"),
        ("✅ Resuelto", f"f:dis:{acc}:{uid}"),
        ("⏰ +3 días", f"f:lat:{acc}:{uid}"),
    ]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Log would-be nudges; no Telegram, no state writes.")
    args = ap.parse_args()

    days = followup_days()
    today = dt.date.today()
    now = dt.datetime.now(dt.timezone.utc)
    state = load_state()
    threads = state.setdefault("threads", {})

    nudges: list[dict] = []
    for account in list_accounts():
        try:
            cfg = MailConfig.load(account)
            conn = imap_connect(cfg)
        except Exception as e:
            log(f"{account}: connect failed: {e}")
            continue
        try:
            try:
                select_folder(conn, sent_folder(account))
            except RuntimeError as e:
                log(f"{account}: cannot open Sent folder "
                    f"({sent_folder(account)}): {e}")
                continue
            candidates = drop_superseded(
                collect_candidates(conn, account, cfg, lookback_days=days + 14)
            )
            for c in candidates:
                age_days = (now - c["sent_at"]).days
                if age_days < days:
                    continue
                entry = threads.get(c["msgid"], {})
                status = entry.get("status", "")
                if status in ("dismissed", "answered", "nudged"):
                    continue
                if status == "later" and entry.get("remind_on", "") > today.isoformat():
                    continue
                if has_reply(conn, account, c["msgid"], c["uid"]):
                    threads[c["msgid"]] = {
                        "account": account, "sent_uid": c["uid"],
                        "subject": c["subject"], "to": c["to"],
                        "sent_at": c["sent_at"].date().isoformat(),
                        "status": "answered",
                    }
                    continue
                c["age_days"] = age_days
                nudges.append(c)
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    if not nudges:
        log("nothing to nudge")
        if not args.dry_run:
            prune_state(state, today)
            save_state(state)
        return 0

    nudges.sort(key=lambda c: c["sent_at"])
    skipped = len(nudges) - MAX_NUDGES_PER_RUN
    if skipped > 0:
        log(f"capping nudges at {MAX_NUDGES_PER_RUN} ({skipped} wait for next run)")
    for c in nudges[:MAX_NUDGES_PER_RUN]:
        text = build_nudge(c, c["age_days"])
        if args.dry_run:
            log(f"[dry-run] would nudge: {c['account']}/{c['uid']} "
                f"to={c['to']} subject={c['subject'][:60]!r}")
            continue
        if tg_api.send_html(text, buttons=nudge_buttons(c), log=log):
            threads[c["msgid"]] = {
                "account": c["account"], "sent_uid": c["uid"],
                "subject": c["subject"], "to": c["to"],
                "sent_at": c["sent_at"].date().isoformat(),
                "status": "nudged",
                "nudged_at": today.isoformat(),
            }
            log(f"nudged {c['account']}/{c['uid']} ({c['to']})")
        else:
            log(f"push failed for {c['account']}/{c['uid']}; will retry next run")

    if not args.dry_run:
        prune_state(state, today)
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
