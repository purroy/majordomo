"""Telegram Bot API helpers shared by the bot daemon and the watchers.

`telegram_send.sh` stays as the plain-text push path for shell callers.
This module is for Python callers that need richer features, mainly
inline keyboards (action buttons) and message edits.

Buttons are passed as rows of (label, callback_data) tuples:

    send_html("<b>hola</b>", buttons=[[("Archivar", "m:arch:kiwop:123")]])

callback_data must stay under Telegram's 64-byte limit; keep payloads to
short codes like `m:arch:<account>:<uid>` and resolve anything bigger
through a state file keyed by a short id.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mail import get_secret  # side effect: auto-loads .env

CHUNK = 4000  # Telegram hard limit is 4096


def creds() -> tuple[str, int]:
    """Return (bot_token, owner_chat_id) from env/Keychain."""
    return get_secret("telegram-bot-token"), int(get_secret("telegram-chat-id"))


def call(token: str, method: str, params: dict | None = None,
         timeout: int = 30) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def split_at_lines(text: str, chunk: int = CHUNK) -> list[str]:
    """Split at line boundaries so HTML tags (balanced per line) survive.

    Single lines longer than the limit hard-split as a fallback.
    """
    chunks: list[str] = []
    cur = ""
    for ln in text.split("\n"):
        while len(ln) > chunk:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(ln[:chunk])
            ln = ln[chunk:]
        if cur and len(cur) + 1 + len(ln) > chunk:
            chunks.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        chunks.append(cur)
    return chunks or [""]


def keyboard(buttons: list[list[tuple[str, str]]]) -> str:
    """Encode rows of (label, callback_data) as reply_markup JSON."""
    return json.dumps({
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in buttons
        ]
    })


def send_html(text: str, buttons: list[list[tuple[str, str]]] | None = None,
              *, token: str | None = None, chat_id: int | None = None,
              log=None) -> bool:
    """Send HTML text, chunked at line boundaries. Buttons go on the last chunk.

    Returns True iff every chunk was delivered.
    """
    if token is None or chat_id is None:
        token, chat_id = creds()
    chunks = split_at_lines(text)
    ok = True
    for i, ch in enumerate(chunks):
        params = {
            "chat_id": chat_id,
            "text": ch,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if buttons and i == len(chunks) - 1:
            params["reply_markup"] = keyboard(buttons)
        try:
            resp = call(token, "sendMessage", params)
            if not resp.get("ok"):
                raise RuntimeError(str(resp)[:200])
        except Exception as e:
            if log:
                log(f"telegram_api send_html failed: {e}")
            ok = False
    return ok


def answer_callback(token: str, callback_id: str, text: str = "") -> None:
    """Ack a button press (stops the client-side spinner). Best-effort."""
    try:
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
        call(token, "answerCallbackQuery", params)
    except Exception:
        pass


def edit_html(token: str, chat_id: int, message_id: int, text: str,
              buttons: list[list[tuple[str, str]]] | None = None) -> bool:
    """Replace a message's text (and keyboard). Used to mark actions done."""
    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if buttons is not None:
        params["reply_markup"] = keyboard(buttons)
    try:
        resp = call(token, "editMessageText", params)
        return bool(resp.get("ok"))
    except Exception:
        return False
