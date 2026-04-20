"""Shared IMAP/SMTP helpers for the PA.

Credentials are resolved in this order per secret key `<name>`:
  1. Env var `PA_<NAME>` (Linux / Docker / systemd).
  2. macOS Keychain entry `PA-<name>` (interactive macOS setup).
If neither is set, the caller gets a RuntimeError with the exact command to
add the missing secret.
"""
from __future__ import annotations

import email
import email.policy
import html as html_mod
import imaplib
import os
import re
import smtplib
import subprocess
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable


def _load_dotenv() -> None:
    """Merge repo-root .env into os.environ. Pre-existing env vars win (systemd, shell)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        os.environ.setdefault(k, v)


_load_dotenv()

IMAP_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def imap_date(date) -> str:
    """Return DD-Mon-YYYY (locale-independent) for IMAP SINCE/BEFORE."""
    return f"{date.day:02d}-{IMAP_MONTHS[date.month - 1]}-{date.year}"


def get_secret(name: str, default: str | None = None) -> str:
    """Read PA-<name> from env var PA_<NAME> (Linux/Docker) or macOS Keychain.

    Env var wins if set; otherwise falls back to `security` on macOS. If default
    is provided, use it on miss.
    """
    env_key = "PA_" + name.replace("-", "_").upper()
    if env_key in os.environ:
        return os.environ[env_key]
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-a", _user(), "-s", f"PA-{name}", "-w"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().rstrip("\n")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        if default is not None:
            return default
        raise RuntimeError(
            f"Missing secret PA-{name}. Set env var {env_key} or add to Keychain: "
            f"security add-generic-password -U -a \"$USER\" -s \"PA-{name}\" -w \"<value>\""
        ) from e


def _user() -> str:
    return os.environ.get("USER") or subprocess.check_output(["whoami"]).decode().strip()


DEFAULT_ACCOUNT = "example"


def list_accounts() -> list[str]:
    """Return configured account ids.

    Reads PA-mail-accounts (comma-separated) if present; otherwise returns
    the single DEFAULT_ACCOUNT.
    """
    raw = get_secret("mail-accounts", default="")
    if raw.strip():
        return [a.strip() for a in raw.split(",") if a.strip()]
    return [DEFAULT_ACCOUNT]


@dataclass
class MailConfig:
    account: str
    user: str
    password: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    from_name: str = ""

    @property
    def from_header(self) -> str:
        if self.from_name:
            return f'"{self.from_name}" <{self.user}>'
        return self.user

    @classmethod
    def load(cls, account: str = DEFAULT_ACCOUNT) -> "MailConfig":
        prefix = f"mail-{account}"
        return cls(
            account=account,
            user=get_secret(f"{prefix}-user"),
            password=get_secret(f"{prefix}-pass"),
            imap_host=get_secret(f"{prefix}-imap-host"),
            imap_port=int(get_secret(f"{prefix}-imap-port")),
            smtp_host=get_secret(f"{prefix}-smtp-host"),
            smtp_port=int(get_secret(f"{prefix}-smtp-port")),
            from_name=get_secret(f"{prefix}-from-name", default=""),
        )


IMAP_TIMEOUT_S = 30


def imap_connect(cfg: MailConfig | None = None) -> imaplib.IMAP4_SSL:
    cfg = cfg or MailConfig.load()
    # Without timeout, a half-open TCP / stalled server hangs indefinitely
    # and systemd has to SIGKILL the watcher.
    conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=IMAP_TIMEOUT_S)
    conn.login(cfg.user, cfg.password)
    return conn


def smtp_connect(cfg: MailConfig | None = None) -> smtplib.SMTP:
    """Connect to SMTP. Uses SMTPS (implicit TLS) on port 465; STARTTLS otherwise."""
    cfg = cfg or MailConfig.load()
    if cfg.smtp_port == 465:
        conn = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30)
    else:
        conn = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
        conn.starttls()
    conn.login(cfg.user, cfg.password)
    return conn


def _quote_folder(folder: str) -> str:
    # IMAP requiere comillas si el nombre contiene espacios u otros chars
    # especiales. Citar siempre es seguro.
    return '"' + folder.replace('\\', '\\\\').replace('"', '\\"') + '"'


def select_folder(conn: imaplib.IMAP4_SSL, folder: str = "INBOX") -> int:
    typ, data = conn.select(_quote_folder(folder), readonly=False)
    if typ != "OK":
        raise RuntimeError(f"Cannot select folder {folder}: {data!r}")
    return int(data[0])


def search_uids(
    conn: imaplib.IMAP4_SSL,
    *,
    unread: bool = False,
    since: str | None = None,
    before: str | None = None,
    from_addr: str | None = None,
) -> list[int]:
    """Return matching UIDs (ascending order = oldest first).

    Criteria are combined into a single IMAP search string so imaplib doesn't
    quote each one as a literal (which some servers reject).
    """
    parts: list[str] = []
    if unread:
        parts.append("UNSEEN")
    if since:
        parts.append(f"SINCE {since}")  # DD-Mon-YYYY
    if before:
        parts.append(f"BEFORE {before}")
    if from_addr:
        parts.append(f'FROM "{from_addr}"')
    crit = " ".join(parts) if parts else "ALL"
    typ, data = conn.uid("SEARCH", None, crit)
    if typ != "OK":
        raise RuntimeError(f"IMAP SEARCH failed: {data!r}")
    raw = (data[0] or b"").decode().strip()
    if not raw:
        return []
    return [int(x) for x in raw.split()]


def fetch_message(conn: imaplib.IMAP4_SSL, uid: int) -> email.message.EmailMessage:
    typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if typ != "OK" or not data or not data[0]:
        raise RuntimeError(f"FETCH UID {uid} failed: {data!r}")
    raw = data[0][1]
    return email.message_from_bytes(raw, policy=email.policy.default)


def fetch_envelope(
    conn: imaplib.IMAP4_SSL, uids: Iterable[int]
) -> dict[int, email.message.EmailMessage]:
    """Fetch only headers (faster than full BODY)."""
    out: dict[int, email.message.EmailMessage] = {}
    for uid in uids:
        typ, data = conn.uid(
            "FETCH",
            str(uid),
            "(BODY.PEEK[HEADER] FLAGS)",
        )
        if typ != "OK" or not data:
            continue
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                msg = email.message_from_bytes(item[1], policy=email.policy.default)
                out[uid] = msg
                break
    return out


def text_body(msg: email.message.EmailMessage) -> str:
    """Best-effort text extraction. Prefers text/plain, falls back to HTML→text."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not _is_attachment(part):
                return part.get_content().strip()
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not _is_attachment(part):
                return _html_to_text(part.get_content())
        return ""
    ct = msg.get_content_type()
    if ct == "text/plain":
        return msg.get_content().strip()
    if ct == "text/html":
        return _html_to_text(msg.get_content())
    return ""


def list_attachments(msg: email.message.EmailMessage) -> list[dict]:
    out = []
    if not msg.is_multipart():
        return out
    for part in msg.walk():
        if _is_attachment(part):
            payload = part.get_payload(decode=True) or b""
            out.append({
                "filename": part.get_filename() or "",
                "mime": part.get_content_type(),
                "size": len(payload),
            })
    return out


def _is_attachment(part: email.message.EmailMessage) -> bool:
    cd = (part.get("Content-Disposition") or "").lower()
    return cd.startswith("attachment") or bool(part.get_filename())


def _html_to_text(html: str) -> str:
    # Lightweight: strip tags, decode entities, collapse whitespace. No external deps.
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def snippet(text: str, limit: int = 180) -> str:
    s = re.sub(r"\s+", " ", text).strip()
    return s[: limit - 1] + "…" if len(s) > limit else s
