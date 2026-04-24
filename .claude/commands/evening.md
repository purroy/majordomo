---
description: Evening wrap — what was done today + pending for tomorrow
---

Prepare an end-of-day wrap for the owner.

## Step 0: load deferred tools if needed

As in `/morning`. Call `ToolSearch` with:
```
select:mcp__claude_ai_Slack__slack_search_public_and_private,mcp__claude_ai_Slack__slack_search_users,mcp__claude_ai_Google_Calendar__list_events
```

## Step 1: gather

1. **Today's past events** (Calendar MCP).
2. **Configured accounts**:
   ```
   python3 -c "import sys; sys.path.insert(0,'scripts'); from _mail import list_accounts; print(' '.join(list_accounts()))"
   ```
3. **Mail sent today** per account:
   ```
   python3 scripts/mail_fetch.py --account <ID> --folder "Sent" --since 24h --limit 30
   ```
   (Some providers use "Sent Messages" or "[Gmail]/Sent Mail" — the client tolerates spaces; adjust per account if needed.)
4. **Received mail still unanswered** per account:
   ```
   python3 scripts/mail_fetch.py --account <ID> --unread --limit 30 --with-body
   ```
   Read each with `mail_read.py UID --account <ID>` and classify with `memory/triage_rules.md`. **Do not mark as read.**
5. **Slack**: messages received by the owner since yesterday 19:00 (`to:<@USER_ID> after:<date>`).
6. **Tomorrow's agenda**.
7. **Goals state** (if `goals.local.md` exists):
   ```
   python3 scripts/goals.py check --json
   python3 scripts/goals.py list --json
   ```
   Use both to produce the "Rocks recap" section (done_log entries from today + open rocks without fresh progress).

## Step 2: output

```markdown
# End of day — <date>

## Done today
- Meetings attended: N
- Mail sent: N (top 3 recipients + topic)

## Still open at close
**Unattended fires:**
- [account/UID] ...
**Important not yet replied:**
- [account/UID] ...
N more to review (not urgent).
M pending Slack messages.

## For tomorrow
- First meeting: HH:MM · Title
- N meetings total
- Top 3 things to have ready:
  1. ...
  2. ...
  3. ...

## Rocks recap
**Done today (from done_log):** <items with date == today>
**Rocks still without progress:** <stale_rocks from check>
**Suggestion:** "Want to mark anything done or update a rock's progress before closing?"
(Omit this section entirely if goals.local.md is missing.)
```

## Hard rules

- **Never mark mail as read.**
- If a fire appeared that wasn't in the morning briefing, flag it and, if it's truly urgent, suggest handling tonight.
- If run by cron, the wrapper writes to `briefings/YYYY-MM-DD-evening.md` and pushes to Telegram.
