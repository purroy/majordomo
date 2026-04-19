---
description: Morning briefing — agenda + mail triage + Slack + priorities
---

Prepare a morning briefing. Clear structure, short sentences, actionable.

## Step 0: load deferred tools (if running in --print without them pre-loaded)

Before touching Slack or Calendar, make sure the tools are loaded. If not, call `ToolSearch` with:

```
select:mcp__claude_ai_Slack__slack_search_public,mcp__claude_ai_Slack__slack_search_public_and_private,mcp__claude_ai_Slack__slack_search_users,mcp__claude_ai_Slack__slack_read_channel,mcp__claude_ai_Google_Calendar__list_events
```

## Step 1: gather in parallel

1. **Today's agenda** (Calendar MCP, `list_events`): use the owner's local timezone.
2. **Unread mail last 24h across ALL accounts**:
   - Discover the configured accounts:
     ```
     python3 -c "import sys; sys.path.insert(0,'scripts'); from _mail import list_accounts; print(' '.join(list_accounts()))"
     ```
   - For each account:
     ```
     python3 scripts/mail_fetch.py --account <ID> --unread --since 24h --limit 30 --with-body
     ```
3. **Slack**: messages received by the owner (Slack user id from `PA_SLACK_USER_ID` env var or Keychain `PA-slack-user-id`) in the last 24h. Use `slack_search_public_and_private` with `to:<@USER_ID> after:YYYY-MM-DD`. If the tool is unavailable, note it at the end but do NOT block the briefing.

## Step 2: mail triage (deep read, never mark as read)

For each UID per account:
- If the snippet makes it obviously NOISE (newsletter, no-reply, platform notice without action), skip it.
- Otherwise read the full body with `python3 scripts/mail_read.py UID --account <ID>` (BODY.PEEK, does not mark \Seen).
- Classify using `memory/triage_rules.md`:
  - **FIRE**: production down, angry customer, <24h deadline, hosting/bank blocked.
  - **IMPORTANT**: customer with a concrete question, pre-sales, blocked employee, invoice / contract.
  - **Review**: informational, comments on docs.
  - **Noise**: group.

## Step 3: Slack triage

For each received message:
- Direct DM from a person -> at least "Review"; usually "Important" or "Fire".
- @mention in channel -> "Important" if it asks for something, "Review" if informational.
- Bot / automation -> "Noise" unless it reports a fire (monitoring alert, etc.).

## Step 4: output

Exact format (no Markdown that Telegram cannot render — a converter runs afterwards):

```markdown
# Good morning — <date>

## Today's agenda
- HH:MM–HH:MM · Title · (location / attendees)
...
(if empty: "No meetings scheduled.")

## Mail (N unread total — <id1> A · <id2> B · ...)
**Fire** (reply now):
- [account/UID] Sender — Subject · one-line what + why it's fire
**Important** (today):
- [account/UID] ...
**Review**:
- [account/UID] ...
**Noise**: N items (newsletters / notifications from X, Y, Z)

(if empty: "Inboxes clean over the last 24h.")
Use `[account/UID]` so `/reply UID account` works directly.

## Slack
**Fire** ...
**Important** ...
(if empty: "No pending Slack.")

## Suggested priorities
1. ...
2. ...
3. ...
```

## Hard rules

- **NEVER mark mail as read.** Unread is the owner's inbox-zero signal.
- Read only with `mail_read.py` (PEEK). Never use tools that set `\Seen`.
- Keep UIDs visible so `/reply UID` works.
- If a source fails, mention it at the end of the briefing — do not abort.

If the owner invokes this manually, write the output to the chat. If the `run_briefing.sh` wrapper calls it, the wrapper handles file + Telegram push.
