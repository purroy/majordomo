---
description: Diagnose MCP connectors and PA credentials
---

Check everything the PA needs and report as a checklist.

## 1. Mail credentials in Keychain / env

Discover the configured accounts:

```bash
python3 -c "import sys; sys.path.insert(0,'scripts'); from _mail import list_accounts; print(' '.join(list_accounts()))"
```

For each account `<id>`, verify these secrets with `bash scripts/keychain.sh has <name>` (macOS) or `[ -n "${PA_MAIL_<ID>_USER}" ]` (env):

- `mail-<id>-user`
- `mail-<id>-pass`
- `mail-<id>-imap-host`
- `mail-<id>-imap-port`
- `mail-<id>-smtp-host`
- `mail-<id>-smtp-port`

For any missing secret, show the exact command to add it:

```bash
security add-generic-password -U -a "$USER" -s "PA-<name>" -w "<value>"
```

## 2. IMAP liveness

Run `python3 scripts/mail_fetch.py --account <id> --limit 1` for each account. JSON → OK. Error → diagnose host / port / credentials.

## 3. MCP connectors

For each of `Google_Calendar`, `Google_Drive`, `Slack`:
- Try a cheap read call (e.g. list one event, list channels, etc.).
- If it fails with "not authenticated", tell the owner: "To authorize X say 'auth X' and I will run the flow."
- Do NOT launch `__authenticate` automatically.

## 4. Schedules

- Check whether the launchd agents are loaded: `launchctl list | grep com.example.pa-`.
- If missing, point to `bash scripts/launchd/install.sh`.

## Final output

A compact checklist, e.g.:

```
OK  Keychain:    6/6 secrets (account: example)
OK  IMAP:        connected
OK  Calendar:    authenticated
FAIL Drive:      not authenticated -> say "auth drive"
OK  Slack:       authenticated
FAIL Cron:       morning/evening briefings not loaded
```

Below the table, list the exact commands the owner should run.
