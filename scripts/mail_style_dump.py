#!/usr/bin/env python3
"""Vuelca correos enviados (último año por defecto) para analizar estilo.

Detecta automáticamente la carpeta de enviados (Sent, Sent Items, Enviados,
INBOX.Sent, [Gmail]/Sent Mail, etc.) y produce un JSON con cuerpos limpios:
sin firmas y sin texto citado del hilo previo.

Uso:
  mail_style_dump.py [--days 365] [--limit 500] [--folder NAME] [--out path.json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys

from _mail import (
    fetch_message,
    imap_connect,
    imap_date,
    search_uids,
    select_folder,
    text_body,
)

CANDIDATES = [
    "Sent",
    "Sent Items",
    "Sent Messages",
    "Enviados",
    "INBOX.Sent",
    "INBOX.Enviados",
    "[Gmail]/Sent Mail",
]

# Marcas habituales del bloque citado en respuestas
QUOTE_MARKERS = re.compile(
    r"^(?:"
    r"\s*El\s.+\sescribi[oó]:|"               # ES: El 12 abr 2026, X escribió:
    r"\s*On\s.+\swrote:|"                     # EN
    r"\s*De:\s.+|\s*From:\s.+|"               # forwarded headers
    r"\s*-{2,}\s?(Mensaje original|Original Message)\s?-{2,}|"
    r"\s*_+\s*$"                              # underline separators
    r")",
    re.IGNORECASE,
)
SIG_DELIM = re.compile(r"^-{2,}\s*$")  # "-- "


_LIST_RE = re.compile(
    r'^\([^)]*\)\s+(?:"[^"]*"|NIL)\s+(?:"((?:[^"\\]|\\.)*)"|(\S+))\s*$'
)


def find_sent_folder(conn, override: str | None) -> str:
    if override:
        # validate
        typ, _ = conn.select(override, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"No existe la carpeta {override!r}")
        return override
    typ, data = conn.list()
    if typ != "OK":
        raise RuntimeError("LIST failed")
    names: list[str] = []
    for raw in data or []:
        if not raw:
            continue
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        m = _LIST_RE.match(line)
        if m:
            names.append(m.group(1) or m.group(2))
    for cand in CANDIDATES:
        if cand in names:
            return cand
    # fallback: anything containing "sent" or "enviad"
    for n in names:
        ln = n.lower()
        if "sent" in ln or "enviad" in ln:
            return n
    raise RuntimeError(f"No encuentro carpeta de enviados. Disponibles: {names}")


def clean_body(body: str) -> str:
    """Trim signature and quoted reply block."""
    lines = body.splitlines()
    out: list[str] = []
    for line in lines:
        if SIG_DELIM.match(line):
            break  # signature begins
        if QUOTE_MARKERS.match(line):
            break  # quoted block begins
        if line.lstrip().startswith(">"):
            break  # quoted text marker
        out.append(line)
    text = "\n".join(out).strip()
    # collapse 3+ blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--folder", help="Forzar nombre de carpeta enviados")
    ap.add_argument("--out", default="-", help="Ruta de salida JSON (- = stdout)")
    args = ap.parse_args()

    conn = imap_connect()
    try:
        folder = find_sent_folder(conn, args.folder)
        select_folder(conn, folder)
        since = imap_date(dt.date.today() - dt.timedelta(days=args.days))
        uids = search_uids(conn, since=since)
        # most recent first, capped
        uids = uids[-args.limit:][::-1]
        out = []
        for uid in uids:
            try:
                msg = fetch_message(conn, uid)
            except Exception as e:
                out.append({"uid": uid, "error": str(e)})
                continue
            body = clean_body(text_body(msg))
            if not body:
                continue
            out.append({
                "uid": uid,
                "date": str(msg.get("Date", "")),
                "to": str(msg.get("To", "")),
                "subject": str(msg.get("Subject", "")),
                "body": body,
            })
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    payload = json.dumps(
        {"folder": folder, "count": len(out), "days": args.days, "messages": out},
        ensure_ascii=False, indent=2,
    )
    if args.out == "-":
        sys.stdout.write(payload + "\n")
    else:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(f"OK {args.out} ({len(out)} mensajes en {folder})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
