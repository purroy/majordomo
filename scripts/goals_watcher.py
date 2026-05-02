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

from watcher_base import Logger, atomic_write_json, html_escape, load_json, push_to_telegram

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
# Todos los mensajes salen con parse_mode=HTML. Cualquier texto que venga del
# usuario (goals.local.md → area/title/progress/desc) pasa por html_escape.

# Orden estable para agrupar items por área (EOS: Cash → People → Execution → Strategy).
AREA_ORDER = ["Cash", "People", "Execution", "Strategy"]


def _area_key(area: str) -> tuple[int, str]:
    """Sort key: known areas en orden EOS, resto alfabético al final."""
    try:
        return (AREA_ORDER.index(area), "")
    except ValueError:
        return (len(AREA_ORDER), area.lower())


def _group_by_area(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """Agrupa preservando el orden estable de AREA_ORDER."""
    by_area: dict[str, list[dict]] = {}
    for it in items:
        by_area.setdefault(it["area"], []).append(it)
    return sorted(by_area.items(), key=lambda kv: _area_key(kv[0]))


def build_deadline_msg(overdue: list[dict], due_soon: list[dict]) -> str:
    total = len(overdue) + len(due_soon)
    lines = [f"<b>⏰ Goals — deadlines ({total})</b>"]
    if overdue:
        lines.append("")
        lines.append(f"<b>🔥 OVERDUE ({len(overdue)})</b>")
        for area, group in _group_by_area(overdue):
            lines.append("")
            lines.append(f"<b>{html_escape(area)}</b>")
            for e in group:
                lines.append(
                    f"• {html_escape(e['title'])} "
                    f"<i>(due {e['due']}, {abs(e['days_until'])}d late)</i>"
                )
    if due_soon:
        lines.append("")
        lines.append(f"<b>⚠ Due in ≤2 days ({len(due_soon)})</b>")
        for area, group in _group_by_area(due_soon):
            lines.append("")
            lines.append(f"<b>{html_escape(area)}</b>")
            for e in group:
                d = e["days_until"]
                when = "hoy" if d == 0 else ("mañana" if d == 1 else f"en {d}d")
                lines.append(f"• {html_escape(e['title'])} <i>({when}, {e['due']})</i>")
    lines.append("")
    lines.append("<i>Actualizar:</i> <code>/goals done &lt;match&gt;</code>")
    return "\n".join(lines)


def build_monday_msg(snapshot: dict) -> str:
    rocks = [it for it in snapshot.get("rocks", []) if not it["checked"]]
    q = snapshot.get("period", {}).get("quarter", "current quarter")
    lines = [f"<b>📅 Monday — rocks check-in</b>"]
    lines.append("")
    lines.append(f"<i>Open rocks · {html_escape(q)} ({len(rocks)})</i>")
    for area, group in _group_by_area(rocks):
        lines.append("")
        lines.append(f"<b>{html_escape(area)}</b>")
        for it in group:
            prog = it["attrs"].get("progress", "").strip(' "\'')
            if prog:
                lines.append(
                    f"• {html_escape(it['title'])}\n  <i>{html_escape(prog)}</i>"
                )
            else:
                lines.append(
                    f"• {html_escape(it['title'])}\n  <i>(sin progreso registrado)</i>"
                )
    lines.append("")
    lines.append("<i>¿Cuál es el rock foco de esta semana?</i>")
    lines.append("<code>/goals update &lt;match&gt; --progress \"...\"</code>")
    return "\n".join(lines)


def build_friday_msg(snapshot: dict, stale_rocks: list[dict]) -> str:
    today = dt.date.today()
    week_start = today - dt.timedelta(days=today.weekday())  # Monday
    done_this_week = [d for d in snapshot.get("done_log", [])
                      if d["date"] >= week_start.isoformat()]
    lines = ["<b>📅 Friday — weekly retro</b>"]
    lines.append("")
    if done_this_week:
        lines.append(f"<b>✅ Completado esta semana ({len(done_this_week)})</b>")
        for area, group in _group_by_area(done_this_week):
            lines.append("")
            lines.append(f"<b>{html_escape(area)}</b>")
            for d in group:
                lines.append(f"• {html_escape(d['desc'])} <i>({d['date']})</i>")
    else:
        lines.append("<i>Nada marcado como hecho esta semana.</i>")
    if stale_rocks:
        lines.append("")
        lines.append(f"<b>⏳ Rocks sin progreso ({len(stale_rocks)})</b>")
        for area, group in _group_by_area(stale_rocks):
            lines.append("")
            lines.append(f"<b>{html_escape(area)}</b>")
            for r in group:
                lines.append(f"• {html_escape(r['title'])}")
    lines.append("")
    lines.append("<i>Cierre de semana: ¿algún rock para mover antes del lunes?</i>")
    return "\n".join(lines)


def build_revenue_msg(missing_months: list[str]) -> str:
    if len(missing_months) == 1:
        m = missing_months[0]
        return (
            f"<b>💰 Revenue reminder</b>\n\n"
            f"Revenue de <b>{html_escape(m)}</b> sin rellenar.\n"
            f"<code>python3 scripts/goals.py revenue {html_escape(m)} &lt;amount&gt;</code>\n"
            f"<i>(o null si no hubo facturación)</i>"
        )
    months_str = ", ".join(html_escape(m) for m in missing_months)
    return (
        f"<b>💰 Revenue reminder</b>\n\n"
        f"Faltan: {months_str}\n"
        f"<code>python3 scripts/goals.py revenue YYYY-MM &lt;amount&gt;</code>"
    )


def build_quarter_msg(q: dict) -> str:
    return (
        f"<b>🏁 Quarter closing — {html_escape(q['quarter'])}</b>\n\n"
        f"Termina <b>{q['ends']}</b> ({q['days_until']}d).\n"
        f"Revisar rocks abiertos, marcar lo hecho y preparar los del siguiente trimestre."
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
