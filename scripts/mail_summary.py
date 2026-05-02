#!/usr/bin/env python3
"""Drain pending IMPORTANT mail items and push a 4h digest to Telegram."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mail_watcher import format_triage_lines_html
from watcher_base import Logger, atomic_write_json, load_json, push_to_telegram

REPO_DIR = Path(__file__).resolve().parent.parent
STATE = REPO_DIR / ".mail_watch_state.json"
log = Logger(REPO_DIR / "briefings" / "mail_watcher.log")


def main() -> int:
    state = load_json(STATE, {"accounts": {}, "pending_important": []})
    pending = state.get("pending_important", [])
    if not pending:
        log("summary 4h: nothing pending")
        return 0

    state["pending_important"] = []
    atomic_write_json(STATE, state)

    log(f"summary 4h: pushing {len(pending)} items")
    push_to_telegram(format_triage_lines_html(pending, "Mail — resumen 4h"), log=log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
