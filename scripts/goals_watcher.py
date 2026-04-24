#!/usr/bin/env python3
"""Goals watcher — daily EOS nudges to Telegram.

Runs 3× per day via launchd (09:00 / 12:30 / 18:00). Each tick decides what
to push based on day-of-week, hour, and state:

  - Monday 09:00–10:59  → rocks check-in (1 msg, once/week).
  - Friday 18:00–19:59  → weekly retro (1 msg, once/week).
  - Any tick           → deadline alerts: overdue items + due in ≤2 days.
                         At most one push per item per day.
  - Day 3+ of month    → revenue nudge if previous month is still null.
  - Quarter end ≤ 14d  → one quarter-closing nudge per quarter.

State: .goals_watch_state.json (atomic writes, gitignored).
Telegram only — reuses watcher_base.push_to_telegram.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from watcher_base import Logger, atomic_write_json, load_json, push_to_telegram

REPO_DIR = Path(__file__).resolve().parent.parent
STATE = REPO_DIR / ".goals_watch_state.json"
GOALS_PY = REPO_DIR / "scripts" / "goals.py"
log = Logger(REPO_DIR / "briefings" / "goals_watcher.log")


def run_check() -> dict | None:
    """Invoke `goals.py check --json`. Returns parsed dict or None on error."""
    try:
        res = subprocess.run(
            ["python3", str(GOALS_PY), "check", "--json"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        log("goals.py check: timeout")
        return None
    if res.returncode != 0:
        log(f"goals.py check rc={res.returncode} stderr={res.stderr.strip()[:300]}")
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as e:
        log(f"goals.py check: invalid JSON ({e})")
        return None


def run_list() -> dict | None:
    """Invoke `goals.py list --json`. Used for Monday/Friday rituals."""
    try:
        res = subprocess.run(
            ["python3", str(GOALS_PY), "list", "--json"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


# --- message builders --------------------------------------------------------

def build_deadline_msg(overdue: list[dict], due_soon: list[dict]) -> str:
    lines = ["Goals - deadlines"]
    if overdue:
        lines.append("")
        lines.append("OVERDUE:")
        for e in overdue:
            lines.append(f"- {e['area']} | {e['title']}  (due {e['due']}, {abs(e['days_until'])}d late)")
    if due_soon:
        lines.append("")
        lines.append("Due in 2 days or less:")
        for e in due_soon:
            d = e["days_until"]
            when = "today" if d == 0 else ("tomorrow" if d == 1 else f"in {d}d")
            lines.append(f"- {e['area']} | {e['title']}  ({when}, {e['due']})")
    lines.append("")
    lines.append("Update with: /goals done <match>  or  /goals update <match> --progress ...")
    return "\n".join(lines)


def build_monday_msg(snapshot: dict) -> str:
    rocks = [it for it in snapshot.get("rocks", []) if not it["checked"]]
    lines = ["Monday - rocks check-in"]
    lines.append("")
    q = snapshot.get("period", {}).get("quarter", "current quarter")
    lines.append(f"Open rocks ({q}):")
    for it in rocks:
        prog = it["attrs"].get("progress", "").strip(' "\'')
        prog_str = f" - {prog}" if prog else " - (no progress recorded)"
        lines.append(f"- {it['area']} | {it['title']}{prog_str}")
    lines.append("")
    lines.append("Which is the focus rock this week? Update with:")
    lines.append("  /goals update <match> --progress \"...\"")
    return "\n".join(lines)


def build_friday_msg(snapshot: dict, stale_rocks: list[dict]) -> str:
    today = dt.date.today()
    week_start = today - dt.timedelta(days=today.weekday())  # Monday
    done_this_week = [d for d in snapshot.get("done_log", [])
                      if d["date"] >= week_start.isoformat()]
    lines = ["Friday - weekly retro"]
    lines.append("")
    if done_this_week:
        lines.append(f"Completed this week ({len(done_this_week)}):")
        for d in done_this_week:
            lines.append(f"- {d['date']} | {d['area']} | {d['desc']}")
    else:
        lines.append("Nothing marked done this week.")
    if stale_rocks:
        lines.append("")
        lines.append("Rocks without progress:")
        for r in stale_rocks:
            lines.append(f"- {r['area']} | {r['title']}")
    lines.append("")
    lines.append("Close the week: any rock to move before Monday?")
    return "\n".join(lines)


def build_revenue_msg(missing_months: list[str]) -> str:
    if len(missing_months) == 1:
        m = missing_months[0]
        return (
            f"Revenue reminder\n\n"
            f"Revenue for {m} is not filled in.\n"
            f"Set it with: python3 scripts/goals.py revenue {m} <amount>\n"
            f"(or null if no revenue booked)"
        )
    return (
        "Revenue reminder\n\n"
        f"Missing revenue for: {', '.join(missing_months)}\n"
        f"Fill each with: python3 scripts/goals.py revenue YYYY-MM <amount>"
    )


def build_quarter_msg(q: dict) -> str:
    return (
        f"Quarter closing - {q['quarter']}\n\n"
        f"Ends {q['ends']} ({q['days_until']}d).\n"
        f"Time to review open rocks, mark what's done, and prepare next-quarter rocks."
    )


# --- decisions ---------------------------------------------------------------

def should_push_monday(now: dt.datetime, state: dict) -> bool:
    if now.weekday() != 0:  # 0 = Monday
        return False
    if now.hour < 9 or now.hour >= 11:
        return False
    return state.get("last_monday_checkin") != now.date().isoformat()


def should_push_friday(now: dt.datetime, state: dict) -> bool:
    if now.weekday() != 4:  # 4 = Friday
        return False
    if now.hour < 18 or now.hour >= 20:
        return False
    return state.get("last_friday_retro") != now.date().isoformat()


def should_push_revenue(now: dt.datetime, state: dict, missing: list[str]) -> bool:
    if not missing:
        return False
    if now.day < 3:
        return False
    # Nudge at most once per month.
    return state.get("last_revenue_nudge_month") != now.strftime("%Y-%m")


def should_push_quarter(state: dict, q: dict | None) -> bool:
    if q is None:
        return False
    return state.get("last_quarter_nudge") != q["quarter"]


def new_deadline_pushes(state: dict, today: str,
                        overdue: list[dict], due_soon: list[dict]) -> tuple[list[dict], list[dict]]:
    """Filter out items already pushed today."""
    pushed = state.get("deadline_pushed", {})  # key "title" -> last_date_pushed
    def keep(e: dict) -> bool:
        return pushed.get(e["title"]) != today
    return [e for e in overdue if keep(e)], [e for e in due_soon if keep(e)]


# --- main --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", default=True,
                    help="Single tick and exit (default).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print would-push messages; no Telegram, no state mutation.")
    ap.add_argument("--force-monday", action="store_true",
                    help="Send Monday check-in regardless of day/hour.")
    ap.add_argument("--force-friday", action="store_true",
                    help="Send Friday retro regardless of day/hour.")
    args = ap.parse_args()

    now = dt.datetime.now()
    today_iso = now.date().isoformat()
    report = run_check()
    if report is None:
        log("aborting: goals.py check failed")
        return 1

    state = load_json(STATE, {"deadline_pushed": {}})

    pushes: list[tuple[str, str]] = []  # (reason, message)

    # 1. Deadlines (every tick).
    fresh_overdue, fresh_due_soon = new_deadline_pushes(
        state, today_iso, report.get("overdue", []), report.get("due_soon", [])
    )
    if fresh_overdue or fresh_due_soon:
        pushes.append(("deadlines", build_deadline_msg(fresh_overdue, fresh_due_soon)))

    # 2. Monday check-in.
    if args.force_monday or should_push_monday(now, state):
        snap = run_list()
        if snap is not None:
            pushes.append(("monday", build_monday_msg(snap)))

    # 3. Friday retro.
    if args.force_friday or should_push_friday(now, state):
        snap = run_list()
        if snap is not None:
            pushes.append(("friday", build_friday_msg(snap, report.get("stale_rocks", []))))

    # 4. Revenue nudge.
    missing = report.get("revenue_missing", [])
    if should_push_revenue(now, state, missing):
        pushes.append(("revenue", build_revenue_msg(missing)))

    # 5. Quarter closing.
    q = report.get("quarter_ending")
    if should_push_quarter(state, q):
        pushes.append(("quarter", build_quarter_msg(q)))

    if not pushes:
        log("nothing to push")
        return 0

    for reason, msg in pushes:
        if args.dry_run:
            log(f"[dry-run] would push ({reason}): {msg.splitlines()[0]}")
            continue
        rc = push_to_telegram(msg, log=log)
        if rc != 0:
            log(f"push failed ({reason}) rc={rc}; not advancing state for this item")
            continue
        log(f"pushed ({reason}): {msg.splitlines()[0]}")
        # Advance state per reason.
        if reason == "deadlines":
            d = state.setdefault("deadline_pushed", {})
            for e in fresh_overdue + fresh_due_soon:
                d[e["title"]] = today_iso
        elif reason == "monday":
            state["last_monday_checkin"] = today_iso
        elif reason == "friday":
            state["last_friday_retro"] = today_iso
        elif reason == "revenue":
            state["last_revenue_nudge_month"] = now.strftime("%Y-%m")
        elif reason == "quarter" and q is not None:
            state["last_quarter_nudge"] = q["quarter"]

    if not args.dry_run:
        atomic_write_json(STATE, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
