# PA — Personal Assistant on Claude Code

A self-hosted personal assistant that runs on top of [Claude Code](https://claude.com/claude-code). It reads your mail (IMAP/SMTP), calendar, drive, and Slack; triages inbound; drafts replies; and sends you scheduled briefings — always with explicit confirmation before any write action.

## Features

- **Mail** — list, read, triage, draft, and send replies over plain IMAP/SMTP (stdlib only, no external Python deps).
- **Multi-account** — one Keychain / env prefix per mailbox (`PA-mail-<id>-*`).
- **Calendar / Drive / Slack** — read and write via the Claude Code MCP connectors.
- **Scheduled briefings** — morning and evening summaries wrapped by `scripts/run_briefing.sh`, optionally pushed to Telegram and macOS notifications.
- **Watchers** — mail and Slack pollers that only wake Claude when there's actually something new, and only notify on classified "fire" or "important" items.
- **Telegram bridge** — two-way chat with the PA from your phone, via a long-polling bot that shells out to `claude --print` per message.

## Requirements

- macOS (primary target) or Linux.
- Python 3.10+ (stdlib only).
- [Claude Code CLI](https://claude.com/claude-code) (`claude`) on `$PATH`.
- IMAP + SMTP credentials for your mailbox(es).
- Optional: a Telegram bot token (`@BotFather`) for the mobile bridge.
- Optional: authenticated Claude Code MCP connectors for Google Calendar, Google Drive, and Slack.

## Architecture

```
                          ┌──────────────────────────┐
                          │         You (CLI)        │
                          │ /morning /inbox /reply … │
                          └────────────┬─────────────┘
                                       │
                    ┌──────────────────▼──────────────────┐
                    │          Claude Code (claude)       │
                    │  CLAUDE.md + .claude/commands/*.md  │
                    └─┬─────────┬──────────┬────────────┬─┘
                      │         │          │            │
              ┌───────▼──┐  ┌───▼────┐  ┌──▼─────┐  ┌───▼──────┐
              │ scripts/ │  │  MCP   │  │  MCP   │  │   MCP    │
              │  mail_*  │  │Calendar│  │ Drive  │  │  Slack   │
              └────┬─────┘  └────────┘  └────────┘  └──────────┘
                   │
          ┌────────▼────────┐
          │  IMAP  /  SMTP  │
          │  (your server)  │
          └─────────────────┘

                    ┌─────────── launchd (macOS) ────────────┐
                    │                                         │
         run_briefing.sh morning/evening      mail_watcher.py / slack_watcher.py
                    │                                         │
                    └──────────┐                ┌─────────────┘
                               ▼                ▼
                        ┌──────────────────────────┐
                        │   notify.sh · Telegram   │
                        └──────────────────────────┘
```

## Setup

### 1. Clone and install

```bash
git clone https://github.com/purroy/majordomo.git
cd majordomo
```

No pip install needed — the mail client is stdlib only.

### 2. Credentials

Pick **one** of the two mechanisms. The scripts check env vars first and fall back to Keychain on macOS.

**macOS Keychain** — interactive per-account wizard:

```bash
bash scripts/account_add.sh example you@example.com mail.example.com 993 mail.example.com 587 "Your Name"
```

This stores `PA-mail-example-{user,pass,imap-host,imap-port,smtp-host,smtp-port,from-name}` and appends `example` to the `PA-mail-accounts` index.

**Linux / Docker** — copy `.env.example` to `.env` and fill it in:

```bash
cp .env.example .env
$EDITOR .env
```

Never commit `.env`. It is in `.gitignore`.

### 3. Claude Code MCP connectors

Launch Claude Code in this directory and run:

```
/auth
```

The command diagnoses which of Calendar / Drive / Slack are authenticated and walks you through the OAuth flow for the missing ones.

### 4. Smoke test

```
/auth        # diagnostics
/morning     # manual briefing
```

### 5. Telegram bridge (optional)

```bash
bash scripts/telegram_setup.sh
```

The script asks for your `@BotFather` token, captures your chat id on first message, sends a test message, and loads the `launchd` agent so the bot runs on login.

### 6. Scheduled briefings, mail watcher, Slack watcher (optional)

Sample `launchd` plists live in `scripts/launchd/`. They contain `__REPO_DIR__` / `__USER__` / `__HOME__` placeholders — install them via the wrapper, which substitutes the real values:

```bash
bash scripts/launchd/install.sh                    # install all agents
bash scripts/launchd/install.sh briefing-morning   # just one (substring match)
bash scripts/launchd/install.sh briefing-morning briefing-evening
```

The wrapper runs `launchctl unload` then `launchctl load` on each agent, so re-running it is safe.

On Linux, wire `scripts/run_briefing.sh`, `scripts/mail_watcher.py`, `scripts/slack_watcher.py`, and `scripts/telegram_bot.py` to systemd timers or cron entries pointing at the same wrapper.

## Layout

```
CLAUDE.md                 persona, language, confirmation rule
.claude/commands/         slash commands: /morning /evening /inbox /reply /schedule /book /auth
scripts/                  IMAP/SMTP client, Keychain helpers, watchers, Telegram bridge
scripts/launchd/          sample macOS agent plists
.env.example              non-secret template for Linux / Docker
briefings/                daily output (gitignored)
drafts/                   unsent .eml drafts (gitignored)
```

## Philosophy

- **Explicit confirmation** before any send / create.
- **No secrets in the repo** — Keychain or `.env`.
- **Reuse what exists** — MCP connectors for Google / Slack, Python stdlib for mail.
- **Cheap polling** — watchers do the bare IMAP work themselves and only wake Claude when headers changed.

## Support

This is a personal setup, published as-is under MIT. **No support guarantee.** Issues and pull requests are welcome — fix bugs, add providers, share hardening — but do not expect replies on a schedule.
