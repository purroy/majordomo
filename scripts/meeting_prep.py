#!/usr/bin/env python3
"""Meeting prep — push a dossier to Telegram before external meetings.

Every tick (cron, every 30 min during work hours) asks Claude — which has
the Google Calendar MCP connector, same as the briefings — for events
starting in the next 60–100 minutes that include attendees from outside
the owner's domains. For each new one it builds a short dossier (who they
are, last mail exchanged with them, open follow-ups, related goals) and
pushes it.

State (.meeting_prep_state.json) dedupes by event id so each meeting is
prepped once. The Claude call is gated in Python (work hours, weekday) so
idle ticks cost nothing.

Config via get_secret:
    PA-own-domains   comma-separated owner domains (default: domain of the
                     default account's user).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import telegram_api as tg_api
from _mail import MailConfig, get_secret, list_accounts
from watcher_base import (
    Logger,
    atomic_write_json,
    html_escape,
    load_json,
    run_claude,
)

REPO_DIR = Path(__file__).resolve().parent.parent
STATE = REPO_DIR / ".meeting_prep_state.json"
log = Logger(REPO_DIR / "briefings" / "meeting_prep.log")

CLAUDE_TIMEOUT_S = 420
WORK_START, WORK_END = 7, 20   # local hours; outside this, ticks are free
LOOKAHEAD_MIN = (60, 100)      # prep window before the meeting
STATE_KEEP_DAYS = 7


def own_domains() -> list[str]:
    raw = get_secret("own-domains", default="")
    if raw.strip():
        return [d.strip().lower() for d in raw.split(",") if d.strip()]
    try:
        user = MailConfig.load(list_accounts()[0]).user
        return [user.split("@", 1)[-1].lower()]
    except Exception:
        return []


def build_prompt(domains: list[str], skip_ids: list[str]) -> str:
    lo, hi = LOOKAHEAD_MIN
    skip = ", ".join(skip_ids) if skip_ids else "(ninguno)"
    return f"""Tarea autónoma de PA: preparación de reuniones.

1. Carga la tool de calendario si hace falta (ToolSearch: select:mcp__claude_ai_Google_Calendar__list_events) y lista los eventos de HOY del calendario principal.
2. Filtra los que empiezan entre {lo} y {hi} minutos a partir de ahora y tienen al menos un asistente cuyo email NO pertenece a estos dominios propios: {", ".join(domains) or "(desconocidos: considera externo a cualquiera que no sea el owner)"}.
3. Descarta los eventos con estos ids ya preparados: {skip}
4. Si no queda ninguno, responde EXACTAMENTE `NONE` y termina (no uses más herramientas).
5. Para cada evento restante, prepara un dossier breve:
   - Título, hora, asistentes externos (nombre y empresa si se deduce del dominio).
   - Último intercambio de mail con cada asistente externo: `python3 scripts/mail_fetch.py --account <id> --from <email> --since 30d --limit 5` en las cuentas configuradas; lee con mail_read.py solo lo necesario (BODY.PEEK, nunca marcar leído).
   - Cabos sueltos: ¿hay correos suyos sin responder, o follow-ups pendientes? (`.followup_state.json` si existe).
   - Si el tema conecta con goals/rocks (`python3 scripts/goals.py list` si existe goals.local.md), una línea.
6. Responde SOLO con JSON válido:
{{"prepped": [{{"event_id": "...", "title": "...", "start": "HH:MM", "dossier": "<dossier en castellano, 5-12 líneas, texto plano>"}}]}}

Reglas: no envíes nada a Telegram tú mismo; no marques correos como leídos; no inventes datos — si no hay historial con un asistente, dilo en una línea."""


def render(p: dict) -> str:
    return "\n".join([
        f"<b>📋 Reunión a las {html_escape(str(p.get('start', '?')))} — "
        f"{html_escape(str(p.get('title', '')))}</b>",
        "",
        html_escape(str(p.get("dossier", "")).strip()),
    ])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Run Claude but do not push or persist state.")
    ap.add_argument("--force", action="store_true",
                    help="Ignore the work-hours gate.")
    args = ap.parse_args()

    now = dt.datetime.now()
    if not args.force and not (WORK_START <= now.hour < WORK_END
                               and now.weekday() < 6):
        return 0  # silent free tick outside work hours

    state = load_json(STATE, {"prepped": {}})
    prepped = state.setdefault("prepped", {})
    # Prune old event ids.
    cutoff = (now.date() - dt.timedelta(days=STATE_KEEP_DAYS)).isoformat()
    for eid in [e for e, d in prepped.items() if d < cutoff]:
        del prepped[eid]

    rc, stdout, stderr = run_claude(
        build_prompt(own_domains(), list(prepped)),
        timeout=CLAUDE_TIMEOUT_S,
    )
    if rc != 0:
        log(f"claude rc={rc}: {stderr.strip()[:200]}")
        return 1
    out = stdout.strip()
    if not out or out == "NONE":
        log("no upcoming external meetings to prep")
        return 0
    try:
        raw = out[out.index("{"):out.rindex("}") + 1]
        items = json.loads(raw).get("prepped", [])
    except (ValueError, json.JSONDecodeError) as e:
        log(f"parse failed ({e}): {out[:200]}")
        return 1

    today = now.date().isoformat()
    for p in items:
        eid = str(p.get("event_id", "")).strip()
        if not eid or eid in prepped:
            continue
        if args.dry_run:
            log(f"[dry-run] would push dossier for {eid}: {p.get('title', '')!r}")
            continue
        if tg_api.send_html(render(p), log=log):
            prepped[eid] = today
            log(f"pushed dossier for {eid}: {p.get('title', '')!r}")
        else:
            log(f"push failed for {eid}; will retry next tick")

    if not args.dry_run:
        atomic_write_json(STATE, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
