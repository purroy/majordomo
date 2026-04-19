#!/usr/bin/env python3
"""Janitor: mueve a Trash los reports DMARC/Aggregate/TLS benignos del INBOX.

Patrón "es report" (deben cumplirse AMBAS):
  1. Subject: "Report Domain", "Aggregate Report", "TLS Report", "DMARC",
     "SMTP TLS Reporting", o sender de domain abuse.
  2. From: emisor AUTOMÁTICO conocido (dmarc-noreply@google, postmaster@,
     abuse-report@, noreply@*report*, etc.). Si el From es una persona
     reenviándote un report (típico cliente: "he rebut això, he de fer algo?"),
     NO se considera report — es una pregunta importante.

Patrón "es benigno" (lo movemos a Trash):
  - Cuerpo NO contiene "fail", "reject", "quarantine", "policy violation",
    "forensic", "incident", "breach".
  - O sin cuerpo (solo adjunto XML — típico DMARC, todos suelen ser pass).

Si NO es benigno, lo deja en INBOX para que aparezca en el briefing.

Uso:
  mail_clean_reports.py            # vivo (mueve a Trash)
  mail_clean_reports.py --dry-run  # solo reporta
  mail_clean_reports.py --days 14 --limit 50

Salida JSON con {checked, trashed, kept}.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys

from _mail import (
    MailConfig,
    fetch_envelope,
    fetch_message,
    imap_connect,
    imap_date,
    list_accounts,
    search_uids,
    select_folder,
    text_body,
)

REPORT_SUBJECT = re.compile(
    r"(report[\s_-]+domain|aggregate\s+report|tls\s+report|"
    r"\bdmarc\b|smtp\s+tls\s+reporting?)",
    re.I,
)
# From debe ser claramente un emisor automático de reports.
# Evita falsos positivos como un cliente reenviando un DMARC.
# Cubre cualquier orden: dmarc-noreply / noreply-dmarc / dmarc-report / etc.
REPORT_FROM = re.compile(
    r"("
    # local part con 'dmarc' + automation marker en cualquier orden
    r"[\w.+-]*dmarc[\w.+-]*@|"
    r"[\w.+-]*(no?reply|noreply)[\w.+-]*dmarc[\w.+-]*@|"
    # postmaster en proveedores grandes
    r"postmaster@([\w.-]*\.)?"
    r"(google|outlook|microsoft|yahoo|protonmail|amazonses|amazonaws|hotmail|live|fastmail)|"
    # abuse / tls report aliases
    r"abuse[-_]?report@|"
    r"tls[-_]?report@|"
    r"noreply[-_.]*tls@"
    r")",
    re.I,
)
# Indicios de "Fwd:" o reenvío manual → no es un report directo
FORWARD_HINT = re.compile(r"^\s*(fw|fwd|rv|re):\s", re.I)
DANGER = re.compile(
    r"\b(fail(ed|ure)?|rejected?|quarantined?|policy[\s_]?violation|"
    r"forensic|incident|breach)\b",
    re.I,
)


def is_report(subject: str, frm: str) -> bool:
    """Reporte automático directo (no reenviado por humano)."""
    if FORWARD_HINT.match(subject):
        return False
    return bool(REPORT_SUBJECT.search(subject) and REPORT_FROM.search(frm))


def is_benign(body: str) -> bool:
    if not body or len(body.strip()) < 30:
        return True  # solo adjunto XML / cuerpo trivial
    return not DANGER.search(body)


def clean_account(account: str, days: int, limit: int, dry_run: bool) -> dict:
    out = {
        "account": account,
        "checked": 0,
        "trashed": [],
        "kept": [],
        "error": None,
    }
    try:
        conn = imap_connect(MailConfig.load(account))
    except Exception as e:
        out["error"] = f"connect: {e}"
        return out
    try:
        select_folder(conn, "INBOX")
        since = imap_date(dt.date.today() - dt.timedelta(days=days))
        all_uids = search_uids(conn, since=since)[-limit:]
        envs = fetch_envelope(conn, all_uids)
        candidates = []
        for uid in all_uids:
            msg = envs.get(uid)
            if not msg:
                continue
            subj = str(msg.get("Subject", ""))
            frm = str(msg.get("From", ""))
            if is_report(subj, frm):
                candidates.append((uid, subj, frm))
        out["checked"] = len(candidates)
        for uid, subj, frm in candidates:
            try:
                full = fetch_message(conn, uid)
            except Exception as e:
                out["kept"].append({"uid": uid, "subject": subj[:90],
                                    "reason": f"fetch error: {e}"})
                continue
            body = text_body(full)
            if is_benign(body):
                if not dry_run:
                    conn.uid("COPY", str(uid), "Trash")
                    conn.uid("STORE", str(uid), "+FLAGS", r"(\Deleted)")
                out["trashed"].append({"uid": uid, "subject": subj[:90]})
            else:
                out["kept"].append({"uid": uid, "subject": subj[:90],
                                    "reason": "signal de fallo/incidente"})
        if not dry_run and out["trashed"]:
            conn.expunge()
    except Exception as e:
        out["error"] = str(e)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=7,
                    help="Solo mira mensajes de los últimos N días")
    ap.add_argument("--limit", type=int, default=50,
                    help="Tope absoluto de mensajes por cuenta")
    ap.add_argument("--account", default=None,
                    help="Procesar solo esta cuenta (default: TODAS las configuradas)")
    args = ap.parse_args()

    accounts = [args.account] if args.account else list_accounts()
    payload = {
        "dry_run": args.dry_run,
        "accounts": [clean_account(a, args.days, args.limit, args.dry_run)
                     for a in accounts],
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
