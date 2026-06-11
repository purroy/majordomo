#!/usr/bin/env python3
"""Weekly triage learning — propose muting senders the owner always ignores.

Reads .triage_outcomes.jsonl (written by mail_summary.py: what happened to
each item the watcher flagged as IMPORTANT). Senders whose mail the owner
systematically archives or deletes without ever answering are proposed as
noise-filter additions via Telegram, one message per sender with buttons:

    🔇 Silenciar  → the bot appends the pattern to noise_filters.local.txt
    👁 Mantener   → keep flagging this sender

The watcher itself never auto-mutes anything: muting always goes through
an explicit button press.

"pending" outcomes (still unanswered in INBOX at digest time) are
re-checked against current IMAP flags before aggregating, so mail the
owner handled after the digest counts with its real outcome.

Run weekly via cron. `--dry-run` logs would-be proposals.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import telegram_api as tg_api
from _mail import MailConfig, fetch_flags, imap_connect, select_folder
from mail_watcher import META, is_noise, load_noise_patterns
from watcher_base import Logger, atomic_write_json, html_escape, load_json

REPO_DIR = Path(__file__).resolve().parent.parent
OUTCOMES = REPO_DIR / ".triage_outcomes.jsonl"
PROPOSALS = REPO_DIR / ".triage_proposals.json"
STATE = REPO_DIR / ".triage_learn_state.json"
log = Logger(REPO_DIR / "briefings" / "triage_learn.log")

WINDOW_DAYS = 30          # outcomes considered for aggregation
OUTCOMES_KEEP_DAYS = 60   # prune horizon for the outcomes log
META_KEEP_DAYS = 45       # prune horizon for the watcher meta sidecar
MIN_OCCURRENCES = 4       # at least this many flagged mails from the sender
MIN_IGNORED = 3           # of which archived/deleted without answering
REPROPOSE_AFTER_DAYS = 90
MAX_PROPOSALS_PER_RUN = 3


def read_outcomes(window_days: int) -> list[dict]:
    """Latest outcome per (account, uid) within the window."""
    if not OUTCOMES.exists():
        return []
    cutoff = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    latest: dict[tuple[str, int], dict] = {}
    for raw in OUTCOMES.read_text(encoding="utf-8").splitlines():
        try:
            o = json.loads(raw)
            if o.get("ts", "") >= cutoff:
                latest[(o["account"], int(o["uid"]))] = o
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
    return list(latest.values())


def recheck_pending(outcomes: list[dict]) -> None:
    """Resolve 'pending' outcomes against current IMAP flags, in place."""
    by_account: dict[str, list[dict]] = defaultdict(list)
    for o in outcomes:
        if o["outcome"] == "pending":
            by_account[o["account"]].append(o)
    for account, items in by_account.items():
        try:
            conn = imap_connect(MailConfig.load(account))
            try:
                select_folder(conn, "INBOX")
                flags = fetch_flags(conn, [o["uid"] for o in items])
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            log(f"{account}: pending re-check failed ({e}); keeping as pending")
            continue
        for o in items:
            f = flags.get(o["uid"])
            if f is None:
                o["outcome"] = "archived"
            elif "\\Answered" in f:
                o["outcome"] = "answered"
            elif "\\Deleted" in f:
                o["outcome"] = "deleted"


def sender_address(from_header: str) -> str:
    _name, addr = email.utils.parseaddr(from_header)
    return addr.lower()


def aggregate(outcomes: list[dict]) -> list[dict]:
    """Return candidate senders: {address, label, total, ignored, answered}."""
    by_sender: dict[str, list[dict]] = defaultdict(list)
    for o in outcomes:
        addr = sender_address(o.get("from", ""))
        if addr:
            by_sender[addr].append(o)
    candidates = []
    for addr, items in by_sender.items():
        total = len(items)
        answered = sum(1 for o in items if o["outcome"] == "answered")
        ignored = sum(1 for o in items if o["outcome"] in ("archived", "deleted"))
        if total >= MIN_OCCURRENCES and answered == 0 and ignored >= MIN_IGNORED:
            label = items[-1].get("from", addr)[:80]
            candidates.append({
                "address": addr, "label": label,
                "total": total, "ignored": ignored,
            })
    candidates.sort(key=lambda c: -c["ignored"])
    return candidates


def prune_jsonl(path: Path, keep_days: int) -> None:
    """Drop entries older than keep_days (by their `ts` field)."""
    if not path.exists():
        return
    cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    kept = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(raw).get("ts", "") >= cutoff:
                kept.append(raw)
        except json.JSONDecodeError:
            continue
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    tmp.replace(path)


def build_proposal_msg(c: dict) -> str:
    return "\n".join([
        "<b>🔇 ¿Silencio a este remitente?</b>",
        f"<b>{html_escape(c['label'])}</b>",
        f"En 30 días le marqué {c['total']} correos como importantes y "
        f"archivaste/borraste {c['ignored']} sin responder ninguno.",
        "Si lo silencio, sus correos dejan de aparecer en avisos y resúmenes "
        "(seguirán en el inbox).",
    ])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Log would-be proposals; no Telegram, no state writes.")
    args = ap.parse_args()

    outcomes = read_outcomes(WINDOW_DAYS)
    if not outcomes:
        log("no outcomes in window; nothing to learn")
        return 0
    recheck_pending(outcomes)

    state = load_json(STATE, {"proposed": {}})
    proposed = state.setdefault("proposed", {})
    cutoff = (dt.date.today() - dt.timedelta(days=REPROPOSE_AFTER_DAYS)).isoformat()
    noise = load_noise_patterns()

    candidates = [
        c for c in aggregate(outcomes)
        if not is_noise(c["label"], noise)          # already muted
        and not is_noise(c["address"], noise)
        and proposed.get(c["address"], "") < cutoff  # not proposed recently
    ]
    if not candidates:
        log(f"{len(outcomes)} outcomes, no new candidates")
        return 0

    run_id = f"{int(time.time()) % 0xFFFFFF:x}"
    proposals: dict[str, dict] = {}
    today = dt.date.today().isoformat()
    for i, c in enumerate(candidates[:MAX_PROPOSALS_PER_RUN]):
        idx = f"{run_id}{i}"
        if args.dry_run:
            log(f"[dry-run] would propose muting {c['address']} "
                f"(total={c['total']} ignored={c['ignored']})")
            continue
        proposals[idx] = {
            "pattern": re.escape(c["address"]),
            "label": c["address"],
        }
        buttons = [[("🔇 Silenciar", f"t:mute:{idx}"),
                    ("👁 Mantener", f"t:keep:{idx}")]]
        if tg_api.send_html(build_proposal_msg(c), buttons=buttons, log=log):
            proposed[c["address"]] = today
            log(f"proposed muting {c['address']} "
                f"(total={c['total']} ignored={c['ignored']})")

    if not args.dry_run:
        if proposals:
            atomic_write_json(PROPOSALS, proposals)
        atomic_write_json(STATE, state)
        prune_jsonl(OUTCOMES, OUTCOMES_KEEP_DAYS)
        prune_jsonl(META, META_KEEP_DAYS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
