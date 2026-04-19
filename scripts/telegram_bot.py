#!/usr/bin/env python3
"""PA Telegram bot — long-poll forwarding messages to `claude -p`.

Lee `PA-telegram-bot-token` y `PA-telegram-chat-id` vía `get_secret` del
_mail.py (env var PA_TELEGRAM_BOT_TOKEN / PA_TELEGRAM_CHAT_ID en Linux;
Keychain en macOS).

Solo responde al chat_id whitelisted. Cada mensaje es una llamada independiente
a `claude --print` (session-id efímero) — no hay memoria de conversación entre
mensajes. La memoria persistente del PA (en memory/) sí se mantiene.

Comandos especiales (no se envían a claude):
  /ping    — respuesta inmediata "pong".

Run via launchd (macOS), systemd (Linux), o directamente:
  python3 scripts/telegram_bot.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
STATE = REPO_DIR / ".telegram_state.json"
LOG_DIR = REPO_DIR / "briefings"
LOG = LOG_DIR / "telegram_bot.log"

sys.path.insert(0, str(REPO_DIR / "scripts"))
from _mail import get_secret  # side effect: auto-loads .env
try:
    from md_to_telegram import convert as md_to_text
except ImportError:
    def md_to_text(s: str) -> str:
        return s

POLL_TIMEOUT_S = 50
HTTP_TIMEOUT_S = POLL_TIMEOUT_S + 15
CLAUDE_TIMEOUT_S = 600
CHUNK = 4000  # Telegram hard limit is 4096


def log(msg: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    sys.stderr.write(line)
    sys.stderr.flush()


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except json.JSONDecodeError:
            pass
    return {"offset": 0}


def save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=2))


def tg(token: str, method: str, params: dict | None = None,
       timeout: int = 30) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        body = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=body)
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def send_message(token: str, chat_id: int, text: str,
                 parse_mode: str | None = None) -> None:
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
    for ch in chunks:
        params = {"chat_id": chat_id, "text": ch,
                  "disable_web_page_preview": "true"}
        if parse_mode:
            params["parse_mode"] = parse_mode
        try:
            tg(token, "sendMessage", params)
        except Exception as e:
            log(f"sendMessage failed: {e}")


def send_typing(token: str, chat_id: int) -> None:
    try:
        tg(token, "sendChatAction",
           {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


def ask_claude(prompt: str) -> str:
    """Invoke claude --print with a fresh ephemeral session per message.

    Modelo: Sonnet 4.6 por defecto (más permisivo con cuota; sobrado para
    triage / consultas cortas). Si el prompt empieza con `!opus ` se usa Opus.
    """
    use_opus = prompt.startswith("!opus ")
    if use_opus:
        prompt = prompt[len("!opus "):]
    cmd = [
        "claude", "--print",
        "--model", "opus" if use_opus else "sonnet",
        "--session-id", str(uuid.uuid4()),
        "--no-session-persistence",
        "--setting-sources", "project,user",
        "--output-format", "text",
        "--permission-mode", "bypassPermissions",
        prompt,
    ]
    try:
        out = subprocess.run(
            cmd, cwd=REPO_DIR, capture_output=True, text=True,
            timeout=CLAUDE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return "⚠ Tiempo agotado (>10 min). Prueba a partir el mensaje."
    except FileNotFoundError:
        return "⚠ `claude` CLI no está en PATH del daemon."
    if out.returncode != 0:
        tail = (out.stderr or out.stdout or "").strip()[-800:]
        return f"⚠ claude rc={out.returncode}:\n{tail}"
    return (out.stdout or "").strip() or "(sin respuesta)"


def main() -> int:
    try:
        token = get_secret("telegram-bot-token")
        allowed_chat = int(get_secret("telegram-chat-id"))
    except Exception as e:
        log(f"FATAL: secret read failed: {e}")
        return 2

    state = load_state()
    log(f"Bot up. Allowed chat={allowed_chat} (session efímera por mensaje)")

    while True:
        try:
            resp = tg(token, "getUpdates", {
                "offset": state["offset"] + 1,
                "timeout": POLL_TIMEOUT_S,
                "allowed_updates": json.dumps(["message"]),
            }, timeout=HTTP_TIMEOUT_S)
        except Exception as e:
            log(f"getUpdates error: {e}; retry in 5s")
            time.sleep(5)
            continue

        if not resp.get("ok"):
            log(f"Telegram API not ok: {resp}")
            time.sleep(5)
            continue

        for update in resp.get("result", []):
            state["offset"] = update["update_id"]
            save_state(state)

            msg = update.get("message") or update.get("edited_message")
            if not msg:
                continue
            chat_id = msg["chat"]["id"]
            text = (msg.get("text") or "").strip()

            if chat_id != allowed_chat:
                log(f"DROP from chat {chat_id}: {text[:80]}")
                continue
            if not text:
                continue

            if text == "/ping":
                send_message(token, chat_id, "pong")
                continue

            log(f"<- {text[:200]}")
            send_typing(token, chat_id)
            reply = ask_claude(text)
            log(f"-> {reply[:200]}")
            # Telegram no renderiza Markdown estilo GitHub.
            # Convertimos a texto plano legible.
            send_message(token, chat_id, md_to_text(reply))


if __name__ == "__main__":
    sys.exit(main())
