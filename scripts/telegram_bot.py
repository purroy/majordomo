#!/usr/bin/env python3
"""PA Telegram bot - long-poll forwarding owner's messages to `claude -p`.

Reads `PA-telegram-bot-token` and `PA-telegram-chat-id` via `get_secret`
(env var on Linux/Docker, macOS Keychain otherwise).

Only responds to the whitelisted chat_id. Conversations are persistent
per chat: messages within a 24-hour window share a single Claude session,
so the bot remembers what was just discussed. After 24h of silence, the
next message starts a fresh session. `/reset` forces a new session
immediately.

Voice / audio messages are downloaded, transcribed locally with
whisper.cpp via scripts/transcribe.py, and the resulting text is fed
into the same conversational flow as a regular text message. The user
sees a short "(audio: <transcript>)" preview followed by the bot's
reply, so they can spot misrecognitions.

Per-chat state lives in .telegram_state.json under `chats[chat_id]`:
  { session_id, last_at } (epoch seconds).

Special commands (not forwarded to Claude):
  /ping    - immediate "pong" reply.
  /reset   - drop the current session id; next message starts fresh.

Runs via systemd (Linux) or launchd (macOS), or directly:
  python3 scripts/telegram_bot.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
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

# Conversation persistence: reuse the same claude session for a chat
# while messages are within this window. Past it, the next message
# rolls over to a fresh session_id (avoids unbounded context growth).
SESSION_TTL_S = 24 * 3600

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


# --- Voice / audio handling ------------------------------------------------

# message keys Telegram uses for audio-bearing payloads, in priority order.
# `voice` = recorded voice note (.oga). `audio` = music/audio file.
# `video_note` = circular video; the audio track is extracted by ffmpeg.
AUDIO_KEYS = ("voice", "audio", "video_note")
TRANSCRIBE_PY = REPO_DIR / "scripts" / "transcribe.py"


def find_audio(msg: dict) -> tuple[str, dict] | None:
    for k in AUDIO_KEYS:
        v = msg.get(k)
        if v and v.get("file_id"):
            return k, v
    return None


def download_telegram_file(token: str, file_id: str, dst: Path) -> None:
    info = tg(token, "getFile", {"file_id": file_id}, timeout=30)
    if not info.get("ok"):
        raise RuntimeError(f"getFile failed: {info}")
    file_path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    with urllib.request.urlopen(url, timeout=120) as r, open(dst, "wb") as fh:
        while chunk := r.read(64 * 1024):
            fh.write(chunk)


def transcribe_audio(audio_path: Path) -> str:
    """Run scripts/transcribe.py and return the recognised text."""
    res = subprocess.run(
        ["python3", str(TRANSCRIBE_PY), str(audio_path), "auto"],
        capture_output=True, text=True, timeout=600,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"transcribe rc={res.returncode}: {res.stderr.strip()[:300]}"
        )
    return res.stdout.strip()


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


def session_for_chat(state: dict, chat_id: int) -> tuple[str, bool]:
    """Return (session_id, is_new) for this chat, rotating on TTL.

    Mutates state in-place; caller is responsible for persisting it.
    """
    chats = state.setdefault("chats", {})
    key = str(chat_id)
    entry = chats.get(key)
    now = int(time.time())
    if entry and now - int(entry.get("last_at", 0)) <= SESSION_TTL_S and entry.get("session_id"):
        return entry["session_id"], False
    new_id = str(uuid.uuid4())
    chats[key] = {"session_id": new_id, "last_at": now}
    return new_id, True


def touch_session(state: dict, chat_id: int) -> None:
    """Update last_at after a successful exchange."""
    entry = state.get("chats", {}).get(str(chat_id))
    if entry:
        entry["last_at"] = int(time.time())


def reset_session(state: dict, chat_id: int) -> None:
    state.get("chats", {}).pop(str(chat_id), None)


def ask_claude(prompt: str, session_id: str, is_new: bool) -> str:
    """Invoke claude --print, creating or resuming the chat's session.

    Default model: Sonnet (cheaper, fine for triage / short questions).
    If the prompt starts with `!opus ` Opus is used instead.

    Session continuity: `--session-id <id>` is a CREATE-only flag — the id
    cannot be reused for a second invocation. To continue, use
    `--resume <id>`. So:
      - First message of a chat (is_new=True): --session-id <new-uuid>
      - Subsequent messages:                   --resume <same-uuid>
    `--no-session-persistence` stays OFF so claude writes the transcript
    between invocations.
    """
    use_opus = prompt.startswith("!opus ")
    if use_opus:
        prompt = prompt[len("!opus "):]
    session_flag = ["--session-id", session_id] if is_new else ["--resume", session_id]
    cmd = [
        "claude", "--print",
        "--model", "opus" if use_opus else "sonnet",
        *session_flag,
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

    state = load_json(STATE, {"offset": 0, "chats": {}})
    state.setdefault("chats", {})
    log(f"Bot up. Allowed chat={allowed_chat} (persistent session, TTL {SESSION_TTL_S//3600}h)")

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

            # Voice / audio / video_note: download, transcribe, treat as text.
            if not text:
                audio = find_audio(msg)
                if audio:
                    kind, payload = audio
                    file_id = payload["file_id"]
                    duration = payload.get("duration", "?")
                    mime = payload.get("mime_type", "")
                    log(f"<- [audio:{kind} dur={duration}s mime={mime}] file_id={file_id}")
                    send_typing(token, chat_id)
                    try:
                        suffix = "." + (mime.split("/")[-1] if "/" in mime else "ogg")
                        with tempfile.NamedTemporaryFile(prefix="pa-tg-", suffix=suffix, delete=False) as fh:
                            tmp_audio = Path(fh.name)
                        try:
                            download_telegram_file(token, file_id, tmp_audio)
                            text = transcribe_audio(tmp_audio)
                        finally:
                            try:
                                tmp_audio.unlink()
                            except OSError:
                                pass
                    except Exception as e:
                        log(f"transcription failed: {e}")
                        send_message(token, chat_id,
                                     f"⚠ No pude transcribir el audio: {str(e)[:200]}")
                        state["offset"] = update_id
                        atomic_write_json(STATE, state)
                        continue
                    if not text:
                        send_message(token, chat_id,
                                     "⚠ El audio se transcribió vacío. Prueba a hablar más alto o más cerca.")
                        state["offset"] = update_id
                        atomic_write_json(STATE, state)
                        continue
                    log(f"transcribed: {text[:200]}")
                    send_message(token, chat_id, f"(audio) {text}")
                else:
                    state["offset"] = update_id
                    atomic_write_json(STATE, state)
                    continue

            if text == "/ping":
                if send_message(token, chat_id, "pong"):
                    state["offset"] = update_id
                    atomic_write_json(STATE, state)
                continue

            if text == "/reset":
                reset_session(state, chat_id)
                state["offset"] = update_id
                atomic_write_json(STATE, state)
                send_message(token, chat_id, "Sesión reiniciada. El próximo mensaje empieza de cero.")
                log(f"session reset for chat {chat_id}")
                continue

            session_id, is_new = session_for_chat(state, chat_id)
            atomic_write_json(STATE, state)  # persist new session_id before the call
            log(f"<- [{'NEW' if is_new else 'CONT'} {session_id[:8]}] {text[:200]}")
            send_typing(token, chat_id)
            reply = ask_claude(text, session_id, is_new)
            log(f"-> {reply[:200]}")
            # Telegram does not render GitHub-flavoured markdown; flatten.
            delivered = send_message(token, chat_id, md_to_text(reply))
            if delivered:
                touch_session(state, chat_id)
                state["offset"] = update_id
                atomic_write_json(STATE, state)
            else:
                # Leave offset as-is so the next poll cycle retries this update.
                log(f"delivery failed for update {update_id}; will retry")
                break


if __name__ == "__main__":
    sys.exit(main())
