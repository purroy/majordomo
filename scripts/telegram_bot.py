#!/usr/bin/env python3
"""PA Telegram bot - long-poll forwarding owner's messages to `claude -p`.

Reads `PA-telegram-bot-token` and `PA-telegram-chat-id` via `get_secret`
(env var on Linux/Docker, macOS Keychain otherwise).

Only responds to the whitelisted chat_id. Each message is an independent
call to `claude --print` (ephemeral session-id) - no conversation memory
between messages; persistent PA memory in `~/.claude/projects/.../memory/`
is still available.

Special commands (not forwarded to Claude):
  /ping    - immediate "pong" reply.

Runs via systemd (Linux) or launchd (macOS), or directly:
  python3 scripts/telegram_bot.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
STATE = REPO_DIR / ".telegram_state.json"

sys.path.insert(0, str(REPO_DIR / "scripts"))
from _mail import get_secret  # side effect: auto-loads .env
from watcher_base import Logger, atomic_write_json, load_json
try:
    from md_to_telegram import convert as md_to_text
except ImportError:
    def md_to_text(s: str) -> str:
        return s

log = Logger(REPO_DIR / "briefings" / "telegram_bot.log")

POLL_TIMEOUT_S = 50
HTTP_TIMEOUT_S = POLL_TIMEOUT_S + 15
CLAUDE_TIMEOUT_S = 600
CHUNK = 4000  # Telegram message hard limit is 4096

# Exponential backoff caps for network failures against the Telegram API.
BACKOFF_START_S = 5
BACKOFF_MAX_S = 300


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
                 parse_mode: str | None = None) -> bool:
    """Send text (split into chunks). Returns True iff all chunks delivered."""
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
    ok = True
    for ch in chunks:
        params = {"chat_id": chat_id, "text": ch,
                  "disable_web_page_preview": "true"}
        if parse_mode:
            params["parse_mode"] = parse_mode
        try:
            tg(token, "sendMessage", params)
        except Exception as e:
            log(f"sendMessage failed: {e}")
            ok = False
    return ok


def send_typing(token: str, chat_id: int) -> None:
    try:
        tg(token, "sendChatAction",
           {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


def ask_claude(prompt: str) -> str:
    """Invoke claude --print with a fresh ephemeral session per message.

    Default model: Sonnet (cheaper, fine for triage / short questions).
    If the prompt starts with `!opus ` Opus is used instead.
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

    state = load_json(STATE, {"offset": 0})
    log(f"Bot up. Allowed chat={allowed_chat} (ephemeral session per message)")

    backoff = BACKOFF_START_S

    while True:
        try:
            resp = tg(token, "getUpdates", {
                "offset": state["offset"] + 1,
                "timeout": POLL_TIMEOUT_S,
                "allowed_updates": json.dumps(["message"]),
            }, timeout=HTTP_TIMEOUT_S)
            backoff = BACKOFF_START_S  # reset on success
        except Exception as e:
            log(f"getUpdates error: {e}; retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)
            continue

        if not resp.get("ok"):
            log(f"Telegram API not ok: {resp}")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)
            continue

        for update in resp.get("result", []):
            update_id = update["update_id"]

            msg = update.get("message") or update.get("edited_message")
            if not msg:
                state["offset"] = update_id
                atomic_write_json(STATE, state)
                continue
            chat_id = msg["chat"]["id"]
            text = (msg.get("text") or "").strip()

            if chat_id != allowed_chat:
                log(f"DROP from chat {chat_id}: {text[:80]}")
                state["offset"] = update_id
                atomic_write_json(STATE, state)
                continue
            if not text:
                state["offset"] = update_id
                atomic_write_json(STATE, state)
                continue

            if text == "/ping":
                if send_message(token, chat_id, "pong"):
                    state["offset"] = update_id
                    atomic_write_json(STATE, state)
                continue

            log(f"<- {text[:200]}")
            send_typing(token, chat_id)
            reply = ask_claude(text)
            log(f"-> {reply[:200]}")
            # Telegram does not render GitHub-flavoured markdown; flatten.
            delivered = send_message(token, chat_id, md_to_text(reply))
            if delivered:
                state["offset"] = update_id
                atomic_write_json(STATE, state)
            else:
                # Leave offset as-is so the next poll cycle retries this update.
                log(f"delivery failed for update {update_id}; will retry")
                break


if __name__ == "__main__":
    sys.exit(main())
