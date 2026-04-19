#!/usr/bin/env python3
"""Slack watcher — every N minutes, check for new inbound Slack messages.

Loaded by launchd (com.example.pa-slack-watcher) every 10 min. On each tick:
  1. Load the last-checked timestamp from .slack_watch_state.json.
  2. Call `claude --print` with a prompt that:
       - loads the Slack MCP tools via ToolSearch,
       - searches for messages received by the owner since that timestamp,
       - classifies according to memory/triage_rules.md,
       - emits ONE line per IMPORTANT/FIRE message or exactly "NONE".
  3. If the output is not "NONE": push to Telegram.
  4. Advance the timestamp.

Nothing is marked as read on Slack (search is read-only).

Requires `PA_SLACK_USER_ID` (env or Keychain `PA-slack-user-id`) — the Slack
member id of the account whose DMs / mentions we watch. Get it from the
"Copy member ID" action in Slack's profile view.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mail import get_secret  # reused: env var → Keychain fallback

REPO_DIR = Path(__file__).resolve().parent.parent
STATE = REPO_DIR / ".slack_watch_state.json"
LOG = REPO_DIR / "briefings" / "slack_watcher.log"
CLAUDE_TIMEOUT_S = 240


def log(msg: str) -> None:
    LOG.parent.mkdir(exist_ok=True)
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
    return {}


def save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=2))


def build_prompt(since_iso: str, user_id: str) -> str:
    return f"""Autonomous PA task: check Slack for the owner (user_id {user_id}).

STEP 1. Load the Slack MCP tools via `ToolSearch`:
  query="select:mcp__claude_ai_Slack__slack_search_public_and_private,mcp__claude_ai_Slack__slack_search_users,mcp__claude_ai_Slack__slack_read_channel"
  max_results=3

STEP 2. Search messages received by the owner since {since_iso}:
  - Call slack_search_public_and_private with query "to:<@{user_id}> after:{since_iso[:10]}", limit=20, response_format=concise.

STEP 3. For each message, decide importance using memory/triage_rules.md:
  - FIRE: production down, angry customer, <24h deadline, hosting/bank blocked, security incident.
  - IMPORTANT: direct DM from a person, @mention asking for something, customer question.
  - Otherwise: ignore (not in output).

STEP 4. Output. Exactly one of:
  a) Nothing important: the literal word `NONE`.
  b) Otherwise, one line per message:
     `<tag> [HH:MM] @<name> in #<channel or DM> · <one-line summary>`
     Examples:
     `FIRE [11:42] @alice in DM · customer reports site down, losing sales`
     `IMPORTANT [12:10] @bob in #sales · asks if we sign the ACME proposal today`

Rules:
- No explanations, no greetings, no Markdown.
- If the Slack search fails, output: `ERROR: <one-line reason>`.
- Read-only; nothing is marked as read.
"""


def run_claude(prompt: str) -> tuple[int, str, str]:
    cmd = [
        "claude", "--print",
        "--model", "sonnet",
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
        return out.returncode, out.stdout or "", out.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "claude CLI not in PATH"


def push_to_telegram(text: str) -> None:
    res = subprocess.run(
        ["bash", str(REPO_DIR / "scripts" / "telegram_send.sh"), "-", ""],
        input=text, text=True, capture_output=True,
    )
    if res.returncode != 0:
        log(f"telegram_send rc={res.returncode}: {res.stderr.strip()[:200]}")


def main() -> int:
    try:
        user_id = get_secret("slack-user-id")
    except Exception as e:
        log(f"FATAL: {e}")
        return 2

    state = load_state()
    now = dt.datetime.now(dt.timezone.utc)
    if not state.get("last_check_iso"):
        state["last_check_iso"] = now.isoformat(timespec="seconds")
        save_state(state)
        log(f"first run, baseline set to {state['last_check_iso']}")
        return 0

    since_iso = state["last_check_iso"]
    log(f"checking since {since_iso}")
    rc, stdout, stderr = run_claude(build_prompt(since_iso, user_id))
    output = stdout.strip()

    if rc != 0:
        log(f"claude rc={rc}, stderr={stderr.strip()[:300]}")
        return rc

    state["last_check_iso"] = now.isoformat(timespec="seconds")
    save_state(state)

    if not output or output == "NONE":
        log("nothing important")
        return 0
    if output.startswith("ERROR:"):
        log(f"claude reported: {output[:300]}")
        return 0

    log(f"sending to telegram ({len(output)} chars)")
    header = "Slack — something for you"
    push_to_telegram(f"{header}\n\n{output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
