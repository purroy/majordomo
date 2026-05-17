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
import os
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

# The Dev/-projects routing layer ("/proj" command, multi-slot sessions,
# preflight/postflight git ops) ships in a separate private repo
# (pa-dev-tools). It's optional: if the module is not on the Python path
# the bot still works for the default chat. Set PA_PROJECT_ROUTER_ENABLED=1
# in the systemd unit on the server to enable it; on the Mac it stays off
# so the two bots never edit the same working tree at once.
PROJECT_ROUTER_ENABLED = os.environ.get("PA_PROJECT_ROUTER_ENABLED") == "1"
pr = None
if PROJECT_ROUTER_ENABLED:
    try:
        import project_router as pr  # noqa: F401
    except ImportError as e:
        # Will be logged once the Logger is up; for now keep it quiet.
        PROJECT_ROUTER_ENABLED = False
        _pr_import_error = str(e)
    else:
        _pr_import_error = ""
else:
    _pr_import_error = ""

# Local slot constant so the rest of the bot does not depend on `pr`.
# Sessions are persisted under `chats[chat_id].sessions[slot]`. Without
# the router every message lives in the "default" slot.
DEFAULT_SLOT = "default"

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


def _migrate_legacy_state(chat_entry: dict) -> dict:
    """Move legacy `{session_id, last_at}` into `{sessions: {default: {...}}}`.

    Idempotent. Inlined so the bot does not depend on `project_router` for
    state-format upkeep — even with the routing layer absent we keep the
    new schema so future enables don't lose history.
    """
    if "sessions" in chat_entry:
        chat_entry.setdefault("active", DEFAULT_SLOT)
        return chat_entry
    legacy_sid = chat_entry.pop("session_id", None)
    legacy_at = chat_entry.pop("last_at", 0)
    chat_entry["active"] = DEFAULT_SLOT
    chat_entry["sessions"] = {}
    if legacy_sid:
        chat_entry["sessions"][DEFAULT_SLOT] = {
            "session_id": legacy_sid,
            "last_at": int(legacy_at or 0),
        }
    return chat_entry


def _chat_entry(state: dict, chat_id: int) -> dict:
    """Get-or-create the chat entry, applying legacy migration on first touch."""
    chats = state.setdefault("chats", {})
    key = str(chat_id)
    entry = chats.setdefault(key, {})
    return _migrate_legacy_state(entry)


def session_for_slot(state: dict, chat_id: int, slot: str) -> tuple[str, bool, str | None]:
    """Return (session_id, is_new, expired_session_id) for (chat, slot).

    Each slot has its own session and its own 24h TTL. Rolling over a slot
    only affects that slot — opening /proj <my-project> doesn't disturb the default
    chat session.
    """
    entry = _chat_entry(state, chat_id)
    sessions = entry.setdefault("sessions", {})
    now = int(time.time())
    s = sessions.get(slot)
    if s and now - int(s.get("last_at", 0)) <= SESSION_TTL_S and s.get("session_id"):
        return s["session_id"], False, None
    expired = s.get("session_id") if s else None
    new_id = str(uuid.uuid4())
    sessions[slot] = {"session_id": new_id, "last_at": now}
    return new_id, True, expired


def touch_slot(state: dict, chat_id: int, slot: str) -> None:
    entry = state.get("chats", {}).get(str(chat_id))
    if not entry:
        return
    s = entry.get("sessions", {}).get(slot)
    if s:
        s["last_at"] = int(time.time())


def reset_slot(state: dict, chat_id: int, slot: str) -> None:
    entry = state.get("chats", {}).get(str(chat_id))
    if not entry:
        return
    entry.get("sessions", {}).pop(slot, None)
    # If we just deleted the active slot, fall back to default.
    if entry.get("active") == slot and slot != DEFAULT_SLOT:
        entry["active"] = DEFAULT_SLOT


def reset_all_slots(state: dict, chat_id: int) -> None:
    entry = state.get("chats", {}).get(str(chat_id))
    if not entry:
        return
    entry["sessions"] = {}
    entry["active"] = DEFAULT_SLOT


def active_slot(state: dict, chat_id: int) -> str:
    entry = _chat_entry(state, chat_id)
    return entry.get("active", DEFAULT_SLOT)


def set_active_slot(state: dict, chat_id: int, slot: str) -> None:
    entry = _chat_entry(state, chat_id)
    entry["active"] = slot


# --- Memory-distill hook (TTL rollover) ------------------------------------

DISTILL_LOG = REPO_DIR / "briefings" / "telegram_distill.log"

# Prompt para el subproceso de extracción. Se inyecta como un nuevo turno
# de usuario en la sesión expirada (--resume). El modelo ve TODO el historial
# de la conversación que acaba de morir y aplica las reglas de auto-memoria
# que ya tiene en su system prompt (el harness de Claude Code las inyecta).
#
# El objetivo es cubrir el agujero estructural: dentro de UNA sesión de 24h,
# una rutina con cadencia >24h sólo aparece una vez y nunca parece "recurrente"
# para Sonnet. Este hook le da una pasada explícita al cerrar la sesión, con
# la instrucción inequívoca de mirar lo de los últimos días via memoria + git.
DISTILL_PROMPT = """Esta sesión va a cerrarse por inactividad (>24h sin mensajes). Antes de descartarla, repasa TODO el transcript de esta conversación con ojo de auto-memoria.

Aplica estrictamente las reglas del sistema de memoria que ya tienes en tu system prompt. En particular:
- Rutinas / actividades recurrentes del usuario (vocabulario, repaso, hábitos, deportes, lectura, lo que sea) → memoria tipo `user` o `project`, indicando dónde vive el estado real (archivo, herramienta) si lo hay.
- Preferencias confirmadas (de palabra o por aceptación implícita: el usuario aprobó algo no obvio) → memoria tipo `feedback` con **Why:** y **How to apply:**.
- Decisiones, contactos, datos del proyecto que no se derivan de leer el código → `project`.
- Punteros a sistemas externos mencionados → `reference`.

Antes de escribir, comprueba si ya existe una memoria parecida — actualiza en lugar de duplicar. NO escribas nada que sea trivial, derivable del repo, o ya esté cubierto por CLAUDE.md.

Importante:
- NO respondas al usuario. NO mandes Telegram. NO ejecutes herramientas que toquen mail, calendar o slack.
- Sólo Read/Write/Edit/Grep/Bash de lectura sobre el directorio de memoria. Si decides no guardar nada, está bien.

Output (a stdout, lo recoge el log del bot):
- Una línea por memoria creada o actualizada: `CREATE <file> — <razón>` o `UPDATE <file> — <razón>`.
- Si no hay nada que merezca guardarse: la palabra `NONE`.
"""


def kickoff_memory_distill(prev_session_id: str, slot: str = DEFAULT_SLOT) -> None:
    """Lanza en background `claude -p --resume <prev>` con el prompt de extracción.

    Non-blocking: el usuario no espera. Output redirigido a un log dedicado.
    `slot` decide el cwd, de modo que las memorias destiladas de una sesión
    de proyecto quedan bajo `~/.claude/projects/-home-pa-Dev-<slug>/memory/`
    y no contaminan la memoria del PA.
    """
    cwd = pr.cwd_for(slot) if (pr is not None and slot != DEFAULT_SLOT) else REPO_DIR
    # The slot directory may have been deleted/renamed between when the
    # session was created and now (>24h later). Without this guard the
    # Popen below raises FileNotFoundError and the distill is dropped
    # silently.
    if not cwd.is_dir():
        log(f"distill: slot dir {cwd} missing, falling back to REPO_DIR")
        cwd = REPO_DIR
    try:
        DISTILL_LOG.parent.mkdir(exist_ok=True)
        fh = open(DISTILL_LOG, "ab")
        fh.write(
            f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} distill session={prev_session_id} slot={slot} cwd={cwd} ===\n".encode()
        )
        fh.flush()
        subprocess.Popen(
            [
                "claude", "--print",
                "--model", "sonnet",
                "--resume", prev_session_id,
                "--no-session-persistence",
                "--setting-sources", "project,user",
                "--output-format", "text",
                "--permission-mode", "bypassPermissions",
                DISTILL_PROMPT,
            ],
            cwd=cwd,
            stdout=fh, stderr=subprocess.STDOUT,
            start_new_session=True,  # detach: sobrevive si el bot se reinicia
        )
        log(f"distill kicked off for session {prev_session_id[:8]} (slot={slot})")
    except FileNotFoundError:
        log("distill skipped: `claude` CLI not in PATH")
    except Exception as e:
        log(f"distill kickoff failed: {e}")


def ask_claude(
    prompt: str,
    session_id: str,
    is_new: bool,
    *,
    slot: str = DEFAULT_SLOT,
) -> str:
    """Invoke claude --print, creating or resuming the chat's session.

    Default model: Sonnet (cheaper, fine for triage / short questions).
    If the prompt starts with `!opus ` Opus is used instead.

    When `slot != default` the call runs with `cwd = DEV_ROOT/<slot>` and
    `--add-dir <PA repo>` so Claude can still read PA scripts and memory.
    """
    use_opus = prompt.startswith("!opus ")
    if use_opus:
        prompt = prompt[len("!opus "):]
    session_flag = ["--session-id", session_id] if is_new else ["--resume", session_id]
    if slot != DEFAULT_SLOT and pr is not None:
        cwd = pr.cwd_for(slot)
        extra_args = ["--add-dir", str(REPO_DIR)]
    else:
        cwd = REPO_DIR
        extra_args = []
    # Pass the prompt via stdin, not as a positional arg. Two reasons:
    #   - `--add-dir` is variadic in the claude CLI: anything that follows it
    #     until the next `--flag` is treated as another directory, so a
    #     positional prompt right after `--add-dir <dir>` gets swallowed and
    #     claude errors with "Input must be provided…".
    #   - Long prompts (we wrap the user request in a multi-line prefix that
    #     can be >1 KB) can hit ARG_MAX limits on some kernels.
    cmd = [
        "claude", "--print",
        "--model", "opus" if use_opus else "sonnet",
        *session_flag,
        "--setting-sources", "project,user",
        "--output-format", "text",
        "--permission-mode", "bypassPermissions",
        *extra_args,
    ]
    try:
        out = subprocess.run(
            cmd, cwd=cwd, input=prompt, capture_output=True, text=True,
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
    if PROJECT_ROUTER_ENABLED and pr is not None:
        router = f"router=on (DEV_ROOT={pr.DEV_ROOT})"
    elif _pr_import_error:
        router = f"router=off (import failed: {_pr_import_error})"
    else:
        router = "router=off"
    log(f"Bot up. Allowed chat={allowed_chat} (TTL {SESSION_TTL_S//3600}h, {router})")

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

            # /reset and /reset all (tolerant to extra whitespace / mixed case).
            reset_parts = text.split()
            if reset_parts and reset_parts[0].lower() == "/reset" and len(reset_parts) <= 2:
                arg = reset_parts[1].lower() if len(reset_parts) == 2 else ""
                if arg and arg != "all":
                    send_message(token, chat_id, "Uso: /reset  o  /reset all")
                    state["offset"] = update_id
                    atomic_write_json(STATE, state)
                    continue
                slot = active_slot(state, chat_id)
                if arg == "all":
                    reset_all_slots(state, chat_id)
                    note = "Todas las sesiones reiniciadas (default + proyectos)."
                else:
                    reset_slot(state, chat_id, slot)
                    landed = active_slot(state, chat_id)
                    if landed != slot:
                        note = f"Sesión «{slot}» reiniciada. Vuelta al slot «{landed}»."
                    else:
                        note = f"Sesión «{slot}» reiniciada. El próximo mensaje empieza de cero."
                state["offset"] = update_id
                atomic_write_json(STATE, state)
                send_message(token, chat_id, note)
                log(f"session reset for chat {chat_id} ({text!r})")
                continue

            # Project routing is opt-in: only if PA_PROJECT_ROUTER_ENABLED=1
            # AND the private `project_router` module is importable. Without
            # it, every message goes to the default chat exactly as the old
            # bot used to behave — including a `/proj …` typed by accident,
            # which we politely refuse below.
            if PROJECT_ROUTER_ENABLED and pr is not None:
                decision = pr.parse_command(text, active_slot=active_slot(state, chat_id))
            elif text.startswith("/proj"):
                send_message(
                    token, chat_id,
                    "El routing /proj no está disponible en este host. "
                    "Requiere el módulo project_router (repo privado).",
                )
                state["offset"] = update_id
                atomic_write_json(STATE, state)
                continue
            else:
                # Fall through to the legacy default-slot flow.
                decision = None

            # decision is None means "no router; treat as default-slot exec".
            if decision is not None and decision.kind == "list":
                projects = pr.discover_projects()
                send_message(token, chat_id, pr.render_project_list_html(projects), parse_mode="HTML")
                state["offset"] = update_id
                atomic_write_json(STATE, state)
                continue

            if decision is not None and decision.kind == "info":
                info = pr.project_info(decision.slot)
                if info is None:
                    send_message(token, chat_id, f"No encuentro «{decision.slot}» en {pr.DEV_ROOT}.")
                else:
                    set_active_slot(state, chat_id, decision.slot)
                    send_message(token, chat_id, pr.render_project_info_html(info), parse_mode="HTML")
                state["offset"] = update_id
                atomic_write_json(STATE, state)
                continue

            if decision is not None and decision.kind == "exit":
                set_active_slot(state, chat_id, DEFAULT_SLOT)
                state["offset"] = update_id
                atomic_write_json(STATE, state)
                send_message(token, chat_id, "Vuelta al chat default.")
                continue

            if decision is not None and decision.kind == "error":
                send_message(token, chat_id, decision.error)
                state["offset"] = update_id
                atomic_write_json(STATE, state)
                continue

            # Either decision is None (no router) or decision.kind == "exec".
            if decision is None:
                slot = DEFAULT_SLOT
                user_prompt = text
            else:
                slot = decision.slot
                user_prompt = decision.prompt

            preflight = None
            if slot != DEFAULT_SLOT:
                preflight = pr.preflight(slot)
                if not preflight.ok:
                    send_message(token, chat_id, f"⚠ {preflight.reason}")
                    state["offset"] = update_id
                    atomic_write_json(STATE, state)
                    log(f"preflight blocked for slot={slot}: {preflight.reason}")
                    continue
                # Lock in the active slot for follow-up messages.
                set_active_slot(state, chat_id, slot)

            session_id, is_new, expired = session_for_slot(state, chat_id, slot)
            atomic_write_json(STATE, state)  # persist new session_id before the call
            if expired:
                kickoff_memory_distill(expired, slot=slot)

            log(f"<- [{'NEW' if is_new else 'CONT'} {session_id[:8]} slot={slot}] {user_prompt[:200]}")
            send_typing(token, chat_id)

            if slot != DEFAULT_SLOT:
                full_prompt = pr.build_prompt_prefix(slot, user_prompt, branch=preflight.branch if preflight else "")
            else:
                full_prompt = user_prompt

            reply = ask_claude(full_prompt, session_id, is_new, slot=slot)
            log(f"-> {reply[:200]}")

            if slot != DEFAULT_SLOT:
                report = pr.postflight(slot, preflight.before_sha)
                out_html = pr.render_report_html(md_to_text(reply), report)
                delivered = send_message(token, chat_id, out_html, parse_mode="HTML")
            else:
                delivered = send_message(token, chat_id, md_to_text(reply))

            if delivered:
                touch_slot(state, chat_id, slot)
                state["offset"] = update_id
                atomic_write_json(STATE, state)
            else:
                # Leave offset as-is so the next poll cycle retries this update.
                log(f"delivery failed for update {update_id}; will retry")
                break


if __name__ == "__main__":
    sys.exit(main())
