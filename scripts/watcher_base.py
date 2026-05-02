"""Shared utilities for PA watchers (mail, slack, telegram bot).

Extracts what was duplicated across the watchers:
  - atomic JSON state I/O (`.tmp` + rename; recoverable if killed mid-write)
  - log with size-based rotation (10 MB x 3 keep)
  - `claude --print` subprocess wrapper (timeout, ephemeral session id)
  - Telegram push
  - `safe_wrap()`: XML-tag wrapping for untrusted data in prompts
    (prevents prompt injection via mail subjects / slack text)
"""
from __future__ import annotations

import json
import os
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


# --- Claude subprocess ------------------------------------------------------

def run_claude(
    prompt: str,
    *,
    model: str = "sonnet",
    timeout: int = 240,
    cwd: Path = REPO_DIR,
) -> tuple[int, str, str]:
    """Invoke `claude --print` with an ephemeral session.

    Returns (returncode, stdout, stderr). Maps timeout → 124 and missing CLI
    → 127 so callers can branch without catching exceptions.
    """
    cmd = [
        "claude", "--print",
        "--model", model,
        "--session-id", str(uuid.uuid4()),
        "--no-session-persistence",
        "--setting-sources", "project,user",
        "--output-format", "text",
        "--permission-mode", "bypassPermissions",
        prompt,
    ]
    try:
        out = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        return out.returncode, out.stdout or "", out.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "claude CLI not in PATH"


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
