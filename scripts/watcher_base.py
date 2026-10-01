"""Shared utilities for PA watchers (mail, slack, telegram bot).

Extracts what was duplicated across the watchers:
  - atomic JSON state I/O (`.tmp` + rename; recoverable if killed mid-write)
  - log with size-based rotation (10 MB x 3 keep)
  - `claude --print` subprocess wrapper (timeout, ephemeral session id,
    no tools by default) plus a health check that warns on Telegram when
    Claude keeps failing
  - access to the PA's Claude Code memory files (triage rules, style)
  - Telegram push
  - `safe_wrap()`: XML-tag wrapping for untrusted data in prompts
    (prevents prompt injection via mail subjects / slack text)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent


# --- State files (atomic writes + corruption recovery) ----------------------

def atomic_write_json(path: Path, data: dict, *, indent: int = 2) -> None:
    """Write JSON atomically: write sibling `.tmp`, then rename.

    `os.replace` is atomic on POSIX: either the file is old or new, never
    truncated. Safe against SIGKILL / power loss mid-write.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False))
    os.replace(tmp, path)


def load_json(path: Path, default: dict) -> dict:
    """Load JSON; fall back to sibling `.tmp` if the main file is corrupt.

    Returns a fresh copy of `default` if both are missing or invalid.
    """
    for candidate in (path, path.with_suffix(path.suffix + ".tmp")):
        if not candidate.exists():
            continue
        try:
            return json.loads(candidate.read_text())
        except (json.JSONDecodeError, OSError):
            continue
    return dict(default)


# --- Logging ----------------------------------------------------------------

class Logger:
    """Append-only logger that rotates the file at `size_limit` bytes.

    Rotation keeps `keep` numbered backups (log.1, log.2, ...). Best-effort:
    never raises from __call__.
    """

    def __init__(
        self,
        path: Path,
        *,
        size_limit: int = 10 * 1024 * 1024,
        keep: int = 3,
    ):
        self.path = path
        self.size_limit = size_limit
        self.keep = keep

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.stat().st_size <= self.size_limit:
                return
        except FileNotFoundError:
            return
        for i in range(self.keep, 0, -1):
            src = self.path.with_suffix(self.path.suffix + f".{i}")
            dst = self.path.with_suffix(self.path.suffix + f".{i+1}")
            if src.exists():
                if i == self.keep:
                    try:
                        src.unlink()
                    except OSError:
                        pass
                else:
                    try:
                        src.rename(dst)
                    except OSError:
                        pass
        try:
            self.path.rename(self.path.with_suffix(self.path.suffix + ".1"))
        except OSError:
            pass

    def __call__(self, msg: str) -> None:
        try:
            self.path.parent.mkdir(exist_ok=True)
            self._rotate_if_needed()
        except OSError:
            pass
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass
        sys.stderr.write(line)
        sys.stderr.flush()


# --- Prompt safety (anti-injection wrapper) ---------------------------------

def safe_wrap(content: str, tag: str) -> str:
    """Wrap untrusted user content for Claude so it is treated as data.

    Any literal `</tag>` inside content is neutralised by inserting a
    zero-width space, preventing an attacker-controlled field (e.g. a
    subject line) from closing the tag and smuggling instructions.
    """
    closer = f"</{tag}>"
    # Zero-width space inside the closer defeats tag-breakout without
    # visually corrupting the content Claude reads.
    safe = content.replace(closer, closer.replace("/", "/\u200b"))
    return f"<{tag}>{safe}</{tag}>"


# --- Claude Code memory ----------------------------------------------------

def memory_dir() -> Path:
    """Claude Code's per-project memory dir for this repo.

    Claude Code keys it by the repo's absolute path with slashes turned
    into hyphens: ~/.claude/projects/<slug>/memory.
    """
    slug = str(REPO_DIR).replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug / "memory"


def read_memory(name: str) -> str:
    """Body of a memory file (frontmatter stripped), or "" if missing.

    Watchers call Claude without tools, so anything the model must know
    from memory (triage rules, writing style) is inlined by the caller.
    """
    try:
        text = (memory_dir() / name).read_text(encoding="utf-8")
    except OSError:
        return ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


# --- Claude subprocess ------------------------------------------------------

HEALTH = REPO_DIR / ".claude_health.json"
# Consecutive failures before warning the owner (the mail watcher runs
# every 15 min, so 3 = ~45 min of a dead pipeline), and how often to
# repeat the warning while it stays broken.
HEALTH_ALERT_AFTER = 3
HEALTH_REALERT_S = 24 * 3600


def run_claude(
    prompt: str,
    *,
    model: str = "sonnet",
    timeout: int = 240,
    cwd: Path = REPO_DIR,
    tools: bool = False,
) -> tuple[int, str, str]:
    """Invoke `claude --print` with an ephemeral session.

    Returns (returncode, stdout, stderr). Maps timeout → 124 and missing CLI
    → 127 so callers can branch without catching exceptions.

    `tools=False` (the default) runs the model with no tools and no MCP
    servers: it only reads the prompt and writes text. Use it whenever the
    prompt carries untrusted content (mail bodies, subjects): a model with
    a shell that reads attacker-written text can be talked into running
    commands, e.g. sending mail. Pass `tools=True` only for jobs that need
    connectors and never see raw external content.
    """
    # Resolve the binary explicitly: cron runs with a minimal PATH that
    # does not include /usr/local/bin, where the CLI lives on the server.
    claude_bin = (
        shutil.which("claude")
        or next((p for p in ("/usr/local/bin/claude", "/usr/bin/claude")
                 if Path(p).exists()), "claude")
    )
    cmd = [
        claude_bin, "--print",
        "--model", model,
        "--session-id", str(uuid.uuid4()),
        "--no-session-persistence",
        "--setting-sources", "project,user",
        "--output-format", "text",
    ]
    if tools:
        cmd += ["--permission-mode", "bypassPermissions"]
    else:
        cmd += ["--tools", "", "--strict-mcp-config"]
    try:
        # Prompt via stdin: mail bodies make prompts long enough to worry
        # about ARG_MAX, and stdin needs no quoting.
        out = subprocess.run(
            cmd, cwd=cwd, input=prompt, capture_output=True, text=True,
            timeout=timeout,
        )
        rc, stdout, stderr = out.returncode, out.stdout or "", out.stderr or ""
    except subprocess.TimeoutExpired:
        rc, stdout, stderr = 124, "", "timeout"
    except FileNotFoundError:
        rc, stdout, stderr = 127, "", "claude CLI not in PATH"
    _record_health(rc, stdout, stderr)
    return rc, stdout, stderr


def _error_line(stdout: str, stderr: str) -> str:
    """The line that explains a failure, skipping the CLI's warnings."""
    lines = [l.strip() for l in (stderr + "\n" + stdout).splitlines()
             if l.strip() and not l.strip().startswith("Ignoring ")]
    return (lines[-1] if lines else "sin detalle")[:300]


def _record_health(rc: int, stdout: str, stderr: str) -> None:
    """Track consecutive Claude failures; warn on Telegram when they pile up.

    Without this a dead login fails silently every 15 minutes and the
    owner only finds out when he notices the digests stopped. Timeouts
    (124) count too: they also mean no triage. Best-effort: never raises.
    """
    try:
        h = load_json(HEALTH, {"fails": 0})
        now = int(time.time())
        if rc == 0:
            if h.get("alerted_at"):
                push_to_telegram(
                    "✅ <b>PA</b>: Claude vuelve a responder. "
                    "Los vigilantes de correo siguen donde lo dejaron.")
            atomic_write_json(HEALTH, {"fails": 0})
            return
        h["fails"] = int(h.get("fails", 0)) + 1
        h.setdefault("first_fail_at", now)
        h["last_error"] = _error_line(stdout, stderr)
        last_alert = int(h.get("alerted_at") or 0)
        if (h["fails"] >= HEALTH_ALERT_AFTER
                and now - last_alert >= HEALTH_REALERT_S):
            since = time.strftime("%d/%m %H:%M",
                                  time.localtime(h["first_fail_at"]))
            err = h["last_error"]
            if any(k in err for k in ("401", "authenticat", "OAuth", "login")):
                fix = ("Hay que volver a iniciar sesión en Claude con el "
                       "usuario del PA: abre <code>claude</code> como ese "
                       "usuario y haz <code>/login</code>.")
            else:
                fix = "Revisa los logs en <code>briefings/</code>."
            push_to_telegram(
                f"⚠️ <b>PA sin Claude desde el {since}</b> "
                f"({h['fails']} intentos fallidos). Mientras siga así no "
                f"se clasifica el correo ni se preparan borradores.\n\n"
                f"Error: <code>{html_escape(err)}</code>\n{fix}")
            h["alerted_at"] = now
        atomic_write_json(HEALTH, h)
    except Exception:
        pass


# --- Telegram push ----------------------------------------------------------

import html as _html


def html_escape(s: str) -> str:
    """Escape <, >, & for Telegram HTML parse_mode.

    Use on any user-controlled content (mail subjects, sender names, Claude
    output, goals.local.md fields) before embedding inside <b>/<code>/etc.
    `_` and `*` need NO escaping in HTML mode (unlike Markdown), which is
    why we picked HTML — they appear inside subjects (`shopify_order_to_sap`)
    and the Markdown parser was rendering them as italic.
    """
    return _html.escape(s, quote=False)


def push_to_telegram(text: str, *, log=None, parse_mode: str = "HTML") -> int:
    """Send `text` to the owner's Telegram via `telegram_send.sh`.

    Defaults to HTML parse_mode. Pass parse_mode="" for plain text.
    """
    res = subprocess.run(
        ["bash", str(REPO_DIR / "scripts" / "telegram_send.sh"), "-", parse_mode],
        input=text, text=True, capture_output=True,
    )
    if res.returncode != 0 and log:
        log(f"telegram_send rc={res.returncode}: {res.stderr.strip()[:200]}")
    return res.returncode
