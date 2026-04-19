#!/usr/bin/env python3
"""Extract the most common signature per account by inspecting its Sent folder.

Heuristic:
  - Read the last N sent messages (default 60).
  - For each, locate the tail block after the real prose (explicit `-- `
    delimiter, mobile footer, or a sharp break into short contact-info lines).
  - Count the most repeated block. If it covers >=30% of samples, keep it.

Result: dict {account: signature_text}, written to stdout as JSON. With
`--save`, also writes a memory file the PA can read via CLAUDE.md's memory
directory.

Usage:
  extract_signatures.py [--account ID] [--limit 60] [--save]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

from _mail import (
    MailConfig,
    fetch_message,
    imap_connect,
    list_accounts,
    search_uids,
    select_folder,
    text_body,
)

SENT_CANDIDATES = ["Sent", "Sent Messages", "Sent Items", "Enviados",
                   "INBOX.Sent", "INBOX.Enviados", "[Gmail]/Sent Mail"]
SIG_DELIMS = re.compile(
    r"^\s*(--\s*$|Enviado desde mi iPhone|Sent from my iPhone|Sent from my Mobile)",
    re.I,
)
QUOTE_START = re.compile(
    r"^\s*(El\s.+\sescribi[oó]:|On\s.+\swrote:|De:\s|From:\s|"
    r"-{2,}\s?(Mensaje original|Original Message)\s?-{2,}|>\s)",
    re.I,
)


_LIST_RE = re.compile(
    r'^\([^)]*\)\s+(?:"[^"]*"|NIL)\s+(?:"((?:[^"\\]|\\.)*)"|(\S+))\s*$'
)


def find_sent(conn) -> str | None:
    typ, data = conn.list()
    names = []
    for raw in data or []:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        m = _LIST_RE.match(line)
        if m:
            names.append(m.group(1) or m.group(2))
    for c in SENT_CANDIDATES:
        if c in names:
            return c
    for n in names:
        ln = n.lower()
        if "sent" in ln or "enviad" in ln:
            return n
    return None


DEVICE_FOOTER = re.compile(
    r"^\s*(Enviado desde mi (iPhone|iPad|Android|m[oó]vil)|"
    r"Sent from my (iPhone|iPad|Android|Mobile|mobile))\s*$",
    re.I,
)


def extract_sig_block(body: str) -> str | None:
    """Return the signature block, or None if unclear.

    Ignores the mobile client footer ("Sent from my iPhone", etc.) — that
    is not a personal signature.
    """
    lines = body.splitlines()
    cut = len(lines)
    for i, ln in enumerate(lines):
        if QUOTE_START.match(ln):
            cut = i
            break
    lines = lines[:cut]
    while lines and DEVICE_FOOTER.match(lines[-1]):
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*--\s*$", ln):
            sig = "\n".join(lines[i + 1:i + 13]).strip()
            if 5 <= len(sig) <= 800 and not DEVICE_FOOTER.match(sig):
                return sig
    tail = "\n".join(lines[-8:]).strip()
    if re.search(r"^(gracias|gr[àa]cies|salud[oa]s|thanks|thank you|cheers|"
                 r"un cordial saludo|atentamente|best( regards)?)\b",
                 tail, re.I | re.M):
        return None
    has_contact = re.search(r"(www\.|https?://|tel\.?|m[oó]vil|mobile|"
                             r"@\w+\.[a-z]{2,}|\+\d{2,})", tail, re.I)
    if has_contact and len(tail) <= 300 and len(tail.split()) >= 3:
        return tail
    return None


def signature_for(account: str, limit: int) -> tuple[str, dict]:
    info = {"account": account, "candidates_seen": 0, "errors": None}
    try:
        conn = imap_connect(MailConfig.load(account))
    except Exception as e:
        info["errors"] = f"connect: {e}"
        return "", info
    try:
        sent = find_sent(conn)
        if not sent:
            info["errors"] = "no Sent folder"
            return "", info
        info["sent_folder"] = sent
        select_folder(conn, sent)
        uids = search_uids(conn)[-limit:]
        info["candidates_seen"] = len(uids)
        sigs: list[str] = []
        for uid in uids:
            try:
                msg = fetch_message(conn, uid)
            except Exception:
                continue
            sig = extract_sig_block(text_body(msg))
            if sig:
                sigs.append(sig)
        if not sigs:
            info["errors"] = "no signature blocks detected"
            return "", info
        # Normaliza para contar variantes (ignora trailing whitespace por línea)
        norm = ["\n".join(l.rstrip() for l in s.splitlines()).strip() for s in sigs]
        cnt = collections.Counter(norm)
        most, n = cnt.most_common(1)[0]
        info["confidence"] = round(n / len(sigs), 2)
        info["unique_blocks"] = len(cnt)
        if n / len(sigs) >= 0.3:
            return most, info
        info["errors"] = f"top sig only {n}/{len(sigs)} = below 30% threshold"
        return "", info
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--save", action="store_true",
                    help="Write to the Claude Code memory dir in addition to stdout")
    args = ap.parse_args()

    accounts = [args.account] if args.account else list_accounts()
    out = {}
    diag = {}
    for a in accounts:
        sig, info = signature_for(a, args.limit)
        out[a] = sig
        diag[a] = info

    json.dump({"signatures": out, "diagnostics": diag},
              sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")

    if args.save:
        # Claude Code stores per-project memory under ~/.claude/projects/<slug>/memory,
        # where <slug> is the repo's absolute path with slashes turned into hyphens.
        repo = Path(__file__).resolve().parent.parent
        slug = str(repo).replace("/", "-")
        memdir = Path(os.environ["HOME"]) / ".claude/projects" / slug / "memory"
        memdir.mkdir(parents=True, exist_ok=True)
        fp = memdir / "signatures.md"
        body = ["---",
                "name: Email signatures per account",
                "description: Per-account default signature. Only append to a draft when the owner asks.",
                "type: user", "---", ""]
        for a, sig in out.items():
            body.append(f"## {a}\n\n```\n{sig or '(not detected)'}\n```\n")
        fp.write_text("\n".join(body), encoding="utf-8")
        sys.stderr.write(f"saved to {fp}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
