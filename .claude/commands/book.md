---
description: Create a Calendar event (always confirm first)
---

Args: `$ARGUMENTS` can be a free-form description ("tomorrow 10 with Alice, 30 min, project Y") or empty.

## Steps

1. If anything is unclear, ask. Minimum fields:
   - Title.
   - Date + start time.
   - Duration or end time.
   - Attendees (optional, with email).
   - Location / Meet (optional).
   - Notes / description (optional).

2. Before creating, check for conflicts: list events for the proposed day via Calendar MCP. Warn on overlap.

3. **Show the proposed event** in this format:

```
Proposed event:
  Title     : ...
  When      : Thu 18 Apr 2026 · 10:00–10:30 (Europe/Madrid)
  Attendees : alice@example.com, bob@example.com
  Location  : Google Meet (auto)
  Notes     : ...
```

4. Ask: "Create it?"

5. Only on a clear "yes" / "create" / "ok" -> call the MCP `create_event` with those fields. Report the `eventId` or link.

6. If the owner says "change X", adjust and repeat step 3.

## Rules

- Default timezone: the owner's local.
- With external attendees, offer to auto-add a Google Meet link.
- If it overlaps another event, do not proceed without an extra confirmation.
- NEVER create the event without explicit confirmation in the current turn.
